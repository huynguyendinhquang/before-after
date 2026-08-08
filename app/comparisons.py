"""Comparison Set and Frame persistence workflows."""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from wtforms import (
    BooleanField,
    FileField,
    FloatField,
    IntegerField,
    StringField,
    SubmitField,
)
from wtforms.validators import Length, Optional

from app.audit import append_audit
from app.auth import editor_required
from app.captures import CaptureError, create_capture, list_captures
from app.db import db
from app.models import Capture, ComparisonSet, Frame, Patient, User
from app.storage import ImageInspection, ManagedStorage, StorageError


comparisons_bp = Blueprint("comparisons", __name__)


class ComparisonError(ValueError):
    """Raised when a Comparison Set workflow cannot change state."""


class SamePatientError(ComparisonError):
    """Raised when a Frame would reference another Patient's Capture."""


class InlineCaptureFrameError(ComparisonError):
    """The Capture committed, but its following Frame mutation did not."""

    def __init__(self, capture_id: int, cause: Exception) -> None:
        self.capture_id = capture_id
        super().__init__(f"Capture {capture_id} was saved but its Frame was not added")
        self.__cause__ = cause


class ComparisonSetForm(FlaskForm):
    name = StringField("Set name", validators=[Length(max=200)])
    canvas_width_mm = StringField("Canvas width (mm)", validators=[Optional(), Length(max=32)])
    canvas_height_mm = StringField("Canvas height (mm)", validators=[Optional(), Length(max=32)])
    preset_key = StringField("Canvas preset", validators=[Optional(), Length(max=32)])
    frame_ratio = FloatField("Frame ratio", validators=[Optional()])
    columns = IntegerField("Columns", validators=[Optional()])
    show_patient_id = BooleanField("Show Patient ID")
    show_patient_name = BooleanField("Show Patient name")
    show_birth_year = BooleanField("Show birth year")
    date_label_default = BooleanField("Show Capture Date", default=True)
    submit = SubmitField("Create Set")


class FrameForm(FlaskForm):
    capture_id = IntegerField("Existing Capture", validators=[Optional()])
    image = FileField("New image")
    capture_date = StringField("Capture Date", validators=[Length(max=10)])
    capture_date_confirmed = BooleanField("I confirm this Capture Date is correct")
    shot_type_name = StringField("Shot Type", validators=[Length(max=200)])
    create_proposal = BooleanField("Use this name as a Shot Type Proposal")
    submit = SubmitField("Add Frame")


def _require_editor(actor: User) -> None:
    if actor is None or not actor.is_editor:
        raise PermissionError("only an Editor or Admin can change Comparison Sets")


def _active_patient(patient: Patient | None) -> Patient:
    if patient is None or patient.archived_at is not None:
        raise ComparisonError("Patient is unavailable")
    return patient


