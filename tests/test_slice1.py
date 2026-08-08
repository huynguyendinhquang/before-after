from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, select, text

from app import create_app
from app.audit import SYSTEM_ACTOR
from app.auth import _safe_next_url
from app.db import db, normalize_database_url
from app.image_policy import configured_request_limit
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
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "MEDIA_ROOT": str(tmp_path / "media"),
        "SECRET_KEY": "slice-1-test-secret",
        "SESSION_COOKIE_SECURE": False,
    }


@pytest.fixture(scope="session")
def migrated_test_database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 1 PostgreSQL tests")

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
    database_url = migrated_test_database
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


def test_create_app_requires_postgres_runtime_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
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
    assert client.get("/patients").headers["Cache-Control"] == "no-store"
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

    database_url = os.environ["TEST_DATABASE_URL"]
    restarted = create_app(app_config(tmp_path, database_url))
    restarted_client = restarted.test_client()
    login(restarted_client, "/login", "editor")
    assert "VG-0001" in restarted_client.get("/patients").get_data(as_text=True)

    client.post("/logout", data={"csrf_token": csrf_token(client, "/patients")})
    login(client, "/login", "viewer")
    assert client.get("/patients").status_code == 200
    assert client.get(f"/patients/{patient.id}").status_code == 200
    assert client.get(f"/patients/{patient.id}").headers["Cache-Control"] == "no-store"
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

    for index, value in enumerate(("0", "false", "arbitrary")):
        response = client.post(
            "/patients/new",
            data={
                "patient_id": f"VG-BAD-CONSENT-{index}",
                "name": "Not Stored",
                "birth_year": "1990",
                "consent_confirmed": value,
                "csrf_token": token,
            },
        )
        assert response.status_code == 400

    with app.app_context():
        actor = db.session.scalar(select(User).where(User.username == "editor"))
        assert actor is not None
        for value in ("0", "false", "arbitrary", 1):
            with pytest.raises(ValueError, match="Consent Confirmation"):
                patients.create_patient(
                    actor=actor,
                    patient_id=f"VG-DIRECT-BAD-{value}",
                    name="Not Stored",
                    birth_year=1990,
                    consent_confirmed=value,
                )
        assert db.session.scalar(select(func.count(Patient.id))) == 0

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


def test_legacy_renderer_routes_are_removed_after_export_cutover(app) -> None:
    add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "/login", "editor")
    assert client.get("/prototype").status_code == 404
    assert client.post("/render").status_code == 404


def test_create_admin_records_bootstrap_audit_and_rolls_back_with_audit_failure(
    app, monkeypatch
) -> None:
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "create-admin",
            "--username",
            "bootstrap",
            "--display-name",
            "Bootstrap Admin",
            "--password",
            "correct horse battery staple",
        ]
    )
    assert result.exit_code == 0, result.output
    with app.app_context():
        user = db.session.scalar(select(User).where(User.username == "bootstrap"))
        assert user is not None
        event = db.session.scalar(
            select(AuditEvent).where(AuditEvent.entity_id == str(user.id))
        )
        assert event is not None
        assert event.action == "user.create"
        assert event.actor_id is None
        assert event.details["actor"] == SYSTEM_ACTOR

    from app import auth

    def fail_audit(**_kwargs: object) -> None:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(auth, "append_audit", fail_audit)
    result = runner.invoke(
        args=[
            "create-admin",
            "--username",
            "failed-bootstrap",
            "--display-name",
            "Failed Bootstrap",
            "--password",
            "correct horse battery staple",
        ]
    )
    assert result.exit_code != 0
    with app.app_context():
        assert db.session.scalar(select(User).where(User.username == "failed-bootstrap")) is None
        assert db.session.scalar(select(func.count(AuditEvent.id))) == 1


def test_secure_session_defaults_are_enabled(app) -> None:
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["WTF_CSRF_ENABLED"] is True
    assert app.config["DEBUG"] is False
    assert app.config["MAX_CONTENT_LENGTH"] == configured_request_limit()

    production = create_app(
        {
            "APP_ENV": "production",
            "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"],
            "MEDIA_ROOT": str(Path(app.config["MEDIA_ROOT"]) / "production-media"),
            "SECRET_KEY": "production-secret",
            "SESSION_COOKIE_SECURE": False,
        }
    )
    assert production.config["SESSION_COOKIE_SECURE"] is True


def test_safe_next_url_rejects_browser_normalized_open_redirects() -> None:
    assert _safe_next_url("/patients") == "/patients"
    for value in (
        "//evil.example",
        "/\\evil.example",
        "/patients\\evil.example",
        "https://evil.example",
        "patients",
        "/patients\n",
        "/patients\x00",
    ):
        assert _safe_next_url(value) is None
