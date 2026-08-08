from __future__ import annotations

import hashlib
import io
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from PIL import Image
from sqlalchemy import create_engine, select, text

from app import create_app
from app.board import CanvasRenderSpec, FrameRenderSpec, cover_crop_normalized, layout_frames, render_canvas
from app.comparisons import (
    EditLeaseError,
    StaleVersionError,
    acquire_edit_lease,
    add_frame,
    create_comparison_set,
    render_persisted_set,
    save_comparison_set,
)
from app.captures import create_capture
from app.db import db, normalize_database_url
from app.models import Capture, ComparisonSet, Patient, ShotType, User
from app.storage import ManagedStorage


CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def app_config(tmp_path: Path, database_url: str) -> dict[str, object]:
    return {
        "TESTING": True,
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "MEDIA_ROOT": str(tmp_path / "media"),
        "SECRET_KEY": "slice-4-test-secret",
        "SESSION_COOKIE_SECURE": False,
    }


@pytest.fixture(scope="session")
def migrated_test_database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 4 PostgreSQL tests")
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


def add_user(application, username: str) -> int:
    with application.app_context():
        user = User(username=username, display_name=username.title(), role="editor", active=True)
        user.set_password("correct horse battery staple")
        db.session.add(user)
        db.session.commit()
        return user.id


def fixture_patient(application, user_id: int) -> int:
    with application.app_context():
        patient = Patient(
            patient_id="SLICE4-0001",
            name="Slice Four Fixture",
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


def fixture_image() -> bytes:
    image = Image.new("RGB", (100, 100))
    image.paste("red", (0, 0, 50, 50))
    image.paste("green", (50, 0, 100, 50))
    image.paste("blue", (0, 50, 50, 100))
    image.paste("yellow", (50, 50, 100, 100))
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=100)
    return stream.getvalue()


def fixture_capture(application, user_id: int, patient_id: int, day: str) -> int:
    with application.app_context():
        actor = db.session.get(User, user_id)
        patient = db.session.get(Patient, patient_id)
        shot_type = db.session.scalar(select(ShotType).where(ShotType.name == "Anterior"))
        assert actor is not None and patient is not None and shot_type is not None
        capture = create_capture(
            actor=actor,
            patient=patient,
            upload=fixture_image(),
            capture_date=day,
            capture_date_confirmed=True,
            shot_type=shot_type,
            original_filename=f"{day}.jpg",
        )
        return capture.id


def login(client, username: str) -> str:
    page = client.get("/login")
    token = CSRF_RE.search(page.get_data(as_text=True)).group(1)
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": "correct horse battery staple",
            "csrf_token": token,
        },
    )
    assert response.status_code == 302
    return token


def test_four_corner_crop_extremes_and_hidden_frames_are_deterministic() -> None:
    image = Image.open(io.BytesIO(fixture_image()))
    try:
        for pan_x, pan_y, expected in (
            (-1, -1, (255, 0, 0)),
            (1, -1, (0, 128, 0)),
            (-1, 1, (0, 0, 255)),
            (1, 1, (255, 255, 0)),
        ):
            crop = cover_crop_normalized(image, 20, 20, zoom=5, pan_x=pan_x, pan_y=pan_y)
            try:
                pixel = crop.getpixel((3, 3))
                assert sum(abs(pixel[index] - expected[index]) for index in range(3)) < 90
            finally:
                crop.close()
    finally:
        image.close()

    spec = CanvasRenderSpec(
        width_mm=100,
        height_mm=100,
        frame_ratio=1,
        columns=3,
        frames=[
            FrameRenderSpec(1, visible=True),
            FrameRenderSpec(2, visible=False),
            FrameRenderSpec(3, visible=True),
            FrameRenderSpec(4, visible=True),
        ],
    )
    geometry = layout_frames(spec)
    assert [item.id for item in geometry] == [1, 3, 4]
    assert geometry[-1].x_mm == pytest.approx(200 / 3)


def test_set_state_preview_and_original_checksum_survive_reload(app, tmp_path: Path):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    capture_ids = [
        fixture_capture(app, editor_id, patient_id, day)
        for day in ("2024-01-01", "2024-02-01")
    ]
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="History")
        for capture_id in capture_ids:
            add_frame(actor=actor, comparison_set=comparison_set, capture_id=capture_id)
        comparison_set = db.session.get(ComparisonSet, comparison_set.id)
        assert comparison_set is not None
        original_sha = [capture.sha256 for capture in db.session.scalars(select(Capture).order_by(Capture.id))]
        saved = save_comparison_set(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=comparison_set.version,
            title="Configured history",
            preset_key="16:10",
            frame_ratio=2,
            columns=2,
            show_patient_id=True,
            date_label_default=False,
            frames=[
                {"id": comparison_set.frames[0].id, "label": "Before", "zoom": 5, "pan_x": -1, "pan_y": -1},
                {"id": comparison_set.frames[1].id, "visible": False, "date_visible_override": True},
            ],
            ordered_frame_ids=[comparison_set.frames[1].id, comparison_set.frames[0].id],
        )
        version = saved.version
        reloaded = db.session.get(ComparisonSet, saved.id)
        assert reloaded is not None
        assert reloaded.name == "Configured history"
        assert reloaded.preset_key == "16:10"
        assert float(reloaded.canvas_width_mm) == 297
        assert float(reloaded.canvas_height_mm) == pytest.approx(185.63)
        assert [frame.id for frame in reloaded.frames] == [2, 1]
        assert reloaded.frames[0].visible is False
        assert reloaded.frames[1].label == "Before"
        assert reloaded.frames[1].zoom == 5
        assert [capture.sha256 for capture in db.session.scalars(select(Capture).order_by(Capture.id))] == original_sha
        expected_preview = render_persisted_set(reloaded, expected_version=version)
        media_root = Path(app.config["MEDIA_ROOT"])
        original_keys = list(db.session.scalars(select(Capture.storage_key)))

    storage = ManagedStorage(media_root)
    assert [hashlib.sha256(storage.resolve(key).read_bytes()).hexdigest() for key in original_keys] == original_sha
    client = app.test_client()
    login(client, "editor")
    response = client.get(f"/comparison-sets/{saved.id}/preview?version={version}")
    assert response.status_code == 200
    assert response.data == expected_preview
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Comparison-Set-Version"] == str(version)


