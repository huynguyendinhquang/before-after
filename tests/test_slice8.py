from __future__ import annotations

import hashlib
import json
import grp
import os
import pwd
from pathlib import Path
import stat
import subprocess
import shutil
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
import uuid

import pytest

from ops.backup import OpsError, create_backup, postgres_preflight, prune_generations
from ops.restore_check import (
    _drop_owned_database,
    _owner_marker_path,
    cleanup_stale_restore_staging,
    assert_isolated_targets,
    cleanup_isolated_target,
    provision_isolated_target,
    select_generation,
    stage_verified_generation,
    verify_generation,
)


def test_production_proxy_configuration_trusts_only_configured_hop(tmp_path: Path) -> None:
    from flask import request
    from werkzeug.middleware.proxy_fix import ProxyFix
    from app import create_app

    application = create_app(
        {
            "TESTING": True,
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql+psycopg://user:password@127.0.0.1/clinic",
            "MEDIA_ROOT": str(tmp_path / "media"),
            "SECRET_KEY": "x" * 64,
            "TRUSTED_PROXY_COUNT": 1,
            "SESSION_COOKIE_SECURE": True,
        }
    )

    @application.get("/_slice8_proxy_probe")
    def proxy_probe():
        return {"secure": request.is_secure, "host": request.host}

    assert isinstance(application.wsgi_app, ProxyFix)
    response = application.test_client().get(
        "/_slice8_proxy_probe",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "clinic.lan"},
    )
    assert response.json == {"secure": True, "host": "clinic.lan"}
    assert application.config["SESSION_COOKIE_SECURE"] is True


def private_media_root(path: Path) -> Path:
    path.mkdir(mode=0o2750)
    for name in ("originals", "previews", "derivatives", "quarantine"):
        (path / name).mkdir(mode=0o2750)
    return path


def fake_pg_dump(path: Path) -> Path:
    script = path / "pg_dump-fake"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "args = sys.argv\n"
        "output = pathlib.Path(args[args.index('--file') + 1])\n"
        "output.write_bytes(b'PGDMP synthetic dump')\n",
        encoding="ascii",
    )
    script.chmod(0o700)
    return script


def fake_pg_restore(path: Path) -> Path:
    script = path / "pg_restore-fake"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "dump = pathlib.Path(sys.argv[-1])\n"
        "if not dump.read_bytes().startswith(b'PGDMP'):\n"
        "    raise SystemExit(1)\n",
        encoding="ascii",
    )
    script.chmod(0o700)
    return script


def test_managed_storage_uses_media_group_modes(tmp_path: Path) -> None:
    from app.storage import ManagedStorage

    storage = ManagedStorage(tmp_path / "media")
    assert stat.S_IMODE(storage.root.stat().st_mode) == 0o2750
    for name in ("originals", "previews", "derivatives", "quarantine"):
        assert stat.S_IMODE((storage.root / name).stat().st_mode) == 0o2750
    stored = storage.store_derivative(b"synthetic", "png")
    assert stat.S_IMODE(storage.resolve(stored.storage_key).stat().st_mode) == 0o640
    storage.finalize(stored)


def test_permission_repair_keeps_markers_private_and_reconcile_works(tmp_path: Path) -> None:
    from app.storage import ManagedStorage, apply_media_permissions

    storage = ManagedStorage(tmp_path / "media")
    pending = storage.root / "quarantine" / (".pending-" + "a" * 32)
    pending.write_bytes(b"marker")
    upload_temp = storage.root / "originals" / ".upload-crash.tmp"
    upload_temp.write_bytes(b"partial")
    restore_marker = storage.root / "quarantine" / ".restore-crash"
    restore_marker.write_bytes(b"failed")
    with storage.reconciliation_lock():
        lock = storage.root / ".reconcile.lock"
        assert lock.is_file()
    clinical = storage.root / "originals" / "clinical.bin"
    clinical.write_bytes(b"clinical")
    for entry in storage.root.rglob("*"):
        os.chmod(entry, 0o640 if entry.is_file() else 0o750)
    apply_media_permissions(storage.root)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert stat.S_IMODE(upload_temp.stat().st_mode) == 0o600
    assert stat.S_IMODE(restore_marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(clinical.stat().st_mode) == 0o640
    storage.reconcile(set(), grace_seconds=3600, now=clinical.stat().st_mtime + 3600)


def test_restore_owner_markers_hash_nested_target_identity(tmp_path: Path) -> None:
    from ops.restore_check import _read_owner_marker, _write_owner_marker

    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    first = parent / "one" / "media"
    second = parent / "two" / "media"
    assert _owner_marker_path(parent, first) != _owner_marker_path(parent, second)
    marker = "before-after-restore-" + "a" * 32
    _write_owner_marker(parent, first, marker)
    _write_owner_marker(parent, second, marker)
    assert _read_owner_marker(parent, first) == marker
    assert _read_owner_marker(parent, second) == marker


def test_postgres_preflight_rejects_fake_major_mismatch(tmp_path: Path) -> None:
    def client(name: str, major: int, server: str | None = None) -> Path:
        script = tmp_path / name
        version = f"{name} (PostgreSQL) {major}.4"
        output = server if server is not None else version
        script.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = \"--version\" ]; then printf '%s\\n'; else printf '%s\\n'; fi\n" % (version, output),
            encoding="ascii",
        )
        script.chmod(0o700)
        return script

    dump = client("pg_dump", 17)
    restore = client("pg_restore", 17)
    psql = client("psql", 17, "160004")
    with pytest.raises(OpsError, match="does not match server"):
        postgres_preflight(
            "postgresql://user:password@127.0.0.1/clinic",
            pg_dump=str(dump),
            pg_restore=str(restore),
            psql=str(psql),
        )


