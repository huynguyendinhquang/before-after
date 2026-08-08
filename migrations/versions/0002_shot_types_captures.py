"""shot types, captures, and immutable media metadata

Revision ID: 0002_shot_types_captures
Revises: 0001_identity_patients_audit
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_shot_types_captures"
down_revision = "0001_identity_patients_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shot_types",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="canonical", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("canonical_target_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('canonical', 'proposal', 'merged')",
            name="ck_shot_types_state",
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_shot_types_name_not_blank"),
        sa.CheckConstraint(
            "canonical_target_id IS NULL OR canonical_target_id <> id",
            name="ck_shot_types_target_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name="fk_shot_types_created_by_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_target_id"],
            ["shot_types.id"],
            name="fk_shot_types_canonical_target_id_shot_types",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_shot_types_name"),
    )
    op.create_table(
        "captures",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("capture_date", sa.Date(), nullable=False),
        sa.Column("shot_type_id", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(storage_key)) > 0", name="ck_captures_storage_key_not_blank"),
        sa.CheckConstraint("length(trim(original_filename)) > 0", name="ck_captures_filename_not_blank"),
        sa.CheckConstraint(
            "format IN ('BMP', 'JPEG', 'PNG', 'TIFF', 'WEBP')",
            name="ck_captures_format",
        ),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_captures_dimensions"),
        sa.CheckConstraint("byte_count > 0", name="ck_captures_byte_count"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_captures_sha256"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name="fk_captures_patient_id_patients", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["shot_type_id"], ["shot_types.id"], name="fk_captures_shot_type_id_shot_types", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_id"], ["users.id"], name="fk_captures_archived_by_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name="fk_captures_created_by_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["users.id"], name="fk_captures_updated_by_id_users", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_captures_storage_key"),
        sa.UniqueConstraint("patient_id", "sha256", name="uq_captures_patient_sha256"),
    )
    op.create_index(
        "ix_captures_patient_capture_date",
        "captures",
        ["patient_id", "capture_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_captures_patient_capture_date", table_name="captures")
    op.drop_table("captures")
    op.drop_table("shot_types")
