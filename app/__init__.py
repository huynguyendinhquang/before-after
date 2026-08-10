"""Application factory for the Clinical Image Comparison MVP."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import click
from flask import Flask, redirect, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import select

from app.admin import admin_bp
from app.auth import auth_bp, login_manager, register_cli
from app.captures import captures_bp
from app.comparisons import comparisons_bp
from app.exports import exports_bp
from app.lifecycle import lifecycle_bp
from app.db import db, postgres_route
from app.demo import register_demo
from app.image_policy import configured_request_limit
from app.models import Capture, Export
from app.patients import patients_bp
from app.storage import DEFAULT_ORPHAN_GRACE_SECONDS, MEDIA_DIRECTORY_MODE, ManagedStorage, StorageError

__version__ = "0.1.0"

csrf = CSRFProtect()


def _required_text(config: dict, key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{key} is required")
    return value.strip()


def _trusted_proxy_count(value: object) -> int:
    if isinstance(value, bool):
        raise RuntimeError("TRUSTED_PROXY_COUNT must be a non-negative integer")
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("TRUSTED_PROXY_COUNT must be a non-negative integer") from exc
    if count < 0 or count > 10:
        raise RuntimeError("TRUSTED_PROXY_COUNT must be a non-negative integer")
    return count


def _validate_configuration(app: Flask) -> None:
    database_url = app.config.get("DATABASE_URL") or app.config.get("SQLALCHEMY_DATABASE_URI")
    route = postgres_route(database_url)
    database_url = route.sqlalchemy_url
    media_root = _required_text(app.config, "MEDIA_ROOT")
    secret_key = _required_text(app.config, "SECRET_KEY")

    media_path = Path(media_root).expanduser()
    if ".." in media_path.parts:
        raise RuntimeError("MEDIA_ROOT must not contain lexical traversal")
    try:
        created = not media_path.exists()
        media_path.mkdir(parents=True, exist_ok=True, mode=MEDIA_DIRECTORY_MODE)
        if created:
            media_path.chmod(MEDIA_DIRECTORY_MODE)
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
        mode = stat.S_IMODE(media_path.stat().st_mode)
        if mode & 0o007 or mode & 0o020:
            raise RuntimeError("MEDIA_ROOT must be private to the application and media group (not world-accessible or group-writable)")
    except OSError as exc:
        raise RuntimeError(f"MEDIA_ROOT is not usable: {exc}") from exc

    app.config.update(
        DATABASE_URL=database_url,
        SQLALCHEMY_DATABASE_URI=database_url,
        MEDIA_ROOT=str(media_path.resolve()),
        SECRET_KEY=secret_key,
        TRUSTED_PROXY_COUNT=_trusted_proxy_count(app.config.get("TRUSTED_PROXY_COUNT", 0)),
    )
    if app.config.get("APP_ENV") != "development":
        app.config["DEBUG"] = False


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
        TRUSTED_PROXY_COUNT=int(os.environ.get("TRUSTED_PROXY_COUNT", "0")),
        DEMO_AUTO_LOGIN=os.environ.get("DEMO_AUTO_LOGIN", "0") == "1",
        DEMO_USERNAME=os.environ.get("DEMO_USERNAME", "demo"),
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
    if app.config["TRUSTED_PROXY_COUNT"]:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=app.config["TRUSTED_PROXY_COUNT"],
            x_proto=app.config["TRUSTED_PROXY_COUNT"],
            x_host=app.config["TRUSTED_PROXY_COUNT"],
            x_port=app.config["TRUSTED_PROXY_COUNT"],
            x_prefix=0,
        )

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(patients_bp)
    app.register_blueprint(captures_bp)
    app.register_blueprint(comparisons_bp)
    app.register_blueprint(exports_bp)
    app.register_blueprint(lifecycle_bp)
    register_cli(app)
    register_demo(app)

    @app.cli.command("reconcile-media")
    @click.option(
        "--grace-seconds",
        type=click.FloatRange(min=0),
        default=DEFAULT_ORPHAN_GRACE_SECONDS,
        show_default=True,
    )
    def reconcile_media(grace_seconds: float) -> None:
        """Remove stale media that no committed Capture or Export references."""
        storage = ManagedStorage(app.config["MEDIA_ROOT"])
        try:
            with storage.reconciliation_lock():
                referenced = set(db.session.scalars(select(Capture.storage_key)))
                referenced.update(db.session.scalars(select(Export.storage_key)))
                removed = storage.reconcile(
                    referenced,
                    grace_seconds=grace_seconds,
                    capture_exists=lambda capture_id: db.session.scalar(
                        select(Capture.id).where(Capture.id == capture_id)
                    )
                    is not None,
                )
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
        if current_user.is_authenticated and request.path.startswith(
            ("/admin", "/patients", "/captures", "/shot-types", "/comparison-sets", "/exports")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