@pytest.mark.parametrize("unmanaged", ["root-clinical.bin", "unknown/nested-clinical.bin"])
def test_backup_fails_closed_on_unmanaged_media_content(tmp_path: Path, unmanaged: str) -> None:
    media = private_media_root(tmp_path / "media")
    path = media / unmanaged
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    path.write_bytes(b"clinical")
    os.chmod(path, 0o640)
    backup_root = tmp_path / "backup"
    backup_root.mkdir(mode=0o700)

    with pytest.raises(OpsError, match="unmanaged content"):
        create_backup(
            media_root=media,
            backup_root=backup_root,
            database_url="postgresql://user:password@127.0.0.1/clinic",
            pg_dump=str(fake_pg_dump(tmp_path)),
            pg_restore=str(fake_pg_restore(tmp_path)),
            _storage_policy=lambda _media, _backup: None,
        )
    assert not list(backup_root.glob(".staging-*"))
    assert not list(backup_root.glob("[0-9]*T*"))


def test_backup_fails_before_staging_when_recovery_marker_is_present(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    (media / "originals" / "clinical.bin").write_bytes(b"clinical")
    os.chmod(media / "originals" / "clinical.bin", 0o640)
    marker = media / "quarantine" / (".pending-" + "a" * 32)
    marker.write_bytes(b"originals/a.jpg\npreviews/a.jpg\n")
    os.chmod(marker, 0o600)
    backup_root = tmp_path / "backup"
    backup_root.mkdir(mode=0o700)

    with pytest.raises(OpsError, match="recovery required"):
        create_backup(
            media_root=media,
            backup_root=backup_root,
            database_url="postgresql://user:password@127.0.0.1/clinic",
            pg_dump=str(fake_pg_dump(tmp_path)),
            pg_restore=str(fake_pg_restore(tmp_path)),
            _storage_policy=lambda _media, _backup: None,
        )
    assert not list(backup_root.glob(".staging-*"))
    assert not list(backup_root.glob("[0-9]*T*"))


def test_mixed_v1_generation_is_ignored_and_retention_keeps_v2(tmp_path: Path) -> None:
    _media, backup, generation, _restore = _synthetic_backup(tmp_path)
    v1 = backup / ("20990101T010101Z-" + "a" * 32)
    shutil.copytree(generation, v1)
    manifest_path = v1 / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["generation"] = v1.name
    manifest["format_version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")
    os.chmod(manifest_path, 0o600)

    assert select_generation(backup).path == generation
    assert prune_generations(backup, retain=1) == []
    assert generation.exists()
    assert v1.exists()


def test_backup_staging_cleanup_failure_is_poisoned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.backup as backup_module

    media = private_media_root(tmp_path / "media")
    (media / "originals" / "clinical.bin").write_bytes(b"clinical")
    os.chmod(media / "originals" / "clinical.bin", 0o640)
    backup_root = tmp_path / "backup"
    backup_root.mkdir(mode=0o700)
    dump = tmp_path / "failing-pg-dump"
    dump.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
    dump.chmod(0o700)
    monkeypatch.setattr(
        backup_module.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("delete blocked")),
    )

    with pytest.raises(OpsError, match="clinical staging is poisoned"):
        create_backup(
            media_root=media,
            backup_root=backup_root,
            database_url="postgresql://user:password@127.0.0.1/clinic",
            pg_dump=str(dump),
            pg_restore=str(fake_pg_restore(tmp_path)),
            _storage_policy=lambda _media, _backup: None,
        )
    assert list(backup_root.glob(".staging-*"))


