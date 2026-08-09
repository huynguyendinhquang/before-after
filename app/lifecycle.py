"""Comparison Set lifecycle and Frame removal workflows."""

from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from wtforms import StringField, SubmitField
from wtforms.validators import Length

from app.auth import editor_required
from app.comparisons import (
    ComparisonError,
    EditLeaseError,
    SetLifecycleError,
    StaleVersionError,
    _name,
    _request_payload,
    _route_set,
    _server_now,
    _version,
    _write_guard,
)
from app.db import db
from app.models import ComparisonSet, Frame, Patient, User


lifecycle_bp = Blueprint("lifecycle", __name__)


class DuplicateSetForm(FlaskForm):
    name = StringField("Duplicate name", validators=[Length(max=200)])
    submit = SubmitField("Duplicate Set")


def append_audit(**kwargs: object):
    """Keep the old comparisons-module audit seam while separated."""
    from app import comparisons

    return comparisons.append_audit(**kwargs)


def _set_id(value: ComparisonSet | int | None) -> int:
    if isinstance(value, ComparisonSet):
        state = inspect(value)
        value = state.identity[0] if state.identity else value.id
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SetLifecycleError("Comparison Set is unavailable")
    return value


def duplicate_comparison_set(
    *,
    actor: User,
    comparison_set: ComparisonSet | int | None = None,
    set_id: int | None = None,
    name: str | None = None,
    expected_version: object | None = None,
) -> ComparisonSet:
    """Duplicate a Set snapshot after checking its expected persisted version."""
    if actor is None or not actor.is_editor:
        raise PermissionError("only an Editor or Admin can change Comparison Sets")
    if comparison_set is not None and set_id is not None:
        raise SetLifecycleError("choose one Comparison Set")
    source_id = _set_id(comparison_set if comparison_set is not None else set_id)
    duplicate_name = _name(name)
    try:
        source = db.session.scalar(
            select(ComparisonSet)
            .where(ComparisonSet.id == source_id)
            .with_for_update(of=ComparisonSet)
            .execution_options(populate_existing=True)
        )
        if source is None or source.archived_at is not None:
            raise SetLifecycleError("Comparison Set is unavailable")
        expected = source.version if expected_version is None else _version(expected_version)
        if source.version != expected:
            raise StaleVersionError("Set has changed; reload before duplicating")
        patient = _locked_patient(source.patient)
        source.patient = patient
        active_name = db.session.scalar(
            select(ComparisonSet.id).where(
                ComparisonSet.patient_id == source.patient_id,
                ComparisonSet.archived_at.is_(None),
                func.lower(ComparisonSet.name) == duplicate_name.lower(),
            )
        )
        if active_name is not None:
            raise SetLifecycleError("An active Set with this name already exists for the Patient")
        frames = list(
            db.session.scalars(
                select(Frame)
                .where(Frame.comparison_set_id == source.id)
                .order_by(Frame.position, Frame.id)
                .with_for_update(of=Frame)
            )
        )
        duplicate = ComparisonSet(
            patient_id=source.patient_id,
            name=duplicate_name,
            canvas_width_mm=source.canvas_width_mm,
            canvas_height_mm=source.canvas_height_mm,
            preset_key=source.preset_key,
            frame_ratio=source.frame_ratio,
            columns=source.columns,
            show_patient_id=source.show_patient_id,
            show_patient_name=source.show_patient_name,
            show_birth_year=source.show_birth_year,
            date_label_default=source.date_label_default,
            version=1,
            lock_holder_id=None,
            lock_expires_at=None,
            archived_at=None,
            created_by_id=actor.id,
            updated_by_id=actor.id,
        )
        db.session.add(duplicate)
        db.session.flush()
        for source_frame in frames:
            db.session.add(
                Frame(
                    comparison_set_id=duplicate.id,
                    capture_id=source_frame.capture_id,
                    position=source_frame.position,
                    visible=source_frame.visible,
                    label=source_frame.label,
                    date_visible_override=source_frame.date_visible_override,
                    zoom=source_frame.zoom,
                    pan_x=source_frame.pan_x,
                    pan_y=source_frame.pan_y,
                )
            )
        db.session.flush()
        append_audit(
            actor=actor,
            action="comparison_set.duplicate",
            entity_type="comparison_set",
            entity_id=duplicate.id,
            details={"source_id": source.id, "frame_count": len(frames)},
        )
        db.session.commit()
        return duplicate
    except Exception:
        db.session.rollback()
        raise


