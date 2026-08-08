from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app import captures
from app.captures import ConsentRequired, create_capture
from app.db import db, normalize_database_url
from app.models import AuditEvent, Capture, Patient, ShotType, User
from app import storage as storage_module
from app.storage import ManagedStorage, StorageError


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
        "SECRET_KEY": "slice-2-test-secret",
        "SESSION_COOKIE_SECURE": False,
    }


@pytest.fixture(scope="session")
def migrated_test_database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 2 PostgreSQL tests")

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
            text("TRUNCATE audit_events, captures, shot_types, patients, users RESTART IDENTITY CASCADE")
        )
        db.session.commit()
    yield application
    with application.app_context():
        db.session.rollback()
        db.session.execute(
            text("TRUNCATE audit_events, captures, shot_types, patients, users RESTART IDENTITY CASCADE")
        )
        db.session.commit()


def add_user(application, username: str, role: str) -> int:
    with application.app_context():
        user = User(username=username, display_name=username.title(), role=role, active=True)
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


def create_patient(application, client) -> int:
    token = csrf_token(client, "/patients/new")
    response = client.post(
        "/patients/new",
        data={
            "patient_id": "CAP-0001",
            "name": "Capture Fixture",
            "birth_year": "1990",
            "consent_confirmed": "y",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    with application.app_context():
        patient = db.session.scalar(select(Patient).where(Patient.patient_id == "CAP-0001"))
        assert patient is not None
        return patient.id


def oriented_jpeg() -> bytes:
    image = Image.new("RGB", (8, 4), "#447799")
    exif = image.getexif()
    exif[274] = 6
    exif[36867] = "2024:03:04 05:06:07"
    stream = io.BytesIO()
    image.save(stream, format="JPEG", exif=exif.tobytes())
    return stream.getvalue()


def metadata_jpeg() -> bytes:
    image = Image.new("RGB", (8, 4), "#447799")
    exif = image.getexif()
    exif[36867] = "2024:03:04 05:06:07"
    stream = io.BytesIO()
    image.save(
        stream,
        format="JPEG",
        exif=exif.tobytes(),
        comment=b"sensitive comment",
        icc_profile=b"sensitive ICC profile",
        xmp=b"sensitive XMP packet",
    )
    return stream.getvalue()


def test_media_root_is_resolved_before_static_containment_and_permissions(tmp_path: Path) -> None:
    base = {
        "DATABASE_URL": "postgresql+psycopg://localhost/before_after",
        "SECRET_KEY": "test-secret",
        "TESTING": True,
        "APP_ENV": "test",
    }
    probe = create_app({**base, "MEDIA_ROOT": str(tmp_path / "probe")})
    static_root = Path(probe.static_folder).resolve()
    static_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match="static web root"):
        create_app({**base, "MEDIA_ROOT": str(static_root / "media")})

    alias = tmp_path / "static-alias"
    alias.symlink_to(static_root, target_is_directory=True)
    with pytest.raises(RuntimeError, match="static web root"):
        create_app({**base, "MEDIA_ROOT": str(alias / "media")})

    with pytest.raises(RuntimeError, match="lexical traversal"):
        create_app({**base, "MEDIA_ROOT": str(tmp_path / "outside" / ".." / "media")})

    public = tmp_path / "public-media"
    public.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="private"):
        create_app({**base, "MEDIA_ROOT": str(public)})


def test_preview_removes_all_metadata_but_preserves_pixels_and_mode(tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "media")
    inspection = storage.inspect(metadata_jpeg())

    with Image.open(io.BytesIO(inspection.preview_bytes)) as preview:
        preview.load()
        assert preview.mode == "RGB"
        assert preview.size == (8, 4)
        assert preview.getexif() == {}
        assert not any(key in preview.info for key in ("comment", "xmp", "icc_profile"))

    assert not any(
        marker in inspection.preview_bytes
        for marker in (b"sensitive comment", b"sensitive ICC profile", b"sensitive XMP packet")
    )


