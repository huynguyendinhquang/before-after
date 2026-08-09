"""Local login, session handling, and server-side role guards."""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlsplit

import click
from flask import Blueprint, Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import FlaskForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, Optional

from app.audit import append_audit
from app.db import db
from app.models import User

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "warning"

auth_bp = Blueprint("auth", __name__)
USER_ROLES = ("admin", "editor", "viewer")
_UNSET = object()


class AdminError(ValueError):
    """Raised when an Admin workflow input is invalid."""


class FinalAdminError(AdminError):
    """Raised when a mutation would remove the final active Admin."""


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=128)])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log in")


class UserAdminForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=128)])
    display_name = StringField("Display name", validators=[DataRequired(), Length(max=200)])
    password = PasswordField("Password", validators=[Optional(), Length(max=512)])
    role = SelectField("Role", choices=[(role, role.title()) for role in USER_ROLES])
    active = BooleanField("Active", default=True)
    submit = SubmitField("Save user")


def _safe_next_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if "\\" in value or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
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
        saved_version = int(session.get("session_version", 0))
    except (TypeError, ValueError, OverflowError):
        return None
    user = db.session.get(User, numeric_id)
    if user is None or not user.active or saved_version != user.session_version:
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
            session["session_version"] = user.session_version
            return redirect(_safe_next_url(request.args.get("next")) or url_for("patients.index"))
        flash("Invalid username or password.", "error")

    return render_template("auth/login.html", form=form)


@auth_bp.post("/logout")
@login_required
def logout():
    logout_user()
    session.pop("session_version", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


def _require_admin(actor: User) -> None:
    if actor is None or not actor.is_active or not actor.is_admin:
        raise PermissionError("only an active Admin can manage users")


def _user_id(value: User | int) -> int:
    candidate = value if isinstance(value, int) and not isinstance(value, bool) else getattr(value, "id", None)
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
        raise AdminError("user is unavailable")
    return candidate


def _username(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdminError("username is required")
    value = value.strip().casefold()
    if len(value) > 128:
        raise AdminError("username is too long")
    return value


def _display_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdminError("display name is required")
    value = value.strip()
    if len(value) > 200:
        raise AdminError("display name is too long")
    return value


def _role(value: object) -> str:
    if not isinstance(value, str) or value.strip().casefold() not in USER_ROLES:
        raise AdminError("role is invalid")
    return value.strip().casefold()


def _active(value: object) -> bool:
    if not isinstance(value, bool):
        raise AdminError("active must be boolean")
    return value


def _lock_users() -> list[User]:
    """Serialize user mutations and refresh every row before checking invariants."""
    return list(
        db.session.scalars(
            select(User)
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _locked_actor(actor: User, users: list[User]) -> User:
    actor_id = _user_id(actor)
    locked = next((item for item in users if item.id == actor_id), None)
    if locked is None or not locked.active or not locked.is_admin:
        raise PermissionError("only an active Admin can manage users")
    return locked


def create_user(
    *,
    actor: User | None,
    username: str,
    display_name: str,
    password: str,
    role: str = "viewer",
    active: bool = True,
    bootstrap: bool = False,
) -> User:
    """Create a user and audit it in one transaction; CLI bootstrap reuses this workflow."""
    if actor is None and not bootstrap:
        raise PermissionError("only an active Admin can manage users")
    if actor is not None:
        _require_admin(actor)
    username = _username(username)
    display_name = _display_name(display_name)
    role = _role(role)
    active = _active(active)
    if not isinstance(password, str) or not password:
        raise AdminError("password is required")
    try:
        users = _lock_users()
        locked_actor = _locked_actor(actor, users) if actor is not None else None
        if any(item.username == username for item in users):
            raise AdminError("username already exists")
        user = User(username=username, display_name=display_name, role=role, active=active)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        append_audit(
            actor=locked_actor,
            action="user.create",
            entity_type="user",
            entity_id=user.id,
            details={"role": role, "active": active},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return user


def update_user(
    *,
    actor: User,
    user: User | int,
    username: object = _UNSET,
    display_name: object = _UNSET,
    password: object = _UNSET,
    role: object = _UNSET,
    active: object = _UNSET,
) -> User:
    """Update a user while preserving the concurrent final-Admin invariant."""
    _require_admin(actor)
    try:
        users = _lock_users()
        locked_actor = _locked_actor(actor, users)
        target_id = _user_id(user)
        target = next((item for item in users if item.id == target_id), None)
        if target is None:
            raise AdminError("user is unavailable")
        next_username = target.username if username is _UNSET or username is None else _username(username)
        next_display_name = target.display_name if display_name is _UNSET or display_name is None else _display_name(display_name)
        next_role = target.role if role is _UNSET or role is None else _role(role)
        next_active = target.active if active is _UNSET else _active(active)
        if target.active and target.is_admin and (not next_active or next_role != "admin"):
            remaining = sum(item.id != target.id and item.active and item.is_admin for item in users)
            if remaining == 0:
                raise FinalAdminError("the final active Admin cannot be disabled or demoted")
        if password is not _UNSET and password is not None:
            if not isinstance(password, str) or not password:
                raise AdminError("password must not be empty")
            target.set_password(password)
        target.username = next_username
        target.display_name = next_display_name
        target.role = next_role
        target.active = next_active
        target.session_version += 1
        append_audit(
            actor=locked_actor,
            action="user.update",
            entity_type="user",
            entity_id=target.id,
            details={"role": next_role, "active": next_active},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return target


def disable_user(*, actor: User, user: User | int) -> User:
    return update_user(actor=actor, user=user, active=False)


def enable_user(*, actor: User, user: User | int) -> User:
    return update_user(actor=actor, user=user, active=True)


def list_users() -> list[User]:
    return list(db.session.scalars(select(User).order_by(User.username, User.id)))


def register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--username", prompt="Username")
    @click.option("--display-name", prompt="Display name")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(username: str, display_name: str, password: str) -> None:
        """Create an active Admin account during bootstrap."""
        try:
            user = create_user(
                actor=None,
                username=username,
                display_name=display_name,
                password=password,
                role="admin",
                active=True,
                bootstrap=True,
            )
        except (AdminError, IntegrityError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Admin account created: {user.username}")
