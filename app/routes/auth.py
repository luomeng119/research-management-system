# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import time

import sqlalchemy as sa
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from app.security.auth import rotate_session

# Retained only for the isolated T01 test harness that has no PostgreSQL repository.
from app.models import UserModel

bp = Blueprint("auth", __name__, url_prefix="/auth")
GENERIC_LOGIN_ERROR = "用户名或密码错误"


def _client_address() -> str:
    return request.remote_addr or "unknown"


def _opaque_username(username: str) -> str:
    secret = str(current_app.config["SECRET_KEY"]).encode("utf-8")
    return hashlib.sha256(secret + username.strip().casefold().encode("utf-8")).hexdigest()


def _audit_login(*, result, error_code=None, properties=None, user_id=None, username="", duration_ms=0):
    service = current_app.extensions.get("audit_service")
    engine = current_app.extensions.get("database_engine")
    if not service or not engine:
        return
    with engine.begin() as connection:
        service.record(
            connection,
            event_name="login_completed",
            user_id=user_id,
            object_type="ACCOUNT",
            object_id=_opaque_username(username),
            result=result,
            request_id=getattr(request, "request_id", "unknown"),
            duration_ms=duration_ms,
            error_code=error_code,
            properties=properties,
        )


def _audit_login_failure_safely(**kwargs):
    try:
        _audit_login(result="FAILURE", **kwargs)
    except Exception:
        current_app.logger.warning("login failure audit unavailable")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not current_app.config.get("SECURITY_AUTH_ENABLED"):
        user_model = UserModel()
        user = user_model.get_by_username(username)
        if user and user_model.verify(username, password):
            session.clear()
            session.update(user=username, name=user.get("name"), role=user.get("role"))
            return redirect(url_for("index"))
        flash(GENERIC_LOGIN_ERROR, "error")
        return render_template("login.html")

    limiter = current_app.extensions["login_rate_limiter"]
    address = _client_address()
    limited, retry_after = limiter.is_limited(address, username)
    if limited:
        _audit_login_failure_safely(
            error_code="LOGIN_RATE_LIMITED",
            properties={"reason": "rate_limited"},
            username=username,
        )
        return render_template("login.html"), 429, {"Retry-After": str(retry_after)}

    started = time.monotonic()
    repository = current_app.extensions["users_repository"]
    try:
        authenticated = repository.authenticate(username, password)
    except sa.exc.SQLAlchemyError:
        current_app.logger.error("authentication database unavailable")
        return "认证服务暂不可用", 503

    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    if authenticated is None:
        limited, retry_after = limiter.record_failure(address, username)
        _audit_login_failure_safely(
            error_code="INVALID_CREDENTIALS",
            properties={"reason": "invalid_credentials"},
            username=username,
            duration_ms=duration_ms,
        )
        flash(GENERIC_LOGIN_ERROR, "error")
        if limited:
            return render_template("login.html"), 429, {"Retry-After": str(retry_after)}
        return render_template("login.html")

    user = authenticated.user
    try:
        _audit_login(
            result="SUCCESS",
            properties={"password_upgraded": authenticated.password_upgraded},
            user_id=user["id"],
            username=username,
            duration_ms=duration_ms,
        )
    except Exception:
        current_app.logger.error("successful login audit unavailable")
        return "认证服务暂不可用", 503
    rotate_session(user, must_change_password=user.get("must_change_password", False))
    limiter.clear(address, username)
    destination = "users.change_password" if user.get("must_change_password") else "index"
    return redirect(url_for(destination))


@bp.route("/logout", methods=["POST"])
def logout():
    service = current_app.extensions.get("audit_service")
    engine = current_app.extensions.get("database_engine")
    if service and engine:
        try:
            with engine.begin() as connection:
                service.record(
                    connection,
                    event_name="logout_completed",
                    user_id=session.get("user_id"),
                    object_type="SESSION",
                    object_id=None,
                    result="SUCCESS",
                    request_id=getattr(request, "request_id", "unknown"),
                    duration_ms=0,
                )
        except Exception:
            current_app.logger.warning("logout audit unavailable")
    session.clear()
    return redirect(url_for("auth.login"))
