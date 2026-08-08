"""Local login, session handling, and server-side role guards."""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlsplit

import click
from flask import Blueprint, Flask, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import db
from app.audit import append_audit
from app.models import User

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "warning"

auth_bp = Blueprint("auth", __name__)


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=128)])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log in")


def _safe_next_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return None
    return value


def roles_required(*roles: str):
    """Require an authenticated user whose role is one of ``roles``."""

    allowed = set(roles)

    def decorator(view):
        @wraps(view)
        @login_required
        def guarded(*args, **kwargs):
            if not current_user.is_active or current_user.role not in allowed:
                from flask import abort

                abort(403)
            return view(*args, **kwargs)

        return guarded

    return decorator


def editor_required(view):
    return roles_required("admin", "editor")(view)


def admin_required(view):
    return roles_required("admin")(view)


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    try:
        numeric_id = int(user_id)
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, numeric_id)
    if user is None or not user.active:
        return None
    return user


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("patients.index"))

    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip().casefold()
        user = db.session.scalar(select(User).where(User.username == username))
        if user is not None and user.active and user.check_password(form.password.data):
            login_user(user)
            return redirect(_safe_next_url(request.args.get("next")) or url_for("patients.index"))
        flash("Invalid username or password.", "error")

    return render_template("auth/login.html", form=form)


@auth_bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


def register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--username", prompt="Username")
    @click.option("--display-name", prompt="Display name")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(username: str, display_name: str, password: str) -> None:
        """Create an active Admin account during bootstrap."""
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
            db.session.flush()
            append_audit(
                actor=None,
                action="user.create",
                entity_type="user",
                entity_id=user.id,
                details={"source": "create-admin"},
            )
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise click.ClickException("username already exists") from exc
        except Exception:
            db.session.rollback()
            raise
        click.echo(f"Admin account created: {username}")
