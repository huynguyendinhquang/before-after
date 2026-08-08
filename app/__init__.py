"""Application factory for the Clinical Image Comparison MVP."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import click
from flask import Flask, redirect, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import select

from app.auth import auth_bp, editor_required, login_manager, register_cli
from app.captures import captures_bp
from app.db import db, normalize_database_url
from app.image_policy import configured_request_limit
from app.models import Capture
from app.patients import patients_bp
from app.storage import DEFAULT_ORPHAN_GRACE_SECONDS, ManagedStorage, StorageError

__version__ = "0.1.0"

csrf = CSRFProtect()


def _required_text(config: dict, key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{key} is required")
    return value.strip()


def _validate_configuration(app: Flask) -> None:
    database_url = app.config.get("DATABASE_URL") or app.config.get("SQLALCHEMY_DATABASE_URI")
    database_url = normalize_database_url(database_url)
    media_root = _required_text(app.config, "MEDIA_ROOT")
    secret_key = _required_text(app.config, "SECRET_KEY")

    media_path = Path(media_root).expanduser()
    if ".." in media_path.parts:
        raise RuntimeError("MEDIA_ROOT must not contain lexical traversal")
    try:
        media_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        media_path = media_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"MEDIA_ROOT is not usable: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"MEDIA_ROOT is not usable: {exc}") from exc
    if not media_path.is_dir():
        raise RuntimeError("MEDIA_ROOT must be a directory")
    static_root = Path(app.static_folder).resolve()
    try:
        media_path.relative_to(static_root)
    except ValueError:
        pass
    else:
        raise RuntimeError("MEDIA_ROOT must be outside the static web root")
    try:
        if stat.S_IMODE(media_path.stat().st_mode) & 0o077:
            raise RuntimeError("MEDIA_ROOT must be private to the application user")
    except OSError as exc:
        raise RuntimeError(f"MEDIA_ROOT is not usable: {exc}") from exc

    app.config.update(
        DATABASE_URL=database_url,
        SQLALCHEMY_DATABASE_URI=database_url,
        MEDIA_ROOT=str(media_path.resolve()),
        SECRET_KEY=secret_key,
    )
    if app.config.get("APP_ENV") != "development":
        app.config["DEBUG"] = False


def _register_prototype_routes(app: Flask) -> None:
    """Keep Slice 0's renderer available without exposing it at the new root."""
    from app import web as legacy

    @app.get("/prototype")
    @editor_required
    def prototype_index():
        return legacy.index()

    @app.get("/prototype/api/template/<name>")
    @editor_required
    def prototype_template(name: str):
        return legacy.api_template(name)

    @app.post("/prototype/render")
    @editor_required
    def prototype_render():
        response = legacy.do_render()
        if response.status_code == 200:
            from app.audit import append_audit

            try:
                append_audit(
                    actor=current_user,
                    action="prototype.export",
                    entity_type="prototype",
                    entity_id="legacy",
                    details={"format": (request.form.get("format") or "png").lower()},
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
        return response


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        APP_ENV=os.environ.get("APP_ENV", os.environ.get("FLASK_ENV", "production")),
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        MEDIA_ROOT=os.environ.get("MEDIA_ROOT"),
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=configured_request_limit(),
        SESSION_COOKIE_SECURE=None,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
        WTF_CSRF_ENABLED=True,
        DEBUG=False,
    )
    if config:
        app.config.from_mapping(config)
    if app.config["SESSION_COOKIE_SECURE"] is None:
        app.config["SESSION_COOKIE_SECURE"] = not (
            app.config.get("TESTING")
            or app.config.get("APP_ENV") in {"development", "test"}
        )
    elif app.config.get("APP_ENV") not in {"development", "test"} and not app.config.get("TESTING"):
        app.config["SESSION_COOKIE_SECURE"] = True
    _validate_configuration(app)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(patients_bp)
    app.register_blueprint(captures_bp)
    _register_prototype_routes(app)
    register_cli(app)

    @app.cli.command("reconcile-media")
    @click.option(
        "--grace-seconds",
        type=click.FloatRange(min=0),
        default=DEFAULT_ORPHAN_GRACE_SECONDS,
        show_default=True,
    )
    def reconcile_media(grace_seconds: float) -> None:
        """Remove stale media that no committed Capture references."""
        storage = ManagedStorage(app.config["MEDIA_ROOT"])
        try:
            with storage.reconciliation_lock():
                referenced = set(db.session.scalars(select(Capture.storage_key)))
                removed = storage.reconcile(referenced, grace_seconds=grace_seconds)
        except StorageError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Removed {len(removed)} orphan media files.")

    @app.errorhandler(CSRFError)
    def csrf_failure(_error):
        # Keep the permission contract explicit even when a Viewer omits a token;
        # the request is still rejected before any route mutation runs.
        if current_user.is_authenticated and current_user.role == "viewer" and request.method != "GET":
            return "Forbidden", 403
        return "CSRF validation failed", 400

    @app.get("/")
    def root():
        return redirect(url_for("patients.index"))

    @app.after_request
    def no_store_authenticated_patient_pages(response):
        if current_user.is_authenticated and request.path.startswith(("/patients", "/captures", "/shot-types", "/prototype")):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
