"""Application factory for the Clinical Image Comparison MVP."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import click
from flask import Flask, redirect, request, url_for
from flask_login import current_user, login_required
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import auth_bp, editor_required, login_manager
from app.db import db
from app.models import User
from app.patients import patients_bp

__version__ = "0.1.0"

csrf = CSRFProtect()


def _postgres_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("DATABASE_URL is required")
    value = value.strip()
    if value.startswith(("postgres://", "postgresql://")):
        prefix = "postgres://" if value.startswith("postgres://") else "postgresql://"
        value = "postgresql+psycopg://" + value[len(prefix) :]
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        raise RuntimeError("DATABASE_URL must use PostgreSQL")
    return value


def _required_text(config: dict, key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{key} is required")
    return value.strip()


def _validate_configuration(app: Flask) -> None:
    database_url = app.config.get("DATABASE_URL") or app.config.get("SQLALCHEMY_DATABASE_URI")
    database_url = _postgres_url(database_url)
    media_root = _required_text(app.config, "MEDIA_ROOT")
    secret_key = _required_text(app.config, "SECRET_KEY")

    media_path = Path(media_root).expanduser()
    try:
        media_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"MEDIA_ROOT is not usable: {exc}") from exc
    if not media_path.is_dir():
        raise RuntimeError("MEDIA_ROOT must be a directory")

    app.config.update(
        DATABASE_URL=database_url,
        SQLALCHEMY_DATABASE_URI=database_url,
        MEDIA_ROOT=str(media_path.resolve()),
        SECRET_KEY=secret_key,
    )
    if app.config.get("APP_ENV") != "development":
        app.config["DEBUG"] = False


def _register_create_admin_command(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--username", prompt="Username")
    @click.option("--display-name", prompt="Display name")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(username: str, display_name: str, password: str) -> None:
        """Create the first or another active Admin account."""
        username = username.strip().casefold()
        display_name = display_name.strip()
        if not username:
            raise click.ClickException("username is required")
        if not display_name:
            raise click.ClickException("display name is required")
        if not password:
            raise click.ClickException("password is required")
        if db.session.scalar(select(User).where(User.username == username)) is not None:
            raise click.ClickException("username already exists")

        user = User(username=username, display_name=display_name, role="admin", active=True)
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise click.ClickException("username already exists") from exc
        click.echo(f"Admin account created: {username}")


def _register_prototype_routes(app: Flask) -> None:
    """Keep Slice 0's renderer available without exposing it at the new root."""
    from app import web as legacy

    @app.get("/prototype")
    @login_required
    def prototype_index():
        return legacy.index()

    @app.get("/prototype/api/template/<name>")
    @login_required
    def prototype_template(name: str):
        return legacy.api_template(name)

    @app.post("/prototype/render")
    @editor_required
    def prototype_render():
        return legacy.do_render()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        APP_ENV=os.environ.get("APP_ENV", os.environ.get("FLASK_ENV", "production")),
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        MEDIA_ROOT=os.environ.get("MEDIA_ROOT"),
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
        WTF_CSRF_ENABLED=True,
        DEBUG=False,
    )
    if config:
        app.config.from_mapping(config)
    _validate_configuration(app)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(patients_bp)
    _register_prototype_routes(app)
    _register_create_admin_command(app)

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

    return app
