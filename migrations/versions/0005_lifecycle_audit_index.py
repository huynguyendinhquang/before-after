"""index bounded lifecycle audit browsing

Revision ID: 0005_lifecycle_audit_index
Revises: 0004_exports
Create Date: 2026-08-09
"""

from alembic import op


revision = "0005_lifecycle_audit_index"
down_revision = "0004_exports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_events_created_id",
        "audit_events",
        ["created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_id", table_name="audit_events")
