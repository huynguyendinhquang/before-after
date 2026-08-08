"""Comparison Set and Frame persistence workflows."""

from __future__ import annotations

import hashlib
import io
import math
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
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
from app.board import (
    CANVAS_PRESETS,
    CanvasRenderSpec,
    FrameRenderSpec,
    canvas_dimensions,
    encode,
    normalize_canvas_preset,
    render_canvas,
)
from app.captures import (
    CaptureError,
    CaptureReconciliationError,
    CaptureUploadForm,
    PendingCapture,
    list_captures,
    prepare_capture,
)
from app.db import db
from app.models import Capture, ComparisonSet, Export, Frame, Patient, User
from app.image_policy import DEFAULT_MAX_PIXELS, ImagePolicyError, read_bounded
from app.storage import ImageInspection, ManagedStorage, StorageError, StoredDerivative


comparisons_bp = Blueprint("comparisons", __name__)


@comparisons_bp.app_context_processor
def comparison_template_context() -> dict[str, object]:
    return {"canvas_presets": CANVAS_PRESETS}


class ComparisonError(ValueError):
    """Raised when a Comparison Set workflow cannot change state."""


class SamePatientError(ComparisonError):
    """Raised when a Frame would reference another Patient's Capture."""


class StaleVersionError(ComparisonError):
    """Raised when a reorder was based on an older Set version."""


class EditLeaseError(ComparisonError):
    """Raised when another Editor owns the Set's edit lease."""


class PreviewLimitError(ComparisonError):
    """Raised when a preview exceeds its configured resource budget."""


class ExportReconciliationError(ComparisonError):
    """Raised when the database outcome of an export cannot be confirmed."""


EDIT_LEASE_SECONDS = 5 * 60
DEFAULT_RENDER_DPI = 150
DEFAULT_PREVIEW_MAX_VISIBLE_FRAMES = 100
DEFAULT_PREVIEW_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_PREVIEW_MAX_PIXELS = DEFAULT_MAX_PIXELS


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
    if isinstance(set_id, bool) or not isinstance(set_id, int):
        return None
    return db.session.scalar(
        select(ComparisonSet)
        .where(ComparisonSet.id == set_id)
        .execution_options(autoflush=False)
        .with_for_update(of=ComparisonSet)
    )


def _server_now() -> datetime:
    """Read the database clock; leases must not depend on browser clocks."""
    value = db.session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise StaleVersionError("Set version is required")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StaleVersionError("Set version is required") from exc
    if parsed <= 0:
        raise StaleVersionError("Set version is required")
    return parsed


def _lease_is_active(comparison_set: ComparisonSet, actor_id: int, now: datetime) -> bool:
    expires = comparison_set.lock_expires_at
    if expires is None or expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc) if expires is not None else None
    return (
        comparison_set.lock_holder_id == actor_id
        and expires is not None
        and expires > now
    )


def _lease_is_available(comparison_set: ComparisonSet, now: datetime) -> bool:
    expires = comparison_set.lock_expires_at
    if expires is None or expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc) if expires is not None else None
    return comparison_set.lock_holder_id is None or expires is None or expires <= now


def _claim_lease_locked(
    comparison_set: ComparisonSet,
    actor: User,
    now: datetime,
    *,
    acquire_if_free: bool,
) -> None:
    if _lease_is_active(comparison_set, actor.id, now):
        return
    if _lease_is_available(comparison_set, now) and acquire_if_free:
        comparison_set.lock_holder_id = actor.id
        comparison_set.lock_expires_at = now + timedelta(seconds=EDIT_LEASE_SECONDS)
        return
    raise EditLeaseError("This Comparison Set is being edited by another user")


def acquire_edit_lease(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    expected_version: object,
) -> ComparisonSet:
    """Acquire or renew the five-minute Set lease using database server time."""
    _require_editor(actor)
    expected = _version(expected_version)
    comparison_set = _active_set(comparison_set)
    try:
        locked = _locked_comparison_set(comparison_set)
        if locked is None or locked.archived_at is not None:
            raise ComparisonError("Comparison Set is unavailable")
        if locked.version != expected:
            raise StaleVersionError("Set has changed; reload before acquiring the lease")
        now = _server_now()
        _claim_lease_locked(locked, actor, now, acquire_if_free=True)
        db.session.commit()
        return locked
    except Exception:
        db.session.rollback()
        raise


def heartbeat_edit_lease(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    expected_version: object,
) -> ComparisonSet:
    """Extend only the caller's still-active lease; never steal an expired one."""
    _require_editor(actor)
    expected = _version(expected_version)
    comparison_set = _active_set(comparison_set)
    try:
        locked = _locked_comparison_set(comparison_set)
        if locked is None or locked.archived_at is not None:
            raise ComparisonError("Comparison Set is unavailable")
        if locked.version != expected:
            raise StaleVersionError("Set has changed; reload before renewing the lease")
        now = _server_now()
        if not _lease_is_active(locked, actor.id, now):
            raise EditLeaseError("The edit lease has expired or belongs to another user")
        locked.lock_expires_at = now + timedelta(seconds=EDIT_LEASE_SECONDS)
        db.session.commit()
        return locked
    except Exception:
        db.session.rollback()
        raise


