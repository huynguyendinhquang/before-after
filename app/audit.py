"""Append-only audit events and safe audit-view projections.

Callers own the transaction: append_audit only adds an event to the current
session so the mutation and its audit row commit or roll back together.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from sqlalchemy import desc, select

from app.db import db
from app.models import AuditEvent, User

SYSTEM_ACTOR = "system/bootstrap"
_MAX_DETAIL_KEYS = 20
_MAX_DETAIL_ITEMS = 20
_MAX_DETAIL_TEXT = 64
_VIEWABLE_DETAIL_KEYS = frozenset(
    {
        "active",
        "byte_count",
        "capture_count",
        "capture_id",
        "comparison_set_id",
        "format",
        "frame_count",
        "frame_ids",
        "moved_captures",
        "rendered_version",
        "role",
        "sha256",
        "source_id",
        "source_set_id",
        "source_shot_type_id",
        "target_id",
        "target_shot_type_id",
        "version",
    }
)


def append_audit(
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: int | str,
    details: Mapping[str, object] | None = None,
) -> AuditEvent:
    event_details = dict(details or {})
    if actor is None:
        event_details["actor"] = SYSTEM_ACTOR
    event = AuditEvent(
        actor_id=actor.id if actor is not None else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=event_details,
    )
    db.session.add(event)
    return event


def _safe_detail_value(value: object) -> object | None:
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_MAX_DETAIL_TEXT]
    if isinstance(value, list):
        result: list[object] = []
        for item in value[:_MAX_DETAIL_ITEMS]:
            safe = _safe_detail_value(item)
            if safe is not None and not isinstance(safe, list):
                result.append(safe)
        return result
    return None


def bounded_audit_details(details: object) -> dict[str, object]:
    """Project audit JSON to a small allowlisted, non-PII display shape."""
    if not isinstance(details, Mapping):
        return {}
    result: dict[str, object] = {}
    for key, value in details.items():
        if len(result) >= _MAX_DETAIL_KEYS or not isinstance(key, str):
            break
        if key not in _VIEWABLE_DETAIL_KEYS:
            continue
        safe = _safe_detail_value(value)
        if safe is not None:
            result[key] = safe
    return result


def list_audit_events(*, limit: int = 50, before_id: int | None = None) -> list[AuditEvent]:
    """Return a hard-bounded newest-first audit page."""
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        limit = 50
    limit = max(1, min(limit, 100))
    statement = select(AuditEvent).order_by(desc(AuditEvent.id)).limit(limit)
    if before_id is not None and isinstance(before_id, int) and before_id > 0:
        statement = statement.where(AuditEvent.id < before_id)
    return list(db.session.scalars(statement))
