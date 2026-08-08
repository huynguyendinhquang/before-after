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
from app import comparisons as comparisons_module
from app.board import (
    CanvasRenderSpec,
    FrameRenderSpec,
    cover_crop_normalized,
    layout_frames,
    render_canvas,
)
from app.comparisons import (
    ComparisonError,
    EditLeaseError,
    PreviewLimitError,
    StaleVersionError,
    acquire_edit_lease,
    add_frame,
    create_comparison_set,
    render_persisted_set,
    render_spec_for_set,
    save_comparison_set,
    _canvas_values,
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


def add_set_frame(actor: User, comparison_set: ComparisonSet, capture_id: int):
    current = db.session.get(ComparisonSet, comparison_set.id)
    assert current is not None
    if current.lock_holder_id != actor.id:
        current = acquire_edit_lease(
            actor=actor,
            comparison_set=current,
            expected_version=current.version,
        )
    return add_frame(
        actor=actor,
        comparison_set=current,
        capture_id=capture_id,
        expected_version=current.version,
    )


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


def test_custom_canvas_dimensions_use_decimal_validation() -> None:
    key, width, height = _canvas_values(
        preset_key="custom-mm",
        width_mm="123.45",
        height_mm="67.00",
    )
    assert key == "custom"
    assert str(width) == "123.45"
    assert str(height) == "67.00"
    for value in ("0", "-1", "1.001", "1000000.00"):
        with pytest.raises(ComparisonError):
            _canvas_values(preset_key="custom", width_mm=value, height_mm="10")


def test_custom_canvas_form_round_trip_and_invalid_values_are_controlled(app) -> None:
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    client = app.test_client()
    token = login(client, "editor")
    base = {
        "preset_key": "custom",
        "canvas_width_mm": "123.45",
        "canvas_height_mm": "67.00",
        "frame_ratio": "1",
        "columns": "2",
        "date_label_default": "y",
        "csrf_token": token,
    }
    response = client.post(
        f"/patients/{patient_id}/comparison-sets/new",
        data={**base, "name": "Custom form"},
    )
    assert response.status_code == 302
    with app.app_context():
        saved = db.session.scalar(
            select(ComparisonSet).where(ComparisonSet.name == "Custom form")
        )
        assert saved is not None
        assert str(saved.canvas_width_mm) == "123.45"
        assert str(saved.canvas_height_mm) == "67.00"

    for index, value in enumerate(("0", "1.001", "1000000.00")):
        response = client.post(
            f"/patients/{patient_id}/comparison-sets/new",
            data={**base, "name": f"Invalid {index}", "canvas_width_mm": value},
        )
        assert response.status_code == 400


@pytest.mark.parametrize("name", ["Bad\x00name", "Bad\x1fname"])
def test_create_rejects_control_characters_in_set_name(app, name: str) -> None:
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    client = app.test_client()
    token = login(client, "editor")

    response = client.post(
        f"/patients/{patient_id}/comparison-sets/new",
        data={"name": name, "csrf_token": token},
    )

    assert response.status_code == 400
    assert "invalid control characters" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(select(ComparisonSet)) is None


@pytest.mark.parametrize("label", ["Bad\x00label", "Bad\x1flabel"])
def test_save_rejects_control_characters_in_frame_label(app, label: str) -> None:
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    capture_id = fixture_capture(app, editor_id, patient_id, "2024-01-01")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Labels")
        add_set_frame(actor, comparison_set, capture_id)
        current = db.session.get(ComparisonSet, comparison_set.id)
        assert current is not None and current.frames
        set_id = current.id
        frame_id = current.frames[0].id
        version = current.version

    client = app.test_client()
    token = login(client, "editor")
    response = client.post(
        f"/comparison-sets/{set_id}/save",
        json={"version": version, "frames": [{"id": frame_id, "label": label}]},
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 400
    assert "invalid control characters" in response.json["error"]
    with app.app_context():
        saved = db.session.get(ComparisonSet, set_id)
        assert saved is not None and saved.frames
        assert saved.version == version
        assert saved.frames[0].label is None


def test_add_frame_without_version_is_conflict(app) -> None:
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    capture_id = fixture_capture(app, editor_id, patient_id, "2024-01-01")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Add")
        acquire_edit_lease(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=comparison_set.version,
        )
        set_id = comparison_set.id

    client = app.test_client()
    token = login(client, "editor")
    response = client.post(
        f"/comparison-sets/{set_id}/frames",
        data={"capture_id": str(capture_id), "csrf_token": token},
    )
    assert response.status_code == 409


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
            add_set_frame(actor, comparison_set, capture_id)
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
        assert render_spec_for_set(reloaded, expected_version=version).version == version
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
        add_set_frame(first, comparison_set, capture_id)
        current = db.session.get(ComparisonSet, comparison_set.id)
        assert current is not None
        acquire_edit_lease(actor=first, comparison_set=current, expected_version=current.version)
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


def test_other_editor_and_viewer_see_active_set_read_only(app):
    owner_id = add_user(app, "owner")
    other_id = add_user(app, "other")
    viewer_id = add_user(app, "viewer")
    patient_id = fixture_patient(app, owner_id)
    capture_id = fixture_capture(app, owner_id, patient_id, "2024-01-01")
    with app.app_context():
        owner = db.session.get(User, owner_id)
        patient = db.session.get(Patient, patient_id)
        assert owner is not None and patient is not None
        comparison_set = create_comparison_set(actor=owner, patient=patient, name="Read-only")
        add_set_frame(owner, comparison_set, capture_id)
        current = db.session.get(ComparisonSet, comparison_set.id)
        assert current is not None and current.frames
        save_comparison_set(
            actor=owner,
            comparison_set=current,
            expected_version=current.version,
            title="Visible Canvas",
            frames=[{"id": current.frames[0].id, "label": "Before"}],
        )
        set_id = current.id

    for username in ("other", "viewer"):
        client = app.test_client()
        login(client, username)
        response = client.get(f"/comparison-sets/{set_id}")
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Visible Canvas" in body
        assert "Before" in body
        assert "<dt>Canvas</dt>" in body
        assert "<h2>Frames</h2>" in body
        assert 'id="canvas-editor"' not in body
        assert "Acquire edit lease" not in body


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
        acquired = acquire_edit_lease(
            actor=second,
            comparison_set=comparison_set,
            expected_version=comparison_set.version,
        )
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
    acquired = first_client.post(
        f"/comparison-sets/{set_id}/lease/acquire",
        data={"version": version},
        headers={"X-CSRFToken": first_token},
    )
    assert acquired.status_code == 302
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


def test_editor_checkbox_flags_can_toggle_every_set_output_flag(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Flags")
        acquire_edit_lease(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=comparison_set.version,
        )
        set_id = comparison_set.id
        version = comparison_set.version

    client = app.test_client()
    token = login(client, "editor")
    detail = client.get(f"/comparison-sets/{set_id}")
    body = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert 'input[type="checkbox"][name="${name}"]' in body
    assert "editor.querySelector('[name=\"show_patient_id\"]')" not in body

    enabled = client.post(
        f"/comparison-sets/{set_id}/save",
        json={
            "version": version,
            "show_patient_id": True,
            "show_patient_name": True,
            "show_birth_year": True,
            "date_label_default": True,
        },
        headers={"X-CSRFToken": token},
    )
    assert enabled.status_code == 200
    next_version = enabled.json["version"]
    with app.app_context():
        saved = db.session.get(ComparisonSet, set_id)
        assert saved is not None
        assert all(
            (
                saved.show_patient_id,
                saved.show_patient_name,
                saved.show_birth_year,
                saved.date_label_default,
            )
        )

    disabled = client.post(
        f"/comparison-sets/{set_id}/save",
        json={
            "version": next_version,
            "show_patient_id": False,
            "show_patient_name": False,
            "show_birth_year": False,
            "date_label_default": False,
        },
        headers={"X-CSRFToken": token},
    )
    assert disabled.status_code == 200
    with app.app_context():
        saved = db.session.get(ComparisonSet, set_id)
        assert saved is not None
        assert not any(
            (
                saved.show_patient_id,
                saved.show_patient_name,
                saved.show_birth_year,
                saved.date_label_default,
            )
        )


def test_save_duplicate_active_name_is_controlled_and_rolls_back(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        create_comparison_set(actor=actor, patient=patient, name="Existing")
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Other")
        acquire_edit_lease(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=comparison_set.version,
        )
        set_id = comparison_set.id
        version = comparison_set.version

    client = app.test_client()
    token = login(client, "editor")
    response = client.post(
        f"/comparison-sets/{set_id}/save",
        json={"version": version, "title": "Existing"},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 409
    assert "already exists" in response.json["error"]
    with app.app_context():
        assert not db.session().in_transaction()
        saved = db.session.get(ComparisonSet, set_id)
        assert saved is not None
        assert saved.name == "Other"
        assert saved.version == version


def test_add_frame_error_keeps_current_editor_context_and_submitted_values(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    capture_id = fixture_capture(app, editor_id, patient_id, "2024-01-01")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Errors")
        acquire_edit_lease(
            actor=actor,
            comparison_set=comparison_set,
            expected_version=comparison_set.version,
        )
        set_id = comparison_set.id
        version = comparison_set.version

    client = app.test_client()
    token = login(client, "editor")
    response = client.post(
        f"/comparison-sets/{set_id}/frames",
        data={
            "capture_id": str(capture_id),
            "version": str(version - 1),
            "csrf_token": token,
        },
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 409
    assert 'id="canvas-editor"' in body
    assert "Add Frame" in body
    assert "Set version is required" in body
    assert f'<option value="{capture_id}" selected>' in body
    assert f'name="version" value="{version}"' in body


def test_get_does_not_claim_lease_and_heartbeat_requires_version(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Lease")
        set_id = comparison_set.id
        version = comparison_set.version

    client = app.test_client()
    token = login(client, "editor")
    assert client.get(f"/comparison-sets/{set_id}").status_code == 200
    with app.app_context():
        current = db.session.get(ComparisonSet, set_id)
        assert current is not None and current.lock_holder_id is None

    acquired = client.post(
        f"/comparison-sets/{set_id}/lease/acquire",
        json={"version": version},
        headers={"X-CSRFToken": token},
    )
    assert acquired.status_code == 200
    missing = client.post(
        f"/comparison-sets/{set_id}/lease/heartbeat",
        json={},
        headers={"X-CSRFToken": token},
    )
    assert missing.status_code == 409
    stale = client.post(
        f"/comparison-sets/{set_id}/lease/heartbeat",
        json={"version": version - 1},
        headers={"X-CSRFToken": token},
    )
    assert stale.status_code == 409
    renewed = client.post(
        f"/comparison-sets/{set_id}/lease/heartbeat",
        json={"version": version},
        headers={"X-CSRFToken": token},
    )
    assert renewed.status_code == 200
    assert renewed.json["version"] == version


def test_preview_limits_are_checked_before_media_open(app, monkeypatch):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    capture_id = fixture_capture(app, editor_id, patient_id, "2024-01-01")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Bounded")
        add_set_frame(actor, comparison_set, capture_id)
        current = db.session.get(ComparisonSet, comparison_set.id)
        assert current is not None
        app.config["COMPARISON_PREVIEW_MAX_BYTES"] = 1
        with monkeypatch.context() as patch:
            opened = []
            original_open = ManagedStorage.open_read

            def tracked_open(storage, key):
                opened.append(key)
                return original_open(storage, key)

            patch.setattr(ManagedStorage, "open_read", tracked_open)
            with pytest.raises(PreviewLimitError):
                render_persisted_set(current)
            assert opened == []


def test_preview_decoded_pixel_limit_is_checked_before_media_open(app, monkeypatch):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    capture_id = fixture_capture(app, editor_id, patient_id, "2024-01-01")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        comparison_set = create_comparison_set(actor=actor, patient=patient, name="Pixels")
        add_set_frame(actor, comparison_set, capture_id)
        current = db.session.get(ComparisonSet, comparison_set.id)
        assert current is not None
        app.config["COMPARISON_PREVIEW_MAX_PIXELS"] = 1
        with monkeypatch.context() as patch:
            opened = []
            original_open = ManagedStorage.open_read

            def tracked_open(storage, key):
                opened.append(key)
                return original_open(storage, key)

            patch.setattr(ManagedStorage, "open_read", tracked_open)
            with pytest.raises(PreviewLimitError, match="decoded pixels"):
                render_persisted_set(current)
            assert opened == []

def test_render_canvas_clamps_a4_frame_edges_at_150_dpi(monkeypatch) -> None:
    source = Image.new("RGB", (20, 20), "red")
    calls = []
    original_paste = Image.Image.paste

    def tracked_paste(image, value, box=None, mask=None):
        calls.append((image, value, box))
        return original_paste(image, value, box, mask)

    monkeypatch.setattr(Image.Image, "paste", tracked_paste)
    spec = CanvasRenderSpec(
        width_mm=297,
        height_mm=210,
        frame_ratio=1.414,
        columns=3,
        frames=[FrameRenderSpec(index, image=source) for index in range(4)],
    )
    board = render_canvas(spec, dpi=150)
    try:
        board_calls = [call for call in calls if call[0] is board]
        assert len(board_calls) == 4
        for _, cell, box in board_calls:
            assert isinstance(box, tuple) and len(box) == 2
            x, y = box
            assert 0 <= x < board.width
            assert 0 <= y < board.height
            assert x + cell.width <= board.width
            assert y + cell.height <= board.height
            assert cell.width > 0 and cell.height > 0
    finally:
        board.close()
        source.close()


def test_renderer_applies_exif_before_geometry_without_mutating_source() -> None:
    source = Image.new("RGB", (4, 2))
    pixels = source.load()
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    for x, color in enumerate(colors):
        pixels[x, 0] = color
        pixels[x, 1] = color
    exif = source.getexif()
    exif[274] = 6
    before = source.tobytes()
    oriented = cover_crop_normalized(source, 2, 4)
    try:
        assert oriented.size == (2, 4)
        assert [oriented.getpixel((0, y)) for y in range(4)] == colors
        assert source.tobytes() == before
    finally:
        oriented.close()
        source.close()


def test_zoom_resize_is_bounded_to_the_destination(monkeypatch) -> None:
    source = Image.new("RGB", (4000, 4000), "red")
    calls = []
    original_resize = Image.Image.resize

    def tracked_resize(image, size, *args, **kwargs):
        calls.append(size)
        return original_resize(image, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", tracked_resize)
    result = cover_crop_normalized(source, 100, 100, zoom=5)
    try:
        assert result.size == (100, 100)
        assert calls == [(100, 100)]
    finally:
        result.close()
        source.close()


def test_frame_label_rejects_lone_surrogate_before_render() -> None:
    spec = CanvasRenderSpec(20, 20, 1, 1, [FrameRenderSpec(1, label="\ud800")])
    with pytest.raises(ValueError, match="Unicode"):
        layout_frames(spec)
