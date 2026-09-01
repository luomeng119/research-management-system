# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from app.security.auth import current_identity, maintenance_required, rotate_session

bp = Blueprint("users", __name__, url_prefix="/users")


@bp.route("/")
@maintenance_required
def index():
    users = current_app.extensions["users_repository"].list_accounts()
    return render_template("users/index.html", users=users, directories=())


def _account_status_operation(username: str, status: str, operation: str):
    engine = current_app.extensions["database_engine"]
    repository = current_app.extensions["users_repository"]
    service = current_app.extensions["audit_service"]
    identity = current_identity()
    try:
        with engine.begin() as connection:
            if status == "disabled" and username == identity.username:
                service.record(
                    connection,
                    event_name="account_operation_completed",
                    user_id=identity.user_id,
                    object_type="ACCOUNT",
                    object_id=str(identity.user_id),
                    result="FAILURE",
                    request_id=getattr(request, "request_id", "unknown"),
                    duration_ms=0,
                    error_code="SELF_DISABLE_REJECTED",
                    properties={"operation": operation, "target_user_id": str(identity.user_id)},
                )
                self_disable = True
                missing = False
                user = None
            else:
                self_disable = False
                user = repository.set_status(connection, username, status)
            if user is None and not self_disable:
                service.record(
                    connection,
                    event_name="account_operation_completed",
                    user_id=identity.user_id,
                    object_type="ACCOUNT",
                    object_id=None,
                    result="FAILURE",
                    request_id=getattr(request, "request_id", "unknown"),
                    duration_ms=0,
                    error_code="ACCOUNT_NOT_FOUND",
                    properties={"operation": operation},
                )
                missing = True
            elif user is not None:
                missing = False
                service.record(
                    connection,
                    event_name="account_operation_completed",
                    user_id=identity.user_id,
                    object_type="ACCOUNT",
                    object_id=str(user["id"]),
                    result="SUCCESS",
                    request_id=getattr(request, "request_id", "unknown"),
                    duration_ms=0,
                    properties={"operation": operation, "target_user_id": str(user["id"])},
                )
        if self_disable:
            return jsonify({"success": False, "message": "不能停用当前账号"}), 409
        if missing:
            return jsonify({"success": False, "message": "账号不存在"}), 404
        return jsonify({"success": True, "message": "账号已更新"})
    except Exception:
        current_app.logger.error("account operation failed closed")
        return jsonify({"success": False, "message": "账号操作未完成"}), 503


@bp.route("/approve/<username>", methods=["POST"])
@maintenance_required
def approve(username):
    return _account_status_operation(username, "active", "enable")


@bp.route("/reject/<username>", methods=["POST"])
@maintenance_required
def reject(username):
    return _account_status_operation(username, "disabled", "disable")


@bp.route("/delete/<username>", methods=["POST"])
@maintenance_required
def delete(username):
    return _account_status_operation(username, "disabled", "disable")


@bp.route("/reset-password/<username>", methods=["POST"])
@maintenance_required
def reset_password(username):
    engine = current_app.extensions["database_engine"]
    repository = current_app.extensions["users_repository"]
    service = current_app.extensions["audit_service"]
    identity = current_identity()
    try:
        with engine.begin() as connection:
            error_code = None
            try:
                user = repository.reset_password(
                    connection, username, request.form.get("new_password", "")
                )
            except ValueError:
                user = repository.get_by_username(username, connection)
                error_code = "PASSWORD_POLICY_REJECTED"
            if user is None:
                error_code = "ACCOUNT_NOT_FOUND"
            service.record(
                connection,
                event_name="account_operation_completed",
                user_id=identity.user_id,
                object_type="ACCOUNT",
                object_id=str(user["id"]) if user else None,
                result="FAILURE" if error_code else "SUCCESS",
                request_id=getattr(request, "request_id", "unknown"),
                duration_ms=0,
                error_code=error_code,
                properties={
                    "operation": "reset_password",
                    **({"target_user_id": str(user["id"])} if user else {}),
                },
            )
        if error_code == "ACCOUNT_NOT_FOUND":
            return jsonify({"success": False, "message": "账号不存在"}), 404
        if error_code:
            return jsonify({"success": False, "message": "临时密码不符合规则"}), 422
        return jsonify({"success": True, "message": "密码已重置"})
    except Exception:
        current_app.logger.error("password reset failed closed")
        return jsonify({"success": False, "message": "密码重置未完成"}), 503


@bp.route("/permissions/<username>", methods=["GET", "POST"])
@maintenance_required
def permissions(username):
    return jsonify({"error": {"code": "NOT_SUPPORTED", "message": "V1 业务账号权限一致"}}), 404


@bp.route("/change-password", methods=["GET", "POST"])
def change_password():
    identity = current_identity()
    if identity is None:
        return redirect(url_for("auth.login"))
    if request.method == "GET":
        return render_template("users/change_password.html")

    old_password = request.form.get("old_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    repository = current_app.extensions["users_repository"]
    service = current_app.extensions["audit_service"]
    engine = current_app.extensions["database_engine"]
    error_code = None
    user = None
    try:
        with engine.begin() as connection:
            if new_password != confirm_password:
                error_code = "PASSWORD_CONFIRMATION_MISMATCH"
            else:
                try:
                    user = repository.change_password(
                        connection, identity.user_id, old_password, new_password
                    )
                except ValueError:
                    error_code = "PASSWORD_POLICY_REJECTED"
                if user is None and error_code is None:
                    error_code = "PASSWORD_CHANGE_REJECTED"
            service.record(
                connection,
                event_name="password_change_completed",
                user_id=identity.user_id,
                object_type="ACCOUNT",
                object_id=str(identity.user_id),
                result="FAILURE" if error_code else "SUCCESS",
                request_id=getattr(request, "request_id", "unknown"),
                duration_ms=0,
                error_code=error_code,
                properties={"reason": error_code.casefold() if error_code else "completed"},
            )
    except Exception:
        current_app.logger.error("password change transaction unavailable")
        return "密码修改服务暂不可用", 503
    if error_code:
        flash("原密码不正确或新密码不符合要求", "error")
        return redirect(url_for("users.change_password"))
    rotate_session(user, must_change_password=False)
    flash("密码修改成功", "success")
    return redirect(url_for("index"))
