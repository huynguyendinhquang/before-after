from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select, text

from app import captures as captures_module
from app import create_app
from app.captures import BatchLimitError, ConsentRequired, create_batch_captures, create_capture
from app.db import db, normalize_database_url
from app.models import AuditEvent, Capture, Patient, ShotType, User
from app.storage import ImageInspection, ManagedStorage


CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def app_config(tmp_path: Path, database_url: str) -> dict[str, object]:
    return {
        "TESTING": True,
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "MEDIA_ROOT": str(tmp_path / "media"),
        "SECRET_KEY": "slice-7-test-secret",
        "SESSION_COOKIE_SECURE": False,
    }


@pytest.fixture(scope="session")
def migrated_test_database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 7 PostgreSQL tests")
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


def fixture_patient(application, user_id: int, patient_id: str = "SLICE7-0001") -> int:
    with application.app_context():
        patient = Patient(
            patient_id=patient_id,
            name="Slice Seven Fixture",
            birth_year=1990,
            consent_confirmed_by_id=user_id,
            consent_confirmed_at=datetime.now(timezone.utc),
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        db.session.add(patient)
        db.session.commit()
        return patient.id


def fixture_shot_type(application, user_id: int, name: str = "Anterior") -> int:
    with application.app_context():
        shot_type = ShotType(name=name, state="canonical", created_by_id=user_id)
        db.session.add(shot_type)
        db.session.commit()
        return shot_type.id


def jpeg(color: str, *, exif_date: str | None = None) -> bytes:
    stream = io.BytesIO()
    image = Image.new("RGB", (16, 8), color)
    kwargs: dict[str, object] = {}
    if exif_date:
        exif = image.getexif()
        exif[36867] = exif_date
        kwargs["exif"] = exif.tobytes()
    image.save(stream, format="JPEG", **kwargs)
    return stream.getvalue()


def png(color: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 4), color).save(stream, format="PNG")
    return stream.getvalue()


def animated_webp() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 4), "red").save(
        stream,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (8, 4), "blue")],
        duration=1,
        loop=0,
    )
    return stream.getvalue()


def complete_reviews(payloads: list[bytes], reviews: list[dict[str, object]]) -> list[dict[str, object]]:
    assert len(payloads) == len(reviews)
    completed: list[dict[str, object]] = []
    for payload, review in zip(payloads, reviews):
        item = dict(review)
        item.setdefault("shot_type_reviewed", True)
        item.setdefault("sha256", hashlib.sha256(payload).hexdigest())
        completed.append(item)
    return completed


def raw_batch_data(payloads: list[bytes], reviews: list[dict[str, object]], token: str):
    return {
        "review_json": json.dumps(reviews),
        "csrf_token": token,
        "images": [
            (io.BytesIO(payload), f"client-last-modified-2099-{index}.jpg")
            for index, payload in enumerate(payloads)
        ],
    }


def batch_data(payloads: list[bytes], reviews: list[dict[str, object]], token: str):
    return raw_batch_data(payloads, complete_reviews(payloads, reviews), token)