def _locked_patient(patient: Patient | None) -> Patient:
    if patient is None:
        raise SetLifecycleError("Patient is unavailable")
    state = inspect(patient)
    patient_id = state.identity[0] if state.identity else state.dict.get("id")
    if isinstance(patient_id, bool) or not isinstance(patient_id, int):
        raise SetLifecycleError("Patient is unavailable")
    locked = db.session.scalar(
        select(Patient)
        .where(Patient.id == patient_id)
        .with_for_update(of=Patient)
        .execution_options(populate_existing=True)
    )
    if locked is None or locked.archived_at is not None:
        raise SetLifecycleError("Patient is unavailable")
    return locked


def _locked_set(set_id: int) -> ComparisonSet:
    locked = db.session.scalar(
        select(ComparisonSet)
        .where(ComparisonSet.id == set_id)
        .with_for_update(of=ComparisonSet)
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise SetLifecycleError("Comparison Set is unavailable")
    patient = db.session.scalar(
        select(Patient)
        .where(Patient.id == locked.patient_id)
        .with_for_update(of=Patient)
        .execution_options(populate_existing=True)
    )
    if patient is None or patient.archived_at is not None:
        raise SetLifecycleError("Patient is unavailable")
    locked.patient = patient
    return locked


def archive_comparison_set(
    *,
    actor: User,
    comparison_set: ComparisonSet | int | None = None,
    set_id: int | None = None,
) -> ComparisonSet:
    """Archive a Set without deleting its Frames or Capture references."""
    if actor is None or not actor.is_editor:
        raise PermissionError("only an Editor or Admin can change Comparison Sets")
    if comparison_set is not None and set_id is not None:
        raise SetLifecycleError("choose one Comparison Set")
    try:
        locked = _locked_set(_set_id(comparison_set if comparison_set is not None else set_id))
        if locked.archived_at is None:
            locked.archived_at = _server_now()
            locked.archived_by_id = actor.id
            locked.lock_holder_id = None
            locked.lock_expires_at = None
            locked.updated_by_id = actor.id
            locked.version += 1
            append_audit(actor=actor, action="comparison_set.archive", entity_type="comparison_set", entity_id=locked.id)
        db.session.commit()
        return locked
    except Exception:
        db.session.rollback()
        raise


def unarchive_comparison_set(
    *,
    actor: User,
    comparison_set: ComparisonSet | int | None = None,
    set_id: int | None = None,
) -> ComparisonSet:
    """Restore an archived Set when its active name remains unique."""
    if actor is None or not actor.is_editor:
        raise PermissionError("only an Editor or Admin can change Comparison Sets")
    if comparison_set is not None and set_id is not None:
        raise SetLifecycleError("choose one Comparison Set")
    try:
        locked = _locked_set(_set_id(comparison_set if comparison_set is not None else set_id))
        if locked.patient is None or locked.patient.archived_at is not None:
            raise SetLifecycleError("Patient is unavailable")
        if locked.archived_at is not None:
            duplicate = db.session.scalar(
                select(ComparisonSet.id).where(
                    ComparisonSet.id != locked.id,
                    ComparisonSet.patient_id == locked.patient_id,
                    ComparisonSet.archived_at.is_(None),
                    func.lower(ComparisonSet.name) == locked.name.lower(),
                )
            )
            if duplicate is not None:
                raise SetLifecycleError("An active Set with this name already exists for the Patient")
            locked.archived_at = None
            locked.archived_by_id = None
            locked.updated_by_id = actor.id
            locked.version += 1
            append_audit(actor=actor, action="comparison_set.unarchive", entity_type="comparison_set", entity_id=locked.id)
        db.session.commit()
        return locked
    except Exception:
        db.session.rollback()
        raise


def _lifecycle_set(set_pk: int, patient_pk: int | None = None) -> ComparisonSet:
    comparison_set = db.session.get(ComparisonSet, set_pk)
    if (
        comparison_set is None
        or comparison_set.patient is None
        or comparison_set.patient.archived_at is not None
        or (patient_pk is not None and comparison_set.patient_id != patient_pk)
    ):
        abort(404)
    return comparison_set


@lifecycle_bp.post("/comparison-sets/<int:set_pk>/duplicate")
@lifecycle_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/duplicate")
@editor_required
def duplicate_route(set_pk: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    form = DuplicateSetForm()
    if not form.validate_on_submit():
        return redirect(url_for("comparisons.detail", set_pk=set_pk)), 400
    try:
        duplicate = duplicate_comparison_set(
            actor=current_user,
            comparison_set=comparison_set,
            name=form.name.data,
            expected_version=request.form.get("version"),
        )
    except (SetLifecycleError, StaleVersionError, IntegrityError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("comparisons.detail", set_pk=set_pk)), 409
    flash("Comparison Set duplicated.", "success")
    return redirect(url_for("comparisons.detail", set_pk=duplicate.id))


@lifecycle_bp.post("/comparison-sets/<int:set_pk>/archive")
@lifecycle_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/archive")
@editor_required
def archive_route(set_pk: int, patient_pk: int | None = None):
    comparison_set = _lifecycle_set(set_pk, patient_pk)
    try:
        archive_comparison_set(actor=current_user, comparison_set=comparison_set)
    except SetLifecycleError as exc:
        flash(str(exc), "error")
        return redirect(url_for("comparisons.index", patient_pk=comparison_set.patient_id)), 400
    flash("Comparison Set archived.", "success")
    return redirect(url_for("comparisons.index", patient_pk=comparison_set.patient_id))


@lifecycle_bp.post("/comparison-sets/<int:set_pk>/unarchive")
@lifecycle_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/unarchive")
@editor_required
def unarchive_route(set_pk: int, patient_pk: int | None = None):
    comparison_set = _lifecycle_set(set_pk, patient_pk)
    try:
        unarchive_comparison_set(actor=current_user, comparison_set=comparison_set)
    except (SetLifecycleError, IntegrityError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("comparisons.index", patient_pk=comparison_set.patient_id, archived=1)), 409
    flash("Comparison Set restored.", "success")
    return redirect(url_for("comparisons.index", patient_pk=comparison_set.patient_id))


def remove_frame(
    *,
    actor: User,
    comparison_set: ComparisonSet,
    frame_id: int,
    expected_version: object,
) -> Frame:
    """Remove one Frame only under the Set's active lease and version."""
    if actor is None or not actor.is_editor:
        raise PermissionError("only an Editor or Admin can change Comparison Sets")
    try:
        locked_set, expected = _write_guard(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=expected_version,
        )
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id <= 0:
            raise ComparisonError("Frame is unavailable")
        frame = db.session.scalar(
            select(Frame)
            .where(Frame.id == frame_id, Frame.comparison_set_id == locked_set.id)
            .with_for_update(of=Frame)
        )
        if frame is None:
            raise ComparisonError("Frame is unavailable")
        remaining = list(
            db.session.scalars(
                select(Frame)
                .where(Frame.comparison_set_id == locked_set.id, Frame.id != frame.id)
                .order_by(Frame.position, Frame.id)
                .with_for_update(of=Frame)
            )
        )
        db.session.delete(frame)
        db.session.flush()
        offset = max((item.position for item in remaining), default=-1) + len(remaining) + 1
        for index, item in enumerate(remaining):
            item.position = offset + index
        db.session.flush()
        for index, item in enumerate(remaining):
            item.position = index
        locked_set.version = expected + 1
        locked_set.updated_by_id = actor.id
        append_audit(
            actor=actor,
            action="frame.remove",
            entity_type="frame",
            entity_id=frame.id,
            details={"comparison_set_id": locked_set.id, "version": expected + 1},
        )
        db.session.commit()
        return frame
    except Exception:
        db.session.rollback()
        raise


@lifecycle_bp.post("/comparison-sets/<int:set_pk>/frames/<int:frame_id>/remove")
@lifecycle_bp.post("/comparison-sets/<int:set_pk>/frames/<int:frame_id>/delete")
@lifecycle_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/frames/<int:frame_id>/remove")
@lifecycle_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/frames/<int:frame_id>/delete")
@editor_required
def remove_frame_route(set_pk: int, frame_id: int, patient_pk: int | None = None):
    comparison_set = _route_set(set_pk, patient_pk)
    try:
        payload = _request_payload()
        removed = remove_frame(
            actor=current_user,
            comparison_set=comparison_set,
            frame_id=frame_id,
            expected_version=payload.get("version", payload.get("expected_version")),
        )
    except (StaleVersionError, EditLeaseError) as exc:
        return jsonify(error=str(exc)), 409
    except ComparisonError as exc:
        return jsonify(error=str(exc)), 400
    version = db.session.scalar(
        select(ComparisonSet.version).where(ComparisonSet.id == set_pk)
    )
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(frame_id=removed.id, version=version)
    response = redirect(url_for("comparisons.detail", set_pk=set_pk))
    response.headers["X-Comparison-Set-Version"] = str(version)
    return response
