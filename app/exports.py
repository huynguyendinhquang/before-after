"""Comparison Set rendering, export persistence, and download workflows."""

from __future__ import annotations

import hashlib
import io

from flask import Blueprint, abort, current_app, jsonify, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import editor_required
from app.board import CanvasRenderSpec, FrameRenderSpec, encode, render_canvas
from app.comparisons import (
    DEFAULT_RENDER_DPI,
    ComparisonError,
    ExportReconciliationError,
    PreviewLimitError,
    StaleVersionError,
    _active_set,
    _request_payload,
    _route_set,
    _version,
    open_comparison_set,
)
from app.db import db
from app.image_policy import DEFAULT_MAX_PIXELS, ImagePolicyError, read_bounded
from app.models import ComparisonSet, Export, Frame
from app.storage import ManagedStorage, StorageError, StoredDerivative


exports_bp = Blueprint("exports", __name__)

DEFAULT_PREVIEW_MAX_VISIBLE_FRAMES = 100
DEFAULT_PREVIEW_MAX_BYTES = 50 * 1024 * 1024


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


def append_audit(**kwargs: object):
    """Keep the old comparisons-module monkeypatch seam while separated."""
    from app import comparisons

    return comparisons.append_audit(**kwargs)


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
    max_pixels = _preview_limit("COMPARISON_PREVIEW_MAX_PIXELS", DEFAULT_MAX_PIXELS)
    storage = ManagedStorage(current_app.config["MEDIA_ROOT"])
    session = db.session.session_factory()
    try:
        with session.begin():
            locked = session.scalar(
                select(ComparisonSet)
                .where(ComparisonSet.id == set_id)
                .with_for_update(of=ComparisonSet)
                .execution_options(populate_existing=True)
            )
            if locked is None or locked.archived_at is not None:
                raise ComparisonError("Comparison Set is unavailable")
            version = int(locked.version)
            if expected is not None and version != expected:
                raise StaleVersionError("Set has changed; reload before previewing")
            patient = locked.patient
            if patient is None or patient.archived_at is not None:
                raise ComparisonError("Patient is unavailable")

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


def render_spec_for_set(
    comparison_set: ComparisonSet,
    *,
    expected_version: object | None = None,
) -> CanvasRenderSpec:
    """Build render data from one persisted Set version without media paths."""
    spec, _ = _materialize_render_spec(comparison_set, expected_version=expected_version)
    return spec


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
            pass
    else:
        managed.discard(stored)


def create_export(
    *,
    actor,
    comparison_set: ComparisonSet,
    output_format: object,
    expected_version: object,
    storage: ManagedStorage | None = None,
    dpi: float = DEFAULT_RENDER_DPI,
) -> Export:
    """Render one requested Set version, persist its derivative, and audit it."""
    if actor is None or not actor.is_editor:
        raise PermissionError("only an Editor or Admin can export Comparison Sets")
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
            pass
    return exported


export_comparison_set = create_export


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


@exports_bp.get("/comparison-sets/<int:set_pk>/preview")
@exports_bp.get("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/preview")
@exports_bp.get("/api/comparison-sets/<int:set_pk>/preview")
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


@exports_bp.post("/comparison-sets/<int:set_pk>/export")
@exports_bp.post("/comparison-sets/<int:set_pk>/export/<output_format>")
@exports_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/export")
@exports_bp.post("/patients/<int:patient_pk>/comparison-sets/<int:set_pk>/export/<output_format>")
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


@exports_bp.get("/exports/<int:export_pk>")
@login_required
def export_download(export_pk: int):
    exported = db.session.get(Export, export_pk)
    if exported is None:
        abort(404)
    return _export_response(exported)
