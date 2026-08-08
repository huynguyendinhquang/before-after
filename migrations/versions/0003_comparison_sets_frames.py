"""comparison sets and frames

Revision ID: 0003_comparison_sets_frames
Revises: 0002_shot_types_captures
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_comparison_sets_frames"
down_revision = "0002_shot_types_captures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comparison_sets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("canvas_width_mm", sa.Numeric(8, 2), server_default="297", nullable=False),
        sa.Column("canvas_height_mm", sa.Numeric(8, 2), server_default="210", nullable=False),
        sa.Column("preset_key", sa.String(length=32), server_default="a4-landscape", nullable=False),
        sa.Column("frame_ratio", sa.Float(), server_default="1", nullable=False),
        sa.Column("columns", sa.Integer(), server_default="3", nullable=False),
        sa.Column("show_patient_id", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("show_patient_name", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("show_birth_year", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("date_label_default", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("lock_holder_id", sa.Integer(), nullable=True),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_comparison_sets_name_not_blank"),
        sa.CheckConstraint(
            "canvas_width_mm > 0 AND canvas_height_mm > 0",
            name="ck_comparison_sets_canvas_dimensions",
        ),
        sa.CheckConstraint("frame_ratio > 0", name="ck_comparison_sets_frame_ratio"),
        sa.CheckConstraint("columns > 0", name="ck_comparison_sets_columns"),
        sa.CheckConstraint("version > 0", name="ck_comparison_sets_version"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"],
            name="fk_comparison_sets_patient_id_patients", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lock_holder_id"], ["users.id"],
            name="fk_comparison_sets_lock_holder_id_users", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_id"], ["users.id"],
            name="fk_comparison_sets_archived_by_id_users", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_comparison_sets_created_by_id_users", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["users.id"],
            name="fk_comparison_sets_updated_by_id_users", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_comparison_sets_patient_name_active",
        "comparison_sets",
        ["patient_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "frames",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("comparison_set_id", sa.Integer(), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("visible", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("label", sa.String(length=500), nullable=True),
        sa.Column("date_visible_override", sa.Boolean(), nullable=True),
        sa.Column("zoom", sa.Float(), server_default="1", nullable=False),
        sa.Column("pan_x", sa.Float(), server_default="0", nullable=False),
        sa.Column("pan_y", sa.Float(), server_default="0", nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_frames_position"),
        sa.CheckConstraint("zoom >= 1 AND zoom <= 5", name="ck_frames_zoom"),
        sa.CheckConstraint("pan_x >= -1 AND pan_x <= 1", name="ck_frames_pan_x"),
        sa.CheckConstraint("pan_y >= -1 AND pan_y <= 1", name="ck_frames_pan_y"),
        sa.ForeignKeyConstraint(
            ["comparison_set_id"], ["comparison_sets.id"],
            name="fk_frames_comparison_set_id_comparison_sets", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["captures.id"],
            name="fk_frames_capture_id_captures", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("comparison_set_id", "position", name="uq_frames_set_position"),
    )


def downgrade() -> None:
    op.drop_table("frames")
    op.drop_index("uq_comparison_sets_patient_name_active", table_name="comparison_sets")
    op.drop_table("comparison_sets")