@pytest.mark.skipif(not storage_module._POSIX_DIRFD, reason="POSIX dirfd primitives unavailable")
def test_managed_storage_rejects_parent_component_symlink(tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "media")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    originals = Path(storage.root) / "originals"
    originals.rename(tmp_path / "originals-real")
    originals.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageError):
        storage.open_read("originals/missing.jpg")


def test_atomic_store_reports_directory_fsync_failure_and_cleans(tmp_path: Path, monkeypatch) -> None:
    storage = ManagedStorage(tmp_path / "media")
    payload = oriented_jpeg()
    inspection = storage.inspect(payload)

    def fail_fsync(_fd: int) -> None:
        raise StorageError("directory fsync failed")

    monkeypatch.setattr(storage, "_fsync_directory", fail_fsync)
    with pytest.raises(StorageError, match="fsync"):
        storage.store(payload, inspection)
    assert list((Path(storage.root) / "originals").iterdir()) == []
    assert list((Path(storage.root) / "previews").iterdir()) == []


def test_capture_workflow_confirms_override_preserves_original_and_survives_restart(
    app, tmp_path: Path, migrated_test_database: str
) -> None:
    editor_id = add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "/login", "editor")
    patient_id = create_patient(app, client)
    with app.app_context():
        shot_type = ShotType(name="Anterior", state="canonical", created_by_id=editor_id)
        db.session.add(shot_type)
        db.session.commit()

    payload = oriented_jpeg()
    token = csrf_token(client, f"/patients/{patient_id}/captures/new")
    response = client.post(
        f"/patients/{patient_id}/captures/new",
        data={
            "image": (io.BytesIO(payload), "../../original-with-exif.jpg"),
            "capture_date": "2025-02-03",
            "capture_date_confirmed": "y",
            "shot_type_name": "Anterior",
            "csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302, response.get_data(as_text=True)

    with app.app_context():
        capture = db.session.scalar(select(Capture))
        assert capture is not None
        capture_id = capture.id
        assert capture.capture_date == date(2025, 2, 3)
        assert capture.sha256 == hashlib.sha256(payload).hexdigest()
        assert capture.original_filename == "original-with-exif.jpg"
        assert capture.storage_key.startswith("originals/")
        assert ".." not in capture.storage_key
        original_key = capture.storage_key
        event = db.session.scalar(
            select(AuditEvent).where(AuditEvent.action == "capture.create")
        )
        assert event is not None
        assert event.actor_id == editor_id

    storage = ManagedStorage(tmp_path / "media")
    assert storage.resolve(original_key).read_bytes() == payload
    assert len(list((tmp_path / "media" / "originals").iterdir())) == 1
    preview_response = client.get(f"/captures/{capture_id}/preview")
    assert preview_response.status_code == 200
    assert preview_response.headers["Cache-Control"] == "no-store"
    with Image.open(io.BytesIO(preview_response.data)) as preview:
        preview.load()
        assert max(preview.size) <= 1600
        assert len(preview.getexif()) == 0
    original_response = client.get(f"/captures/{capture_id}/original")
    assert original_response.status_code == 200
    assert original_response.headers["Cache-Control"] == "no-store"
    assert original_response.data == payload

    restarted = create_app(app_config(tmp_path, migrated_test_database))
    restarted_client = restarted.test_client()
    login(restarted_client, "/login", "editor")
    library = restarted_client.get(f"/patients/{patient_id}/captures")
    assert library.status_code == 200
    assert "Anterior" in library.get_data(as_text=True)
    assert restarted_client.get(f"/captures/{capture_id}/preview").status_code == 200


def test_duplicate_has_one_original_and_viewer_cannot_mutate(app) -> None:
    add_user(app, "editor", "editor")
    add_user(app, "viewer", "viewer")
    client = app.test_client()
    login(client, "/login", "editor")
    patient_id = create_patient(app, client)
    with app.app_context():
        editor = db.session.scalar(select(User).where(User.username == "editor"))
        assert editor is not None
        db.session.add(ShotType(name="Lateral", state="canonical", created_by_id=editor.id))
        db.session.commit()

    payload = oriented_jpeg()
    path = f"/patients/{patient_id}/captures/new"
    token = csrf_token(client, path)
    fields = {
        "capture_date": "2025-02-03",
        "capture_date_confirmed": "y",
        "shot_type_name": "Lateral",
        "csrf_token": token,
    }
    first = dict(fields, image=(io.BytesIO(payload), "first.jpg"))
    assert client.post(path, data=first, content_type="multipart/form-data").status_code == 302
    second = dict(fields, image=(io.BytesIO(payload), "second.jpg"))
    assert client.post(path, data=second, content_type="multipart/form-data").status_code == 302

    with app.app_context():
        assert db.session.scalar(select(func.count(Capture.id))) == 1
    assert len(list((Path(app.config["MEDIA_ROOT"]) / "originals").iterdir())) == 1

    client.post("/logout", data={"csrf_token": csrf_token(client, "/patients")})
    login(client, "/login", "viewer")
    assert client.get(path).status_code == 403
    assert client.post(path, data={}).status_code == 403


def test_failed_audit_transaction_cleans_media_and_capture_row(app, monkeypatch: pytest.MonkeyPatch) -> None:
    editor_id = add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "/login", "editor")
    patient_id = create_patient(app, client)
    with app.app_context():
        patient = db.session.get(Patient, patient_id)
        assert patient is not None
        shot_type = ShotType(name="Oblique", state="canonical", created_by_id=editor_id)
        db.session.add(shot_type)
        db.session.flush()
        actor = db.session.get(User, editor_id)
        assert actor is not None

        def fail_audit(**_kwargs: object) -> None:
            raise RuntimeError("audit failed")

        monkeypatch.setattr(captures, "append_audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit failed"):
            create_capture(
                actor=actor,
                patient=patient,
                upload=oriented_jpeg(),
                capture_date="2025-02-03",
                capture_date_confirmed=True,
                shot_type=shot_type,
                original_filename="failed.jpg",
            )
        assert db.session.scalar(select(func.count(Capture.id))) == 0
        assert db.session.scalar(select(func.count(AuditEvent.id))) == 1

    media_root = Path(app.config["MEDIA_ROOT"])
    assert list((media_root / "originals").iterdir()) == []
    assert list((media_root / "previews").iterdir()) == []


def test_commit_then_raise_reconciles_and_preserves_committed_media(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor_id = add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "/login", "editor")
    patient_id = create_patient(app, client)

    with app.app_context():
        patient = db.session.get(Patient, patient_id)
        actor = db.session.get(User, editor_id)
        assert patient is not None and actor is not None
        shot_type = ShotType(name="Commit acknowledgement", created_by_id=editor_id)
        db.session.add(shot_type)
        db.session.flush()
        original_commit = db.session.commit

        def commit_then_raise() -> None:
            original_commit()
            raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(db.session, "commit", commit_then_raise)
        capture = create_capture(
            actor=actor,
            patient=patient,
            upload=oriented_jpeg(),
            capture_date="2025-02-03",
            capture_date_confirmed=True,
            shot_type=shot_type,
            original_filename="commit-then-raise.jpg",
        )
        assert capture.id is not None
        assert db.session.scalar(select(Capture).where(Capture.id == capture.id)) is not None

    storage = ManagedStorage(app.config["MEDIA_ROOT"])
    assert storage.resolve(capture.storage_key).is_file()
    assert len(list((Path(app.config["MEDIA_ROOT"]) / "originals").iterdir())) == 1


def test_inconclusive_commit_reconciliation_preserves_pending_media(
    app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor_id = add_user(app, "editor", "editor")
    client = app.test_client()
    login(client, "/login", "editor")
    patient_id = create_patient(app, client)

    with app.app_context():
        patient = db.session.get(Patient, patient_id)
        actor = db.session.get(User, editor_id)
        assert patient is not None and actor is not None
        shot_type = ShotType(name="Inconclusive reconciliation", created_by_id=editor_id)
        db.session.add(shot_type)
        db.session.flush()
        original_commit = db.session.commit

        def commit_then_raise() -> None:
            original_commit()
            raise RuntimeError("commit acknowledgement lost")

        def reconciliation_unavailable(*_args, **_kwargs):
            raise RuntimeError("reconciliation unavailable")

        monkeypatch.setattr(db.session, "commit", commit_then_raise)
        monkeypatch.setattr(captures, "_reconcile_capture", reconciliation_unavailable)
        storage = ManagedStorage(tmp_path / "media")
        with pytest.raises(captures.CaptureReconciliationError, match="could not confirm"):
            create_capture(
                actor=actor,
                patient=patient,
                upload=oriented_jpeg(),
                capture_date="2025-02-03",
                capture_date_confirmed=True,
                shot_type=shot_type,
                original_filename="inconclusive.jpg",
                storage=storage,
            )

        capture = db.session.scalar(select(Capture))
        assert capture is not None
        assert len(list((storage.root / "originals").iterdir())) == 1
        assert len(list((storage.root / "previews").iterdir())) == 1
        pending = list((storage.root / "quarantine").glob(".pending-*"))
        assert len(pending) == 1

        assert storage.reconcile({capture.storage_key}, grace_seconds=0) == []
        assert not pending[0].exists()
        assert storage.resolve(capture.storage_key).is_file()


def test_filename_persists_without_control_or_bidi_characters() -> None:
    filename = captures._filename(
        None,
        "../safe\n-name\u202e.gpj",
    )
    assert filename == "safe-name.gpj"
    assert all(unicodedata.category(char) not in {"Cc", "Cf"} for char in filename)


def test_shot_type_constraints_cover_case_and_state_target_invariants(app) -> None:
    editor_id = add_user(app, "editor", "editor")
    with app.app_context():
        canonical = ShotType(name="Anterior", state="canonical", created_by_id=editor_id)
        db.session.add(canonical)
        db.session.commit()

        db.session.add(ShotType(name="anterior", state="canonical", created_by_id=editor_id))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()

        for state, target_id in (("merged", None), ("canonical", canonical.id)):
            db.session.add(
                ShotType(
                    name=f"invalid-{state}-{target_id}",
                    state=state,
                    canonical_target_id=target_id,
                    created_by_id=editor_id,
                )
            )
            with pytest.raises(IntegrityError):
                db.session.flush()
            db.session.rollback()


def test_consent_confirmation_and_storage_path_safety_fail_before_storage(app, tmp_path: Path) -> None:
    editor_id = add_user(app, "editor", "editor")
    with app.app_context():
        actor = db.session.get(User, editor_id)
        assert actor is not None
        no_consent = Patient(id=999, consent_confirmed_at=None, archived_at=None)
        with pytest.raises(ConsentRequired):
            create_capture(
                actor=actor,
                patient=no_consent,
                upload=oriented_jpeg(),
                capture_date="2025-02-03",
                capture_date_confirmed=True,
                shot_type_name="Proposal only",
                create_proposal=True,
            )

    storage = ManagedStorage(tmp_path / "safe-media")
    for key in ("../outside", "/tmp/outside", "originals/../outside", "previews\\escape"):
        with pytest.raises(StorageError):
            storage.resolve(key)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do not read")
    (tmp_path / "safe-media" / "originals" / "link").symlink_to(outside)
    with pytest.raises(StorageError):
        storage.open_read("originals/link")
    assert outside.read_bytes() == b"do not read"
    assert list((tmp_path / "safe-media" / "originals").iterdir()) == [
        tmp_path / "safe-media" / "originals" / "link"
    ]


def test_managed_storage_accepts_all_policy_formats(tmp_path: Path) -> None:
    storage = ManagedStorage(tmp_path / "media")
    for image_format in ("BMP", "JPEG", "PNG", "TIFF", "WEBP"):
        stream = io.BytesIO()
        Image.new("RGB", (8, 4), "#447799").save(stream, format=image_format)
        inspection = storage.inspect(stream.getvalue())
        stored = storage.store(stream.getvalue(), inspection)
        assert storage.resolve(stored.original_key).read_bytes() == stream.getvalue()
        storage.cleanup(stored.original_key, stored.preview_key)
