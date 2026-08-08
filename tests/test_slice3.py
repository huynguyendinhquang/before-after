from __future__ import annotations

import io
import os
import re
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select, text

from app import create_app
from app.comparisons import ComparisonError, SamePatientError, add_frame, create_comparison_set
from app.captures import create_capture
from app.db import db, normalize_database_url
from app.models import AuditEvent, Capture, ComparisonSet, Frame, Patient, ShotType, User


CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def app_config(tmp_path: Path, database_url: str) -> dict[str, object]:
    return {
        "TESTING": True,
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "MEDIA_ROOT": str(tmp_path / "media"),
        "SECRET_KEY": "slice-3-test-secret",
        "SESSION_COOKIE_SECURE": False,
    }


@pytest.fixture(scope="session")
def migrated_test_database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 3 PostgreSQL tests")

    database_url = normalize_database_url(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATABASE_URL", database_url)
    try:
        command.upgrade(alembic_config, "head")
    finally:
        monkeypatch.undo()
    yield database_url


@pytest.fixture
def app(tmp_path: Path, migrated_test_database: str):
    application = create_app(app_config(tmp_path, migrated_test_database))
    with application.app_context():
        db.session.execute(
            text(
                "TRUNCATE audit_events, frames, comparison_sets, captures, "
                "shot_types, patients, users RESTART IDENTITY CASCADE"
            )
        )
        db.session.commit()
    yield application
    with application.app_context():
        db.session.rollback()
        db.session.execute(
            text(
                "TRUNCATE audit_events, frames, comparison_sets, captures, "
                "shot_types, patients, users RESTART IDENTITY CASCADE"
            )
        )
        db.session.commit()


def csrf_token(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = CSRF_RE.search(response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)
    return match.group(1)


def add_user(application, username: str, role: str) -> int:
    with application.app_context():
        user = User(username=username, display_name=username.title(), role=role, active=True)
        user.set_password("correct horse battery staple")
        db.session.add(user)
        db.session.commit()
        return user.id


def login(client, username: str) -> None:
    token = csrf_token(client, "/login")
    response = client.post(
        "/login",
        data={"username": username, "password": "correct horse battery staple", "csrf_token": token},
    )
    assert response.status_code == 302


def create_patient(application, client, patient_id: str) -> int:
    token = csrf_token(client, "/patients/new")
    response = client.post(
        "/patients/new",
        data={
            "patient_id": patient_id,
            "name": f"Patient {patient_id}",
            "birth_year": "1990",
            "consent_confirmed": "y",
            "csrf_token": token,
        },
    )
    assert response.status_code == 302, response.get_data(as_text=True)
    with application.app_context():
        patient = db.session.scalar(select(Patient).where(Patient.patient_id == patient_id))
        assert patient is not None
        return patient.id


def jpeg(color: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 4), color).save(stream, format="JPEG")
    return stream.getvalue()


def add_shot_type(application, user_id: int, name: str = "Anterior") -> int:
    with application.app_context():
        shot_type = ShotType(name=name, state="canonical", created_by_id=user_id)
        db.session.add(shot_type)
        db.session.commit()
        return shot_type.id


def stored_capture(application, user_id: int, patient_id: int, payload: bytes, capture_date: str) -> int:
    with application.app_context():
        actor = db.session.get(User, user_id)
        patient = db.session.get(Patient, patient_id)
        shot_type = db.session.scalar(select(ShotType).where(ShotType.name == "Anterior"))
        assert actor is not None and patient is not None and shot_type is not None
        capture = create_capture(
            actor=actor,
            patient=patient,
            upload=payload,
            capture_date=capture_date,
            capture_date_confirmed=True,
            shot_type=shot_type,
            original_filename="existing.jpg",
        )
        return capture.id


def test_set_acceptance_reopens_inline_capture_and_persists_manual_order(app, tmp_path, migrated_test_database):
    editor_id = add_user(app, "editor", "editor")
    add_user(app, "viewer", "viewer")
    client = app.test_client()
    login(client, "editor")
    patient_id = create_patient(app, client, "SET-0001")
    add_shot_type(app, editor_id)
    existing_id = stored_capture(app, editor_id, patient_id, jpeg("blue"), "2025-01-01")

    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="History")
        set_id = comparison_set.id

    token = csrf_token(client, f"/comparison-sets/{set_id}")
    response = client.post(
        f"/comparison-sets/{set_id}/frames",
        data={"capture_id": str(existing_id), "csrf_token": token},
    )
    assert response.status_code == 302

    new_payload = jpeg("red")
    response = client.post(
        f"/comparison-sets/{set_id}/frames",
        data={
            "image": (io.BytesIO(new_payload), "inline.jpg"),
            "capture_date": "2024-01-01",
            "capture_date_confirmed": "y",
            "shot_type_name": "Anterior",
            "csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)

    with app.app_context():
        comparison_set = db.session.get(ComparisonSet, set_id)
        assert comparison_set is not None
        frames = list(comparison_set.frames)
        assert [frame.capture_id for frame in frames] == [
            db.session.scalar(select(Capture.id).where(Capture.capture_date == date(2024, 1, 1))),
            existing_id,
        ]
        assert [frame.position for frame in frames] == [0, 1]
        assert comparison_set.canvas_width_mm == 297
        assert comparison_set.canvas_height_mm == 210
        assert comparison_set.preset_key == "a4-landscape"
        assert all(frame.zoom == 1 and frame.pan_x == 0 and frame.pan_y == 0 for frame in frames)
        frame_ids = [frame.id for frame in frames]
        capture_count = db.session.scalar(select(func.count(Capture.id)))
        assert capture_count == 2
        assert len(list((Path(app.config["MEDIA_ROOT"]) / "originals").iterdir())) == 2

    # Reorder through the same route a user uses, then verify it survives a
    # fresh application/session against the same PostgreSQL rows.
    token = csrf_token(client, f"/comparison-sets/{set_id}")
    response = client.post(
        f"/comparison-sets/{set_id}/reorder",
        data={
            "frame_ids": [str(frame_ids[1]), str(frame_ids[0])],
            "csrf_token": token,
        },
    )
    assert response.status_code == 302

    restarted = create_app(app_config(tmp_path, migrated_test_database))
    restarted_client = restarted.test_client()
    login(restarted_client, "editor")
    detail = restarted_client.get(f"/comparison-sets/{set_id}")
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    with restarted.app_context():
        comparison_set = db.session.get(ComparisonSet, set_id)
        assert comparison_set is not None
        assert [frame.id for frame in comparison_set.frames] == [frame_ids[1], frame_ids[0]]
        assert [frame.position for frame in comparison_set.frames] == [0, 1]

    restarted_client.post("/logout", data={"csrf_token": csrf_token(restarted_client, "/patients")})
    login(restarted_client, "viewer")
    assert restarted_client.get(f"/comparison-sets/{set_id}").status_code == 200
    assert "Add Frame" not in restarted_client.get(f"/comparison-sets/{set_id}").get_data(as_text=True)
    assert restarted_client.post(f"/comparison-sets/{set_id}/reorder", data={}).status_code == 403


def test_cross_patient_frame_and_duplicate_active_name_are_rejected(app):
    editor_id = add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "editor")
    first_patient_id = create_patient(app, client, "SET-0001")
    second_patient_id = create_patient(app, client, "SET-0002")
    add_shot_type(app, editor_id)
    other_capture_id = stored_capture(app, editor_id, second_patient_id, jpeg("green"), "2024-01-01")

    with app.app_context():
        actor = db.session.get(User, editor_id)
        first_patient = db.session.get(Patient, first_patient_id)
        second_patient = db.session.get(Patient, second_patient_id)
        assert actor is not None and first_patient is not None and second_patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=first_patient, name="Summary")
        with pytest.raises(SamePatientError):
            add_frame(actor=actor, comparison_set=comparison_set, capture_id=other_capture_id)
        with pytest.raises(ComparisonError, match="already exists"):
            create_comparison_set(actor=actor, patient=first_patient, name=" summary ")
        assert db.session.scalar(select(func.count(Frame.id))) == 0
        assert db.session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "frame.add")) == 0
