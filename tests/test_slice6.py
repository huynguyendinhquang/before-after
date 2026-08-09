from __future__ import annotations

import io
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import create_engine, func, select, text

from app import create_app
from app.audit import append_audit
from app.admin import FinalAdminError, list_audit_events, update_user
from app.captures import (
    CaptureReferencedError,
    archive_capture,
    delete_capture,
    merge_shot_type,
    ShotTypeError,
    unarchive_capture,
)
from app.comparisons import (
    acquire_edit_lease,
    add_frame,
    archive_comparison_set,
    create_comparison_set,
    duplicate_comparison_set,
    unarchive_comparison_set,
)
from app.db import db, normalize_database_url
from app.models import AuditEvent, Capture, ComparisonSet, Frame, Patient, ShotType, User
from app.patients import archive_patient, search_patients, unarchive_patient
from app.storage import ManagedStorage


CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def app_config(tmp_path: Path, database_url: str) -> dict[str, object]:
    return {
        "TESTING": True,
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "MEDIA_ROOT": str(tmp_path / "media"),
        "SECRET_KEY": "slice-6-test-secret",
        "SESSION_COOKIE_SECURE": False,
    }


@pytest.fixture(scope="session")
def migrated_test_database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 6 PostgreSQL tests")
    database_url = normalize_database_url(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATABASE_URL", database_url)
    try:
        command.upgrade(config, "head")
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


def add_user(application, username: str, role: str) -> int:
    with application.app_context():
        user = User(username=username, display_name=username.title(), role=role, active=True)
        user.set_password("correct horse battery staple")
        db.session.add(user)
        db.session.commit()
        return user.id


def csrf_token(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = CSRF_RE.search(response.get_data(as_text=True))
    assert match
    return match.group(1)


def login(client, username: str) -> None:
    token = csrf_token(client, "/login")
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": "correct horse battery staple",
            "csrf_token": token,
        },
    )
    assert response.status_code == 302


def fixture_patient(application, user_id: int) -> int:
    with application.app_context():
        patient = Patient(
            patient_id="SLICE6-0001",
            name="Slice Six Fixture",
            birth_year=1990,
            consent_confirmed_by_id=user_id,
            consent_confirmed_at=datetime.now(timezone.utc),
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        db.session.add(patient)
        db.session.commit()
        return patient.id


def fixture_image(color: str = "red") -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (16, 8), color).save(stream, format="JPEG")
    return stream.getvalue()


def fixture_shot_type(application, user_id: int, name: str = "Anterior", *, state: str = "canonical") -> int:
    with application.app_context():
        shot_type = ShotType(name=name, state=state, created_by_id=user_id)
        db.session.add(shot_type)
        db.session.commit()
        return shot_type.id


def fixture_capture(application, user_id: int, patient_id: int, shot_type_id: int, color: str) -> int:
    from app.captures import create_capture

    with application.app_context():
        actor = db.session.get(User, user_id)
        patient = db.session.get(Patient, patient_id)
        shot_type = db.session.get(ShotType, shot_type_id)
        assert actor is not None and patient is not None and shot_type is not None
        capture = create_capture(
            actor=actor,
            patient=patient,
            upload=fixture_image(color),
            capture_date="2024-01-01",
            capture_date_confirmed=True,
            shot_type=shot_type,
            original_filename=f"{color}.jpg",
        )
        return capture.id


def test_duplicate_copies_frame_configuration_and_capture_ids(app):
    editor_id = add_user(app, "editor", "editor")
    patient_id = fixture_patient(app, editor_id)
    shot_type_id = fixture_shot_type(app, editor_id)
    capture_ids = [
        fixture_capture(app, editor_id, patient_id, shot_type_id, color)
        for color in ("red", "blue")
    ]

    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        source = create_comparison_set(
            actor=actor,
            patient=patient,
            name="History",
            preset_key="16:10",
            frame_ratio=1.5,
            columns=2,
            show_patient_id=True,
        )
        source = acquire_edit_lease(actor=actor, comparison_set=source, expected_version=source.version)
        add_frame(actor=actor, comparison_set=source, capture_id=capture_ids[0], expected_version=source.version)
        source = db.session.get(ComparisonSet, source.id)
        assert source is not None
        source = acquire_edit_lease(actor=actor, comparison_set=source, expected_version=source.version)
        add_frame(actor=actor, comparison_set=source, capture_id=capture_ids[1], expected_version=source.version)
        source = db.session.get(ComparisonSet, source.id)
        assert source is not None
        source.frames[0].visible = False
        source.frames[0].label = "Before"
        source.frames[0].zoom = 2.5
        source.frames[0].pan_x = -0.5
        source.version += 1
        db.session.commit()

        duplicate = duplicate_comparison_set(actor=actor, comparison_set=source, name="Follow-up")
        assert duplicate.version == 1
        assert duplicate.lock_holder_id is None
        assert duplicate.lock_expires_at is None
        assert duplicate.archived_at is None
        assert duplicate.name == "Follow-up"
        assert duplicate.preset_key == source.preset_key
        assert duplicate.frame_ratio == source.frame_ratio
        assert duplicate.columns == source.columns
        assert [frame.capture_id for frame in duplicate.frames] == capture_ids
        assert duplicate.frames[0].visible is False
        assert duplicate.frames[0].label == "Before"
        assert duplicate.frames[0].zoom == 2.5
        assert duplicate.frames[0].pan_x == -0.5
        assert db.session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "comparison_set.duplicate")
        ) == 1


