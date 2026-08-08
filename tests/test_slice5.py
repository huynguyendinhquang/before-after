from __future__ import annotations

import hashlib
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select, text

from app import create_app
from app import comparisons as comparisons_module
from app.comparisons import (
    acquire_edit_lease,
    add_frame,
    create_comparison_set,
    create_export,
    save_comparison_set,
)
from app.captures import create_capture
from app.db import db, normalize_database_url
from app.models import AuditEvent, Capture, ComparisonSet, Export, Frame, Patient, ShotType, User
from app.storage import ManagedStorage


CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def app_config(tmp_path: Path, database_url: str) -> dict[str, object]:
    return {
        "TESTING": True,
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "MEDIA_ROOT": str(tmp_path / "media"),
        "SECRET_KEY": "slice-5-test-secret",
        "SESSION_COOKIE_SECURE": False,
        "BOARD_RENDER_DPI": 20,
    }


@pytest.fixture(scope="session")
def migrated_test_database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 5 PostgreSQL tests")
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
                "TRUNCATE audit_events, exports, frames, comparison_sets, captures, "
                "shot_types, patients, users RESTART IDENTITY CASCADE"
            )
        )
        db.session.commit()
    yield application
    with application.app_context():
        db.session.rollback()
        db.session.execute(
            text(
                "TRUNCATE audit_events, exports, frames, comparison_sets, captures, "
                "shot_types, patients, users RESTART IDENTITY CASCADE"
            )
        )
        db.session.commit()


def add_user(application, username: str, role: str = "editor") -> int:
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


def fixture_image(color: str) -> bytes:
    image = Image.new("RGB", (100, 100))
    image.paste(color, (0, 0, 50, 50))
    image.paste("green", (50, 0, 100, 50))
    image.paste("blue", (0, 50, 50, 100))
    image.paste("yellow", (50, 50, 100, 100))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def fixture_patient(application, user_id: int) -> int:
    with application.app_context():
        patient = Patient(
            patient_id="SLICE5-0001",
            name="Slice Five Fixture",
            birth_year=1990,
            consent_confirmed_by_id=user_id,
            consent_confirmed_at=datetime.now(timezone.utc),
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        shot_type = ShotType(name="Anterior", state="canonical", created_by_id=user_id)
        db.session.add_all([patient, shot_type])
        db.session.commit()
        return patient.id


def fixture_capture(application, user_id: int, patient_id: int, color: str, day: str) -> int:
    with application.app_context():
        actor = db.session.get(User, user_id)
        patient = db.session.get(Patient, patient_id)
        shot_type = db.session.scalar(select(ShotType).where(ShotType.name == "Anterior"))
        assert actor is not None and patient is not None and shot_type is not None
        capture = create_capture(
            actor=actor,
            patient=patient,
            upload=fixture_image(color),
            capture_date=day,
            capture_date_confirmed=True,
            shot_type=shot_type,
            original_filename=f"{day}.png",
        )
        return capture.id


def set_with_frames(application, user_id: int, patient_id: int) -> tuple[int, int, list[int], list[str]]:
    with application.app_context():
        actor = db.session.get(User, user_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(
            actor=actor,
            patient=patient,
            name="Export fixture",
            canvas_width_mm="40.00",
            canvas_height_mm="40.00",
            preset_key="custom",
            frame_ratio=1,
            columns=1,
            show_patient_id=True,
            show_patient_name=True,
            show_birth_year=True,
        )
        capture_ids = [
            fixture_capture(application, user_id, patient_id, color, day)
            for color, day in (("red", "2024-01-01"), ("purple", "2024-02-01"))
        ]
        # The helper above uses the same application context/session and rows
        # remain available after its commits.
        comparison_set = db.session.get(ComparisonSet, comparison_set.id)
        assert comparison_set is not None
        for capture_id in capture_ids:
            comparison_set = db.session.get(ComparisonSet, comparison_set.id)
            assert comparison_set is not None
            if comparison_set.lock_holder_id != actor.id:
                comparison_set = acquire_edit_lease(
                    actor=actor,
                    comparison_set=comparison_set,
                    expected_version=comparison_set.version,
                )
            add_frame(
                actor=actor,
                comparison_set=comparison_set,
                capture_id=capture_id,
                expected_version=comparison_set.version,
            )
        current = db.session.get(ComparisonSet, comparison_set.id)
        assert current is not None
        frames = list(db.session.scalars(select(Frame).where(Frame.comparison_set_id == current.id).order_by(Frame.position)))
        saved = save_comparison_set(
            actor=actor,
            comparison_set=current,
            expected_version=current.version,
            title="WYSIWYG export",
            show_patient_id=True,
            show_patient_name=True,
            show_birth_year=True,
            frames=[
                {"id": frames[0].id, "zoom": 5, "pan_x": -1, "pan_y": -1, "label": "Visible"},
                {"id": frames[1].id, "visible": False, "label": "Hidden"},
            ],
        )
        originals = [capture.sha256 for capture in db.session.scalars(select(Capture).order_by(Capture.id))]
        return saved.id, saved.version, [frame.id for frame in frames], originals


def test_png_pdf_are_audited_wysiwyg_and_keep_originals_unchanged(app):
    editor_id = add_user(app, "editor")
    add_user(app, "viewer", "viewer")
    patient_id = fixture_patient(app, editor_id)
    set_id, version, _frame_ids, original_sha = set_with_frames(app, editor_id, patient_id)

    client = app.test_client()
    login(client, "editor")
    preview = client.get(f"/comparison-sets/{set_id}/preview?version={version}")
    assert preview.status_code == 200
    assert preview.headers["X-Comparison-Set-Version"] == str(version)

    token = csrf_token(client, f"/comparison-sets/{set_id}")
    png = client.post(
        f"/comparison-sets/{set_id}/export",
        data={"format": "png", "version": str(version), "csrf_token": token},
    )
    assert png.status_code == 200, png.get_data(as_text=True)
    assert png.data == preview.data
    assert png.headers["Cache-Control"] == "no-store"
    assert png.headers["X-Comparison-Set-Version"] == str(version)
    with Image.open(io.BytesIO(png.data)) as image:
        image.load()
        assert image.size == (int(40 * 20 / 25.4), int(40 * 20 / 25.4))
        assert image.getpixel((15, 15))[0] > 200

    pdf = client.post(
        f"/comparison-sets/{set_id}/export/pdf",
        data={"version": str(version), "csrf_token": token},
    )
    assert pdf.status_code == 200
    assert pdf.data.startswith(b"%PDF")
    assert pdf.headers["Cache-Control"] == "no-store"

    with app.app_context():
        exports = list(db.session.scalars(select(Export).order_by(Export.id)))
        assert len(exports) == 2
        assert all(item.rendered_version == version for item in exports)
        assert all(item.byte_count > 0 and len(item.sha256) == 64 for item in exports)
        assert all(item.created_by_id == editor_id for item in exports)
        assert db.session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "export.create")) == 2
        assert all(
            event.actor_id == editor_id
            for event in db.session.scalars(
                select(AuditEvent).where(AuditEvent.action == "export.create")
            )
        )
        keys = [item.storage_key for item in exports]

    storage = ManagedStorage(app.config["MEDIA_ROOT"])
    for item, key in zip(exports, keys):
        payload = storage.resolve(key).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == item.sha256
        assert len(payload) == item.byte_count
    with app.app_context():
        assert [capture.sha256 for capture in db.session.scalars(select(Capture).order_by(Capture.id))] == original_sha