def release_edit_lease(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    expected_version: object,
) -> None:
    _require_editor(actor)
    expected = _version(expected_version)
    comparison_set = _active_set(comparison_set)
    try:
        locked = _locked_comparison_set(comparison_set)
        if locked is None or locked.archived_at is not None:
            raise ComparisonError("Comparison Set is unavailable")
        if locked.version != expected:
            raise StaleVersionError("Set has changed; reload before releasing the lease")
        if not _lease_is_active(locked, actor.id, _server_now()):
            raise EditLeaseError("The edit lease has expired or belongs to another user")
        locked.lock_holder_id = None
        locked.lock_expires_at = None
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def _write_guard(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    expected_version: object,
) -> tuple[ComparisonSet, int]:
    """Lock a Set row, verify its lease/version, and return it for mutation."""
    expected = _version(expected_version)
    comparison_set = _active_set(comparison_set)
    locked = _locked_comparison_set(comparison_set)
    if locked is None or locked.archived_at is not None:
        raise ComparisonError("Comparison Set is unavailable")
    if locked.version != expected:
        raise StaleVersionError("Set has changed; reload before saving")
    _claim_lease_locked(locked, actor, _server_now(), acquire_if_free=False)
    return locked, expected


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


def _label(value: object, field: str, *, maximum: int = 500, allow_blank: bool = True) -> str:
    if value is None and allow_blank:
        return ""
    if not isinstance(value, str):
        raise ComparisonError(f"{field} must be text")
    try:
        value = unicodedata.normalize("NFC", value)
        value.encode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise ComparisonError(f"{field} contains invalid Unicode") from exc
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ComparisonError(f"{field} contains invalid control characters")
    value = value.strip()
    if not allow_blank and not value:
        raise ComparisonError(f"{field} is required")
    if len(value) > maximum:
        raise ComparisonError(f"{field} is too long")
    return value


