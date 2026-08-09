"""Admin workflows for users, Shot Types, and bounded audit browsing."""

from __future__ import annotations

from collections.abc import Mapping
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, Optional

from app.audit import append_audit
from app.auth import admin_required
from app.captures import ShotTypeError, merge_shot_type, promote_shot_type
from app.db import db
from app.models import AuditEvent, ShotType, User


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
_ALLOWED_ROLES = ("admin", "editor", "viewer")
_UNSET = object()


class AdminError(ValueError):
    """Raised when an Admin workflow input is invalid."""


class FinalAdminError(AdminError):
    """Raised when a mutation would remove the final active Admin."""


class UserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=128)])
    display_name = StringField("Display name", validators=[DataRequired(), Length(max=200)])
    password = PasswordField("Password", validators=[Optional(), Length(min=1, max=512)])
    role = SelectField("Role", choices=[(role, role.title()) for role in _ALLOWED_ROLES])
    active = BooleanField("Active", default=True)
    submit = SubmitField("Save user")


class MergeShotTypeForm(FlaskForm):
    target_id = SelectField("Canonical Shot Type", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Merge")


def _require_admin(actor: User) -> None:
    if actor is None or not actor.active or actor.role != "admin":
        raise PermissionError("only an Admin can manage administration")


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
    if not isinstance(value, str) or value not in _ALLOWED_ROLES:
        raise AdminError("role is invalid")
    return value


def _active(value: object) -> bool:
    if not isinstance(value, bool):
        raise AdminError("active must be boolean")
    return value


def _user_id(value: User | int) -> int:
    if isinstance(value, User):
        value = value.id
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdminError("user is unavailable")
    return value


def _locked_user_and_admins(user: User | int) -> tuple[User, list[User]]:
    user_id = _user_id(user)
    admins = list(
        db.session.scalars(
            select(User)
            .where(User.active.is_(True), User.role == "admin")
            .order_by(User.id)
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
    )
    locked = db.session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update(of=User)
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise AdminError("user is unavailable")
    return locked, admins


def create_user(
    *,
    actor: User,
    username: str,
    display_name: str,
    password: str,
    role: str = "viewer",
    active: bool = True,
) -> User:
    """Create a named user and audit it in the same transaction."""
    _require_admin(actor)
    username = _username(username)
    display_name = _display_name(display_name)
    role = _role(role)
    active = _active(active)
    if not isinstance(password, str) or not password:
        raise AdminError("password is required")
    try:
        locked_actor, _active_admins = _locked_user_and_admins(actor)
        if locked_actor.id != actor.id or locked_actor.role != "admin" or not locked_actor.active:
            raise PermissionError("only an Admin can manage administration")
        user = User(
            username=username,
            display_name=display_name,
            role=role,
            active=active,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        append_audit(
            actor=actor,
            action="user.create",
            entity_type="user",
            entity_id=user.id,
            details={"role": role, "active": active},
        )
        db.session.commit()
        return user
    except Exception:
        db.session.rollback()
        raise


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
    """Update a user while serializing all active-Admin mutations."""
    _require_admin(actor)
    try:
        locked, admins = _locked_user_and_admins(user)
        if not any(item.id == actor.id for item in admins):
            raise PermissionError("only an Admin can manage administration")
        next_username = locked.username if username is _UNSET else _username(username)
        next_display_name = locked.display_name if display_name is _UNSET else _display_name(display_name)
        next_role = locked.role if role is _UNSET else _role(role)
        next_active = locked.active if active is _UNSET else _active(active)
        if locked.active and locked.role == "admin" and (not next_active or next_role != "admin"):
            if len(admins) <= 1:
                raise FinalAdminError("the final active Admin cannot be disabled or demoted")
        if password is not _UNSET:
            if not isinstance(password, str) or not password:
                raise AdminError("password must not be empty")
            locked.set_password(password)
        locked.username = next_username
        locked.display_name = next_display_name
        locked.role = next_role
        locked.active = next_active
        append_audit(
            actor=actor,
            action="user.update",
            entity_type="user",
            entity_id=locked.id,
            details={"role": next_role, "active": next_active},
        )
        db.session.commit()
        return locked
    except Exception:
        db.session.rollback()
        raise


def disable_user(*, actor: User, user: User | int) -> User:
    return update_user(actor=actor, user=user, active=False)


def enable_user(*, actor: User, user: User | int) -> User:
    return update_user(actor=actor, user=user, active=True)


def list_users() -> list[User]:
    return list(db.session.scalars(select(User).order_by(User.username, User.id)))


_SAFE_DETAIL_KEYS = frozenset(
    {
        "source_id",
        "target_id",
        "capture_id",
        "capture_count",
        "comparison_set_id",
        "frame_count",
        "frame_ids",
        "format",
        "rendered_version",
        "version",
        "byte_count",
        "sha256",
        "role",
        "active",
    }
)


def _safe_details(details: object) -> dict[str, object]:
    if not isinstance(details, Mapping):
        return {}
    result: dict[str, object] = {}
    for key, value in details.items():
        if key not in _SAFE_DETAIL_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        elif isinstance(value, list) and all(isinstance(item, int) for item in value):
            result[key] = value
    return result


def list_audit_events(*, limit: int = 50, before_id: int | None = None) -> list[AuditEvent]:
    """Return a hard-bounded newest-first audit page without exposing PII."""
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        limit = 50
    limit = max(1, min(limit, 100))
    statement = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if before_id is not None and isinstance(before_id, int) and before_id > 0:
        statement = statement.where(AuditEvent.id < before_id)
    return list(db.session.scalars(statement))


def audit_rows(*, limit: int = 50, before_id: int | None = None) -> list[dict[str, object]]:
    return [
        {
            "id": event.id,
            "created_at": event.created_at,
            "action": event.action,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "details": _safe_details(event.details),
        }
        for event in list_audit_events(limit=limit, before_id=before_id)
    ]


@admin_bp.get("")
@admin_bp.get("/")
@admin_required
def index():
    return redirect(url_for("admin.users"))


@admin_bp.get("/users")
@admin_required
def users():
    return render_template("admin/users.html", users=list_users())


@admin_bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def new_user():
    form = UserForm()
    if request.method == "GET":
        form.role.data = "viewer"
        form.active.data = True
    if form.validate_on_submit():
        try:
            create_user(
                actor=current_user,
                username=form.username.data,
                display_name=form.display_name.data,
                password=form.password.data,
                role=form.role.data,
                active=form.active.data,
            )
        except PermissionError:
            abort(403)
        except (AdminError, IntegrityError) as exc:
            form.username.errors.append(str(exc) if isinstance(exc, AdminError) else "username already exists")
        else:
            flash("User created.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, heading="New user"), (400 if request.method == "POST" else 200)


@admin_bp.route("/users/<int:user_pk>/edit", methods=["GET", "POST"])
@admin_bp.route("/users/<int:user_pk>", methods=["GET", "POST"])
@admin_required
def edit_user(user_pk: int):
    user = db.session.get(User, user_pk)
    if user is None:
        abort(404)
    form = UserForm(obj=user)
    if request.method == "GET":
        form.password.data = ""
    if form.validate_on_submit():
        try:
            update_user(
                actor=current_user,
                user=user,
                username=form.username.data,
                display_name=form.display_name.data,
                password=form.password.data or _UNSET,
                role=form.role.data,
                active=form.active.data,
            )
        except FinalAdminError as exc:
            form.role.errors.append(str(exc))
        except PermissionError:
            abort(403)
        except (AdminError, IntegrityError) as exc:
            form.username.errors.append(str(exc) if isinstance(exc, AdminError) else "username already exists")
        else:
            flash("User updated.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, heading="Edit user", user=user), (400 if request.method == "POST" else 200)


@admin_bp.post("/users/<int:user_pk>/disable")
@admin_required
def disable_user_route(user_pk: int):
    try:
        disable_user(actor=current_user, user=user_pk)
    except FinalAdminError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.users")), 409
    except PermissionError:
        abort(403)
    except AdminError:
        abort(404)
    flash("User disabled.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<int:user_pk>/enable")
@admin_required
def enable_user_route(user_pk: int):
    try:
        enable_user(actor=current_user, user=user_pk)
    except PermissionError:
        abort(403)
    except AdminError:
        abort(404)
    flash("User enabled.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.get("/shot-types")
@admin_required
def shot_types():
    values = list(db.session.scalars(select(ShotType).order_by(ShotType.state, ShotType.name, ShotType.id)))
    canonical = [item for item in values if item.state == "canonical"]
    return render_template("admin/shot_types.html", shot_types=values, canonical_shot_types=canonical)


@admin_bp.post("/shot-types/<int:shot_type_pk>/promote")
@admin_required
def promote_shot_type_route(shot_type_pk: int):
    try:
        promote_shot_type(actor=current_user, shot_type_id=shot_type_pk)
    except PermissionError:
        abort(403)
    except ShotTypeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.shot_types")), 400
    flash("Shot Type promoted.", "success")
    return redirect(url_for("admin.shot_types"))


@admin_bp.post("/shot-types/<int:shot_type_pk>/merge")
@admin_required
def merge_shot_type_route(shot_type_pk: int):
    form = MergeShotTypeForm()
    canonical = list(
        db.session.scalars(
            select(ShotType).where(ShotType.state == "canonical", ShotType.id != shot_type_pk).order_by(ShotType.name)
        )
    )
    form.target_id.choices = [(item.id, item.name) for item in canonical]
    if form.validate_on_submit():
        try:
            merge_shot_type(actor=current_user, source_id=shot_type_pk, target_id=form.target_id.data)
        except PermissionError:
            abort(403)
        except ShotTypeError as exc:
            form.target_id.errors.append(str(exc))
        else:
            flash("Shot Type merged.", "success")
            return redirect(url_for("admin.shot_types"))
    return render_template(
        "admin/shot_types.html",
        shot_types=list(db.session.scalars(select(ShotType).order_by(ShotType.state, ShotType.name, ShotType.id))),
        canonical_shot_types=canonical,
        merge_form=form,
    ), 400


@admin_bp.get("/audit")
@admin_required
def audit():
    return render_template(
        "admin/audit.html",
        rows=audit_rows(limit=request.args.get("limit", 50)),
    )