def test_archive_unarchive_keeps_existing_frame_reference_and_hides_normal_lists(app):
    editor_id = add_user(app, "editor", "editor")
    patient_id = fixture_patient(app, editor_id)
    shot_type_id = fixture_shot_type(app, editor_id)
    capture_id = fixture_capture(app, editor_id, patient_id, shot_type_id, "red")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="History")
        comparison_set = acquire_edit_lease(actor=actor, comparison_set=comparison_set, expected_version=comparison_set.version)
        add_frame(actor=actor, comparison_set=comparison_set, capture_id=capture_id, expected_version=comparison_set.version)
        comparison_set = db.session.get(ComparisonSet, comparison_set.id)
        assert comparison_set is not None
        archive_capture(actor=actor, capture_id=capture_id)
        assert db.session.get(Capture, capture_id).archived_at is not None
        assert db.session.scalar(select(Frame.capture_id).where(Frame.comparison_set_id == comparison_set.id)) == capture_id
        archive_comparison_set(actor=actor, set_id=comparison_set.id)
        assert db.session.get(ComparisonSet, comparison_set.id).archived_at is not None
        unarchive_capture(actor=actor, capture_id=capture_id)
        unarchive_comparison_set(actor=actor, set_id=comparison_set.id)
        assert db.session.get(Capture, capture_id).archived_at is None
        assert db.session.get(ComparisonSet, comparison_set.id).archived_at is None


def test_archive_unarchive_patient_hides_children_from_normal_lists(app):
    editor_id = add_user(app, "editor", "editor")
    patient_id = fixture_patient(app, editor_id)
    with app.app_context():
        actor = db.session.get(User, editor_id)
        assert actor is not None
        archive_patient(actor=actor, patient_id=patient_id)
        assert search_patients() == []
        assert search_patients(include_archived=True)[0].archived_at is not None
        unarchive_patient(actor=actor, patient_id=patient_id)
        assert [patient.id for patient in search_patients()] == [patient_id]


def test_referenced_capture_delete_fails_and_unreferenced_delete_removes_media(app):
    editor_id = add_user(app, "editor", "editor")
    patient_id = fixture_patient(app, editor_id)
    shot_type_id = fixture_shot_type(app, editor_id)
    referenced_id = fixture_capture(app, editor_id, patient_id, shot_type_id, "red")
    free_id = fixture_capture(app, editor_id, patient_id, shot_type_id, "blue")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="History")
        comparison_set = acquire_edit_lease(actor=actor, comparison_set=comparison_set, expected_version=comparison_set.version)
        add_frame(actor=actor, comparison_set=comparison_set, capture_id=referenced_id, expected_version=comparison_set.version)
        with pytest.raises(CaptureReferencedError):
            delete_capture(actor=actor, capture_id=referenced_id)
        free = db.session.get(Capture, free_id)
        assert free is not None
        original_key = free.storage_key
        preview_key = ManagedStorage.preview_key(original_key)
        delete_capture(actor=actor, capture_id=free_id)
        assert db.session.get(Capture, free_id) is None
    storage = ManagedStorage(app.config["MEDIA_ROOT"])
    with pytest.raises(Exception):
        storage.resolve(original_key)
    with pytest.raises(Exception):
        storage.resolve(preview_key)
    with app.app_context():
        assert db.session.get(Capture, referenced_id) is not None


def test_capture_delete_commit_ambiguity_reconciles_media_safely(app, monkeypatch):
    editor_id = add_user(app, "editor", "editor")
    patient_id = fixture_patient(app, editor_id)
    shot_type_id = fixture_shot_type(app, editor_id)
    capture_id = fixture_capture(app, editor_id, patient_id, shot_type_id, "red")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        capture = db.session.get(Capture, capture_id)
        assert actor is not None and capture is not None
        original_key = capture.storage_key
        original_commit = db.session.commit

        def commit_then_raise() -> None:
            original_commit()
            raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(db.session, "commit", commit_then_raise)
        delete_capture(actor=actor, capture_id=capture_id)
        assert db.session.get(Capture, capture_id) is None
    storage = ManagedStorage(app.config["MEDIA_ROOT"])
    with pytest.raises(Exception):
        storage.resolve(original_key)
    assert not list((Path(app.config["MEDIA_ROOT"]) / "quarantine").iterdir())