def test_provision_validates_media_before_database_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.restore_check as restore_module

    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    called = False

    def create_database(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("database creation must not run")

    monkeypatch.setattr(restore_module, "create_isolated_database", create_database)
    with pytest.raises(OpsError, match="inside the private restore parent"):
        provision_isolated_target(
            target_database_url="postgresql://restore@127.0.0.1/restore",
            target_media_root=tmp_path / "outside-media",
            restore_parent=parent,
            production_database_urls=("postgresql://production@127.0.0.1/clinic",),
        )
    assert not called


@pytest.mark.parametrize("database_comment", [None, "another-run"])
def test_cleanup_requires_exact_database_ownership_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_comment: str | None,
) -> None:
    import types
    import ops.restore_check as restore_module

    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    target = parent / "media"
    owner_file = _owner_marker_path(parent, target)
    owner_file.write_text("before-after-restore-" + "a" * 32 + "\n", encoding="ascii")
    owner_file.chmod(0o600)
    statements: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=None):
            statements.append(statement)

        def fetchone(self):
            return (database_comment,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(restore_module, "_connection_server_identity", lambda _value: ("server", 5432))
    monkeypatch.setattr(restore_module, "_connection_database_identity", lambda _value: ("server", 5432, "clinic"))
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()))

    with pytest.raises(OpsError, match="ownership marker does not match"):
        _drop_owned_database(
            target_database_url="postgresql://restore@127.0.0.1/restore",
            ownership_marker="before-after-restore-" + "a" * 32,
            restore_parent=parent,
            target_media_root=target,
            production_database_urls=("postgresql://production@127.0.0.1/clinic",),
            remove_media=False,
        )
    assert not any("DROP DATABASE" in statement or "COMMENT ON DATABASE" in statement for statement in statements)
@pytest.mark.host_probe
def test_backup_user_can_read_media_but_cannot_modify_or_delete(tmp_path: Path) -> None:
    if os.environ.get("FIXED_GATE_DAC_PROOF") == "1":
        proof = os.environ.get("DAC_PROOF_MARKER")
        assert proof and Path(proof).read_text(encoding="ascii") == "before-after.dac-proof.v1\n"
        return
    if os.geteuid() != 0:
        pytest.skip("effective backup-user check requires root")
    try:
        backup_user = pwd.getpwnam("before-after-backup")
        media_group = grp.getgrnam("before-after-media")
    except KeyError:
        pytest.skip("deployment backup user and media group are not installed")
    from app.storage import ManagedStorage

    storage = ManagedStorage(tmp_path / "media")
    stored = storage.store_derivative(b"synthetic", "png")
    path = storage.resolve(stored.storage_key)
    storage.finalize(stored)
    for directory in (storage.root, *(storage.root / name for name in ("originals", "previews", "derivatives", "quarantine"))):
        os.chown(directory, os.geteuid(), media_group.gr_gid)
    os.chown(path, os.geteuid(), media_group.gr_gid)

    def drop_privileges() -> None:
        os.setgroups([media_group.gr_gid])
        os.setgid(backup_user.pw_gid)
        os.setuid(backup_user.pw_uid)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
path = Path(__import__('sys').argv[1])
assert path.read_bytes() == b'synthetic'
try:
    path.write_bytes(b'changed')
except PermissionError:
    pass
else:
    raise SystemExit('backup user can modify media')
try:
    path.unlink()
except PermissionError:
    pass
else:
    raise SystemExit('backup user can delete media')