def _canvas_values(
    *,
    preset_key: object,
    width_mm: object | None,
    height_mm: object | None,
) -> tuple[str, Decimal, Decimal]:
    try:
        key = normalize_canvas_preset(preset_key)
    except (TypeError, ValueError) as exc:
        raise ComparisonError(str(exc)) from exc
    if key == "custom":
        # Form values arrive as strings.  Parse them exactly before asking the
        # board module to resolve a preset so zero, excess precision, and DB
        # bounds are rejected in one controlled path.
        persisted_width = _millimetres(width_mm, "Canvas width")
        persisted_height = _millimetres(height_mm, "Canvas height")
        canvas_dimensions(key, persisted_width, persisted_height)
    else:
        width, height = canvas_dimensions(key, width_mm, height_mm)
        # Validate caller-supplied dimensions even though a named preset owns
        # the final values.  This preserves the persisted two-decimal boundary.
        if width_mm is not None:
            _millimetres(width_mm, "Canvas width")
        if height_mm is not None:
            _millimetres(height_mm, "Canvas height")
        persisted_width = Decimal(str(width)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        persisted_height = Decimal(str(height)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return key, persisted_width, persisted_height


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
    try:
        value = unicodedata.normalize("NFC", value).strip()
        value.encode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise ComparisonError("Set name contains invalid Unicode") from exc
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ComparisonError("Set name contains invalid control characters")
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
    preset_key, width, height = _canvas_values(
        preset_key=preset_key,
        width_mm=canvas_width_mm,
        height_mm=canvas_height_mm,
    )
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
    expected_version: object,
) -> Frame:
    """Add an existing Capture or prepare a new Capture in one transaction."""
    _require_editor(actor)
    pending: PendingCapture | None = None
    try:
        comparison_set, _ = _write_guard(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=expected_version,
        )
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
            # prepare_capture may reconcile a duplicate in a separate session;
            # that rollback releases the Set row lock held above.
            comparison_set, _ = _write_guard(
                actor=actor,
                comparison_set=comparison_set,
                expected_version=expected_version,
            )

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
        locked_set, expected = _write_guard(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=expected_version,
        )
        if locked_set.patient is None or locked_set.patient.archived_at is not None:
            raise ComparisonError("Patient is unavailable")
        ordered = _apply_order_locked(locked_set, ordered_frame_ids)
        result = db.session.execute(
            update(ComparisonSet)
            .where(
                ComparisonSet.id == locked_set.id,
                ComparisonSet.version == expected,
            )
            .values(version=expected + 1, updated_by_id=actor.id)
        )
        if result.rowcount != 1:
            raise StaleVersionError("Set has changed; reload before reordering")
        locked_set.version = expected + 1
        locked_set.updated_by_id = actor.id
        append_audit(
            actor=actor,
            action="comparison_set.reorder",
            entity_type="comparison_set",
            entity_id=locked_set.id,
            details={"frame_ids": [frame.id for frame in ordered]},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return ordered


_UNSET = object()


def _apply_set_values(
    comparison_set: ComparisonSet,
    *,
    name: object = _UNSET,
    title: object = _UNSET,
    preset_key: object = _UNSET,
    canvas_width_mm: object = _UNSET,
    canvas_height_mm: object = _UNSET,
    frame_ratio: object = _UNSET,
    columns: object = _UNSET,
    show_patient_id: object = _UNSET,
    show_patient_name: object = _UNSET,
    show_birth_year: object = _UNSET,
    date_label_default: object = _UNSET,
) -> None:
    if title is not _UNSET and name is _UNSET:
        name = title
    if name is not _UNSET:
        comparison_set.name = _name(name)  # type: ignore[arg-type]

    if preset_key is not _UNSET or canvas_width_mm is not _UNSET or canvas_height_mm is not _UNSET:
        selected_preset = comparison_set.preset_key if preset_key is _UNSET else preset_key
        selected_width = comparison_set.canvas_width_mm if canvas_width_mm is _UNSET else canvas_width_mm
        selected_height = comparison_set.canvas_height_mm if canvas_height_mm is _UNSET else canvas_height_mm
        key, width, height = _canvas_values(
            preset_key=selected_preset,
            width_mm=selected_width,
            height_mm=selected_height,
        )
        comparison_set.preset_key = key
        comparison_set.canvas_width_mm = width
        comparison_set.canvas_height_mm = height

    if frame_ratio is not _UNSET:
        comparison_set.frame_ratio = _number(frame_ratio, "Frame ratio")
    if columns is not _UNSET:
        if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= 100:
            raise ComparisonError("Columns must be a positive integer")
        comparison_set.columns = columns
    for value, field in (
        (show_patient_id, "Patient ID output flag"),
        (show_patient_name, "Patient name output flag"),
        (show_birth_year, "birth year output flag"),
        (date_label_default, "Capture Date output flag"),
    ):
        if value is not _UNSET and not isinstance(value, bool):
            raise ComparisonError(f"{field} must be boolean")
    if show_patient_id is not _UNSET:
        comparison_set.show_patient_id = show_patient_id
    if show_patient_name is not _UNSET:
        comparison_set.show_patient_name = show_patient_name
    if show_birth_year is not _UNSET:
        comparison_set.show_birth_year = show_birth_year
    if date_label_default is not _UNSET:
        comparison_set.date_label_default = date_label_default


def _apply_frame_values(frame: Frame, values: dict[str, object]) -> None:
    if "visible" in values:
        if not isinstance(values["visible"], bool):
            raise ComparisonError("Frame visibility must be boolean")
        frame.visible = values["visible"]
    if "label" in values:
        label = _label(values["label"], "Frame label")
        frame.label = label or None
    if "date_visible_override" in values:
        override = values["date_visible_override"]
        if override is not None and not isinstance(override, bool):
            raise ComparisonError("Capture Date visibility override must be boolean or null")
        frame.date_visible_override = override
    for field, minimum, maximum in (
        ("zoom", 1.0, 5.0),
        ("pan_x", -1.0, 1.0),
        ("pan_y", -1.0, 1.0),
    ):
        if field in values:
            value = values[field]
            if isinstance(value, bool):
                raise ComparisonError(f"{field} is invalid")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ComparisonError(f"{field} is invalid") from exc
            if not math.isfinite(number) or not minimum <= number <= maximum:
                raise ComparisonError(f"{field} is invalid")
            setattr(frame, field, number)


def _apply_order_locked(
    locked_set: ComparisonSet,
    ordered_frame_ids: Sequence[int],
) -> list[Frame]:
    ordered_ids = _frame_ids(ordered_frame_ids)
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
    return [by_id[frame_id] for frame_id in ordered_ids]


def save_comparison_set(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    expected_version: object,
    frames: Sequence[dict[str, object]] | None = None,
    ordered_frame_ids: Sequence[int] | None = None,
    **values: object,
) -> ComparisonSet:
    """Atomically save Canvas, order, visibility, labels, and crop state."""
    _require_editor(actor)
    try:
        locked_set, expected = _write_guard(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=expected_version,
        )
        _apply_set_values(locked_set, **values)
        if ordered_frame_ids is not None:
            _apply_order_locked(locked_set, ordered_frame_ids)
        if frames is not None:
            by_id = {
                frame.id: frame
                for frame in db.session.scalars(
                    select(Frame).where(Frame.comparison_set_id == locked_set.id)
                )
            }
            for raw in frames:
                if not isinstance(raw, dict):
                    raise ComparisonError("Frame updates are invalid")
                frame_id = raw.get("id", raw.get("frame_id"))
                if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id not in by_id:
                    raise ComparisonError("Frame is unavailable")
                _apply_frame_values(
                    by_id[frame_id],
                    {key: value for key, value in raw.items() if key not in {"id", "frame_id"}},
                )
        locked_set.version = expected + 1
        locked_set.updated_by_id = actor.id
        append_audit(
            actor=actor,
            action="comparison_set.update",
            entity_type="comparison_set",
            entity_id=locked_set.id,
            details={"version": expected + 1},
        )
        db.session.commit()
        return locked_set
    except Exception:
        db.session.rollback()
        raise


def update_comparison_set(**kwargs: object) -> ComparisonSet:
    """Named workflow alias for callers updating only Set-level fields."""
    return save_comparison_set(**kwargs)  # type: ignore[arg-type]


def update_frame(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    frame_id: int,
    expected_version: object,
    **values: object,
) -> Frame:
    """Save one Frame placement while holding the Set lease/version."""
    _require_editor(actor)
    try:
        locked_set, expected = _write_guard(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=expected_version,
        )
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            raise ComparisonError("Frame is unavailable")
        frame = db.session.scalar(
            select(Frame).where(Frame.id == frame_id, Frame.comparison_set_id == locked_set.id)
        )
        if frame is None:
            raise ComparisonError("Frame is unavailable")
        _apply_frame_values(frame, values)
        locked_set.version = expected + 1
        locked_set.updated_by_id = actor.id
        append_audit(
            actor=actor,
            action="frame.update",
            entity_type="frame",
            entity_id=frame.id,
            details={"comparison_set_id": locked_set.id, "version": expected + 1},
        )
        db.session.commit()
        return frame
    except Exception:
        db.session.rollback()
        raise


def _preview_limit(name: str, default: int) -> int:
    value = current_app.config.get(name, default)
    if isinstance(value, bool):
        raise ComparisonError(f"{name} must be a positive integer")
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ComparisonError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ComparisonError(f"{name} must be a positive integer")
    return value


def _materialize_render_spec(
    comparison_set: ComparisonSet,
    *,
    expected_version: object | None = None,
) -> tuple[CanvasRenderSpec, int]:
    """Read one locked Set snapshot and its visible media into a render spec."""
    active = _active_set(comparison_set)
    set_id = active.id
    expected = _version(expected_version) if expected_version is not None else None
    max_frames = _preview_limit(
        "COMPARISON_PREVIEW_MAX_VISIBLE_FRAMES",
        DEFAULT_PREVIEW_MAX_VISIBLE_FRAMES,
    )
    max_bytes = _preview_limit("COMPARISON_PREVIEW_MAX_BYTES", DEFAULT_PREVIEW_MAX_BYTES)
    max_pixels = _preview_limit("COMPARISON_PREVIEW_MAX_PIXELS", DEFAULT_PREVIEW_MAX_PIXELS)
    storage = ManagedStorage(current_app.config["MEDIA_ROOT"])
    session = db.session.session_factory()
    try:
        with session.begin():
            locked = session.scalar(
                select(ComparisonSet)
                .where(ComparisonSet.id == set_id)
                .with_for_update(of=ComparisonSet)
            )
            if locked is None or locked.archived_at is not None:
                raise ComparisonError("Comparison Set is unavailable")
            version = int(locked.version)
            if expected is not None and version != expected:
                raise StaleVersionError("Set has changed; reload before previewing")
            patient = locked.patient
            if patient is None or patient.archived_at is not None:
                raise ComparisonError("Patient is unavailable")

            # Hidden Frames never need media access.  Check both bounds before
            # opening any image so a large Set cannot exhaust process memory.
            visible_frames = list(
                session.scalars(
                    select(Frame)
                    .where(
                        Frame.comparison_set_id == locked.id,
                        Frame.visible.is_(True),
                    )
                    .order_by(Frame.position, Frame.id)
                )
            )
            if len(visible_frames) > max_frames:
                raise PreviewLimitError("Preview contains too many visible Frames")
            total_bytes = 0
            total_pixels = 0
            for frame in visible_frames:
                try:
                    total_bytes += int(frame.capture.byte_count)
                    width = frame.capture.width
                    height = frame.capture.height
                    if (
                        isinstance(width, bool)
                        or not isinstance(width, int)
                        or width <= 0
                        or isinstance(height, bool)
                        or not isinstance(height, int)
                        or height <= 0
                    ):
                        raise ValueError
                    total_pixels += width * height
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ComparisonError("Capture media metadata is invalid") from exc
            if total_bytes > max_bytes:
                raise PreviewLimitError("Preview media exceeds its byte limit")
            if total_pixels > max_pixels:
                raise PreviewLimitError("Preview decoded pixels exceed their limit")

            frames: list[FrameRenderSpec] = []
            for frame in visible_frames:
                try:
                    with storage.open_read(frame.capture.storage_key) as source:
                        image_bytes = read_bounded(source, max_bytes=int(frame.capture.byte_count))
                except (ImagePolicyError, OSError, StorageError) as exc:
                    raise ComparisonError("Capture media is unavailable") from exc
                show_date = (
                    frame.date_visible_override
                    if frame.date_visible_override is not None
                    else locked.date_label_default
                )
                frames.append(
                    FrameRenderSpec(
                        id=frame.id,
                        image=image_bytes,
                        visible=True,
                        label=frame.label or "",
                        date_label=frame.capture.capture_date.isoformat() if show_date else None,
                        zoom=frame.zoom,
                        pan_x=frame.pan_x,
                        pan_y=frame.pan_y,
                    )
                )

            # The Set row lock blocks all editor mutations.  Recheck anyway so
            # an unexpected writer cannot make a mixed snapshot look current.
            current_version = session.scalar(
                select(ComparisonSet.version).where(ComparisonSet.id == locked.id)
            )
            if current_version != version:
                raise StaleVersionError("Set changed while preparing preview")
            spec = CanvasRenderSpec(
                width_mm=float(locked.canvas_width_mm),
                height_mm=float(locked.canvas_height_mm),
                frame_ratio=float(locked.frame_ratio),
                columns=int(locked.columns),
                frames=frames,
                title=locked.name,
                patient_id=patient.patient_id,
                patient_name=patient.name,
                birth_year=patient.birth_year,
                show_patient_id=bool(locked.show_patient_id),
                show_patient_name=bool(locked.show_patient_name),
                show_birth_year=bool(locked.show_birth_year),
                version=version,
            )
        return spec, version
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def render_spec_for_set(comparison_set: ComparisonSet, *, expected_version: object | None = None) -> CanvasRenderSpec:
    """Build render data from one persisted Set version without exposing media paths."""
    spec, _ = _materialize_render_spec(comparison_set, expected_version=expected_version)
    return spec


def render_persisted_set(
    comparison_set: ComparisonSet,
    *,
    expected_version: object | None = None,
    dpi: float = DEFAULT_RENDER_DPI,
) -> bytes:
    return render_persisted_set_versioned(
        comparison_set,
        expected_version=expected_version,
        dpi=dpi,
    )[0]


def _render_spec_bytes(
    spec: CanvasRenderSpec,
    output_format: str = "png",
    *,
    dpi: float = DEFAULT_RENDER_DPI,
) -> bytes:
    """Render and encode one immutable Set snapshot for preview or export."""
    image = render_canvas(spec, dpi=dpi)
    try:
        return encode(
            image,
            output_format,
            dpi=dpi,
            physical_size_mm=(spec.width_mm, spec.height_mm),
        )
    finally:
        image.close()


def render_persisted_set_versioned(
    comparison_set: ComparisonSet,
    *,
    expected_version: object | None = None,
    dpi: float = DEFAULT_RENDER_DPI,
) -> tuple[bytes, int]:
    """Render a materialized snapshot and return its exact persisted version."""
    spec, version = _materialize_render_spec(comparison_set, expected_version=expected_version)
    return _render_spec_bytes(spec, "png", dpi=dpi), version


def _export_format(value: object) -> str:
    if not isinstance(value, str):
        raise ComparisonError("Export format must be PNG or PDF")
    normalized = value.strip().lower().lstrip(".")
    if normalized not in {"png", "pdf"}:
        raise ComparisonError("Export format must be PNG or PDF")
    return normalized.upper()


def _reconcile_export(storage_key: str) -> Export | None:
    """Read committed export state in a new session after an unclear commit."""
    db.session.rollback()
    session = db.session.session_factory()
    try:
        exported = session.scalar(select(Export).where(Export.storage_key == storage_key))
        if exported is not None:
            session.expunge(exported)
        return exported
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _settle_export_attempt(
    managed: ManagedStorage,
    stored: StoredDerivative | None,
    committed: Export,
) -> None:
    if stored is None:
        return
    if committed.storage_key == stored.storage_key:
        try:
            managed.finalize(stored)
        except StorageError:
            # The row is durable; reconciliation can remove this marker later.
            pass
    else:
        managed.discard(stored)


def create_export(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    output_format: object,
    expected_version: object,
    storage: ManagedStorage | None = None,
    dpi: float = DEFAULT_RENDER_DPI,
) -> Export:
    """Render one requested Set version, persist its derivative, and audit it."""
    _require_editor(actor)
    format_name = _export_format(output_format)
    expected = _version(expected_version)
    spec, version = _materialize_render_spec(
        comparison_set,
        expected_version=expected,
    )
    payload = _render_spec_bytes(spec, format_name, dpi=dpi)
    managed = storage or ManagedStorage(current_app.config["MEDIA_ROOT"])
    stored: StoredDerivative | None = None
    try:
        stored = managed.store_derivative(payload, format_name)
        exported = Export(
            comparison_set_id=comparison_set.id,
            format=format_name,
            storage_key=stored.storage_key,
            byte_count=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            rendered_version=version,
            created_by_id=actor.id,
        )
        db.session.add(exported)
        db.session.flush()
        append_audit(
            actor=actor,
            action="export.create",
            entity_type="export",
            entity_id=exported.id,
            details={
                "format": format_name,
                "byte_count": len(payload),
                "sha256": exported.sha256,
                "rendered_version": version,
            },
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        if stored is None:
            raise
        try:
            committed = _reconcile_export(stored.storage_key)
        except Exception as reconciliation_exc:
            managed.preserve(stored)
            raise ExportReconciliationError(
                "Export save status could not be confirmed; pending derivative was preserved for reconciliation"
            ) from reconciliation_exc
        if committed is not None:
            _settle_export_attempt(managed, stored, committed)
            return committed
        managed.discard(stored)
        raise

    if stored is not None:
        try:
            managed.finalize(stored)
        except StorageError:
            # A committed export remains recoverable through reconciliation.
            pass
    return exported


# Descriptive alias for callers that name the operation rather than the row.
export_comparison_set = create_export


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


def _detail_flags(comparison_set: ComparisonSet) -> dict[str, bool]:
    now = _server_now()
    available = _lease_is_available(comparison_set, now)
    return {
        "can_edit": current_user.is_editor and _lease_is_active(comparison_set, current_user.id, now),
        "can_acquire": current_user.is_editor and available,
        "lease_active": not available,
    }


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
        **_detail_flags(comparison_set),
    )


def _detail_context(
    set_pk: int,
    patient_pk: int | None,
    frame_form: FrameForm,
) -> dict[str, object]:
    """Render detail errors with a fresh Set lease/version snapshot."""
    comparison_set = _route_set(set_pk, patient_pk)
    return {
        "comparison_set": comparison_set,
        "patient": comparison_set.patient,
        "captures": list_captures(comparison_set.patient),
        "frame_form": frame_form,
        **_detail_flags(comparison_set),
    }


@comparisons_bp.post("/comparison-sets/<int:set_pk>/frames")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/frames")
@editor_required
def add_frame_route(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    form = FrameForm()
    if not form.validate_on_submit():
        return render_template(
            "comparisons/detail.html",
            **_detail_context(set_pk, patient_pk, form),
        ), 400

    filename = getattr(form.image.data, "filename", None) if form.image.data else None
    expected_version = request.form.get("version")
    try:
        if form.capture_id.data:
            if form.image.data and filename:
                raise ComparisonError("choose an existing Capture or upload a new image")
            frame = add_frame(
                actor=current_user,
                comparison_set=comparison_set,
                capture_id=form.capture_id.data,
                expected_version=expected_version,
            )
        else:
            if not form.image.data or not filename:
                raise ComparisonError("choose an existing Capture or upload a new image")
            frame = add_frame(
                actor=current_user,
                comparison_set=comparison_set,
                upload=form.image.data,
                capture_date=form.capture_date.data,
                capture_date_confirmed=form.capture_date_confirmed.data,
                shot_type_name=form.shot_type_name.data,
                create_proposal=form.create_proposal.data,
                original_filename=filename,
                expected_version=expected_version,
            )
    except CaptureReconciliationError as exc:
        form.capture_id.errors.append(str(exc))
        return render_template(
            "comparisons/detail.html",
            **_detail_context(set_pk, patient_pk, form),
        ), 409
    except StaleVersionError as exc:
        form.capture_id.errors.append(str(exc))
        return render_template(
            "comparisons/detail.html",
            **_detail_context(set_pk, patient_pk, form),
        ), 409
    except EditLeaseError as exc:
        form.capture_id.errors.append(str(exc))
        return render_template(
            "comparisons/detail.html",
            **_detail_context(set_pk, patient_pk, form),
        ), 409
    except (CaptureError, ComparisonError, StorageError, IntegrityError) as exc:
        form.capture_id.errors.append(str(exc))
        return render_template(
            "comparisons/detail.html",
            **_detail_context(set_pk, patient_pk, form),
        ), 400
    flash("Frame added.", "success")
    new_version = db.session.scalar(
        select(ComparisonSet.version).where(ComparisonSet.id == comparison_set.id)
    )
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(frame_id=frame.id, version=new_version)
    response = redirect(url_for("comparisons.detail", set_pk=comparison_set.id))
    response.headers["X-Comparison-Set-Version"] = str(new_version)
    return response


def _request_payload() -> dict[str, object]:
    if request.is_json:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ComparisonError("JSON payload must be an object")
        return payload
    boolean_fields = {
        "show_patient_id",
        "show_patient_name",
        "show_birth_year",
        "date_label_default",
    }
    payload: dict[str, object] = {}
    for key in request.form:
        values = request.form.getlist(key)
        if key in boolean_fields:
            payload[key] = any(value.strip().casefold() in {"1", "true", "on", "yes"} for value in values)
        else:
            payload[key] = values[-1]
    if "columns" in payload and isinstance(payload["columns"], str):
        try:
            payload["columns"] = int(payload["columns"])
        except (TypeError, ValueError, OverflowError):
            pass
    return payload


@comparisons_bp.post("/comparison-sets/<int:set_pk>/save")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/save")
@editor_required
def save(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    try:
        payload = _request_payload()
        frames = payload.get("frames")
        if frames is not None and not isinstance(frames, list):
            raise ComparisonError("Frame updates are invalid")
        ordered = payload.get("frame_ids", payload.get("ordered_frame_ids"))
        if ordered is not None and not isinstance(ordered, list):
            raise ComparisonError("Frame order is invalid")
        values = {
            key: payload[key]
            for key in (
                "name",
                "title",
                "preset_key",
                "canvas_width_mm",
                "canvas_height_mm",
                "frame_ratio",
                "columns",
                "show_patient_id",
                "show_patient_name",
                "show_birth_year",
                "date_label_default",
            )
            if key in payload
        }
        saved = save_comparison_set(
            actor=current_user,
            comparison_set=comparison_set,
            expected_version=payload.get("version", payload.get("expected_version")),
            frames=frames,  # type: ignore[arg-type]
            ordered_frame_ids=ordered,  # type: ignore[arg-type]
            **values,
        )
    except StaleVersionError as exc:
        return jsonify(error=str(exc)), 409
    except EditLeaseError as exc:
        return jsonify(error=str(exc)), 409
    except IntegrityError:
        return jsonify(error="An active Set with this name already exists for the Patient"), 409
    except ComparisonError as exc:
        return jsonify(error=str(exc)), 400
    if request.is_json:
        return jsonify(version=saved.version)
    flash("Comparison Set saved.", "success")
    response = redirect(url_for("comparisons.detail", set_pk=saved.id))
    response.headers["X-Comparison-Set-Version"] = str(saved.version)
    return response


@comparisons_bp.post("/comparison-sets/<int:set_pk>/frames/<int:frame_id>")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/frames/<int:frame_id>")
@editor_required
def update_frame_route(set_pk: int, frame_id: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    try:
        payload = _request_payload()
        values = {key: value for key, value in payload.items() if key not in {"version", "expected_version"}}
        frame = update_frame(
            actor=current_user,
            comparison_set=comparison_set,
            frame_id=frame_id,
            expected_version=payload.get("version", payload.get("expected_version")),
            **values,
        )
    except StaleVersionError as exc:
        return jsonify(error=str(exc)), 409
    except EditLeaseError as exc:
        return jsonify(error=str(exc)), 409
    except ComparisonError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(frame_id=frame.id, version=frame.comparison_set.version)


@comparisons_bp.post("/comparison-sets/<int:set_pk>/lease/acquire")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/lease/acquire")
@comparisons_bp.post("/comparison-sets/<int:set_pk>/lease")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/lease")
@editor_required
def lease_acquire(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    try:
        payload = _request_payload()
        acquired = acquire_edit_lease(
            actor=current_user,
            comparison_set=comparison_set,
            expected_version=payload.get("version", payload.get("expected_version")),
        )
    except (StaleVersionError, EditLeaseError) as exc:
        return jsonify(error=str(exc)), 409
    except ComparisonError as exc:
        return jsonify(error=str(exc)), 400
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(version=acquired.version, expires_at=acquired.lock_expires_at.isoformat())
    flash("Edit lease acquired.", "success")
    return redirect(url_for("comparisons.detail", set_pk=acquired.id))


@comparisons_bp.post("/comparison-sets/<int:set_pk>/lease/heartbeat")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/lease/heartbeat")
@comparisons_bp.post("/comparison-sets/<int:set_pk>/heartbeat")
@editor_required
def lease_heartbeat(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    try:
        payload = _request_payload()
        renewed = heartbeat_edit_lease(
            actor=current_user,
            comparison_set=comparison_set,
            expected_version=payload.get("version", payload.get("expected_version")),
        )
    except (StaleVersionError, EditLeaseError) as exc:
        return jsonify(error=str(exc)), 409
    except ComparisonError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(version=renewed.version, expires_at=renewed.lock_expires_at.isoformat())


@comparisons_bp.post("/comparison-sets/<int:set_pk>/lease/release")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/lease/release")
@editor_required
def lease_release(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    try:
        payload = _request_payload()
        expected = _version(payload.get("version", payload.get("expected_version")))
        release_edit_lease(
            actor=current_user,
            comparison_set=comparison_set,
            expected_version=expected,
        )
    except (StaleVersionError, EditLeaseError) as exc:
        return jsonify(error=str(exc)), 409
    except ComparisonError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(released=True, version=expected)


@comparisons_bp.get("/comparison-sets/<int:set_pk>/preview")
@comparisons_bp.get("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/preview")
@comparisons_bp.get("/api/comparison-sets/<int:set_pk>/preview")
@login_required
def preview(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    raw_version = request.args.get("version")
    try:
        expected = _version(raw_version) if raw_version is not None else None
        payload, version = render_persisted_set_versioned(
            comparison_set,
            expected_version=expected,
            dpi=float(current_app.config.get("BOARD_RENDER_DPI", DEFAULT_RENDER_DPI)),
        )
    except StaleVersionError as exc:
        return jsonify(error=str(exc)), 409
    except PreviewLimitError as exc:
        return jsonify(error=str(exc)), 413
    except (ComparisonError, StorageError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    response = send_file(
        io.BytesIO(payload),
        mimetype="image/png",
        download_name=f"comparison-set-{comparison_set.id}-v{version}.png",
        max_age=0,
        conditional=False,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Comparison-Set-Version"] = str(version)
    return response


def _export_payload(exported: Export) -> bytes:
    storage: ManagedStorage | None = None
    try:
        byte_count = exported.byte_count
        digest = exported.sha256
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count <= 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise StorageError("export metadata is invalid")
        storage = ManagedStorage(current_app.config["MEDIA_ROOT"])
        with storage.open_read(exported.storage_key) as handle:
            payload = read_bounded(handle, max_bytes=byte_count)
        if len(payload) != byte_count or hashlib.sha256(payload).hexdigest() != digest:
            raise StorageError("export integrity check failed")
        return payload
    except (ImagePolicyError, OSError, StorageError, TypeError, ValueError, OverflowError):
        if storage is not None:
            try:
                storage.quarantine(exported.storage_key)
            except StorageError:
                pass
        abort(410)


def _export_response(exported: Export):
    # Exports inherit the same active Patient/Set visibility contract as the
    # owning Set routes; archived owners are never downloadable.
    if open_comparison_set(exported.comparison_set_id) is None:
        abort(404)
    payload = _export_payload(exported)
    try:
        format_name = exported.format
        if not isinstance(format_name, str):
            raise ValueError
        suffix = format_name.lower()
        if suffix not in {"png", "pdf"}:
            raise ValueError
    except (TypeError, ValueError):
        abort(500)
    mimetype = "image/png" if suffix == "png" else "application/pdf"
    response = send_file(
        io.BytesIO(payload),
        mimetype=mimetype,
        download_name=f"comparison-set-{exported.comparison_set_id}-v{exported.rendered_version}.{suffix}",
        max_age=0,
        conditional=False,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Comparison-Set-Version"] = str(exported.rendered_version)
    response.headers["X-Export-ID"] = str(exported.id)
    response.headers["X-Export-SHA256"] = exported.sha256
    return response


@comparisons_bp.post("/comparison-sets/<int:set_pk>/export")
@comparisons_bp.post("/comparison-sets/<int:set_pk>/export/<output_format>")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/export")
@comparisons_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/export/<output_format>")
@editor_required
def export_route(
    set_pk: int,
    output_format: str | None = None,
    patient_pk: int | None = None,
):
    comparison_set = _route_set(set_pk, patient_pk)
    try:
        payload = _request_payload()
        exported = create_export(
            actor=current_user,
            comparison_set=comparison_set,
            output_format=output_format or payload.get("format"),
            expected_version=payload.get("version", payload.get("expected_version")),
            dpi=float(current_app.config.get("BOARD_RENDER_DPI", DEFAULT_RENDER_DPI)),
        )
    except StaleVersionError as exc:
        return jsonify(error=str(exc)), 409
    except ExportReconciliationError as exc:
        return jsonify(error=str(exc)), 503
    except PreviewLimitError as exc:
        return jsonify(error=str(exc)), 413
    except (ComparisonError, StorageError, ValueError, IntegrityError) as exc:
        return jsonify(error=str(exc)), 400
    return _export_response(exported)


@comparisons_bp.get("/exports/<int:export_pk>")
@login_required
def export_download(export_pk: int):
    exported = db.session.get(Export, export_pk)
    if exported is None:
        abort(404)
    return _export_response(exported)


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
    except EditLeaseError as exc:
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
    response = redirect(url_for("comparisons.detail", set_pk=comparison_set.id))
    response.headers["X-Comparison-Set-Version"] = str(int(expected_version) + 1)
    return response
