"""audited Comparison Set PNG/PDF exports

Revision ID: 0004_exports
Revises: 0003_comparison_sets_frames
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_exports"
down_revision = "0003_comparison_sets_frames"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("comparison_set_id", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("rendered_version", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("format IN ('PNG', 'PDF')", name="ck_exports_format"),
        sa.CheckConstraint("length(trim(storage_key)) > 0", name="ck_exports_storage_key_not_blank"),
        sa.CheckConstraint("byte_count > 0", name="ck_exports_byte_count"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_exports_sha256"),
        sa.CheckConstraint("rendered_version > 0", name="ck_exports_rendered_version"),
        sa.ForeignKeyConstraint(
            ["comparison_set_id"], ["comparison_sets.id"],
            name="fk_exports_comparison_set_id_comparison_sets", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_exports_created_by_id_users", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_exports_storage_key"),
    )
    op.create_index(
        "ix_exports_comparison_set_version",
        "exports",
        ["comparison_set_id", "rendered_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_exports_comparison_set_version", table_name="exports")
    op.drop_table("exports")
