from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import shutil
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
import uuid

import pytest

from ops import backup as backup_module
from ops.backup import OpsError, create_backup, prune_generations
from ops.restore_check import (
    assert_isolated_targets,
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
    path.mkdir(mode=0o700)
    for name in ("originals", "previews", "derivatives", "quarantine"):
        (path / name).mkdir(mode=0o700)
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


def test_backup_publishes_private_atomic_generation_and_manifest(tmp_path: Path) -> None:
    media = private_media_root(tmp_path / "media")
    original = b"synthetic original"
    (media / "originals" / "capture.bin").write_bytes(original)
    # The generated media key is intentionally opaque; the manifest contains
    # no original filename or Patient fields.
    os.chmod(media / "originals" / "capture.bin", 0o600)
    (media / "previews" / "preview.jpg").write_bytes(b"preview")
    os.chmod(media / "previews" / "preview.jpg", 0o600)
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
    os.chmod(media / "originals" / "capture.jpg", 0o600)
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
        pytest.skip("TEST_DATABASE_URL is required for Slice 8 PostgreSQL acceptance")
    if not shutil_which("pg_dump") or not shutil_which("pg_restore"):
        pytest.skip("pg_dump and pg_restore are required for Slice 8 restore acceptance")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg is required for Slice 8 restore acceptance")
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

    restored_media = fixture["root"] / "restored-media"
    try:
        restored = run_restore_check(
            backup_root=backup_root,
            generation=result.generation,
            target_database_url=target_url,
            target_media_root=restored_media,
            source_database_url=fixture["database_url"],
            production_database_urls=(fixture["database_url"],),
            production_media_roots=(fixture["media"],),
            smoke_username=fixture["username"],
            smoke_password=fixture["password"],
        )
        assert restored.generation == result.generation
        assert restored.migration_checked and restored.database_media_checked and restored.smoke_checked
    finally:
        drop_restore_database(target_url)


def drop_restore_database(target_url: str) -> None:
    parsed = urlsplit(target_url.replace("postgresql+psycopg://", "postgresql://"))
    database = parsed.path.lstrip("/")
    admin = urlunsplit(("postgresql", parsed.netloc, "/postgres", parsed.query, parsed.fragment))
    import psycopg

    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def make_restore_database_url(source: str) -> str:
    parsed = urlsplit(source.replace("postgresql+psycopg://", "postgresql://"))
    database = "before_after_restore_" + uuid.uuid4().hex[:12]
    target = urlunsplit(("postgresql", parsed.netloc, "/" + database, parsed.query, parsed.fragment))
    admin = urlunsplit(("postgresql", parsed.netloc, "/postgres", parsed.query, parsed.fragment))
    import psycopg

    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{database}"')
    return target.replace("postgresql://", "postgresql+psycopg://", 1)


def _synthetic_backup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    media = private_media_root(tmp_path / "media")
    (media / "originals" / "capture.jpg").write_bytes(b"original")
    os.chmod(media / "originals" / "capture.jpg", 0o600)
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


def test_stale_staging_is_removed_and_never_selected(tmp_path: Path) -> None:
    _media, backup, generation, _restore = _synthetic_backup(tmp_path)
    stale = backup / ".staging-stale"
    stale.mkdir(mode=0o700)
    (stale / "partial").write_bytes(b"partial")
    selected = select_generation(backup)
    assert selected.path == generation
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
    assert (tmp_path / ".restore-media.restore-failed").is_file()


def test_runbook_uses_bootstrap_and_secret_files_not_password_arguments() -> None:
    root = Path(__file__).resolve().parents[1]
    runbook = (root / "docs/deployment.md").read_text()
    assert "deploy/bootstrap.sh --prepare-only" in runbook
    assert "before-after-backup" in runbook
    assert "--smoke-password-file" in runbook
    assert "--smoke-password '" not in runbook
    assert "--target-database-url" not in runbook
    assert "/var/tmp" not in runbook
