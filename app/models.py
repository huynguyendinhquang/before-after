"""Persistence models for identity, Patients, Captures, and audit history."""

from __future__ import annotations

from flask_login import UserMixin
from sqlalchemy.orm import validates
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(128), nullable=False)
    display_name = db.Column(db.String(200), nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="viewer", server_default="viewer")
    active = db.Column(db.Boolean, nullable=False, default=True, server_default="true")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.func.now(),
        onupdate=db.func.now(),
    )

    __table_args__ = (
        db.CheckConstraint(
            "role IN ('admin', 'editor', 'viewer')",
            name="ck_users_role",
        ),
        db.CheckConstraint("length(trim(username)) > 0", name="ck_users_username_not_blank"),
        db.CheckConstraint("length(trim(display_name)) > 0", name="ck_users_display_name_not_blank"),
        db.UniqueConstraint("username", name="uq_users_username"),
    )

    @validates("username")
    def normalize_username(self, _key: str, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("username must be a string")
        return value.strip().casefold()

    def set_password(self, password: str) -> None:
        if not isinstance(password, str) or not password:
            raise ValueError("password must not be empty")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    @property
    def is_active(self) -> bool:
        return self.active

    @property
    def is_editor(self) -> bool:
        return self.role in {"admin", "editor"}


class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    birth_year = db.Column(db.Integer, nullable=False)
    consent_confirmed_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    consent_confirmed_at = db.Column(db.DateTime(timezone=True), nullable=False)
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True)
    archived_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.func.now(),
        onupdate=db.func.now(),
    )

    __table_args__ = (
        db.CheckConstraint("length(trim(patient_id)) > 0", name="ck_patients_patient_id_not_blank"),
        db.CheckConstraint("length(trim(name)) > 0", name="ck_patients_name_not_blank"),
        db.CheckConstraint("birth_year BETWEEN 1000 AND 9999", name="ck_patients_birth_year"),
        db.UniqueConstraint("patient_id", name="uq_patients_patient_id"),
    )

    consent_confirmed_by = db.relationship(
        "User", foreign_keys=[consent_confirmed_by_id], lazy="joined"
    )
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    updated_by = db.relationship("User", foreign_keys=[updated_by_id], lazy="joined")
    archived_by = db.relationship("User", foreign_keys=[archived_by_id], lazy="joined")


class ShotType(db.Model):
    __tablename__ = "shot_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    state = db.Column(db.String(16), nullable=False, default="canonical", server_default="canonical")
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    canonical_target_id = db.Column(
        db.Integer,
        db.ForeignKey("shot_types.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.func.now(),
        onupdate=db.func.now(),
    )

    __table_args__ = (
        db.CheckConstraint(
            "state IN ('canonical', 'proposal', 'merged')",
            name="ck_shot_types_state",
        ),
        db.CheckConstraint("length(trim(name)) > 0", name="ck_shot_types_name_not_blank"),
        db.CheckConstraint(
            "canonical_target_id IS NULL OR canonical_target_id <> id",
            name="ck_shot_types_target_not_self",
        ),
        db.CheckConstraint(
            "(state IN ('canonical', 'proposal') AND canonical_target_id IS NULL) "
            "OR (state = 'merged' AND canonical_target_id IS NOT NULL)",
            name="ck_shot_types_target_for_state",
        ),
        db.Index("uq_shot_types_name_ci", db.func.lower(name), unique=True),
    )

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    canonical_target = db.relationship(
        "ShotType",
        remote_side=[id],
        foreign_keys=[canonical_target_id],
        lazy="joined",
    )


class Capture(db.Model):
    __tablename__ = "captures"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(
        db.Integer,
        db.ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
    )
    capture_date = db.Column(db.Date, nullable=False)
    shot_type_id = db.Column(
        db.Integer,
        db.ForeignKey("shot_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    storage_key = db.Column(db.String(512), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    format = db.Column(db.String(16), nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    byte_count = db.Column(db.BigInteger, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True)
    archived_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.func.now(),
        onupdate=db.func.now(),
    )

    __table_args__ = (
        db.CheckConstraint("length(trim(storage_key)) > 0", name="ck_captures_storage_key_not_blank"),
        db.CheckConstraint("length(trim(original_filename)) > 0", name="ck_captures_filename_not_blank"),
        db.CheckConstraint("format IN ('BMP', 'JPEG', 'PNG', 'TIFF', 'WEBP')", name="ck_captures_format"),
        db.CheckConstraint("width > 0 AND height > 0", name="ck_captures_dimensions"),
        db.CheckConstraint("byte_count > 0", name="ck_captures_byte_count"),
        db.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_captures_sha256"),
        db.UniqueConstraint("storage_key", name="uq_captures_storage_key"),
        db.UniqueConstraint("patient_id", "sha256", name="uq_captures_patient_sha256"),
        db.Index("ix_captures_patient_capture_date", "patient_id", "capture_date"),
    )

    patient = db.relationship("Patient", foreign_keys=[patient_id], lazy="joined")
    shot_type = db.relationship("ShotType", foreign_keys=[shot_type_id], lazy="joined")
    archived_by = db.relationship("User", foreign_keys=[archived_by_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    updated_by = db.relationship("User", foreign_keys=[updated_by_id], lazy="joined")

class AuditEvent(db.Model):
    __tablename__ = "audit_events"

    id = db.Column(db.BigInteger, primary_key=True)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    action = db.Column(db.String(64), nullable=False)
    entity_type = db.Column(db.String(64), nullable=False)
    entity_id = db.Column(db.String(128), nullable=False)
    details = db.Column(db.JSON, nullable=False, server_default="{}")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())

    actor = db.relationship("User", foreign_keys=[actor_id], lazy="joined")
