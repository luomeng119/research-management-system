from __future__ import annotations

import hmac
import secrets

from flask import current_app, jsonify, request, session

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _submitted_token() -> str:
    return request.form.get("_csrf_token", "") or request.headers.get("X-CSRF-Token", "")


def protect_request():
    if not current_app.config.get("CSRF_ENABLED") or request.method not in UNSAFE_METHODS:
        return None
    expected = session.get("csrf_token", "")
    submitted = _submitted_token()
    if expected and submitted and hmac.compare_digest(str(expected), str(submitted)):
        return None
    _record_rejection()
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": {"code": "CSRF_REJECTED", "message": "请求验证失败", "requestId": getattr(request, "request_id", None)}}), 403
    return "请求验证失败", 403


def _record_rejection() -> None:
    service = current_app.extensions.get("audit_service")
    engine = current_app.extensions.get("database_engine")
    if service and engine:
        try:
            with engine.begin() as connection:
                service.record(
                    connection,
                    event_name="csrf_rejected",
                    user_id=session.get("user_id"),
                    object_type="REQUEST",
                    object_id=request.endpoint,
                    result="FAILURE",
                    request_id=getattr(request, "request_id", "unknown"),
                    duration_ms=0,
                    error_code="CSRF_REJECTED",
                    properties={"method": request.method, "endpoint": request.endpoint or "unknown"},
                )
        except Exception:
            current_app.logger.warning("csrf rejection audit unavailable")
    current_app.logger.warning(
        "csrf rejected",
        extra={
            "request_id": getattr(request, "request_id", None),
            "user_id": session.get("user_id"),
            "app_module": "security",
            "action": "csrf_rejected",
            "object_id": request.endpoint,
            "duration_ms": 0,
            "result": "FAILURE",
            "error_code": "CSRF_REJECTED",
        },
    )
