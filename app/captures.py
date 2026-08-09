"""Capture Library, Shot Type selection, and single-image Capture workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import unicodedata

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from wtforms import BooleanField, FileField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, ValidationError

from app.audit import append_audit
from app.auth import editor_required
from app.db import db
from app.image_policy import FORMAT_EXTENSIONS, mimetype_for_format, read_bounded
from app.models import Capture, ComparisonSet, Frame, Patient, ShotType, User
from app.storage import CaptureQuarantine, ImageInspection, ManagedStorage, StorageError, StoredMedia


captures_bp = Blueprint("captures", __name__)


def _strict_confirmation(form, field) -> None:
    if getattr(form, "capture_id", None) is not None and form.capture_id.data:
        return
    if field.raw_data != ["y"]:
        raise ValidationError("Capture Date confirmation is required.")


class CaptureUploadForm(FlaskForm):
    image = FileField("Image")
    capture_date = StringField("Capture Date", validators=[Length(max=10)])
    capture_date_confirmed = BooleanField(
        "I confirm this Capture Date is correct (or I have overridden the EXIF suggestion).",
        validators=[_strict_confirmation],
    )
    shot_type_name = StringField("Shot Type", validators=[Length(max=200)])
    create_proposal = BooleanField("Use this name as a Shot Type Proposal")


class CaptureForm(CaptureUploadForm):
    image = FileField("Image", validators=[DataRequired()])
    submit = SubmitField("Save Capture")


class CaptureError(ValueError):
    """Raised when a Capture cannot be accepted without changing state."""


class ConsentRequired(CaptureError):
    """Raised before any image bytes are stored when consent is absent."""


class CaptureReconciliationError(CaptureError):
    """Raised when a failed commit cannot be verified safely."""


class CaptureReferencedError(CaptureError):
    """Raised when a Capture is still referenced by a Frame."""


class CaptureDeleteReconciliationError(CaptureError):
    """Raised when Capture deletion outcome or media restoration is unknown."""


class ShotTypeError(ValueError):
    """Raised when an Admin Shot Type workflow cannot change state."""


@dataclass
class PendingCapture:
    """A prepared Capture whose media still follows the caller's transaction."""

    capture: Capture
    managed: ManagedStorage
    stored: StoredMedia | None

    def finalize(self) -> None:
        if self.stored is not None:
            self.managed.finalize(self.stored)

    def discard(self) -> None:
        if self.stored is not None:
            self.managed.discard(self.stored)

    def preserve(self) -> None:
        if self.stored is not None:
            self.managed.preserve(self.stored)

    def settle(self, committed: Capture) -> None:
        _settle_committed_attempt(self.managed, self.stored, committed)


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