def _active_set(comparison_set: ComparisonSet | None) -> ComparisonSet:
    if comparison_set is None or comparison_set.archived_at is not None:
        raise ComparisonError("Comparison Set is unavailable")
    if comparison_set.patient is None or comparison_set.patient.archived_at is not None:
        raise ComparisonError("Patient is unavailable")
    return comparison_set


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ComparisonError(f"{field} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ComparisonError(f"{field} must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ComparisonError(f"{field} must be positive")
    return number


def _millimetres(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ComparisonError(f"{field} must be a positive number")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ComparisonError(f"{field} is required")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ComparisonError(f"{field} must be a positive number") from exc
    if not number.is_finite() or number <= 0 or number > Decimal("999999.99"):
        raise ComparisonError(f"{field} must be a positive number")
    return number.quantize(Decimal("0.01"))


def _name(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComparisonError("Set name is required")
    value = value.strip()
    if len(value) > 200:
        raise ComparisonError("Set name is too long")
    return value


def create_comparison_set(
    *,
    actor: User,
    patient: Patient,
    name: str | None = None,
    title: str | None = None,
    canvas_width_mm: object = 297,
    canvas_height_mm: object = 210,
    preset_key: str = "a4-landscape",
    frame_ratio: object = 1.0,
    columns: int = 3,
    show_patient_id: bool = False,
    show_patient_name: bool = False,
    show_birth_year: bool = False,
    date_label_default: bool = True,
) -> ComparisonSet:
    """Create a named Set and its audit event in one transaction."""
    _require_editor(actor)
    patient = _active_patient(patient)
    if name is None:
        name = title
    set_name = _name(name)
    if not isinstance(preset_key, str) or not preset_key.strip() or len(preset_key.strip()) > 32:
        raise ComparisonError("Canvas preset is invalid")
    preset_key = preset_key.strip()
    width = _millimetres(canvas_width_mm, "Canvas width")
    height = _millimetres(canvas_height_mm, "Canvas height")
    ratio = _number(frame_ratio, "Frame ratio")
    if isinstance(columns, bool) or not isinstance(columns, int) or columns <= 0 or columns > 100:
        raise ComparisonError("Columns must be a positive integer")
    for flag, field in (
        (show_patient_id, "Patient ID output flag"),
        (show_patient_name, "Patient name output flag"),
        (show_birth_year, "birth year output flag"),
        (date_label_default, "Capture Date output flag"),
    ):
        if not isinstance(flag, bool):
            raise ComparisonError(f"{field} must be boolean")

    duplicate = db.session.scalar(
        select(ComparisonSet).where(
            ComparisonSet.patient_id == patient.id,
            ComparisonSet.archived_at.is_(None),
            func.lower(ComparisonSet.name) == set_name.lower(),
        )
    )
    if duplicate is not None:
        raise ComparisonError("An active Set with this name already exists for the Patient")

    comparison_set = ComparisonSet(
        patient_id=patient.id,
        name=set_name,
        canvas_width_mm=width,
        canvas_height_mm=height,
        preset_key=preset_key,
        frame_ratio=ratio,
        columns=columns,
        show_patient_id=show_patient_id,
        show_patient_name=show_patient_name,
        show_birth_year=show_birth_year,
        date_label_default=date_label_default,
        version=1,
        created_by_id=actor.id,
        updated_by_id=actor.id,
    )
    try:
        db.session.add(comparison_set)
        db.session.flush()
        append_audit(
            actor=actor,
            action="comparison_set.create",
            entity_type="comparison_set",
            entity_id=comparison_set.id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return comparison_set


def list_comparison_sets(patient: Patient) -> list[ComparisonSet]:
    patient = _active_patient(patient)
    return list(
        db.session.scalars(
            select(ComparisonSet)
            .where(
                ComparisonSet.patient_id == patient.id,
                ComparisonSet.archived_at.is_(None),
            )
            .order_by(ComparisonSet.name, ComparisonSet.id)
        )
    )


def open_comparison_set(set_id: int, patient: Patient | None = None) -> ComparisonSet | None:
    comparison_set = db.session.get(ComparisonSet, set_id)
    if comparison_set is None or comparison_set.archived_at is not None:
        return None
    if comparison_set.patient is None or comparison_set.patient.archived_at is not None:
        return None
    if patient is not None and comparison_set.patient_id != patient.id:
        return None
    return comparison_set


def _capture_for_frame(capture: Capture | int | None, capture_id: int | None) -> Capture | None:
    if capture is not None and capture_id is not None:
        raise ComparisonError("choose one Capture")
    candidate = capture_id if capture is None else capture
    if candidate is None:
        return None
    if isinstance(candidate, bool):
        raise ComparisonError("Capture is invalid")
    if isinstance(candidate, int):
        result = db.session.get(Capture, candidate)
    elif isinstance(candidate, Capture):
        result = candidate
    else:
        raise ComparisonError("Capture is invalid")
    if result is None:
        raise ComparisonError("Capture is unavailable")
    return result


def add_frame(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    capture: Capture | int | None = None,
    capture_id: int | None = None,
    upload: object | None = None,
    capture_date: object | None = None,
    capture_date_confirmed: bool = False,
    shot_type: object | None = None,
    shot_type_id: int | None = None,
    shot_type_name: str | None = None,
    create_proposal: bool = False,
    original_filename: str | None = None,
    storage: ManagedStorage | None = None,
    inspection: ImageInspection | None = None,
    position: int | None = None,
) -> Frame:
    """Add an existing Capture or call the Slice 2 Capture command once."""
    _require_editor(actor)
    comparison_set = _active_set(comparison_set)
    selected = _capture_for_frame(capture, capture_id)
    inline_capture = selected is None
    if inline_capture:
        if upload is None:
            raise ComparisonError("choose an existing Capture or upload an image")
        # This deliberately delegates all validation, deduplication, storage,
        # and Capture auditing to Slice 2 instead of maintaining a second path.
        selected = create_capture(
            actor=actor,
            patient=comparison_set.patient,
            upload=upload,
            capture_date=capture_date,
            capture_date_confirmed=capture_date_confirmed,
            shot_type=shot_type,
            shot_type_id=shot_type_id,
            shot_type_name=shot_type_name,
            create_proposal=create_proposal,
            original_filename=original_filename,
            storage=storage,
            inspection=inspection,
        )

    if selected.patient_id != comparison_set.patient_id:
        if inline_capture:
            raise InlineCaptureFrameError(selected.id, SamePatientError("Capture belongs to another Patient"))
        raise SamePatientError("Capture belongs to another Patient")
    if selected.archived_at is not None:
        raise ComparisonError("Capture is unavailable")

    automatic_position = position is None
    existing_frames: list[Frame] = []
    if automatic_position:
        existing_frames = list(
            db.session.scalars(
                select(Frame)
                .where(Frame.comparison_set_id == comparison_set.id)
                .order_by(Frame.position, Frame.id)
            )
        )
        maximum = max((item.position for item in existing_frames), default=-1)
        position = 0 if not existing_frames else maximum + len(existing_frames) + 2
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        raise ComparisonError("Frame position is invalid")

    frame = Frame(
        comparison_set_id=comparison_set.id,
        capture_id=selected.id,
        position=position,
        visible=True,
        label=None,
        date_visible_override=None,
        zoom=1.0,
        pan_x=0.0,
        pan_y=0.0,
    )
    try:
        db.session.add(frame)
        db.session.flush()
        if automatic_position and existing_frames:
            # Move through temporary positions so inserting an older Capture
            # cannot collide with the existing unique (Set, position) key.
            offset = position + len(existing_frames) + 1
            for index, existing in enumerate(existing_frames):
                existing.position = offset + index
            frame.position = offset + len(existing_frames)
            db.session.flush()
            ordered = sorted(
                [*existing_frames, frame],
                key=lambda item: (
                    selected.capture_date if item is frame else item.capture.capture_date,
                    selected.id if item is frame else item.capture_id,
                    item.id,
                ),
            )
            for index, item in enumerate(ordered):
                item.position = index
        comparison_set.updated_by_id = actor.id
        append_audit(
            actor=actor,
            action="frame.add",
            entity_type="frame",
            entity_id=frame.id,
            details={"comparison_set_id": comparison_set.id, "capture_id": selected.id},
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        if inline_capture:
            raise InlineCaptureFrameError(selected.id, exc) from exc
        raise
    return frame


def reorder_frames(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    ordered_frame_ids: Sequence[int],
) -> list[Frame]:
    """Persist a complete manual order for the Set's Frames."""
    _require_editor(actor)
    comparison_set = _active_set(comparison_set)
    try:
        ordered_ids = [int(value) for value in ordered_frame_ids]
    except (TypeError, ValueError) as exc:
        raise ComparisonError("Frame order is invalid") from exc

    frames = list(
        db.session.scalars(
            select(Frame)
            .where(Frame.comparison_set_id == comparison_set.id)
            .order_by(Frame.position, Frame.id)
        )
    )
    expected = {frame.id for frame in frames}
    if len(ordered_ids) != len(expected) or set(ordered_ids) != expected:
        raise ComparisonError("Frame order must include every Frame exactly once")

    by_id = {frame.id: frame for frame in frames}
    offset = max((frame.position for frame in frames), default=-1) + len(frames) + 1
    try:
        for index, frame in enumerate(frames):
            frame.position = offset + index
        db.session.flush()
        for index, frame_id in enumerate(ordered_ids):
            by_id[frame_id].position = index
        comparison_set.updated_by_id = actor.id
        append_audit(
            actor=actor,
            action="comparison_set.reorder",
            entity_type="comparison_set",
            entity_id=comparison_set.id,
            details={"frame_ids": ordered_ids},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return [by_id[frame_id] for frame_id in ordered_ids]


# Small aliases keep the feature vocabulary usable by callers without adding
# another persistence path.
create_set = create_comparison_set
list_sets = list_comparison_sets


def _route_patient(patient_pk: int) -> Patient:
    patient = db.session.get(Patient, patient_pk)
    if patient is None or patient.archived_at is not None:
        abort(404)
    return patient


def _route_set(set_pk: int, patient_pk: int | None = None) -> ComparisonSet:
    comparison_set = open_comparison_set(set_pk)
    if comparison_set is None or (patient_pk is not None and comparison_set.patient_id != patient_pk):
        abort(404)
    return comparison_set


@comparisons_bp.get("/patients/<int:patient_pk>/comparison-sets")
@comparisons_bp.get("/patients/<int:patient_pk>/comparison-sets/")
@login_required
def index(patient_pk: int):
    patient = _route_patient(patient_pk)
    return render_template(
        "comparisons/index.html",
        patient=patient,
        comparison_sets=list_comparison_sets(patient),
    )


@comparisons_bp.route("/patients/<int:patient_pk>/comparison-sets/new", methods=["GET", "POST"])
@editor_required
def new(patient_pk: int):
    patient = _route_patient(patient_pk)
    form = ComparisonSetForm()
    if form.validate_on_submit():
        try:
            comparison_set = create_comparison_set(
                actor=current_user,
                patient=patient,
                name=form.name.data,
                canvas_width_mm=form.canvas_width_mm.data or 297,
                canvas_height_mm=form.canvas_height_mm.data or 210,
                preset_key=form.preset_key.data or "a4-landscape",
                frame_ratio=form.frame_ratio.data or 1.0,
                columns=form.columns.data or 3,
                show_patient_id=form.show_patient_id.data,
                show_patient_name=form.show_patient_name.data,
                show_birth_year=form.show_birth_year.data,
                date_label_default=form.date_label_default.data,
            )
        except (ComparisonError, IntegrityError) as exc:
            form.name.errors.append(str(exc) if isinstance(exc, ComparisonError) else "Set name already exists.")
        else:
            flash("Comparison Set created.", "success")
            return redirect(url_for("comparisons.detail", set_pk=comparison_set.id))
    return render_template("comparisons/new.html", patient=patient, form=form), (
        400 if request.method == "POST" else 200
    )


@comparisons_bp.get("/comparison-sets/<int:set_pk>")
@comparisons_bp.get("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>")
@login_required
def detail(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    return render_template(
        "comparisons/detail.html",
        comparison_set=comparison_set,
        patient=comparison_set.patient,
        captures=list_captures(comparison_set.patient),
        frame_form=FrameForm(),
    )


@comparisons_bp.post("/comparison-sets/<int:set_pk>/frames")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/frames")
@editor_required
def add_frame_route(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    form = FrameForm()
    if not form.validate_on_submit():
        return render_template(
            "comparisons/detail.html",
            comparison_set=comparison_set,
            patient=comparison_set.patient,
            captures=list_captures(comparison_set.patient),
            frame_form=form,
        ), 400

    filename = getattr(form.image.data, "filename", None) if form.image.data else None
    try:
        if form.capture_id.data:
            if form.image.data and filename:
                raise ComparisonError("choose an existing Capture or upload a new image")
            add_frame(
                actor=current_user,
                comparison_set=comparison_set,
                capture_id=form.capture_id.data,
            )
        else:
            if not form.image.data or not filename:
                raise ComparisonError("choose an existing Capture or upload a new image")
            add_frame(
                actor=current_user,
                comparison_set=comparison_set,
                upload=form.image.data,
                capture_date=form.capture_date.data,
                capture_date_confirmed=form.capture_date_confirmed.data,
                shot_type_name=form.shot_type_name.data,
                create_proposal=form.create_proposal.data,
                original_filename=filename,
            )
    except InlineCaptureFrameError as exc:
        flash(f"Capture {exc.capture_id} was saved, but its Frame was not added.", "error")
        return redirect(url_for("comparisons.detail", set_pk=comparison_set.id)), 409
    except (CaptureError, ComparisonError, StorageError, IntegrityError) as exc:
        form.capture_id.errors.append(str(exc))
        return render_template(
            "comparisons/detail.html",
            comparison_set=comparison_set,
            patient=comparison_set.patient,
            captures=list_captures(comparison_set.patient),
            frame_form=form,
        ), 400
    flash("Frame added.", "success")
    return redirect(url_for("comparisons.detail", set_pk=comparison_set.id))


@comparisons_bp.post("/comparison-sets/<int:set_pk>/reorder")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/reorder")
@editor_required
def reorder(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    payload = request.get_json(silent=True) if request.is_json else None
    raw_ids = payload.get("frame_ids") if isinstance(payload, dict) else request.form.getlist("frame_ids")
    if raw_ids is None:
        raw_ids = []

    if request.form.get("move_frame_id"):
        try:
            move_id = int(request.form["move_frame_id"])
            direction = request.form.get("direction")
            current = [frame.id for frame in comparison_set.frames]
            index = current.index(move_id)
            other = index - 1 if direction == "up" else index + 1
            if direction not in {"up", "down"} or not 0 <= other < len(current):
                raise ComparisonError("Frame is already at that edge")
            current[index], current[other] = current[other], current[index]
            raw_ids = current
        except (ValueError, IndexError) as exc:
            raw_ids = []
            error: Exception = ComparisonError("Frame order is invalid")
        else:
            error = None
    else:
        error = None

    try:
        if error is not None:
            raise error
        ordered = reorder_frames(
            actor=current_user,
            comparison_set=comparison_set,
            ordered_frame_ids=raw_ids,
        )
    except ComparisonError as exc:
        if request.is_json:
            return {"error": str(exc)}, 400
        flash(str(exc), "error")
        return redirect(url_for("comparisons.detail", set_pk=comparison_set.id)), 400
    if request.is_json:
        return {"frame_ids": [frame.id for frame in ordered]}
    flash("Frame order saved.", "success")
    return redirect(url_for("comparisons.detail", set_pk=comparison_set.id))
