"""Patient and Consent Confirmation workflows."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from wtforms import BooleanField, IntegerField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange, ValidationError

from app.audit import append_audit
from app.auth import editor_required
from app.db import db
from app.models import Patient, User

patients_bp = Blueprint("patients", __name__, url_prefix="/patients")


def _strict_consent_confirmation(_form, field) -> None:
    if field.raw_data != ["y"]:
        raise ValidationError("Consent Confirmation is required.")


class PatientForm(FlaskForm):
    patient_id = StringField("Patient ID", validators=[DataRequired(), Length(max=128)])
    name = StringField("Name", validators=[DataRequired(), Length(max=200)])
    birth_year = IntegerField(
        "Birth year",
        validators=[DataRequired(), NumberRange(min=1000, max=9999)],
    )
    consent_confirmed = BooleanField(
        "I confirm that consent was obtained before clinical images are stored.",
        validators=[_strict_consent_confirmation],
    )
    submit = SubmitField("Create Patient")


class ConsentForm(FlaskForm):
    consent_confirmed = BooleanField(
        "I confirm that consent was obtained before clinical images are stored.",
        validators=[_strict_consent_confirmation],
    )
    submit = SubmitField("Confirm consent")


def create_patient(
    *,
    actor: User,
    patient_id: str,
    name: str,
    birth_year: int,
    consent_confirmed: bool,
) -> Patient:
    """Create a Patient and its audit event in one transaction."""
    if not actor.is_editor:
        raise PermissionError("only an Editor or Admin can create Patients")
    if consent_confirmed is not True:
        raise ValueError("Consent Confirmation is required")
    if not isinstance(patient_id, str) or not patient_id.strip():
        raise ValueError("patient ID is required")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if isinstance(birth_year, bool) or not isinstance(birth_year, int) or not 1000 <= birth_year <= 9999:
        raise ValueError("birth year is invalid")

    now = datetime.now(timezone.utc)
    patient = Patient(
        patient_id=patient_id.strip(),
        name=name.strip(),
        birth_year=birth_year,
        consent_confirmed_by_id=actor.id,
        consent_confirmed_at=now,
        created_by_id=actor.id,
        updated_by_id=actor.id,
    )
    try:
        db.session.add(patient)
        db.session.flush()
        append_audit(
            actor=actor,
            action="patient.create",
            entity_type="patient",
            entity_id=patient.id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return patient


def search_patients(query: str = "") -> list[Patient]:
    query = query.strip()
    statement = select(Patient).where(Patient.archived_at.is_(None))
    if query:
        pattern = f"%{query}%"
        statement = statement.where(or_(Patient.patient_id.ilike(pattern), Patient.name.ilike(pattern)))
    statement = statement.order_by(Patient.patient_id).limit(100)
    return list(db.session.scalars(statement))


@patients_bp.get("")
@patients_bp.get("/")
@login_required
def index():
    query = request.args.get("q", "")
    return render_template("patients/index.html", patients=search_patients(query), query=query)


@patients_bp.route("/new", methods=["GET", "POST"])
@editor_required
def new():
    form = PatientForm()
    if form.validate_on_submit():
        try:
            patient = create_patient(
                actor=current_user,
                patient_id=form.patient_id.data,
                name=form.name.data,
                birth_year=form.birth_year.data,
                consent_confirmed=form.consent_confirmed.data,
            )
        except IntegrityError:
            form.patient_id.errors.append("Patient ID already exists.")
        except ValueError as exc:
            form.patient_id.errors.append(str(exc))
        else:
            flash("Patient created.", "success")
            return redirect(url_for("patients.detail", patient_pk=patient.id))
    return render_template("patients/new.html", form=form), (400 if request.method == "POST" else 200)


@patients_bp.get("/<int:patient_pk>")
@login_required
def detail(patient_pk: int):
    patient = db.session.get(Patient, patient_pk)
    if patient is None or patient.archived_at is not None:
        abort(404)
    return render_template("patients/detail.html", patient=patient, consent_form=ConsentForm())


@patients_bp.post("/<int:patient_pk>/consent")
@editor_required
def confirm_consent(patient_pk: int):
    patient = db.session.get(Patient, patient_pk)
    if patient is None or patient.archived_at is not None:
        abort(404)
    form = ConsentForm()
    if not form.validate_on_submit():
        return render_template("patients/detail.html", patient=patient, consent_form=form), 400

    now = datetime.now(timezone.utc)
    try:
        patient.consent_confirmed_by_id = current_user.id
        patient.consent_confirmed_at = now
        patient.updated_by_id = current_user.id
        append_audit(
            actor=current_user,
            action="patient.consent_confirm",
            entity_type="patient",
            entity_id=patient.id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    flash("Consent Confirmation recorded.", "success")
    return redirect(url_for("patients.detail", patient_pk=patient.id))