""",
            str(path),
        ],
        preexec_fn=drop_privileges,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_backup_publishes_private_atomic_generation_and_manifest(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    original = b"synthetic original"
    (media / "originals" / "capture.bin").write_bytes(original)
    # The generated media key is intentionally opaque; the manifest contains
    # no original filename or Patient fields.
    os.chmod(media / "originals" / "capture.bin", 0o640)
    (media / "previews" / "preview.jpg").write_bytes(b"preview")
    os.chmod(media / "previews" / "preview.jpg", 0o640)
    dump = fake_pg_dump(tmp_path)
    restore = fake_pg_restore(tmp_path)
    backup_root = tmp_path / "backup"
    backup_root.mkdir(mode=0o700)

    result = create_backup(
        media_root=media,
        backup_root=backup_root,
        database_url="postgresql+psycopg://user:password@127.0.0.1/clinic",
        pg_dump=str(dump),
        pg_restore=str(restore),
        _storage_policy=lambda _media, _backup: None,
    )

    assert result.path.name == result.generation
    assert result.path.parent == backup_root
    assert not list(backup_root.glob(".staging-*"))
    verified = verify_generation(result.path)
    manifest = json.loads((result.path / "manifest.json").read_text(encoding="ascii"))
    assert verified.name == result.generation
    assert manifest["complete"] is True
    assert "DATABASE_URL" not in json.dumps(manifest)
    assert "password" not in json.dumps(manifest)
    assert all(
        stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
        for path in result.path.rglob("*")
        if path.is_file() or path.is_dir()
    )
    files = {entry["path"]: entry for entry in manifest["files"]}
    assert files["database.dump"]["sha256"] == hashlib.sha256(
        (result.path / "database.dump").read_bytes()
    ).hexdigest()
    assert files["media/originals/capture.bin"]["sha256"] == hashlib.sha256(original).hexdigest()


def test_backup_requires_mounted_storage_and_only_internal_policy_can_be_injected(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    dump = fake_pg_dump(tmp_path)
    restore = fake_pg_restore(tmp_path)
    backup_root = tmp_path / "backup"
    backup_root.mkdir(mode=0o700)
    with pytest.raises(OpsError):
        create_backup(
            media_root=media,
            backup_root=backup_root,
            database_url="postgresql://user@127.0.0.1/clinic",
            pg_dump=str(dump),
            pg_restore=str(restore),
        )
    (tmp_path / "backup-2").mkdir(mode=0o700)
    result = create_backup(
        media_root=media,
        backup_root=tmp_path / "backup-2",
        database_url="postgresql://user@127.0.0.1/clinic",
        pg_dump=str(dump),
        pg_restore=str(restore),
        _storage_policy=lambda _media, _backup: None,
    )
    assert result.path.is_dir()


def test_corrupt_copy_and_partial_generation_are_rejected(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    (media / "originals" / "capture.jpg").write_bytes(b"original")
    os.chmod(media / "originals" / "capture.jpg", 0o640)
    dump = fake_pg_dump(tmp_path)
    restore = fake_pg_restore(tmp_path)
    backup_root = tmp_path / "backup"
    backup_root.mkdir(mode=0o700)
    result = create_backup(
        media_root=media,
        backup_root=backup_root,
        database_url="postgresql://user@127.0.0.1/clinic",
        pg_dump=str(dump),
        pg_restore=str(restore),
        _storage_policy=lambda _media, _backup: None,
    )
    copied = result.path / "media" / "originals" / "capture.jpg"
    copied.write_bytes(b"corrupted")
    os.chmod(copied, 0o600)
    with pytest.raises(OpsError, match="checksum"):
        verify_generation(result.path)

    partial = backup_root / ("20990101T010101Z-" + "a" * 32)
    partial.mkdir(mode=0o700)
    (partial / "database.dump").write_bytes(b"partial")
    os.chmod(partial / "database.dump", 0o600)
    with pytest.raises(OpsError):
        select_generation(backup_root)


def test_restore_target_guard_rejects_production_and_backup_paths(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    with pytest.raises(OpsError, match="production database"):
        assert_isolated_targets(
            target_database_url="postgresql+psycopg://user:other@127.0.0.1/clinic",
            target_media_root=tmp_path / "restore",
            backup_root=backup,
            generation_path=backup / "generation",
            production_database_urls=("postgresql+psycopg://user:password@127.0.0.1/clinic",),
            production_media_roots=(media,),
            _identity_resolver=lambda _value: ("same-server", 5432, "clinic"),
        )
    with pytest.raises(OpsError, match="production media"):
        assert_isolated_targets(
            target_database_url="postgresql+psycopg://user@127.0.0.1/restore",
            target_media_root=media,
            backup_root=backup,
            generation_path=backup / "generation",
            production_database_urls=("postgresql://user@127.0.0.1/clinic",),
            production_media_roots=(media,),
            _identity_resolver=lambda _value: ("different-server", 5432, "restore"),
        )
    with pytest.raises(OpsError, match="outside backup"):
        assert_isolated_targets(
            target_database_url="postgresql+psycopg://user@127.0.0.1/restore",
            target_media_root=backup / "restore",
            backup_root=backup,
            generation_path=backup / "generation",
            production_database_urls=("postgresql://user@127.0.0.1/clinic",),
            production_media_roots=(media,),
            _identity_resolver=lambda _value: ("different-server", 5432, "restore"),
        )


@pytest.fixture(scope="module")
def postgres_restore_fixture(tmp_path_factory: pytest.TempPathFactory):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured; Slice 8 PostgreSQL acceptance is optional")
    missing_clients = [
        client for client in ("pg_dump", "pg_restore", "psql") if not shutil_which(client)
    ]
    if missing_clients:
        pytest.fail(
            "native PostgreSQL clients are required for Slice 8 acceptance: "
            + ", ".join(missing_clients)
        )
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.fail("psycopg is required for Slice 8 restore acceptance")
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from app import create_app
    from app.db import normalize_database_url
    from app.db import db

    database_url = normalize_database_url(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    root = tmp_path_factory.mktemp("slice8-acceptance")
    media = private_media_root(root / "media")
    application = create_app(
        {
            "TESTING": True,
            "APP_ENV": "test",
            "DATABASE_URL": database_url,
            "MEDIA_ROOT": str(media),
            "SECRET_KEY": "slice-8-acceptance-secret",
            "SESSION_COOKIE_SECURE": False,
            "BOARD_RENDER_DPI": 20,
        }
    )
    from app.captures import create_capture
    from app.comparisons import acquire_edit_lease, add_frame, create_comparison_set
    from app.models import Patient, ShotType, User
    from PIL import Image
    import io

    with application.app_context():
        actor = User(username="restore-drill-editor", display_name="Restore Drill", role="editor", active=True)
        actor.set_password("restore-drill-password")
        db.session.add(actor)
        db.session.flush()
        patient = Patient(
            patient_id="OPS-SYNTHETIC-1",
            name="Synthetic Fixture",
            birth_year=1990,
            consent_confirmed_by_id=actor.id,
            consent_confirmed_at=datetime.now(timezone.utc),
            created_by_id=actor.id,
            updated_by_id=actor.id,
        )
        shot_type = ShotType(name="Synthetic view", state="canonical", created_by_id=actor.id)
        db.session.add_all([patient, shot_type])
        db.session.flush()
        payload_stream = io.BytesIO()
        Image.new("RGB", (16, 8), "red").save(payload_stream, format="PNG")
        capture = create_capture(
            actor=actor,
            patient=patient,
            upload=payload_stream.getvalue(),
            capture_date="2024-01-01",
            capture_date_confirmed=True,
            shot_type=shot_type,
            original_filename="synthetic.png",
        )
        comparison = create_comparison_set(actor=actor, patient=patient, name="Synthetic set", columns=1)
        comparison = acquire_edit_lease(actor=actor, comparison_set=comparison, expected_version=comparison.version)
        add_frame(actor=actor, comparison_set=comparison, capture_id=capture.id, expected_version=comparison.version)
        db.session.commit()
        return {
            "database_url": database_url,
            "application": application,
            "media": media,
            "root": root,
            "username": "restore-drill-editor",
            "password": "restore-drill-password",
        }


def shutil_which(command: str) -> str | None:
    import shutil

    return shutil.which(command)


@pytest.mark.postgres
def test_actual_postgresql_backup_restore_and_authenticated_smoke(postgres_restore_fixture) -> None:
    fixture = postgres_restore_fixture
    backup_root = fixture["root"] / "backup"
    backup_root.mkdir(mode=0o700)
    result = create_backup(
        media_root=fixture["media"],
        backup_root=backup_root,
        database_url=fixture["database_url"],
        _storage_policy=lambda _media, _backup: None,
    )
    assert verify_generation(result.path).name == result.generation

    target_url = make_restore_database_url(fixture["database_url"])
    from ops.restore_check import run_restore_check

    restore_parent = fixture["root"] / "restore-parent"
    restore_parent.mkdir(mode=0o700)
    restored_media = restore_parent / "restored-media"
    marker = provision_isolated_target(
        target_database_url=target_url,
        target_media_root=restored_media,
        restore_parent=restore_parent,
        production_database_urls=(fixture["database_url"],),
        production_media_roots=(fixture["media"],),
        backup_root=backup_root,
    )
    try:
        restored = run_restore_check(
            backup_root=backup_root,
            generation=result.generation,
            target_database_url=target_url,
            target_media_root=restored_media,
            source_database_url=fixture["database_url"],
            production_database_urls=(fixture["database_url"],),
            production_media_roots=(fixture["media"],),
            restore_parent=restore_parent,
            ownership_marker=marker,
            smoke_username=fixture["username"],
            smoke_password=fixture["password"],
        )
        assert restored.generation == result.generation
        assert restored.migration_checked and restored.database_media_checked and restored.smoke_checked
    finally:
        cleanup_isolated_target(
            target_database_url=target_url,
            target_media_root=restored_media,
            restore_parent=restore_parent,
            production_database_urls=(fixture["database_url"],),
            production_media_roots=(fixture["media"],),
            backup_root=backup_root,
            ownership_marker=marker,
        )


def make_restore_database_url(source: str) -> str:
    parsed = urlsplit(source.replace("postgresql+psycopg://", "postgresql://"))
    database = "before_after_restore_" + uuid.uuid4().hex
    target = urlunsplit(("postgresql", parsed.netloc, "/" + database, parsed.query, parsed.fragment))
    return target.replace("postgresql://", "postgresql+psycopg://", 1)


def _synthetic_backup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    media = private_media_root(tmp_path / "media")
    (media / "originals" / "capture.jpg").write_bytes(b"original")
    os.chmod(media / "originals" / "capture.jpg", 0o640)
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    dump = fake_pg_dump(tmp_path)
    restore = fake_pg_restore(tmp_path)
    result = create_backup(
        media_root=media,
        backup_root=backup,
        database_url="postgresql://user:secret@127.0.0.1/clinic",
        pg_dump=str(dump),
        pg_restore=str(restore),
        _storage_policy=lambda _media, _backup: None,
    )
    return media, backup, result.path, restore


def test_backup_script_rejects_unknown_systemctl_and_stop_is_a_privileged_precondition(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(
        "#!/bin/sh\nprintf unknown\nexit 3\n", encoding="ascii"
    )
    (fake_bin / "systemctl").chmod(0o700)
    result = subprocess.run(
        ["bash", "ops/backup.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert result.returncode != 0
    assert "explicitly report" in result.stderr
    service = (Path(__file__).resolve().parents[1] / "deploy/systemd/before-after-backup.service").read_text()
    assert "User=before-after-backup" in service
    assert "ExecStartPre=+/usr/bin/systemctl stop before-after.service" in service


def test_database_aliases_are_compared_by_connection_identity(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)

    def identity(value: str) -> tuple[str, int, str]:
        if "target" in value or "localhost" in value:
            return ("127.0.0.1", 5432, "clinic")
        return ("127.0.0.1", 5432, "clinic")

    with pytest.raises(OpsError, match="production database"):
        assert_isolated_targets(
            target_database_url="postgresql://target@localhost/clinic",
            target_media_root=tmp_path / "restore",
            backup_root=backup,
            generation_path=backup / "generation",
            production_database_urls=("postgresql://production@127.0.0.1/clinic",),
            production_media_roots=(media,),
            _identity_resolver=identity,
        )


def test_restore_target_symlink_is_rejected(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    target = tmp_path / "restore-link"
    target.symlink_to(media, target_is_directory=True)
    with pytest.raises(OpsError, match="symlinks"):
        assert_isolated_targets(
            target_database_url="postgresql://restore@127.0.0.1/restore",
            target_media_root=target,
            backup_root=backup,
            generation_path=backup / "generation",
            production_database_urls=("postgresql://production@127.0.0.1/clinic",),
            production_media_roots=(media,),
            _identity_resolver=lambda _value: ("other", 5432, "other"),
        )


def test_stale_staging_is_ignored_by_selection_and_explicit_cleanup(tmp_path: Path) -> None:
    from ops.backup import cleanup_stale_staging

    _media, backup, generation, _restore = _synthetic_backup(tmp_path)
    run_id = "a" * 32
    stale = backup / f".staging-{run_id}"
    stale.mkdir(mode=0o700)
    owner = stale / ".staging-owner.json"
    owner.write_text(
        json.dumps({"format": "before-after.backup-staging.v1", "run_id": run_id, "pid": 1, "created_ns": 1}),
        encoding="ascii",
    )
    owner.chmod(0o600)
    os.utime(stale, (1, 1))
    selected = select_generation(backup)
    assert selected.path == generation
    assert stale.exists()
    assert cleanup_stale_staging(backup, age_seconds=0, now=2) == [stale.name]
    assert not stale.exists()


def test_corrupt_pgdump_is_rejected_before_publish(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    dump = tmp_path / "bad-pg-dump"
    dump.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys\n"
        "pathlib.Path(sys.argv[sys.argv.index('--file')+1]).write_bytes(b'not-a-pgdump')\n",
        encoding="ascii",
    )
    dump.chmod(0o700)
    restore = fake_pg_restore(tmp_path)
    with pytest.raises(OpsError, match="PGDMP"):
        create_backup(
            media_root=media,
            backup_root=backup,
            database_url="postgresql://user@127.0.0.1/clinic",
            pg_dump=str(dump),
            pg_restore=str(restore),
            _storage_policy=lambda _media, _backup: None,
        )
    assert not list(backup.glob("[0-9]*T*"))


def test_retention_preserves_verified_generation_when_new_generation_is_bad(tmp_path: Path) -> None:
    _media, backup, old_generation, _restore = _synthetic_backup(tmp_path)
    bad_generation = backup / ("20990101T010101Z-" + "b" * 32)
    shutil.copytree(old_generation, bad_generation)
    (bad_generation / "database.dump").write_bytes(b"corrupt")
    os.chmod(bad_generation / "database.dump", 0o600)
    assert prune_generations(backup, retain=1) == []
    assert old_generation.exists()
    assert bad_generation.exists()
    assert select_generation(backup).path == old_generation


def test_restore_staging_cleanup_requires_exact_dead_owner_and_preserves_unowned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.restore_check as restore_module

    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    target = parent / "nested" / "media"
    run_id = "a" * 32
    owned = parent / (".restore-" + "b" * 32)
    owned.mkdir(mode=0o700)
    restore_module._write_restore_stage_owner(
        owned,
        parent=parent,
        target=target,
        run_id=run_id,
        kind="media",
    )
    owner = owned / ".restore-stage-owner.json"
    payload = json.loads(owner.read_text(encoding="ascii"))
    payload["created_at"] = 1
    owner.write_text(json.dumps(payload), encoding="ascii")
    owner.chmod(0o600)
    os.utime(owned, (1, 1))
    unowned = parent / (".restore-source-" + "c" * 32)
    unowned.mkdir(mode=0o700)
    monkeypatch.setattr(restore_module, "_process_is_alive", lambda _pid: False)
    assert cleanup_stale_restore_staging(
        restore_parent=parent,
        target_media_root=target,
        run_id=run_id,
        age_seconds=0,
        now=2,
    ) == [owned.name]
    assert not owned.exists()
    assert unowned.exists()


def test_restore_stages_immutable_copies_before_source_changes(tmp_path: Path) -> None:
    media, _backup, generation_path, restore = _synthetic_backup(tmp_path)
    generation = verify_generation(generation_path)
    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    staged, staging = stage_verified_generation(generation, parent, pg_restore=str(restore))
    (media / "originals" / "capture.jpg").write_bytes(b"changed-after-copy")
    assert verify_generation(staged.path).name == generation.name
    assert stat.S_IMODE((staged.path / "database.dump").stat().st_mode) == 0o400
    import ops.restore_check as restore_module

    restore_module._remove_private_path(staging)


def test_native_client_argv_contains_no_database_url_or_password(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    dump_record = tmp_path / "dump-record.json"
    restore_record = tmp_path / "restore-record.json"
    dump = tmp_path / "argv-pg-dump"
    dump.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys\n"
        f"pathlib.Path({str(dump_record)!r}).write_text(json.dumps({{'argv':sys.argv[1:], 'passfile':pathlib.Path(os.environ['PGPASSFILE']).read_text()}}))\n"
        "pathlib.Path(sys.argv[sys.argv.index('--file')+1]).write_bytes(b'PGDMP synthetic dump')\n",
        encoding="ascii",
    )
    dump.chmod(0o700)
    restore = tmp_path / "argv-pg-restore"
    restore.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys\n"
        f"pathlib.Path({str(restore_record)!r}).write_text(json.dumps({{'argv':sys.argv[1:], 'passfile':pathlib.Path(os.environ['PGPASSFILE']).read_text()}}))\n",
        encoding="ascii",
    )
    restore.chmod(0o700)
    create_backup(
        media_root=media,
        backup_root=backup,
        database_url="postgresql://user:super-secret@127.0.0.1/clinic",
        pg_dump=str(dump),
        pg_restore=str(restore),
        _storage_policy=lambda _media, _backup: None,
    )
    for record in (dump_record, restore_record):
        payload = json.loads(record.read_text())
        assert all("super-secret" not in argument for argument in payload["argv"])
        assert all("DATABASE_URL" not in argument for argument in payload["argv"])
        assert "--dbname" not in payload["argv"]
        assert "super-secret" in payload["passfile"]


def test_proxy_prefix_is_not_trusted_and_nginx_has_host_hsts_guards(tmp_path: Path) -> None:
    from flask import request
    from app import create_app

    application = create_app(
        {
            "TESTING": True,
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql+psycopg://user:password@127.0.0.1/clinic",
            "MEDIA_ROOT": str(tmp_path / "media"),
            "SECRET_KEY": "x" * 64,
            "TRUSTED_PROXY_COUNT": 1,
            "SESSION_COOKIE_SECURE": True,
        }
    )

    @application.get("/_slice8_prefix_probe")
    def prefix_probe():
        return {"root": request.script_root}

    response = application.test_client().get(
        "/_slice8_prefix_probe",
        headers={"X-Forwarded-Prefix": "/spoofed"},
    )
    assert response.json == {"root": ""}
    nginx = (Path(__file__).resolve().parents[1] / "deploy/nginx/before-after.conf").read_text()
    assert "listen 80 default_server" in nginx
    assert "listen 443 ssl http2 default_server" in nginx
    assert "return 444" in nginx
    assert "Strict-Transport-Security" in nginx
    assert 'proxy_set_header X-Forwarded-Prefix ""' in nginx


def test_restore_failure_removes_media_and_marks_target_disposable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.restore_check as restore_module

    _media, backup, generation_path, restore = _synthetic_backup(tmp_path)
    selected = verify_generation(generation_path)
    target = tmp_path / "restore-media"
    source_staging = tmp_path / ".restore-source-test"
    source_staging.mkdir(mode=0o700)
    (source_staging / "dump").write_bytes(b"private")
    monkeypatch.setattr(restore_module, "select_generation", lambda *_args: selected)
    monkeypatch.setattr(restore_module, "assert_isolated_targets", lambda **_kwargs: target)
    monkeypatch.setattr(
        restore_module,
        "stage_verified_generation",
        lambda *_args, **_kwargs: (selected, source_staging),
    )

    def fail_after_media(generation, destination):
        destination.mkdir(mode=0o700)
        (destination / "clinical.bin").write_bytes(b"clinical")
        raise OpsError("synthetic restore failure")

    monkeypatch.setattr(restore_module, "restore_media", fail_after_media)
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        restore_module,
        "_cleanup_restore_database",
        lambda database_url: cleanup_calls.append(database_url),
    )
    with pytest.raises(OpsError, match="synthetic restore failure"):
        restore_module.run_restore_check(
            backup_root=backup,
            target_database_url="postgresql://restore@127.0.0.1/restore",
            target_media_root=target,
            production_database_urls=("postgresql://production@127.0.0.1/clinic",),
            production_media_roots=(_media,),
            smoke_username="editor",
            smoke_password="secret",
        )
    assert not target.exists()
    assert not source_staging.exists()
    assert cleanup_calls == ["postgresql://restore@127.0.0.1/restore"]
    assert (tmp_path / ".restore-media.restore-failed").is_file()


def test_smoke_failure_cleans_database_state_and_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.restore_check as restore_module

    _media, backup, generation_path, restore = _synthetic_backup(tmp_path)
    selected = verify_generation(generation_path)
    target = tmp_path / "restore-media"
    source_staging = tmp_path / ".restore-source-test"
    source_staging.mkdir(mode=0o700)
    (source_staging / "dump").write_bytes(b"private")
    clinical_rows = {"Patient": [1], "Capture": [1]}
    monkeypatch.setattr(restore_module, "select_generation", lambda *_args: selected)
    monkeypatch.setattr(restore_module, "assert_isolated_targets", lambda **_kwargs: target)
    monkeypatch.setattr(
        restore_module,
        "stage_verified_generation",
        lambda *_args, **_kwargs: (selected, source_staging),
    )

    def restore_assets(_generation, destination):
        destination.mkdir(mode=0o700)
        (destination / "clinical.bin").write_bytes(b"clinical")

    monkeypatch.setattr(restore_module, "restore_media", restore_assets)
    monkeypatch.setattr(restore_module, "verify_restored_media", lambda *_args: None)
    monkeypatch.setattr(restore_module, "restore_database", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(restore_module, "migration_check", lambda **_kwargs: None)
    monkeypatch.setattr(restore_module, "_database_media_check", lambda *_args: None)
    monkeypatch.setattr(
        restore_module,
        "smoke_check",
        lambda **_kwargs: (_ for _ in ()).throw(OpsError("injected smoke failure")),
    )

    def clean_database(_database_url: str) -> None:
        clinical_rows.clear()

    monkeypatch.setattr(restore_module, "_cleanup_restore_database", clean_database)
    with pytest.raises(OpsError, match="injected smoke failure"):
        restore_module.run_restore_check(
            backup_root=backup,
            target_database_url="postgresql://restore@127.0.0.1/restore",
            target_media_root=target,
            production_database_urls=("postgresql://production@127.0.0.1/clinic",),
            production_media_roots=(_media,),
            smoke_username="editor",
            smoke_password="secret",
        )
    assert clinical_rows == {}
    assert not target.exists()
    assert not source_staging.exists()


def test_restore_cleanup_deletion_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.restore_check as restore_module

    target = tmp_path / "staging"
    target.mkdir(mode=0o700)
    (target / "clinical.bin").write_bytes(b"clinical")
    monkeypatch.setattr(
        restore_module.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("delete blocked")),
    )
    with pytest.raises(OpsError, match="remove restore cleanup path"):
        restore_module._remove_private_path_strict(target)
    assert target.exists()


def test_runbook_uses_bootstrap_and_secret_files_not_password_arguments() -> None:
    root = Path(__file__).resolve().parents[1]
    runbook = (root / "docs/deployment.md").read_text()
    assert "deploy/bootstrap.sh --prepare-only" in runbook
    assert "before-after-backup" in runbook
    assert "--smoke-password-file" in runbook
    assert "--smoke-password '" not in runbook
    assert "--target-database-url" not in runbook
    assert "/var/tmp" not in runbook
