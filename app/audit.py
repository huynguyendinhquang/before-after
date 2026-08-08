"""Append-only audit events.

Callers own the transaction: this function only adds the event to the current
session so the mutation and its audit row commit or roll back together.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.db import db
from app.models import AuditEvent, User


def append_audit(
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: int | str,
    details: Mapping[str, object] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor.id if actor is not None else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=dict(details or {}),
    )
    db.session.add(event)
    return event