def test_viewer_and_stale_version_cannot_export(app):
    editor_id = add_user(app, "editor")
    add_user(app, "viewer", "viewer")
    patient_id = fixture_patient(app, editor_id)
    set_id, version, _frame_ids, _original_sha = set_with_frames(app, editor_id, patient_id)

    editor = app.test_client()
    login(editor, "editor")
    token = csrf_token(editor, f"/comparison-sets/{set_id}")
    stale = editor.post(
        f"/comparison-sets/{set_id}/export",
        data={"format": "png", "version": str(version - 1), "csrf_token": token},
    )
    assert stale.status_code == 409

    viewer = app.test_client()
    login(viewer, "viewer")
    viewer_token = csrf_token(viewer, f"/comparison-sets/{set_id}")
    denied = viewer.post(
        f"/comparison-sets/{set_id}/export",
        data={"format": "png", "version": str(version), "csrf_token": viewer_token},
    )
    assert denied.status_code == 403
    assert viewer.post(
        f"/comparison-sets/{set_id}/export",
        data={"format": "png", "version": str(version)},
    ).status_code == 403


def test_failed_export_audit_cleans_derivative(app, monkeypatch: pytest.MonkeyPatch):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    set_id, version, _frame_ids, _original_sha = set_with_frames(app, editor_id, patient_id)

    def fail_audit(**_kwargs: object) -> None:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(comparisons_module, "append_audit", fail_audit)
    with app.app_context():
        actor = db.session.get(User, editor_id)
        comparison_set = db.session.get(ComparisonSet, set_id)
        assert actor is not None and comparison_set is not None
        with pytest.raises(RuntimeError, match="audit failed"):
            create_export(
                actor=actor,
                comparison_set=comparison_set,
                output_format="png",
                expected_version=version,
            )
        assert db.session.scalar(select(func.count(Export.id))) == 0

    media = Path(app.config["MEDIA_ROOT"])
    assert list((media / "derivatives").iterdir()) == []
    assert list((media / "quarantine").glob(".pending-*")) == []


def test_commit_acknowledgement_ambiguity_reconciles_export(app, monkeypatch: pytest.MonkeyPatch):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    set_id, version, _frame_ids, _original_sha = set_with_frames(app, editor_id, patient_id)
    with app.app_context():
        actor = db.session.get(User, editor_id)
        comparison_set = db.session.get(ComparisonSet, set_id)
        assert actor is not None and comparison_set is not None
        original_commit = db.session.commit

        def commit_then_raise() -> None:
            original_commit()
            raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(db.session, "commit", commit_then_raise)
        exported = create_export(
            actor=actor,
            comparison_set=comparison_set,
            output_format="png",
            expected_version=version,
        )
        assert exported.id is not None
        assert db.session.scalar(select(Export).where(Export.id == exported.id)) is not None

    media = Path(app.config["MEDIA_ROOT"])
    assert len(list((media / "derivatives").iterdir())) == 1
    assert list((media / "quarantine").glob(".pending-*")) == []