def test_admin_can_create_update_and_disable_users(app):
    add_user(app, "admin", "admin")
    add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "admin")
    assert client.get("/admin/users").status_code == 200
    token = csrf_token(client, "/admin/users/new")
    created = client.post(
        "/admin/users/new",
        data={
            "username": "managed",
            "display_name": "Managed User",
            "password": "new-password",
            "role": "editor",
            "active": "y",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 302
    with app.app_context():
        managed = db.session.scalar(select(User).where(User.username == "managed"))
        assert managed is not None
        managed_id = managed.id
    token = csrf_token(client, f"/admin/users/{managed_id}/edit")
    updated = client.post(
        f"/admin/users/{managed_id}/edit",
        data={
            "username": "managed",
            "display_name": "Managed Viewer",
            "password": "",
            "role": "viewer",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302
    token = csrf_token(client, "/admin/users")
    disabled = client.post(
        f"/admin/users/{managed_id}/disable",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert disabled.status_code == 302
    with app.app_context():
        managed = db.session.get(User, managed_id)
        assert managed is not None and managed.role == "viewer" and managed.active is False
        assert db.session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.entity_id == str(managed_id))) >= 3

    editor = app.test_client()
    login(editor, "editor")
    assert editor.get("/admin/users").status_code == 403


def test_admin_merge_updates_captures_and_editor_is_forbidden(app):
    admin_id = add_user(app, "admin", "admin")
    editor_id = add_user(app, "editor", "editor")
    patient_id = fixture_patient(app, admin_id)
    target_id = fixture_shot_type(app, admin_id, "Anterior")
    proposal_id = fixture_shot_type(app, editor_id, "front view", state="proposal")
    capture_id = fixture_capture(app, editor_id, patient_id, proposal_id, "red")

    with app.app_context():
        admin = db.session.get(User, admin_id)
        proposal = db.session.get(ShotType, proposal_id)
        target = db.session.get(ShotType, target_id)
        assert admin is not None and proposal is not None and target is not None
        merge_shot_type(actor=admin, source=proposal, target=target)
        capture = db.session.get(Capture, capture_id)
        assert capture is not None and capture.shot_type_id == target_id
        assert proposal.state == "merged"
        assert proposal.canonical_target_id == target_id
        other_target = ShotType(name="Lateral", state="canonical", created_by_id=admin_id)
        db.session.add(other_target)
        db.session.commit()
        with pytest.raises(ShotTypeError):
            merge_shot_type(actor=admin, source=target, target=other_target)

    client = app.test_client()
    login(client, "editor")
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/shot-types").status_code == 403


def test_admin_last_account_guard_serializes_concurrent_demotions(app):
    first_id = add_user(app, "first-admin", "admin")
    second_id = add_user(app, "second-admin", "admin")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def demote(target_id: int) -> None:
        with app.app_context():
            actor = db.session.get(User, first_id)
            assert actor is not None
            barrier.wait(timeout=5)
            try:
                update_user(actor=actor, user=target_id, role="editor")
            except (FinalAdminError, PermissionError):
                outcomes.append("blocked")
            else:
                outcomes.append("updated")

    threads = [
        threading.Thread(target=demote, args=(first_id,)),
        threading.Thread(target=demote, args=(second_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["blocked", "updated"]
    with app.app_context():
        assert db.session.scalar(
            select(func.count(User.id)).where(User.active.is_(True), User.role == "admin")
        ) == 1


def test_admin_last_account_guard_and_bounded_audit_omit_pii(app):
    admin_id = add_user(app, "admin", "admin")
    with app.app_context():
        actor = db.session.get(User, admin_id)
        target = db.session.get(User, admin_id)
        assert actor is not None and target is not None
        with pytest.raises(FinalAdminError):
            update_user(actor=actor, user=target, active=False)
        with pytest.raises(FinalAdminError):
            update_user(actor=actor, user=target, role="editor")
        assert target.active is True and target.role == "admin"
        append_audit(
            actor=actor,
            action="capture.create",
            entity_type="capture",
            entity_id=1,
            details={"original_filename": "secret-original.jpg", "patient_name": "Sensitive Patient"},
        )
        db.session.commit()
        events = list_audit_events(limit=10000)
        assert len(events) <= 100

    client = app.test_client()
    login(client, "admin")
    response = client.get("/admin/audit?limit=10000")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "secret-original.jpg" not in response.get_data(as_text=True)
    assert "Sensitive Patient" not in response.get_data(as_text=True)
