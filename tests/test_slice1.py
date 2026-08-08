from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app import create_app
from app.db import db
from app.models import AuditEvent, Patient, User


CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def csrf_token(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = CSRF_RE.search(response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)
    return match.group(1)


def app_config(tmp_path: Path, database_url: str) -> dict[str, object]:
    return {
        "TESTING": True,
        "DATABASE_URL": database_url,
        "MEDIA_ROOT": str(tmp_path / "media"),
        "SECRET_KEY": "slice-1-test-secret",
        "SESSION_COOKIE_SECURE": True,
    }


@pytest.fixture
def app(tmp_path: Path):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 1 PostgreSQL tests")

    application = create_app(app_config(tmp_path, database_url))
    with application.app_context():
        db.session.execute(text("TRUNCATE audit_events, patients, users RESTART IDENTITY CASCADE"))
        db.session.commit()
    yield application
    with application.app_context():
        db.session.rollback()
        db.session.execute(text("TRUNCATE audit_events, patients, users RESTART IDENTITY CASCADE"))
        db.session.commit()


def add_user(application, username: str, role: str) -> int:
    with application.app_context():
        user = User(
            username=username,
            display_name=username.title(),
            role=role,
            active=True,
        )
        user.set_password("correct horse battery staple")
        db.session.add(user)
        db.session.commit()
        return user.id


def login(client, path: str, username: str) -> None:
    token = csrf_token(client, path)
    response = client.post(
        "/login",
        data={"username": username, "password": "correct horse battery staple", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_create_app_requires_postgres_runtime_configuration(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_app({"SECRET_KEY": "secret", "MEDIA_ROOT": str(tmp_path / "media")})

    with pytest.raises(RuntimeError, match="MEDIA_ROOT"):
        create_app({"DATABASE_URL": "postgresql+psycopg://localhost/db", "SECRET_KEY": "secret"})

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"DATABASE_URL": "postgresql+psycopg://localhost/db", "MEDIA_ROOT": str(tmp_path)})

    with pytest.raises(RuntimeError, match="PostgreSQL"):
        create_app(
            {
                "DATABASE_URL": "sqlite:///not-allowed.db",
                "MEDIA_ROOT": str(tmp_path),
                "SECRET_KEY": "secret",
            }
        )


def test_editor_creates_patient_with_consent_and_viewer_can_only_read(app, tmp_path: Path) -> None:
    editor_id = add_user(app, "editor", "editor")
    add_user(app, "viewer", "viewer")
    client = app.test_client()

    login(client, "/login", "editor")
    token = csrf_token(client, "/patients/new")
    response = client.post(
        "/patients/new",
        data={
            "patient_id": "VG-0001",
            "name": "Nguyen Van A",
            "birth_year": "1990",
            "consent_confirmed": "y",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        patient = db.session.scalar(select(Patient).where(Patient.patient_id == "VG-0001"))
        assert patient is not None
        assert patient.name == "Nguyen Van A"
        assert patient.birth_year == 1990
        assert patient.consent_confirmed_by_id == editor_id
        assert patient.consent_confirmed_at is not None
        event = db.session.scalar(
            select(AuditEvent).where(AuditEvent.entity_id == str(patient.id))
        )
        assert event is not None
        assert event.actor_id == editor_id
        assert event.action == "patient.create"
        assert event.entity_type == "patient"

    restarted = create_app(app_config(tmp_path, os.environ["TEST_DATABASE_URL"]))
    restarted_client = restarted.test_client()
    login(restarted_client, "/login", "editor")
    assert "VG-0001" in restarted_client.get("/patients").get_data(as_text=True)

    client.post("/logout", data={"csrf_token": csrf_token(client, "/patients")})
    login(client, "/login", "viewer")
    assert client.get("/patients").status_code == 200
    assert client.get(f"/patients/{patient.id}").status_code == 200
    assert client.get("/patients/new").status_code == 403
    assert client.post(
        "/patients/new",
        data={"patient_id": "VG-0002", "name": "Nope", "birth_year": "1991", "consent_confirmed": "y"},
    ).status_code == 403


def test_patient_creation_requires_consent_and_audit_rolls_back_with_mutation(app, monkeypatch) -> None:
    add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "/login", "editor")

    token = csrf_token(client, "/patients/new")
    response = client.post(
        "/patients/new",
        data={
            "patient_id": "VG-NO-CONSENT",
            "name": "Not Stored",
            "birth_year": "1990",
            "csrf_token": token,
        },
    )
    assert response.status_code == 400
    with app.app_context():
        assert db.session.scalar(select(func.count(Patient.id))) == 0

    from app import patients

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(patients, "append_audit", fail_audit)
    with app.app_context():
        actor = db.session.scalar(select(User).where(User.username == "editor"))
        assert actor is not None
        with pytest.raises(RuntimeError, match="audit failed"):
            patients.create_patient(
                actor=actor,
                patient_id="VG-ROLLBACK",
                name="Not Stored",
                birth_year=1990,
                consent_confirmed=True,
            )
        assert db.session.scalar(select(Patient).where(Patient.patient_id == "VG-ROLLBACK")) is None
        assert db.session.scalar(select(func.count(AuditEvent.id))) == 0


def test_prototype_is_preserved_under_transition_prefix(app) -> None:
    add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "/login", "editor")
    response = client.get("/prototype")
    assert response.status_code == 200
    assert "Before / After Case Board" in response.get_data(as_text=True)
    assert client.get("/prototype/api/template/viengut_case").status_code == 200


def test_secure_session_defaults_are_enabled(app) -> None:
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["WTF_CSRF_ENABLED"] is True
    assert app.config["DEBUG"] is False
