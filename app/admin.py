"""Admin workflows for Shot Types and bounded audit browsing."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from wtforms import SelectField, SubmitField
from wtforms.validators import DataRequired

from app.audit import bounded_audit_details, list_audit_events
from app.auth import (
    AdminError,
    FinalAdminError,
    UserAdminForm as UserForm,
    admin_required,
    create_user,
    disable_user,
    enable_user,
    list_users,
    update_user,
)
from app.captures import ShotTypeError, merge_shot_type, promote_shot_type
from app.db import db
from app.models import ShotType, User


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


class MergeShotTypeForm(FlaskForm):
    target_id = SelectField("Canonical Shot Type", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Merge")


def audit_rows(*, limit: int = 50, before_id: int | None = None) -> list[dict[str, object]]:
    return [
        {
            "id": event.id,
            "created_at": event.created_at,
            "action": event.action,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "actor": event.actor.username if event.actor is not None else "system/bootstrap",
            "actor_id": event.actor_id,
            "details": bounded_audit_details(event.details),
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
            form.username.errors.append(
                str(exc) if isinstance(exc, AdminError) else "username already exists"
            )
        else:
            flash("User created.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, heading="New user"), (
        400 if request.method == "POST" else 200
    )


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
                password=form.password.data or None,
                role=form.role.data,
                active=form.active.data,
            )
        except FinalAdminError as exc:
            form.role.errors.append(str(exc))
        except PermissionError:
            abort(403)
        except (AdminError, IntegrityError) as exc:
            form.username.errors.append(
                str(exc) if isinstance(exc, AdminError) else "username already exists"
            )
        else:
            flash("User updated.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, heading="Edit user", user=user), (
        400 if request.method == "POST" else 200
    )


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
            select(ShotType)
            .where(ShotType.state == "canonical", ShotType.id != shot_type_pk)
            .order_by(ShotType.name)
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
