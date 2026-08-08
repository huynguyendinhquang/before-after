"""Comparison Set and Frame persistence workflows."""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from wtforms import (
    BooleanField,
    FloatField,
    IntegerField,
    StringField,
    SubmitField,
)
from wtforms.validators import Length, Optional

from app.audit import append_audit
from app.auth import editor_required
from app.captures import (
    CaptureError,
    CaptureReconciliationError,
    CaptureUploadForm,
    PendingCapture,
    list_captures,
    prepare_capture,
)
from app.db import db
from app.models import Capture, ComparisonSet, Frame, Patient, User
from app.storage import ImageInspection, ManagedStorage, StorageError


comparisons_bp = Blueprint("comparisons", __name__)


class ComparisonError(ValueError):
    """Raised when a Comparison Set workflow cannot change state."""


class SamePatientError(ComparisonError):
    """Raised when a Frame would reference another Patient's Capture."""


class StaleVersionError(ComparisonError):
    """Raised when a reorder was based on an older Set version."""


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


class FrameForm(CaptureUploadForm):
    capture_id = IntegerField("Existing Capture", validators=[Optional()])
    submit = SubmitField("Add Frame")


def _require_editor(actor: User) -> None:
    if actor is None or not actor.is_editor:
        raise PermissionError("only an Editor or Admin can change Comparison Sets")


def _active_patient(patient: Patient | None) -> Patient:
    if patient is None:
        raise ComparisonError("Patient is unavailable")
    state = inspect(patient)
    patient_id = state.identity[0] if state.identity else state.dict.get("id")
    if isinstance(patient_id, bool) or not isinstance(patient_id, int):
        raise ComparisonError("Patient is unavailable")
    with db.session.no_autoflush:
        current = db.session.scalar(
            select(Patient)
            .where(Patient.id == patient_id)
            .execution_options(autoflush=False)
        )
        if current is not None:
            db.session.expunge(current)
            current = db.session.scalar(
                select(Patient)
                .where(Patient.id == patient_id)
                .execution_options(autoflush=False)
            )
    if current is None or current.archived_at is not None:
        raise ComparisonError("Patient is unavailable")
    return current


def _active_set(comparison_set: ComparisonSet | None) -> ComparisonSet:
    if comparison_set is None:
        raise ComparisonError("Comparison Set is unavailable")
    state = inspect(comparison_set)
    set_id = state.identity[0] if state.identity else state.dict.get("id")
    if isinstance(set_id, bool) or not isinstance(set_id, int):
        raise ComparisonError("Comparison Set is unavailable")
    with db.session.no_autoflush:
        current = db.session.scalar(
            select(ComparisonSet)
            .where(ComparisonSet.id == set_id)
            .execution_options(autoflush=False)
        )
        if current is not None:
            db.session.expunge(current)
            current = db.session.scalar(
                select(ComparisonSet)
                .where(ComparisonSet.id == set_id)
                .execution_options(autoflush=False)
            )
    if current is None or current.archived_at is not None:
        raise ComparisonError("Comparison Set is unavailable")
    if current.patient is None or current.patient.archived_at is not None:
        raise ComparisonError("Patient is unavailable")
    return current


def _locked_comparison_set(comparison_set: ComparisonSet) -> ComparisonSet | None:
    state = inspect(comparison_set)
    set_id = state.identity[0] if state.identity else state.dict.get("id")
    with db.session.no_autoflush:
        current = db.session.scalar(
            select(ComparisonSet)
            .where(ComparisonSet.id == set_id)
            .execution_options(autoflush=False)
            .with_for_update(of=ComparisonSet)
        )
        if current is not None:
            db.session.expunge(current)
            current = db.session.scalar(
                select(ComparisonSet)
                .where(ComparisonSet.id == set_id)
                .execution_options(autoflush=False)
            )
    return current


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


