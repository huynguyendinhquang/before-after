"""Local-only demo data and automatic login."""

from __future__ import annotations

import io
from datetime import date

import click
from flask import Flask, session
from flask_login import current_user, login_user
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from app.audit import append_audit
from app.auth import create_user
from app.captures import create_capture
from app.comparisons import acquire_edit_lease, add_frame, create_comparison_set
from app.db import db
from app.models import Capture, ComparisonSet, Frame, Patient, ShotType, User
from app.patients import create_patient

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo"
DEMO_PATIENT_ID = "DEMO-001"
DEMO_SET_NAME = "Demo progression"


def _require_development(app: Flask) -> None:
    if app.config.get("APP_ENV") != "development":
        raise RuntimeError("Demo Mode is available only when APP_ENV=development")


def _synthetic_image(index: int) -> bytes:
    colors = (
        (36, 99, 143),
        (44, 122, 89),
        (177, 106, 48),
        (117, 80, 146),
    )
    image = Image.new("RGB", (1200, 900), (238, 241, 244))
    draw = ImageDraw.Draw(image)
    color = colors[index % len(colors)]
    draw.rectangle((70, 70, 1130, 830), fill=color, outline=(20, 30, 40), width=12)
    draw.line((600, 70, 600, 830), fill="white", width=8)
    draw.line((70, 450, 1130, 450), fill="white", width=8)
    radius = 105 + index * 22
    center_x = 460 + index * 75
    center_y = 390 + index * 35
    draw.ellipse(
        (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
        fill=(245, 215, 180),
        outline=(90, 45, 30),
        width=10,
    )
    draw.rectangle((90, 90, 420, 165), fill=(255, 255, 255))
    draw.text((115, 112), f"SYNTHETIC VISIT {index + 1}", fill=(15, 25, 35))
    draw.text((90, 790), "Corner grid makes crop / pan / zoom visible", fill="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def seed_demo(app: Flask) -> tuple[Patient, ComparisonSet]:
    _require_development(app)
    username = str(app.config.get("DEMO_USERNAME") or DEMO_USERNAME).strip().casefold()
    user = db.session.scalar(select(User).where(User.username == username))
    if user is None:
        user = create_user(
            actor=None,
            username=username,
            display_name="Demo Admin",
            password=DEMO_PASSWORD,
            role="admin",
            active=True,
            bootstrap=True,
        )
    elif not user.active or user.role != "admin":
        user.active = True
        user.role = "admin"
        user.session_version += 1
        user.set_password(DEMO_PASSWORD)
        db.session.commit()

    patient = db.session.scalar(select(Patient).where(Patient.patient_id == DEMO_PATIENT_ID))
    if patient is None:
        patient = create_patient(
            actor=user,
            patient_id=DEMO_PATIENT_ID,
            name="Synthetic Demo Patient",
            birth_year=1990,
            consent_confirmed=True,
        )

    shot_type = db.session.scalar(
        select(ShotType).where(func.lower(ShotType.name) == "clinical overview")
    )
    if shot_type is None:
        shot_type = ShotType(
            name="Clinical overview",
            state="canonical",
            created_by_id=user.id,
        )
        db.session.add(shot_type)
        db.session.flush()
        append_audit(
            actor=user,
            action="shot_type.create",
            entity_type="shot_type",
            entity_id=shot_type.id,
        )
        db.session.commit()

    capture_dates = (date(2024, 1, 15), date(2024, 5, 20), date(2024, 9, 25), date(2025, 1, 30))
    captures: list[Capture] = []
    for index, capture_date in enumerate(capture_dates):
        existing = db.session.scalar(
            select(Capture).where(
                Capture.patient_id == patient.id,
                Capture.capture_date == capture_date,
            )
        )
        if existing is None:
            payload = _synthetic_image(index)
            existing = create_capture(
                actor=user,
                patient=patient,
                upload=io.BytesIO(payload),
                capture_date=capture_date,
                capture_date_confirmed=True,
                shot_type=shot_type,
                original_filename=f"synthetic-visit-{index + 1}.png",
            )
        captures.append(existing)

    comparison_set = db.session.scalar(
        select(ComparisonSet).where(
            ComparisonSet.patient_id == patient.id,
            ComparisonSet.archived_at.is_(None),
            func.lower(ComparisonSet.name) == DEMO_SET_NAME.casefold(),
        )
    )
    if comparison_set is None:
        comparison_set = create_comparison_set(
            actor=user,
            patient=patient,
            name=DEMO_SET_NAME,
            preset_key="a4-landscape",
            canvas_width_mm=297,
            canvas_height_mm=210,
            frame_ratio=4 / 3,
            columns=2,
            show_patient_id=True,
            show_patient_name=True,
            show_birth_year=True,
            date_label_default=True,
        )
    comparison_set_id = comparison_set.id

    existing_capture_ids = set(
        db.session.scalars(
            select(Frame.capture_id).where(Frame.comparison_set_id == comparison_set.id)
        )
    )
    if any(capture.id not in existing_capture_ids for capture in captures):
        comparison_set = acquire_edit_lease(
            actor=user,
            comparison_set=comparison_set,
            expected_version=comparison_set.version,
        )
        for capture in captures:
            if capture.id in existing_capture_ids:
                continue
            expected_version = comparison_set.version
            add_frame(
                actor=user,
                comparison_set=comparison_set,
                capture=capture,
                expected_version=expected_version,
            )
            db.session.expire_all()
            comparison_set = db.session.get(ComparisonSet, comparison_set_id)
            if comparison_set is None:
                raise RuntimeError("demo Comparison Set disappeared during seeding")

    frames = list(
        db.session.scalars(
            select(Frame)
            .where(Frame.comparison_set_id == comparison_set.id)
            .order_by(Frame.position)
        )
    )
    comparison_set = db.session.get(ComparisonSet, comparison_set_id)
    if comparison_set is None:
        raise RuntimeError("demo Comparison Set is unavailable")
    changed = False
    for index, frame in enumerate(frames):
        label = f"Visit {index + 1}"
        zoom = 1.0 + index * 0.12
        pan_x = (-0.2, 0.15, -0.1, 0.2)[index % 4]
        if (frame.label, frame.zoom, frame.pan_x) != (label, zoom, pan_x):
            frame.label = label
            frame.zoom = zoom
            frame.pan_x = pan_x
            frame.pan_y = 0.0
            changed = True
    if changed:
        comparison_set.version += 1
        comparison_set.updated_by_id = user.id
        db.session.commit()

    return patient, comparison_set


def register_demo(app: Flask) -> None:
    enabled = app.config.get("DEMO_AUTO_LOGIN") is True
    if enabled:
        _require_development(app)

        @app.before_request
        def auto_login_demo_user() -> None:
            if current_user.is_authenticated:
                return
            username = str(app.config.get("DEMO_USERNAME") or DEMO_USERNAME).strip().casefold()
            user = db.session.scalar(
                select(User).where(User.username == username, User.active.is_(True))
            )
            if user is not None and user.role == "admin":
                login_user(user)
                session["session_version"] = user.session_version

    @app.cli.command("seed-demo")
    def seed_demo_command() -> None:
        """Create synthetic local-only data used by scripts/run-demo.sh."""
        try:
            patient, comparison_set = seed_demo(app)
        except Exception as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"Demo ready: /patients/{patient.id}/comparison-sets/{comparison_set.id} "
            f"(fallback login: {DEMO_USERNAME}/{DEMO_PASSWORD})"
        )
