"""Capture Library, Shot Type selection, and single-image Capture workflow."""

from __future__ import annotations

from datetime import date, datetime
import unicodedata

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from wtforms import BooleanField, FileField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, ValidationError

from app.audit import append_audit
from app.auth import editor_required
from app.db import db
from app.image_policy import FORMAT_EXTENSIONS, mimetype_for_format, read_bounded
from app.models import Capture, Patient, ShotType, User
from app.storage import ImageInspection, ManagedStorage, StorageError


captures_bp = Blueprint("captures", __name__)


def _strict_confirmation(_form, field) -> None:
    if field.raw_data != ["y"]:
        raise ValidationError("Capture Date confirmation is required.")


class CaptureForm(FlaskForm):
    image = FileField("Image", validators=[DataRequired()])
    capture_date = StringField("Capture Date", validators=[Length(max=10)])
    capture_date_confirmed = BooleanField(
        "I confirm this Capture Date is correct (or I have overridden the EXIF suggestion).",
        validators=[_strict_confirmation],
    )
    shot_type_name = StringField("Shot Type", validators=[Length(max=200)])
    create_proposal = BooleanField("Use this name as a Shot Type Proposal")
    submit = SubmitField("Save Capture")


class CaptureError(ValueError):
    """Raised when a Capture cannot be accepted without changing state."""


class ConsentRequired(CaptureError):
    """Raised before any image bytes are stored when consent is absent."""


def _storage(storage: ManagedStorage | None = None) -> ManagedStorage:
    return storage or ManagedStorage(current_app.config["MEDIA_ROOT"])


def _payload(upload: object) -> bytes:
    if isinstance(upload, (bytes, bytearray, memoryview)):
        return bytes(upload)
    stream = getattr(upload, "stream", upload)
    if not hasattr(stream, "read"):
        raise CaptureError("an image file is required")
    try:
        return read_bounded(stream)
    except ValueError as exc:
        raise CaptureError(str(exc)) from exc


