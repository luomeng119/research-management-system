from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from functools import wraps
from typing import Any

import bcrypt
import sqlalchemy as sa
from flask import current_app, jsonify, redirect, request, session, url_for

BUSINESS_USER = "BUSINESS_USER"
SYSTEM_MAINTAINER = "SYSTEM_MAINTAINER"
FORMAL_ROLES = frozenset({BUSINESS_USER, SYSTEM_MAINTAINER})
LEGACY_ROLE_MAP = {"用户": BUSINESS_USER, "管理员": SYSTEM_MAINTAINER}


def normalize_role(value: str) -> str:
    normalized = LEGACY_ROLE_MAP.get(value, value)
    if normalized not in FORMAL_ROLES:
        raise ValueError("unsupported account role")
    return normalized


def validate_password(password: str) -> None:
    encoded = password.encode("utf-8")
    if len(password) < 12 or len(encoded) > 72:
        raise ValueError("password must be at least 12 characters and at most 72 bytes")
    if not any(character.isalpha() for character in password):
        raise ValueError("password must include a letter")
    if not any(character.isdigit() for character in password):
        raise ValueError("password must include a number")


def hash_password(password: str) -> str:
    validate_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """Return ``(valid, needs_bcrypt_upgrade)`` without exposing credentials."""
    encoded = password.encode("utf-8")
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        if len(encoded) > 72:
            return False, False
        try:
            return bcrypt.checkpw(encoded, stored.encode("ascii")), False
        except (ValueError, UnicodeError):
            return False, False
    if len(stored) == 96 and all(character in "0123456789abcdefABCDEF" for character in stored):
        salt = stored[:32]
        candidate = salt + hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate, stored), True
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8")), True


def normalize_username(value: str) -> str:
    return value.strip().casefold()


class LoginRateLimiter:
    """Single-process bounded login limiter; restart intentionally clears state."""

    def __init__(
        self,
        secret: str,
        *,
        attempts: int = 5,
        window_seconds: int = 900,
        max_entries: int = 2048,
        clock=time.monotonic,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._attempts = attempts
        self._window_seconds = window_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.RLock()

    def _key(self, address: str, username: str) -> str:
        payload = f"{address}\0{normalize_username(username)}".encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _active(self, key: str) -> deque[float]:
        now = self._clock()
        values = self._failures.setdefault(key, deque())
        while values and now - values[0] >= self._window_seconds:
            values.popleft()
        self._failures.move_to_end(key)
        return values

    def record_failure(self, address: str, username: str) -> tuple[bool, int]:
        with self._lock:
            key = self._key(address, username)
            values = self._active(key)
            values.append(self._clock())
            while len(self._failures) > self._max_entries:
                self._failures.popitem(last=False)
            return self.is_limited(address, username)

    def is_limited(self, address: str, username: str) -> tuple[bool, int]:
        with self._lock:
            key = self._key(address, username)
            values = self._active(key)
            if len(values) < self._attempts:
                return False, 0
            remaining = max(1, int(self._window_seconds - (self._clock() - values[0])))
            return True, remaining

    def clear(self, address: str, username: str) -> None:
        with self._lock:
            self._failures.pop(self._key(address, username), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._failures)

    def __repr__(self) -> str:
        return f"LoginRateLimiter(entries={len(self._failures)})"


@dataclass(frozen=True)
class Identity:
    user_id: int
    username: str
    display_name: str
    role: str
    account_version: int


def current_identity() -> Identity | None:
    if "user_id" not in session:
        return None
    try:
        identity = Identity(
            user_id=int(session["user_id"]),
            username=str(session["user"]),
            display_name=str(session.get("name") or session["user"]),
            role=normalize_role(str(session["role"])),
            account_version=int(session["account_version"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if current_app.config.get("SECURITY_AUTH_ENABLED"):
        repository = current_app.extensions.get("users_repository")
        try:
            user = repository.get_by_id(identity.user_id) if repository else None
        except sa.exc.SQLAlchemyError:
            current_app.logger.error("session validation database unavailable")
            session.clear()
            return None
        try:
            valid = (
                user is not None
                and user.get("status") == "active"
                and normalize_role(str(user["role"])) == identity.role
                and int(user["version"]) == identity.account_version
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            session.clear()
            return None
    return identity


def _api_response(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message, "requestId": getattr(request, "request_id", None)}}), status


def _unauthenticated():
    if request.path.startswith("/api/") or request.path.startswith("/users/api/"):
        return _api_response("AUTH_REQUIRED", "请先登录", 401)
    return redirect(url_for("auth.login"))


def _record_denied() -> None:
    service = current_app.extensions.get("audit_service")
    engine = current_app.extensions.get("database_engine")
    identity = current_identity()
    if not service or not engine:
        return
    try:
        with engine.begin() as connection:
            service.record(
                connection,
                event_name="maintenance_access_denied",
                user_id=identity.user_id if identity else None,
                object_type="REQUEST",
                object_id=request.endpoint,
                result="FAILURE",
                request_id=getattr(request, "request_id", "unknown"),
                duration_ms=0,
                error_code="FORBIDDEN",
                properties={"endpoint": request.endpoint or "unknown"},
            )
    except Exception:
        current_app.logger.warning("maintenance authorization audit unavailable")


def login_required(function):
    @wraps(function)
    def decorated(*args: Any, **kwargs: Any):
        if current_identity() is None:
            return _unauthenticated()
        return function(*args, **kwargs)

    return decorated


def business_required(function):
    @wraps(function)
    def decorated(*args: Any, **kwargs: Any):
        identity = current_identity()
        if identity is None:
            return _unauthenticated()
        if identity.role not in FORMAL_ROLES:
            return _api_response("FORBIDDEN", "无权访问", 403)
        return function(*args, **kwargs)

    return decorated


def maintenance_required(function):
    @wraps(function)
    def decorated(*args: Any, **kwargs: Any):
        identity = current_identity()
        if identity is None:
            return _unauthenticated()
        if identity.role != SYSTEM_MAINTAINER:
            _record_denied()
            return _api_response("FORBIDDEN", "需要系统维护权限", 403)
        return function(*args, **kwargs)

    return decorated


def rotate_session(identity: dict[str, Any], *, must_change_password: bool) -> None:
    session.clear()
    session.permanent = True
    session.update(
        user_id=identity["id"],
        user=identity["username"],
        name=identity.get("name") or identity["username"],
        role=normalize_role(identity["role"]),
        must_change_password=bool(must_change_password),
        account_version=int(identity["version"]),
        csrf_token=secrets.token_urlsafe(32),
    )
    current_app.session_interface.regenerate(session)