def _frame_ids(values: object) -> list[int]:
    try:
        values = iter(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ComparisonError("Frame order is invalid") from exc
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ComparisonError("Frame order is invalid")
        result.append(value)
    return result


def _form_frame_ids(values: object) -> list[int]:
    try:
        values = iter(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ComparisonError("Frame order is invalid") from exc
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise ComparisonError("Frame order is invalid")
        if isinstance(value, int):
            frame_id = value
        elif isinstance(value, str):
            try:
                frame_id = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ComparisonError("Frame order is invalid") from exc
        else:
            raise ComparisonError("Frame order is invalid")
        if frame_id <= 0:
            raise ComparisonError("Frame order is invalid")
        result.append(frame_id)
    return result


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
    try:
        quantized = number.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ComparisonError(f"{field} must have at most 2 decimal places") from exc
    if quantized != number:
        raise ComparisonError(f"{field} must have at most 2 decimal places")
    return quantized


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
    with db.session.no_autoflush:
        comparison_set = db.session.scalar(
            select(ComparisonSet)
            .where(ComparisonSet.id == set_id)
            .execution_options(autoflush=False)
        )
        if comparison_set is not None:
            db.session.expunge(comparison_set)
            comparison_set = db.session.scalar(
                select(ComparisonSet)
                .where(ComparisonSet.id == set_id)
                .execution_options(autoflush=False)
            )
    if comparison_set is None or comparison_set.archived_at is not None:
        return None
    if comparison_set.patient is None or comparison_set.patient.archived_at is not None:
        return None
    if patient is not None:
        state = inspect(patient)
        patient_id = state.identity[0] if state.identity else state.dict.get("id")
        if comparison_set.patient_id != patient_id:
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
        candidate_id = candidate
    elif isinstance(candidate, Capture):
        state = inspect(candidate)
        candidate_id = state.identity[0] if state.identity else state.dict.get("id")
    else:
        raise ComparisonError("Capture is invalid")
    if isinstance(candidate_id, bool) or not isinstance(candidate_id, int):
        raise ComparisonError("Capture is invalid")
    with db.session.no_autoflush:
        result = db.session.scalar(
            select(Capture)
            .where(Capture.id == candidate_id)
            .execution_options(autoflush=False)
        )
        if result is not None:
            db.session.expunge(result)
            result = db.session.scalar(
                select(Capture)
                .where(Capture.id == candidate_id)
                .execution_options(autoflush=False)
            )
    if result is None:
        raise ComparisonError("Capture is unavailable")
    return result


def _inline_commit_state(
    capture_id: int,
    comparison_set_id: int,
    frame_id: int,
) -> tuple[Capture | None, bool]:
    session = db.session.session_factory()
    try:
        capture = session.scalar(select(Capture).where(Capture.id == capture_id))
        frame_saved = session.scalar(
            select(Frame.id).where(
                Frame.id == frame_id,
                Frame.comparison_set_id == comparison_set_id,
                Frame.capture_id == capture_id,
            )
        ) is not None
        if capture is not None:
            session.expunge(capture)
        return capture, frame_saved
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _frame_capture_order_key(frame: Frame) -> tuple[object, int, int]:
    return (frame.capture.capture_date, frame.capture_id, frame.id)


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
    """Add an existing Capture or prepare a new Capture in one transaction."""
    _require_editor(actor)
    comparison_set = _active_set(comparison_set)
    pending: PendingCapture | None = None
    try:
        selected = _capture_for_frame(capture, capture_id)
        if selected is None:
            if upload is None:
                raise ComparisonError("choose an existing Capture or upload an image")
            pending = prepare_capture(
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
            selected = pending.capture

        comparison_set = _locked_comparison_set(comparison_set)
        if comparison_set is None or comparison_set.archived_at is not None:
            raise ComparisonError("Comparison Set is unavailable")
        if comparison_set.patient is None or comparison_set.patient.archived_at is not None:
            raise ComparisonError("Patient is unavailable")
        if selected.patient_id != comparison_set.patient_id:
            raise SamePatientError("Capture belongs to another Patient")
        if selected.archived_at is not None:
            raise ComparisonError("Capture is unavailable")
        capture_id = selected.id
        comparison_set_id = comparison_set.id

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
            position = maximum + 1
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ComparisonError("Frame position is invalid")

        frame = Frame(
            comparison_set_id=comparison_set.id,
            capture_id=selected.id,
            capture=selected,
            position=position,
            visible=True,
            label=None,
            date_visible_override=None,
            zoom=1.0,
            pan_x=0.0,
            pan_y=0.0,
        )
        db.session.add(frame)
        db.session.flush()
        frame_id = frame.id
        canonical = [frame.id for frame in existing_frames] == [
            item.id
            for item in sorted(existing_frames, key=_frame_capture_order_key)
        ]
        if automatic_position and existing_frames and canonical:
            # Move through temporary positions before restoring Capture-Date order.
            offset = position + len(existing_frames) + 1
            for index, existing in enumerate(existing_frames):
                existing.position = offset + index
            frame.position = offset + len(existing_frames)
            db.session.flush()
            ordered = sorted(
                [*existing_frames, frame],
                key=_frame_capture_order_key,
            )
            for index, item in enumerate(ordered):
                item.position = index
        comparison_set.updated_by_id = actor.id
        comparison_set.version += 1
        append_audit(
            actor=actor,
            action="frame.add",
            entity_type="frame",
            entity_id=frame.id,
            details={"comparison_set_id": comparison_set.id, "capture_id": selected.id},
        )
    except Exception:
        db.session.rollback()
        if pending is not None:
            pending.discard()
        raise
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        try:
            committed, frame_saved = _inline_commit_state(
                capture_id=capture_id,
                comparison_set_id=comparison_set_id,
                frame_id=frame_id,
            )
        except Exception as reconciliation_exc:
            if pending is not None:
                pending.preserve()
            raise CaptureReconciliationError(
                "Capture and Frame save status could not be confirmed; pending media was preserved for reconciliation"
            ) from reconciliation_exc
        if committed is not None and frame_saved:
            if pending is not None:
                try:
                    pending.settle(committed)
                except Exception:
                    pending.preserve()
            return frame
        if committed is None and not frame_saved:
            if pending is not None:
                pending.discard()
            raise
        if pending is not None:
            pending.preserve()
        raise CaptureReconciliationError(
            "Capture and Frame save status could not be confirmed; pending media was preserved for reconciliation"
        ) from exc
    if pending is not None:
        try:
            pending.finalize()
        except Exception:
            pending.preserve()
    return frame


def reorder_frames(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    ordered_frame_ids: Sequence[int],
    expected_version: object,
) -> list[Frame]:
    """Persist a complete manual order when the Set version is unchanged."""
    _require_editor(actor)
    try:
        if isinstance(expected_version, bool) or not isinstance(expected_version, (int, str)):
            raise ValueError
        expected_version = int(expected_version)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ComparisonError("Set version is required") from exc
    try:
        if expected_version <= 0:
            raise ComparisonError("Set version is required")
        comparison_set = _active_set(comparison_set)
        ordered_ids = _frame_ids(ordered_frame_ids)
        locked_set = _locked_comparison_set(comparison_set)
        if locked_set is None or locked_set.archived_at is not None:
            raise ComparisonError("Comparison Set is unavailable")
        if locked_set.patient is None or locked_set.patient.archived_at is not None:
            raise ComparisonError("Patient is unavailable")
        if locked_set.version != expected_version:
            raise StaleVersionError("Set has changed; reload before reordering")

        frames = list(
            db.session.scalars(
                select(Frame)
                .where(Frame.comparison_set_id == locked_set.id)
                .order_by(Frame.position, Frame.id)
            )
        )
        expected = {frame.id for frame in frames}
        if len(ordered_ids) != len(expected) or set(ordered_ids) != expected:
            raise ComparisonError("Frame order must include every Frame exactly once")

        by_id = {frame.id: frame for frame in frames}
        offset = max((frame.position for frame in frames), default=-1) + len(frames) + 1
        for index, frame in enumerate(frames):
            frame.position = offset + index
        db.session.flush()
        for index, frame_id in enumerate(ordered_ids):
            by_id[frame_id].position = index
        result = db.session.execute(
            update(ComparisonSet)
            .where(
                ComparisonSet.id == locked_set.id,
                ComparisonSet.version == expected_version,
            )
            .values(version=expected_version + 1, updated_by_id=actor.id)
        )
        if result.rowcount != 1:
            raise StaleVersionError("Set has changed; reload before reordering")
        locked_set.version = expected_version + 1
        locked_set.updated_by_id = actor.id
        append_audit(
            actor=actor,
            action="comparison_set.reorder",
            entity_type="comparison_set",
            entity_id=locked_set.id,
            details={"frame_ids": ordered_ids},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return [by_id[frame_id] for frame_id in ordered_ids]


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
                canvas_width_mm=(
                    form.canvas_width_mm.data
                    if form.canvas_width_mm.data is not None
                    else 297
                ),
                canvas_height_mm=(
                    form.canvas_height_mm.data
                    if form.canvas_height_mm.data is not None
                    else 210
                ),
                preset_key=form.preset_key.data or "a4-landscape",
                frame_ratio=form.frame_ratio.data if form.frame_ratio.data is not None else 1.0,
                columns=form.columns.data if form.columns.data is not None else 3,
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
    except CaptureReconciliationError as exc:
        form.capture_id.errors.append(str(exc))
        return render_template(
            "comparisons/detail.html",
            comparison_set=comparison_set,
            patient=comparison_set.patient,
            captures=list_captures(comparison_set.patient),
            frame_form=form,
        ), 409
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
    expected_version = (
        payload.get("version", payload.get("expected_version"))
        if isinstance(payload, dict)
        else request.form.get("version", request.form.get("expected_version"))
    )
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
        except (ValueError, IndexError, ComparisonError) as exc:
            raw_ids = []
            error = exc if isinstance(exc, ComparisonError) else ComparisonError("Frame order is invalid")
        else:
            error = None
    else:
        error = None

    try:
        if error is not None:
            raise error
        if not request.is_json:
            raw_ids = _form_frame_ids(raw_ids)
        ordered = reorder_frames(
            actor=current_user,
            comparison_set=comparison_set,
            ordered_frame_ids=raw_ids,
            expected_version=expected_version,
        )
    except StaleVersionError as exc:
        if request.is_json:
            return {"error": str(exc)}, 409
        flash(str(exc), "error")
        return redirect(url_for("comparisons.detail", set_pk=comparison_set.id)), 409
    except ComparisonError as exc:
        if request.is_json:
            return {"error": str(exc)}, 400
        flash(str(exc), "error")
        return redirect(url_for("comparisons.detail", set_pk=comparison_set.id)), 400
    if request.is_json:
        return {"frame_ids": [frame.id for frame in ordered], "version": int(expected_version) + 1}
    flash("Frame order saved.", "success")
    return redirect(url_for("comparisons.detail", set_pk=comparison_set.id))