def _locked_shot_type(selected: ShotType) -> ShotType:
    """Reload and lock vocabulary state immediately before Capture insertion."""
    selected_id = selected.id
    locked = db.session.scalar(
        select(ShotType)
        .where(ShotType.id == selected_id)
        .with_for_update(of=ShotType)
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise CaptureError("Shot Type is unavailable")
    if locked.state == "merged":
        if locked.canonical_target_id is None:
            raise CaptureError("Shot Type is unavailable")
        locked = db.session.scalar(
            select(ShotType)
            .where(ShotType.id == locked.canonical_target_id)
            .with_for_update(of=ShotType)
            .execution_options(populate_existing=True)
        )
    if locked is None or locked.state not in {"canonical", "proposal"} or locked.canonical_target_id is not None:
        raise CaptureError("Shot Type is unavailable")
    return locked


def _existing_capture_candidate(patient_id: int, sha256: str) -> int | None:
    return db.session.scalar(
        select(Capture.id).where(Capture.patient_id == patient_id, Capture.sha256 == sha256)
    )


def _existing_capture(patient_id: int, sha256: str) -> Capture | None:
    """Lock a duplicate candidate before reporting it as the stored Capture.

    The key precheck avoids media work in the common case.  The second query
    serializes with delete_capture and refreshes every scalar before returning;
    a row deleted while the precheck was running produces a normal insert path.
    """
    candidate_id = _existing_capture_candidate(patient_id, sha256)
    if candidate_id is None:
        return None
    return db.session.scalar(
        select(Capture)
        .where(Capture.id == candidate_id)
        .with_for_update(of=Capture)
        .execution_options(populate_existing=True)
    )


def _current_patient(patient: Patient) -> Patient:
    if patient is None:
        raise CaptureError("Patient is unavailable")
    state = inspect(patient)
    patient_id = state.identity[0] if state.identity else state.dict.get("id")
    if isinstance(patient_id, bool) or not isinstance(patient_id, int):
        raise CaptureError("Patient is unavailable")
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
    if current is None:
        if state.dict.get("consent_confirmed_at", object()) is None:
            raise ConsentRequired("Consent Confirmation is required before storing images")
        raise CaptureError("Patient is unavailable")
    if current.archived_at is not None:
        raise CaptureError("Patient is unavailable")
    return current


def _reconcile_capture(patient_id: int, sha256: str) -> Capture | None:
    """Check commit state in a new session before deciding whether to clean media."""
    db.session.rollback()
    session = db.session.session_factory()
    try:
        capture = session.scalar(
            select(Capture).where(Capture.patient_id == patient_id, Capture.sha256 == sha256)
        )
        if capture is not None:
            session.expunge(capture)
        return capture
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _settle_committed_attempt(
    managed: ManagedStorage,
    stored: StoredMedia | None,
    committed: Capture,
) -> None:
    if stored is None:
        return
    winner_keys = {committed.storage_key}
    try:
        winner_keys.add(ManagedStorage.preview_key(committed.storage_key))
    except StorageError:
        pass
    if {stored.original_key, stored.preview_key} == winner_keys:
        try:
            managed.finalize(stored)
        except StorageError:
            pass
    else:
        managed.discard(stored)


def prepare_capture(
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
) -> PendingCapture:
    """Prepare one Capture while leaving commit ownership with the caller."""
    if not actor.is_editor:
        raise PermissionError("only an Editor or Admin can create Captures")
    patient = _current_patient(patient)
    if patient.consent_confirmed_at is None:
        raise ConsentRequired("Consent Confirmation is required before storing images")
    if capture_date_confirmed is not True:
        raise CaptureError("Capture Date must be explicitly confirmed")

    patient_id = patient.id
    authoritative_date = _capture_date(capture_date)
    payload = _payload(upload)
    managed = _storage(storage)
    if inspection is None:
        inspection = managed.inspect(payload)
    duplicate = _existing_capture(patient_id, inspection.sha256)
    if duplicate is not None:
        return PendingCapture(duplicate, managed, None)

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
        selected_shot_type = _locked_shot_type(selected_shot_type)
        capture = Capture(
            patient_id=patient_id,
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
        try:
            db.session.flush()
        except Exception:
            try:
                committed = _reconcile_capture(patient_id, inspection.sha256)
            except Exception as exc:
                if stored is not None:
                    managed.preserve(stored)
                raise CaptureReconciliationError(
                    "Capture save status could not be confirmed; pending media was preserved for reconciliation"
                ) from exc
            if committed is not None:
                _settle_committed_attempt(managed, stored, committed)
                return PendingCapture(committed, managed, None)
            raise
        append_audit(
            actor=actor,
            action="capture.create",
            entity_type="capture",
            entity_id=capture.id,
        )
    except CaptureReconciliationError:
        raise
    except Exception:
        if stored is not None:
            managed.discard(stored)
        raise
    return PendingCapture(capture, managed, stored)


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
    pending: PendingCapture | None = None
    try:
        pending = prepare_capture(
            actor=actor,
            patient=patient,
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
        if pending.stored is None:
            return pending.capture
        db.session.commit()
    except CaptureReconciliationError:
        db.session.rollback()
        if pending is not None:
            pending.preserve()
        raise
    except Exception:
        if pending is None:
            db.session.rollback()
        else:
            try:
                committed = _reconcile_capture(pending.capture.patient_id, pending.capture.sha256)
            except Exception as exc:
                db.session.rollback()
                pending.preserve()
                raise CaptureReconciliationError(
                    "Capture save status could not be confirmed; pending media was preserved for reconciliation"
                ) from exc
            if committed is not None:
                pending.settle(committed)
                return committed
            pending.discard()
        raise
    pending.finalize()
    return pending.capture


def _database_now() -> datetime:
    value = db.session.scalar(select(func.clock_timestamp()))
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _capture_id(value: Capture | int | None) -> int:
    if isinstance(value, Capture):
        state = inspect(value)
        value = state.identity[0] if state.identity else value.id
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CaptureError("Capture is unavailable")
    return value


def _require_admin(actor: User) -> None:
    if actor is None or not actor.active or actor.role != "admin":
        raise PermissionError("only an Admin can manage Shot Types")


def _lock_current_admin(actor: User) -> None:
    current = db.session.scalar(
        select(User.id)
        .where(User.id == actor.id, User.active.is_(True), User.role == "admin")
        .with_for_update(of=User)
    )
    if current is None:
        raise PermissionError("only an Admin can manage Shot Types")


def _shot_type_id(value: ShotType | int | None, field: str) -> int:
    if isinstance(value, ShotType):
        state = inspect(value)
        value = state.identity[0] if state.identity else value.id
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShotTypeError(f"{field} is unavailable")
    return value


def promote_shot_type(
    *,
    actor: User,
    shot_type: ShotType | int | None = None,
    shot_type_id: int | None = None,
) -> ShotType:
    """Promote one Editor proposal into a canonical Shot Type atomically."""
    _require_admin(actor)
    if shot_type is not None and shot_type_id is not None:
        raise ShotTypeError("choose one Shot Type")
    target_id = _shot_type_id(shot_type if shot_type is not None else shot_type_id, "Shot Type")
    try:
        _lock_current_admin(actor)
        promoted = db.session.scalar(
            select(ShotType)
            .where(ShotType.id == target_id)
            .with_for_update(of=ShotType)
        )
        if promoted is None:
            raise ShotTypeError("Shot Type is unavailable")
        if promoted.state != "proposal" or promoted.canonical_target_id is not None:
            raise ShotTypeError("only a Proposal can be promoted")
        promoted.state = "canonical"
        append_audit(
            actor=actor,
            action="shot_type.promote",
            entity_type="shot_type",
            entity_id=promoted.id,
        )
        db.session.commit()
        return promoted
    except Exception:
        db.session.rollback()
        raise


def merge_shot_type(
    *,
    actor: User,
    source: ShotType | int | None = None,
    target: ShotType | int | None = None,
    source_id: int | None = None,
    target_id: int | None = None,
) -> ShotType:
    """Merge a Proposal into a canonical Shot Type and repoint Captures."""
    _require_admin(actor)
    if source is not None and source_id is not None:
        raise ShotTypeError("choose one source Shot Type")
    if target is not None and target_id is not None:
        raise ShotTypeError("choose one target Shot Type")
    source_pk = _shot_type_id(source if source is not None else source_id, "source Shot Type")
    target_pk = _shot_type_id(target if target is not None else target_id, "target Shot Type")
    if source_pk == target_pk:
        raise ShotTypeError("a Shot Type cannot merge into itself")
    try:
        _lock_current_admin(actor)
        locked = {
            item.id: item
            for item in db.session.scalars(
                select(ShotType)
                .where(ShotType.id.in_([source_pk, target_pk]))
                .order_by(ShotType.id)
                .with_for_update(of=ShotType)
                .execution_options(populate_existing=True)
            )
        }
        source_row = locked.get(source_pk)
        target_row = locked.get(target_pk)
        if source_row is None or target_row is None:
            raise ShotTypeError("Shot Type is unavailable")
        if source_row.state == "merged":
            if source_row.canonical_target_id == target_row.id:
                db.session.commit()
                return target_row
            raise ShotTypeError("source Shot Type is already merged into another canonical Shot Type")
        if source_row.state != "proposal" or source_row.canonical_target_id is not None:
            raise ShotTypeError("source Shot Type must be an unmerged Proposal")
        if target_row.state != "canonical" or target_row.canonical_target_id is not None:
            raise ShotTypeError("target Shot Type must be canonical")
        capture_ids = list(
            db.session.scalars(
                select(Capture.id)
                .where(Capture.shot_type_id == source_row.id)
                .with_for_update(of=Capture)
            )
        )
        if capture_ids:
            db.session.execute(
                update(Capture)
                .where(Capture.shot_type_id == source_row.id)
                .values(shot_type_id=target_row.id, updated_by_id=actor.id)
            )
        source_row.state = "merged"
        source_row.canonical_target_id = target_row.id
        append_audit(
            actor=actor,
            action="shot_type.merge",
            entity_type="shot_type",
            entity_id=source_row.id,
            details={"target_id": target_row.id, "capture_count": len(capture_ids)},
        )
        db.session.commit()
        return source_row
    except Exception:
        db.session.rollback()
        raise


def _locked_capture(capture: Capture | int | None = None, capture_id: int | None = None) -> Capture:
    if capture is not None and capture_id is not None:
        raise CaptureError("choose one Capture")
    candidate = _capture_id(capture if capture is not None else capture_id)
    locked = db.session.scalar(
        select(Capture)
        .where(Capture.id == candidate)
        .with_for_update(of=Capture)
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise CaptureError("Capture is unavailable")
    patient = db.session.scalar(
        select(Patient).where(Patient.id == locked.patient_id).with_for_update(of=Patient)
    )
    if patient is None or patient.archived_at is not None:
        raise CaptureError("Patient is unavailable")
    locked.patient = patient
    return locked


def archive_capture(
    *,
    actor: User,
    capture: Capture | int | None = None,
    capture_id: int | None = None,
) -> Capture:
    """Hide a Capture while retaining all existing Frame references."""
    if not actor.is_editor:
        raise PermissionError("only an Editor or Admin can archive Captures")
    try:
        locked = _locked_capture(capture, capture_id)
        if locked.archived_at is None:
            locked.archived_at = _database_now()
            locked.archived_by_id = actor.id
            locked.updated_by_id = actor.id
            append_audit(actor=actor, action="capture.archive", entity_type="capture", entity_id=locked.id)
        db.session.commit()
        return locked
    except Exception:
        db.session.rollback()
        raise


def unarchive_capture(
    *,
    actor: User,
    capture: Capture | int | None = None,
    capture_id: int | None = None,
) -> Capture:
    """Restore a Capture to normal Library selection."""
    if not actor.is_editor:
        raise PermissionError("only an Editor or Admin can unarchive Captures")
    try:
        locked = _locked_capture(capture, capture_id)
        if locked.patient is None or locked.patient.archived_at is not None:
            raise CaptureError("Patient is unavailable")
        if locked.archived_at is not None:
            locked.archived_at = None
            locked.archived_by_id = None
            locked.updated_by_id = actor.id
            append_audit(actor=actor, action="capture.unarchive", entity_type="capture", entity_id=locked.id)
        db.session.commit()
        return locked
    except Exception:
        db.session.rollback()
        raise


def _capture_exists(capture_id: int) -> bool:
    session = db.session.session_factory()
    try:
        return session.scalar(select(Capture.id).where(Capture.id == capture_id)) is not None
    finally:
        session.close()


def delete_capture(
    *,
    actor: User,
    capture: Capture | int | None = None,
    capture_id: int | None = None,
    storage: ManagedStorage | None = None,
) -> Capture:
    """Delete an unreferenced Capture with durable quarantine intent."""
    if not actor.is_editor:
        raise PermissionError("only an Editor or Admin can delete Captures")
    managed = storage or _storage()
    manifest: CaptureQuarantine | None = None
    locked: Capture | None = None
    committed = False
    try:
        with managed.reconciliation_lock():
            locked = _locked_capture(capture, capture_id)
            referenced = db.session.scalar(
                select(Frame.id)
                .where(Frame.capture_id == locked.id)
                .with_for_update(of=Frame)
                .limit(1)
            )
            if referenced is not None:
                raise CaptureReferencedError("Capture is still referenced by a Comparison Set")
            media_keys = [locked.storage_key, ManagedStorage.preview_key(locked.storage_key)]
            manifest = managed.prepare_capture_quarantine(locked.id, media_keys)
            managed.quarantine_capture(manifest)
            capture_id_value = locked.id
            db.session.delete(locked)
            db.session.flush()
            append_audit(
                actor=actor,
                action="capture.delete",
                entity_type="capture",
                entity_id=capture_id_value,
            )
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                try:
                    still_exists = _capture_exists(capture_id_value)
                except Exception as reconcile_exc:
                    raise CaptureDeleteReconciliationError(
                        "Capture deletion outcome could not be confirmed; quarantined media was preserved"
                    ) from reconcile_exc
                if still_exists:
                    try:
                        managed.restore_capture_quarantine(manifest)
                    except Exception as restore_exc:
                        raise CaptureDeleteReconciliationError(
                            "Capture deletion failed and media restoration could not be confirmed"
                        ) from restore_exc
                    manifest = None
                    raise
                committed = True
                try:
                    managed.finish_capture_quarantine(manifest)
                except StorageError as finish_exc:
                    raise CaptureDeleteReconciliationError(
                        "Capture was deleted but quarantined media cleanup is pending"
                    ) from finish_exc
                manifest = None
                return locked
            committed = True
            try:
                managed.finish_capture_quarantine(manifest)
            except StorageError as finish_exc:
                raise CaptureDeleteReconciliationError(
                    "Capture was deleted but quarantined media cleanup is pending"
                ) from finish_exc
            manifest = None
            return locked
    except CaptureDeleteReconciliationError:
        raise
    except Exception:
        db.session.rollback()
        if manifest is not None and not committed:
            try:
                managed.restore_capture_quarantine(manifest)
            except Exception as restore_exc:
                raise CaptureDeleteReconciliationError(
                    "Capture deletion failed and media restoration could not be confirmed"
                ) from restore_exc
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


def list_captures(patient: Patient, *, include_archived: bool = False) -> list[Capture]:
    statement = select(Capture).where(Capture.patient_id == patient.id)
    if not include_archived:
        statement = statement.where(Capture.archived_at.is_(None))
    return list(db.session.scalars(statement.order_by(Capture.capture_date, Capture.id)))


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
    include_archived = current_user.is_editor and request.args.get("archived") == "1"
    return render_template(
        "captures/library.html",
        patient=patient,
        captures=list_captures(patient, include_archived=include_archived),
        include_archived=include_archived,
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
            create_capture(
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


def _media_capture(capture_pk: int) -> Capture:
    capture = db.session.get(Capture, capture_pk)
    if capture is None:
        abort(404)
    if capture.archived_at is None and capture.patient is not None and capture.patient.archived_at is None:
        return capture
    if (
        request.args.get("archived") == "1"
        and current_user.is_editor
        and capture.patient is not None
        and capture.patient.archived_at is None
    ):
        return capture
    set_id = request.args.get("set_id", type=int)
    if set_id is None:
        abort(404)
    referenced = db.session.scalar(
        select(Frame.id)
        .join(ComparisonSet, ComparisonSet.id == Frame.comparison_set_id)
        .where(
            Frame.capture_id == capture.id,
            ComparisonSet.id == set_id,
            ComparisonSet.archived_at.is_(None),
            ComparisonSet.patient_id == capture.patient_id,
        )
    )
    if referenced is None or capture.patient is None or capture.patient.archived_at is not None:
        abort(404)
    return capture


@captures_bp.get("/captures/<int:capture_pk>/preview")
@login_required
def preview(capture_pk: int):
    capture = _media_capture(capture_pk)
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
    capture = _media_capture(capture_pk)
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


def _lifecycle_capture(capture_pk: int) -> Capture:
    capture = db.session.get(Capture, capture_pk)
    if capture is None or capture.patient is None or capture.patient.archived_at is not None:
        abort(404)
    return capture


def _lifecycle_redirect(capture: Capture):
    return redirect(url_for("captures.library", patient_pk=capture.patient_id))


@captures_bp.post("/captures/<int:capture_pk>/archive")
@captures_bp.post("/patients/<int:patient_pk>/captures/<int:capture_pk>/archive")
@editor_required
def archive_route(capture_pk: int, patient_pk: int | None = None):
    capture = _lifecycle_capture(capture_pk)
    if patient_pk is not None and capture.patient_id != patient_pk:
        abort(404)
    try:
        archive_capture(actor=current_user, capture=capture)
    except CaptureError as exc:
        flash(str(exc), "error")
        return _lifecycle_redirect(capture), 400
    flash("Capture archived.", "success")
    return _lifecycle_redirect(capture)


@captures_bp.post("/captures/<int:capture_pk>/unarchive")
@captures_bp.post("/patients/<int:patient_pk>/captures/<int:capture_pk>/unarchive")
@editor_required
def unarchive_route(capture_pk: int, patient_pk: int | None = None):
    capture = _lifecycle_capture(capture_pk)
    if patient_pk is not None and capture.patient_id != patient_pk:
        abort(404)
    try:
        unarchive_capture(actor=current_user, capture=capture)
    except CaptureError as exc:
        flash(str(exc), "error")
        return _lifecycle_redirect(capture), 400
    flash("Capture restored.", "success")
    return _lifecycle_redirect(capture)


@captures_bp.post("/captures/<int:capture_pk>/delete")
@captures_bp.post("/patients/<int:patient_pk>/captures/<int:capture_pk>/delete")
@editor_required
def delete_route(capture_pk: int, patient_pk: int | None = None):
    capture = _lifecycle_capture(capture_pk)
    if patient_pk is not None and capture.patient_id != patient_pk:
        abort(404)
    try:
        delete_capture(actor=current_user, capture=capture)
    except CaptureReferencedError as exc:
        flash(str(exc), "error")
        return _lifecycle_redirect(capture), 409
    except (CaptureReconciliationError, CaptureDeleteReconciliationError) as exc:
        flash(str(exc), "error")
        return _lifecycle_redirect(capture), 503
    except CaptureError as exc:
        flash(str(exc), "error")
        return _lifecycle_redirect(capture), 400
    flash("Capture deleted.", "success")
    return _lifecycle_redirect(capture)