def test_two_editors_cannot_save_and_stale_version_is_rejected(app):
    first_id = add_user(app, "first")
    second_id = add_user(app, "second")
    patient_id = fixture_patient(app, first_id)
    capture_id = fixture_capture(app, first_id, patient_id, "2024-01-01")
    with app.app_context():
        first = db.session.get(User, first_id)
        second = db.session.get(User, second_id)
        patient = db.session.get(Patient, patient_id)
        assert first is not None and second is not None and patient is not None
        comparison_set = create_comparison_set(actor=first, patient=patient, name="Locked")
        add_frame(actor=first, comparison_set=comparison_set, capture_id=capture_id)
        current = db.session.get(ComparisonSet, comparison_set.id)
        assert current is not None
        acquire_edit_lease(actor=first, comparison_set=current)
        current = db.session.get(ComparisonSet, current.id)
        assert current is not None
        with pytest.raises(EditLeaseError):
            save_comparison_set(
                actor=second,
                comparison_set=current,
                expected_version=current.version,
                title="Should fail",
            )
        with pytest.raises(StaleVersionError):
            save_comparison_set(
                actor=first,
                comparison_set=current,
                expected_version=current.version - 1,
                title="Stale",
            )


def test_expired_edit_lease_can_be_acquired(app):
    first_id = add_user(app, "first")
    second_id = add_user(app, "second")
    patient_id = fixture_patient(app, first_id)
    with app.app_context():
        first = db.session.get(User, first_id)
        second = db.session.get(User, second_id)
        patient = db.session.get(Patient, patient_id)
        assert first is not None and second is not None and patient is not None
        comparison_set = create_comparison_set(actor=first, patient=patient, name="Expired")
        comparison_set.lock_holder_id = first.id
        comparison_set.lock_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.session.commit()
        acquired = acquire_edit_lease(actor=second, comparison_set=comparison_set)
        assert acquired.lock_holder_id == second.id
        assert acquired.lock_expires_at > datetime.now(timezone.utc)


def test_http_save_requires_the_active_lease_and_expected_version(app):
    first_id = add_user(app, "first")
    second_id = add_user(app, "second")
    patient_id = fixture_patient(app, first_id)
    with app.app_context():
        first = db.session.get(User, first_id)
        patient = db.session.get(Patient, patient_id)
        assert first is not None and patient is not None
        comparison_set = create_comparison_set(actor=first, patient=patient, name="HTTP")
        set_id = comparison_set.id
        version = comparison_set.version

    first_client = app.test_client()
    second_client = app.test_client()
    first_token = login(first_client, "first")
    second_token = login(second_client, "second")
    assert first_client.get(f"/comparison-sets/{set_id}").status_code == 200
    blocked = second_client.post(
        f"/comparison-sets/{set_id}/save",
        json={"version": version, "title": "blocked"},
        headers={"X-CSRFToken": second_token},
    )
    assert blocked.status_code == 409
    saved = first_client.post(
        f"/comparison-sets/{set_id}/save",
        json={"version": version, "title": "saved"},
        headers={"X-CSRFToken": first_token},
    )
    assert saved.status_code == 200
    stale = first_client.post(
        f"/comparison-sets/{set_id}/save",
        json={"version": version, "title": "stale"},
        headers={"X-CSRFToken": first_token},
    )
    assert stale.status_code == 409


def test_renderer_applies_exif_before_geometry_without_mutating_source() -> None:
    source = Image.new("RGB", (4, 2), "red")
    exif = source.getexif()
    exif[274] = 6
    before = source.tobytes()
    spec = CanvasRenderSpec(20, 20, 1, 1, [FrameRenderSpec(1, image=source, zoom=1)])
    rendered = render_canvas(spec, dpi=20)
    try:
        assert rendered.size == (15, 15)
        assert source.tobytes() == before
    finally:
        rendered.close()
        source.close()


def test_frame_label_rejects_lone_surrogate_before_render() -> None:
    spec = CanvasRenderSpec(20, 20, 1, 1, [FrameRenderSpec(1, label="\ud800")])
    with pytest.raises(ValueError, match="Unicode"):
        layout_frames(spec)