def test_batch_inspection_is_server_side_and_picker_drop_share_review_flow(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    client = app.test_client()
    login(client, "editor")
    batch_page = client.get(f"/patients/{patient_id}/captures/batch")
    assert batch_page.status_code == 200
    body = batch_page.get_data(as_text=True)
    assert 'name="images"' in body and "multiple" in body
    assert "dataTransfer.files" in body
    assert "/captures/batch/inspect" in body

    payload = jpeg("red", exif_date="2024:03:04 05:06:07")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    response = client.post(
        f"/patients/{patient_id}/captures/batch/inspect",
        data={"images": [(io.BytesIO(payload), "not-a-date.jpg")], "csrf_token": token},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.json["items"][0]["suggested_capture_date"] == "2024-03-04"
    assert response.json["items"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    media = Path(app.config["MEDIA_ROOT"])
    assert list((media / "originals").iterdir()) == []
    assert list((media / "previews").iterdir()) == []


def test_incomplete_review_commits_no_rows_or_media(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    response = client.post(
        f"/patients/{patient_id}/captures/batch/commit",
        data=batch_data(
            [jpeg("red"), png("blue")],
            [
                {
                    "capture_date": "2025-01-01",
                    "capture_date_confirmed": True,
                    "shot_type_name": "Anterior",
                    "shot_type_reviewed": True,
                },
                {
                    "capture_date": "2025-01-02",
                    "capture_date_confirmed": False,
                    "shot_type_name": "Anterior",
                    "shot_type_reviewed": True,
                },
            ],
            token,
        ),
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 0
        assert db.session.scalar(select(func.count(AuditEvent.id))) == 0
    media = Path(app.config["MEDIA_ROOT"])
    assert not (media / "originals").exists() or list((media / "originals").iterdir()) == []
    assert not (media / "previews").exists() or list((media / "previews").iterdir()) == []


def test_fully_reviewed_batch_creates_one_capture_and_audit_per_image(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payloads = [jpeg("red", exif_date="2024:03:04 05:06:07"), png("blue")]
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    inspected = client.post(
        f"/patients/{patient_id}/captures/batch/inspect",
        data={
            "images": [(io.BytesIO(payload), f"inspect-{index}.jpg") for index, payload in enumerate(payloads)],
            "csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert inspected.status_code == 200
    response = client.post(
        f"/patients/{patient_id}/captures/batch/commit",
        data=batch_data(
            payloads,
            [
                {
                    "capture_date": "2025-06-07",
                    "capture_date_confirmed": True,
                    "shot_type_name": "Anterior",
                    "shot_type_reviewed": True,
                },
                {
                    "capture_date": "2025-06-08",
                    "capture_date_confirmed": True,
                    "shot_type_name": "Anterior",
                    "shot_type_reviewed": True,
                },
            ],
            token,
        ),
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.json["count"] == 2
    with app.app_context():
        captures = list(db.session.scalars(select(Capture).order_by(Capture.id)))
        assert [capture.capture_date.isoformat() for capture in captures] == ["2025-06-07", "2025-06-08"]
        assert db.session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "capture.create")) == 2
        assert all(event.details == {} for event in db.session.scalars(select(AuditEvent)))
        keys = [capture.storage_key for capture in captures]
    storage = ManagedStorage(app.config["MEDIA_ROOT"])
    assert [storage.resolve(key).read_bytes() for key in keys] == payloads
    assert [hashlib.sha256(storage.resolve(key).read_bytes()).hexdigest() for key in keys] == [
        hashlib.sha256(payload).hexdigest() for payload in payloads
    ]
    assert list((Path(app.config["MEDIA_ROOT"]) / "quarantine").glob(".pending-*")) == []


@pytest.mark.parametrize("reviewed_value", [None, False, "true"])
def test_shot_type_review_flag_is_explicit_boolean_true(app, reviewed_value):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payload = jpeg("red")
    review = {
        "capture_date": "2025-01-01",
        "capture_date_confirmed": True,
        "shot_type_name": "Anterior",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    if reviewed_value is not None:
        review["shot_type_reviewed"] = reviewed_value
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    response = client.post(
        f"/patients/{patient_id}/captures/batch/commit",
        data=raw_batch_data([payload], [review], token),
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 0


def test_batch_content_hash_binding_rejects_tampered_extra_and_duplicate_reviews(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payloads = [jpeg("red"), png("blue")]
    reviews = complete_reviews(
        payloads,
        [
            {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
            {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
        ],
    )
    missing_hash = dict(reviews[0])
    missing_hash.pop("sha256")
    cases = [
        ([jpeg("green"), payloads[1]], reviews),
        (payloads, [missing_hash, reviews[1]]),
        (payloads, [{**reviews[0], "sha256": "0" * 64}, reviews[1]]),
        (payloads, [reviews[0], {**reviews[1], "sha256": reviews[0]["sha256"]}]),
    ]
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    for case_payloads, case_reviews in cases:
        response = client.post(
            f"/patients/{patient_id}/captures/batch/commit",
            data=raw_batch_data(case_payloads, case_reviews, token),
            content_type="multipart/form-data",
        )
        assert response.status_code == 400, response.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 0


def test_batch_hash_binding_allows_reversed_file_order_without_swapping_review_metadata(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payloads = [jpeg("red"), png("blue")]
    reviews = complete_reviews(
        payloads,
        [
            {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
            {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
        ],
    )
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    response = client.post(
        f"/patients/{patient_id}/captures/batch/commit",
        data=raw_batch_data([payloads[1], payloads[0]], reviews, token),
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    expected_dates = {
        hashlib.sha256(payloads[0]).hexdigest(): "2025-01-01",
        hashlib.sha256(payloads[1]).hexdigest(): "2025-01-02",
    }
    with app.app_context():
        captures = list(db.session.scalars(select(Capture)))
        assert {capture.sha256: capture.capture_date.isoformat() for capture in captures} == expected_dates


def test_batch_aggregate_limits_stop_reading_and_decoding_after_failing_item(app, monkeypatch):
    reads: list[bytes] = []
    inspections: list[bytes] = []

    def payload(upload: object) -> bytes:
        reads.append(upload)
        return upload  # type: ignore[return-value]

    class FakeStorage:
        def inspect(self, value: bytes) -> ImageInspection:
            inspections.append(value)
            return ImageInspection(
                format="JPEG",
                width=2,
                height=2,
                byte_count=len(value),
                sha256=hashlib.sha256(value).hexdigest(),
                suggested_capture_date=None,
                preview_bytes=b"preview",
            )

    monkeypatch.setattr(captures_module, "_payload", payload)
    with app.app_context():
        app.config["CAPTURE_BATCH_MAX_BYTES"] = 3
        with pytest.raises(BatchLimitError):
            captures_module._batch_inspect_uploads([b"aa", b"bb", b"cc"], storage=FakeStorage())
    assert reads == [b"aa", b"bb"]
    assert inspections == [b"aa"]

    reads.clear()
    inspections.clear()
    with app.app_context():
        app.config["CAPTURE_BATCH_MAX_BYTES"] = 100
        app.config["CAPTURE_BATCH_MAX_PIXELS"] = 4
        with pytest.raises(BatchLimitError):
            captures_module._batch_inspect_uploads([b"aa", b"bb", b"cc"], storage=FakeStorage())
    assert reads == [b"aa", b"bb"]
    assert inspections == [b"aa", b"bb"]


@pytest.mark.parametrize("bad_payload", [b"not an image", animated_webp()])
def test_invalid_item_rejects_the_entire_batch(app, bad_payload: bytes):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payloads = [jpeg("red"), bad_payload]
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    response = client.post(
        f"/patients/{patient_id}/captures/batch/commit",
        data=batch_data(
            payloads,
            [
                {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
                {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
            ],
            token,
        ),
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 0
    media = Path(app.config["MEDIA_ROOT"])
    assert list((media / "originals").iterdir()) == []
    assert list((media / "previews").iterdir()) == []


def test_duplicate_rejects_batch_without_removing_preexisting_capture(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    shot_type_id = fixture_shot_type(app, editor_id)
    existing_payload = jpeg("red")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        shot_type = db.session.get(ShotType, shot_type_id)
        assert actor is not None and patient is not None and shot_type is not None
        existing = create_capture(
            actor=actor,
            patient=patient,
            upload=existing_payload,
            capture_date="2024-01-01",
            capture_date_confirmed=True,
            shot_type=shot_type,
            original_filename="existing.jpg",
        )
        existing_key = existing.storage_key
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    response = client.post(
        f"/patients/{patient_id}/captures/batch/commit",
        data=batch_data(
            [existing_payload, png("blue")],
            [
                {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
                {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
            ],
            token,
        ),
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 1
    storage = ManagedStorage(app.config["MEDIA_ROOT"])
    assert storage.resolve(existing_key).read_bytes() == existing_payload
    assert len(list((Path(app.config["MEDIA_ROOT"]) / "originals").iterdir())) == 1


def test_aggregate_byte_limit_rejects_the_entire_batch(app):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payloads = [jpeg("red"), png("blue")]
    app.config["CAPTURE_BATCH_MAX_BYTES"] = len(payloads[0])
    client = app.test_client()
    login(client, "editor")
    token = csrf_token(client, f"/patients/{patient_id}/captures/batch")
    response = client.post(
        f"/patients/{patient_id}/captures/batch/commit",
        data=batch_data(
            payloads,
            [
                {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
                {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
            ],
            token,
        ),
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 0
    media = Path(app.config["MEDIA_ROOT"])
    assert not (media / "originals").exists() or list((media / "originals").iterdir()) == []
    assert not (media / "previews").exists() or list((media / "previews").iterdir()) == []


def test_batch_requires_consent_and_both_mutations_are_csrf_protected(app):
    editor_id = add_user(app, "editor")
    add_user(app, "viewer", "viewer")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payload = jpeg("red")
    client = app.test_client()
    login(client, "editor")
    inspect_path = f"/patients/{patient_id}/captures/batch/inspect"
    commit_path = f"/patients/{patient_id}/captures/batch/commit"
    assert client.post(inspect_path, data={"images": (io.BytesIO(payload), "one.jpg")}).status_code == 400
    assert client.post(commit_path, data={}).status_code == 400

    with app.app_context():
        actor = db.session.get(User, editor_id)
        assert actor is not None
        no_consent = Patient(id=999, consent_confirmed_at=None, archived_at=None)
        with pytest.raises(ConsentRequired):
            create_batch_captures(
                actor=actor,
                patient=no_consent,
                uploads=[payload],
                reviews=complete_reviews(
                    [payload],
                    [
                        {
                            "capture_date": "2025-01-01",
                            "capture_date_confirmed": True,
                            "shot_type_name": "Anterior",
                        }
                    ],
                ),
            )

    viewer = app.test_client()
    login(viewer, "viewer")
    assert viewer.get(f"/patients/{patient_id}/captures/batch").status_code == 403
    assert viewer.post(inspect_path, data={}).status_code == 403
    assert viewer.post(commit_path, data={}).status_code == 403


def test_transaction_failure_cleans_every_new_media_and_row(app, monkeypatch: pytest.MonkeyPatch):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payloads = [jpeg("red"), png("blue")]
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None

        def fail_commit() -> None:
            raise RuntimeError("simulated transaction failure")

        monkeypatch.setattr(db.session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated transaction failure"):
            create_batch_captures(
                actor=actor,
                patient=patient,
                uploads=payloads,
                reviews=complete_reviews(
                    payloads,
                    [
                        {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
                        {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
                    ],
                ),
            )
        assert db.session.scalar(select(func.count(Capture.id))) == 0
    media = Path(app.config["MEDIA_ROOT"])
    assert list((media / "originals").iterdir()) == []
    assert list((media / "previews").iterdir()) == []
    assert list((media / "quarantine").iterdir()) == []


def test_commit_acknowledgement_ambiguity_reconciles_all_batch_media(app, monkeypatch: pytest.MonkeyPatch):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    fixture_shot_type(app, editor_id)
    payloads = [jpeg("red"), png("blue")]
    with app.app_context():
        actor = db.session.get(User, editor_id)
        patient = db.session.get(Patient, patient_id)
        assert actor is not None and patient is not None
        original_commit = db.session.commit

        def commit_then_raise() -> None:
            original_commit()
            raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(db.session, "commit", commit_then_raise)
        captures = create_batch_captures(
            actor=actor,
            patient=patient,
            uploads=payloads,
            reviews=complete_reviews(
                payloads,
                [
                    {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
                    {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_name": "Anterior"},
                ],
            ),
        )
        assert len(captures) == 2
        assert db.session.scalar(select(func.count(Capture.id))) == 2
    media = Path(app.config["MEDIA_ROOT"])
    assert len(list((media / "originals").iterdir())) == 2
    assert len(list((media / "previews").iterdir())) == 2
    assert list((media / "quarantine").glob(".pending-*")) == []


def test_batch_shot_type_locks_are_deterministic_under_postgresql_concurrency(app, monkeypatch):
    editor_id = add_user(app, "editor")
    patient_id = fixture_patient(app, editor_id)
    low_id = fixture_shot_type(app, editor_id, "Anterior")
    high_id = fixture_shot_type(app, editor_id, "Lateral")
    first_payloads = [jpeg("red"), png("blue")]
    second_payloads = [jpeg("green"), png("yellow")]
    first_reviews = complete_reviews(
        first_payloads,
        [
            {"capture_date": "2025-01-01", "capture_date_confirmed": True, "shot_type_id": low_id},
            {"capture_date": "2025-01-02", "capture_date_confirmed": True, "shot_type_id": high_id},
        ],
    )
    second_reviews = complete_reviews(
        second_payloads,
        [
            {"capture_date": "2025-02-01", "capture_date_confirmed": True, "shot_type_id": high_id},
            {"capture_date": "2025-02-02", "capture_date_confirmed": True, "shot_type_id": low_id},
        ],
    )
    barrier = threading.Barrier(2)
    original_resolve = captures_module._batch_shot_type_resolutions

    def synchronized_resolve(**kwargs):
        barrier.wait(timeout=10)
        return original_resolve(**kwargs)

    monkeypatch.setattr(captures_module, "_batch_shot_type_resolutions", synchronized_resolve)
    errors: list[BaseException] = []
    counts: list[int] = []

    def worker(payloads, reviews):
        with app.app_context():
            try:
                actor = db.session.get(User, editor_id)
                patient = db.session.get(Patient, patient_id)
                assert actor is not None and patient is not None
                counts.append(
                    len(
                        create_batch_captures(
                            actor=actor,
                            patient=patient,
                            uploads=payloads,
                            reviews=reviews,
                        )
                    )
                )
            except BaseException as exc:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(first_payloads, first_reviews)),
        threading.Thread(target=worker, args=(second_payloads, second_reviews)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "batch Shot Type lock ordering deadlocked"
    assert errors == []
    assert counts == [2, 2]
    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 4