def _capture_date(value: date | str | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise CaptureError("Capture Date must be a valid date") from exc
    raise CaptureError("Capture Date is required")


def _filename(upload: object, original_filename: str | None) -> str:
    value = original_filename
    if value is None:
        value = getattr(upload, "filename", None)
    if not isinstance(value, str):
        value = "upload"
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(
        char
        for char in value
        if unicodedata.category(char) not in {"Cc", "Cf"}
        and unicodedata.bidirectional(char)
        not in {"LRE", "LRO", "RLE", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
    ).strip()
    return (value or "upload")[:255]


def _shot_type(
    *,
    actor: User,
    shot_type: ShotType | int | None,
    shot_type_id: int | None,
    shot_type_name: str | None,
    create_proposal: bool,
) -> ShotType:
    selected_id = shot_type_id
    if isinstance(shot_type, ShotType):
        selected_id = shot_type.id
    elif isinstance(shot_type, int) and not isinstance(shot_type, bool):
        selected_id = shot_type
    elif shot_type is not None:
        raise CaptureError("invalid Shot Type")

    if selected_id is not None:
        selected = db.session.get(ShotType, selected_id)
        if selected is None:
            raise CaptureError("Shot Type is unavailable")
        if selected.state == "merged":
            if selected.canonical_target_id is None:
                raise CaptureError("Shot Type is unavailable")
            selected = db.session.get(ShotType, selected.canonical_target_id)
            if (
                selected is None
                or selected.state != "canonical"
                or selected.canonical_target_id is not None
            ):
                raise CaptureError("Shot Type is unavailable")
        elif selected.state not in {"canonical", "proposal"} or selected.canonical_target_id is not None:
            raise CaptureError("Shot Type is unavailable")
        return selected

    if not isinstance(shot_type_name, str) or not shot_type_name.strip():
        raise CaptureError("Shot Type is required")
    name = shot_type_name.strip()
    if len(name) > 200:
        raise CaptureError("Shot Type is too long")
    selected = db.session.scalar(
        select(ShotType)
        .where(func.lower(ShotType.name) == name.lower())
        .where(ShotType.state != "merged")
        .order_by(ShotType.state, ShotType.id)
    )
    if selected is not None:
        return selected
    if not create_proposal:
        raise CaptureError("Choose a canonical Shot Type or create a Proposal")
    selected = ShotType(name=name, state="proposal", created_by_id=actor.id)
    db.session.add(selected)
    db.session.flush()
    append_audit(
        actor=actor,
        action="shot_type.proposal_create",
        entity_type="shot_type",
        entity_id=selected.id,
    )
    return selected


def _existing_capture(patient_id: int, sha256: str) -> Capture | None:
    return db.session.scalar(
        select(Capture).where(Capture.patient_id == patient_id, Capture.sha256 == sha256)
    )


def _reconcile_capture(patient_id: int, sha256: str) -> Capture | None:
    """Check commit state in a new session before deciding whether to clean media."""
    db.session.rollback()
    db.session.close()
    db.session.remove()
    session = db.session.session_factory()
    try:
        capture = session.scalar(
            select(Capture).where(Capture.patient_id == patient_id, Capture.sha256 == sha256)
        )
        if capture is not None:
            session.expunge(capture)
        session.commit()
        return capture
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_capture(
    *,
    actor: User,
    patient: Patient,
    upload: object,
    capture_date: date | str | None,
    capture_date_confirmed: bool,
    shot_type: ShotType | int | None = None,
    shot_type_id: int | None = None,
    shot_type_name: str | None = None,
    create_proposal: bool = False,
    original_filename: str | None = None,
    storage: ManagedStorage | None = None,
    inspection: ImageInspection | None = None,
) -> Capture:
    """Store one Capture and its audit event, or return an existing duplicate."""
    if not actor.is_editor:
        raise PermissionError("only an Editor or Admin can create Captures")
    if patient is None or patient.archived_at is not None:
        raise CaptureError("Patient is unavailable")
    if patient.consent_confirmed_at is None:
        raise ConsentRequired("Consent Confirmation is required before storing images")
    if capture_date_confirmed is not True:
        raise CaptureError("Capture Date must be explicitly confirmed")

    authoritative_date = _capture_date(capture_date)
    payload = _payload(upload)
    managed = _storage(storage)
    if inspection is None:
        inspection = managed.inspect(payload)
    duplicate = _existing_capture(patient.id, inspection.sha256)
    if duplicate is not None:
        return duplicate

    stored = None
    try:
        selected_shot_type = _shot_type(
            actor=actor,
            shot_type=shot_type,
            shot_type_id=shot_type_id,
            shot_type_name=shot_type_name,
            create_proposal=create_proposal,
        )
        stored = managed.store(payload, inspection)
        capture = Capture(
            patient_id=patient.id,
            capture_date=authoritative_date,
            shot_type_id=selected_shot_type.id,
            storage_key=stored.original_key,
            original_filename=_filename(upload, original_filename),
            format=inspection.format,
            width=inspection.width,
            height=inspection.height,
            byte_count=inspection.byte_count,
            sha256=inspection.sha256,
            created_by_id=actor.id,
            updated_by_id=actor.id,
        )
        db.session.add(capture)
        db.session.flush()
        append_audit(
            actor=actor,
            action="capture.create",
            entity_type="capture",
            entity_id=capture.id,
        )
        db.session.commit()
        return capture
    except Exception:
        committed = _reconcile_capture(patient.id, inspection.sha256)
        if committed is not None:
            return committed
        if stored is not None:
            managed.cleanup(stored.original_key, stored.preview_key)
        raise


def search_shot_types(query: str = "", limit: int = 20) -> list[ShotType]:
    value = query.strip()
    statement = select(ShotType).where(ShotType.state != "merged")
    if value:
        statement = statement.where(ShotType.name.ilike(f"%{value}%"))
    return list(
        db.session.scalars(
            statement.order_by(ShotType.state, ShotType.name).limit(max(1, min(limit, 100)))
        )
    )


def list_captures(patient: Patient) -> list[Capture]:
    return list(
        db.session.scalars(
            select(Capture)
            .where(Capture.patient_id == patient.id, Capture.archived_at.is_(None))
            .order_by(Capture.capture_date, Capture.id)
        )
    )


def _active_patient(patient_pk: int) -> Patient:
    patient = db.session.get(Patient, patient_pk)
    if patient is None or patient.archived_at is not None:
        abort(404)
    return patient


def _active_capture(capture_pk: int) -> Capture:
    capture = db.session.get(Capture, capture_pk)
    if (
        capture is None
        or capture.archived_at is not None
        or capture.patient is None
        or capture.patient.archived_at is not None
    ):
        abort(404)
    return capture


@captures_bp.get("/shot-types")
@login_required
def shot_types():
    query = request.args.get("q", "")
    return jsonify(
        [
            {"id": item.id, "name": item.name, "state": item.state}
            for item in search_shot_types(query)
        ]
    )


@captures_bp.get("/patients/<int:patient_pk>/captures")
@login_required
def library(patient_pk: int):
    patient = _active_patient(patient_pk)
    return render_template(
        "captures/library.html",
        patient=patient,
        captures=list_captures(patient),
    )


@captures_bp.route("/patients/<int:patient_pk>/captures/new", methods=["GET", "POST"])
@editor_required
def new(patient_pk: int):
    patient = _active_patient(patient_pk)
    form = CaptureForm()
    suggested_date = None
    staged_payload: bytes | None = None
    inspection: ImageInspection | None = None
    original_filename = None
    if request.method == "POST" and form.image.data:
        original_filename = form.image.data.filename
        try:
            staged_payload = _payload(form.image.data)
            inspection = _storage().inspect(staged_payload)
            suggested_date = inspection.suggested_capture_date
        except (CaptureError, StorageError) as exc:
            form.image.errors.append(str(exc))

    if form.validate_on_submit():
        try:
            capture = create_capture(
                actor=current_user,
                patient=patient,
                upload=staged_payload if staged_payload is not None else form.image.data,
                original_filename=original_filename,
                capture_date=form.capture_date.data,
                capture_date_confirmed=form.capture_date_confirmed.data,
                shot_type_name=form.shot_type_name.data,
                create_proposal=form.create_proposal.data,
                inspection=inspection,
            )
        except (CaptureError, StorageError, ValueError) as exc:
            form.capture_date.errors.append(str(exc))
        except IntegrityError:
            form.image.errors.append("The Capture could not be saved.")
        else:
            flash("Capture saved.", "success")
            return redirect(url_for("captures.library", patient_pk=patient.id))
    status = 400 if request.method == "POST" else 200
    return render_template(
        "captures/new.html",
        patient=patient,
        form=form,
        suggested_date=suggested_date,
    ), status


@captures_bp.post("/patients/<int:patient_pk>/captures/inspect")
@editor_required
def inspect_upload(patient_pk: int):
    _active_patient(patient_pk)
    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return jsonify(error="an image file is required"), 400
    try:
        inspection = _storage().inspect(_payload(upload))
    except (CaptureError, StorageError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(
        {
            "format": inspection.format,
            "width": inspection.width,
            "height": inspection.height,
            "suggested_capture_date": (
                inspection.suggested_capture_date.isoformat()
                if inspection.suggested_capture_date is not None
                else None
            ),
        }
    )


@captures_bp.get("/captures/<int:capture_pk>/preview")
@login_required
def preview(capture_pk: int):
    capture = _active_capture(capture_pk)
    try:
        handle = _storage().open_read(ManagedStorage.preview_key(capture.storage_key))
    except StorageError:
        abort(404)
    response = send_file(
        handle,
        mimetype="image/jpeg",
        download_name=f"capture-{capture.id}-preview.jpg",
        max_age=0,
        conditional=False,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@captures_bp.get("/captures/<int:capture_pk>/original")
@login_required
def original(capture_pk: int):
    capture = _active_capture(capture_pk)
    try:
        handle = _storage().open_read(capture.storage_key)
    except StorageError:
        abort(404)
    response = send_file(
        handle,
        mimetype=mimetype_for_format(capture.format),
        download_name=f"capture-{capture.id}.{FORMAT_EXTENSIONS[capture.format]}",
        max_age=0,
        conditional=False,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
