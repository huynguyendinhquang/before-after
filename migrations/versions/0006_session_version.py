"""add per-user session revocation versions

Revision ID: 0006_session_version
Revises: 0005_lifecycle_audit_index
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_session_version"
down_revision = "0005_lifecycle_audit_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        "ck_users_session_version",
        "users",
        "session_version > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_session_version", "users", type_="check")
    op.drop_column("users", "session_version")
