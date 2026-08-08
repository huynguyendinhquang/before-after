"""Persistence models for identity, Patients, consent, and audit history."""

from __future__ import annotations

from datetime import datetime

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
    def is_admin(self) -> bool:
        return self.role == "admin"

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

    @property
    def consent_actor_id(self) -> int:
        """Domain-language alias for the audited consent confirmer."""
        return self.consent_confirmed_by_id

    @property
    def consent_confirmed_at_utc(self) -> datetime:
        return self.consent_confirmed_at


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

    @property
    def timestamp(self) -> datetime:
        """Compatibility alias for callers that use the audit vocabulary."""
        return self.created_at
