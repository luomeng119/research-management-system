from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app import create_app
from app.repositories.audit import AuditRepository
from app.repositories.users import UsersRepository
from app.security.auth import (
    BUSINESS_USER,
    SYSTEM_MAINTAINER,
    LoginRateLimiter,
    hash_password,
    normalize_role,
    validate_password,
    verify_password,
)
from app.security.logging import RedactingJsonFormatter
from app.services.audit import AuditEventError, AuditService


def _schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text, nullable=False, unique=True),
        sa.Column("password", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("name", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    sa.Table(
        "audit_events",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_user_id", sa.Integer),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("object_type", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text),
        sa.Column("result", sa.Text, nullable=False),
        sa.Column("request_id", sa.Text),
        sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)


@pytest.fixture()
def engine():
    value = sa.create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _schema(value)
    return value


@pytest.fixture()
def repositories(engine):
    users = UsersRepository(engine)
    audit = AuditService(AuditRepository(engine), app_version="test-v1")
    return users, audit


def _insert_user(
    engine,
    *,
    username="alice",
    password="ValidPassword123",
    stored_password=None,
    role=BUSINESS_USER,
    status="active",
    must_change_password=False,
):
    users = sa.Table("users", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        result = connection.execute(
            users.insert()
            .values(
                username=username,
                password=stored_password or hash_password(password),
                role=role,
                name=f"{username}-display",
                status=status,
                must_change_password=must_change_password,
                version=1,
            )
            .returning(users.c.id)
        )
        return result.scalar_one()


@pytest.fixture()
def app(tmp_path, engine, repositories):
    users, audit = repositories
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-stable-secret",
            "DATA_DIR": str(tmp_path / "data"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
            "DOCUMENTS_DIR": str(tmp_path / "documents"),
            "SESSION_FILE_DIR": str(tmp_path / "sessions"),
            "DATABASE_ENGINE": engine,
            "USERS_REPOSITORY": users,
            "AUDIT_SERVICE": audit,
            "SECURITY_AUTH_ENABLED": True,
            "CSRF_ENABLED": True,
            "LOGIN_RATE_LIMIT_ATTEMPTS": 2,
            "LOGIN_RATE_LIMIT_WINDOW_SECONDS": 900,
            "PERMANENT_SESSION_LIFETIME": timedelta(minutes=30),
            "SESSION_COOKIE_SECURE": False,
            "LOG_FILE": str(tmp_path / "app.jsonl"),
        }
    )


def _csrf(client, path="/auth/login") -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="_csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def _login(client, username="alice", password="ValidPassword123"):
    token = _csrf(client)
    return client.post(
        "/auth/login",
        data={"username": username, "password": password, "_csrf_token": token},
    )


def _audit_rows(engine):
    table = sa.Table("audit_events", sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table)).mappings()]


def test_passwords_use_bcrypt_and_legacy_sha256_upgrades_once(engine):
    salt = "a" * 32
    legacy = salt + hashlib.sha256((salt + "LegacyPassword123").encode()).hexdigest()
    user_id = _insert_user(
        engine,
        username="legacy",
        stored_password=legacy,
    )
    repository = UsersRepository(engine)

    first = repository.authenticate("legacy", "LegacyPassword123")
    upgraded = repository.get_by_id(user_id)
    second = repository.authenticate("legacy", "LegacyPassword123")

    assert first is not None and first.password_upgraded is True
    assert upgraded["password"].startswith("$2")
    assert verify_password("LegacyPassword123", upgraded["password"])[0] is True
    assert second is not None and second.password_upgraded is False


def test_legacy_plaintext_uses_safe_comparison_and_upgrades(engine):
    _insert_user(engine, username="plain", stored_password="LegacyPassword123")
    repository = UsersRepository(engine)

    assert repository.authenticate("plain", "wrong") is None
    assert repository.authenticate("plain", "LegacyPassword123").password_upgraded is True
    assert repository.get_by_username("plain")["password"].startswith("$2")


@pytest.mark.parametrize(
    "password",
    ["short1", "onlyletterslong", "123456789012", "密" * 30 + "a1"],
)
def test_password_rule_rejects_weak_or_over_72_byte_values(password):
    with pytest.raises(ValueError):
        validate_password(password)


def test_only_two_formal_roles_and_legacy_values_are_normalized():
    assert normalize_role("用户") == BUSINESS_USER
    assert normalize_role("管理员") == SYSTEM_MAINTAINER
    assert normalize_role(BUSINESS_USER) == BUSINESS_USER
    with pytest.raises(ValueError):
        normalize_role("APPROVER")


def test_unauthenticated_page_redirects_and_api_returns_401(app):
    client = app.test_client()

    assert client.get("/projects/").status_code == 302
    response = client.get("/api/tree")
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "AUTH_REQUIRED"


def test_login_and_other_unsafe_requests_require_csrf(app, engine):
    _insert_user(engine)
    client = app.test_client()

    rejected_login = client.post(
        "/auth/login", data={"username": "alice", "password": "ValidPassword123"}
    )
    assert rejected_login.status_code == 403
    assert _login(client).status_code == 302
    rejected_write = client.post("/users/approve/alice")
    assert rejected_write.status_code == 403
    assert any(row["action"] == "csrf_rejected" for row in _audit_rows(engine))


def test_unknown_inactive_and_wrong_password_share_one_message(app, engine):
    _insert_user(engine, username="alice")
    _insert_user(engine, username="disabled", status="disabled")

    messages = []
    for username, password in (
        ("missing", "WrongPassword123"),
        ("disabled", "ValidPassword123"),
        ("alice", "WrongPassword123"),
    ):
        client = app.test_client()
        response = _login(client, username, password)
        messages.append(response.get_data(as_text=True))

    assert all("用户名或密码错误" in body for body in messages)
    assert all("停用" not in body and "不存在" not in body for body in messages)


def test_login_rate_limit_returns_429_and_success_clears_counter(app, engine):
    _insert_user(engine)
    client = app.test_client()

    assert _login(client, password="WrongPassword123").status_code == 200
    assert _login(client).status_code == 302
    limiter = app.extensions["login_rate_limiter"]
    assert len(limiter) == 0
    token = _csrf(client, "/users/change-password")
    client.post("/auth/logout", data={"_csrf_token": token})

    assert _login(client, password="WrongPassword123").status_code == 200
    limited = _login(client, password="WrongPassword123")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0

    limiter.clear("127.0.0.1", "alice")
    assert _login(client).status_code == 302
    token = _csrf(client, "/users/change-password")
    client.post("/auth/logout", data={"_csrf_token": token})
    assert limiter.is_limited("127.0.0.1", "alice")[0] is False


def test_rate_limit_key_is_hmac_and_bounded():
    limiter = LoginRateLimiter("secret", attempts=1, window_seconds=900, max_entries=2)
    limiter.record_failure("10.0.0.1", " Alice ")
    limiter.record_failure("10.0.0.2", "bob")
    limiter.record_failure("10.0.0.3", "carol")

    assert len(limiter) == 2
    assert "alice" not in repr(limiter)
    assert "10.0.0.1" not in repr(limiter)


def test_rate_limiter_is_safe_under_concurrent_access():
    limiter = LoginRateLimiter("secret", attempts=200, window_seconds=900, max_entries=8)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda index: limiter.record_failure("10.0.0.1", f"user-{index % 12}"),
                range(400),
            )
        )

    assert len(results) == 400
    assert len(limiter) <= 8


def test_login_rotates_session_and_stores_minimal_identity(app, engine):
    user_id = _insert_user(engine)
    client = app.test_client()
    with client.session_transaction() as session:
        session["attacker_marker"] = "fixed"
        old_sid = session.sid

    assert _login(client).status_code == 302
    with client.session_transaction() as session:
        assert session.sid != old_sid
        assert set(session.keys()) == {
            "_permanent",
            "csrf_token",
            "user_id",
            "user",
            "name",
            "role",
            "must_change_password",
            "account_version",
        }
        assert session["user_id"] == user_id
        assert session["role"] == BUSINESS_USER


def test_disabled_or_password_reset_account_revokes_existing_session(app, engine):
    _insert_user(engine, username="alice")
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)
    alice = app.test_client()
    maint = app.test_client()
    assert _login(alice, "alice").status_code == 302
    assert _login(maint, "maint").status_code == 302
    token = _csrf(maint, "/users/change-password")

    disabled = maint.post("/users/reject/alice", data={"_csrf_token": token})

    assert disabled.status_code == 200
    revoked = alice.get("/", follow_redirects=False)
    assert revoked.status_code == 302
    assert revoked.headers["Location"].endswith("/auth/login")
    with alice.session_transaction() as active_session:
        assert "user_id" not in active_session

    token = _csrf(maint, "/users/change-password")
    assert maint.post("/users/approve/alice", data={"_csrf_token": token}).status_code == 200
    assert _login(alice, "alice").status_code == 302
    token = _csrf(maint, "/users/change-password")
    reset = maint.post(
        "/users/reset-password/alice",
        data={"new_password": "TemporaryPass789", "_csrf_token": token},
    )
    assert reset.status_code == 200
    assert alice.get("/", follow_redirects=False).headers["Location"].endswith("/auth/login")


def test_first_password_change_blocks_business_then_rotates_session(app, engine):
    _insert_user(engine, must_change_password=True)
    client = app.test_client()

    assert _login(client).headers["Location"].endswith("/users/change-password")
    blocked = client.get("/")
    assert blocked.status_code == 302
    assert blocked.headers["Location"].endswith("/users/change-password")
    with client.session_transaction() as session:
        old_sid = session.sid
    token = _csrf(client, "/users/change-password")
    changed = client.post(
        "/users/change-password",
        data={
            "old_password": "ValidPassword123",
            "new_password": "NewValidPassword456",
            "confirm_password": "NewValidPassword456",
            "_csrf_token": token,
        },
    )

    assert changed.status_code == 302
    assert changed.headers["Location"].endswith("/")
    with client.session_transaction() as session:
        assert session.sid != old_sid
        assert session["must_change_password"] is False


def test_business_accounts_share_permission_and_maintenance_isolated(app, engine):
    _insert_user(engine, username="alice", role=BUSINESS_USER)
    _insert_user(engine, username="bob", role=BUSINESS_USER)
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)

    for username in ("alice", "bob"):
        client = app.test_client()
        assert _login(client, username).status_code == 302
        token = _csrf(client, "/users/change-password")
        denied = client.post("/users/approve/bob", data={"_csrf_token": token})
        assert denied.status_code == 403

    maint = app.test_client()
    assert _login(maint, "maint").status_code == 302
    token = _csrf(maint, "/users/change-password")
    allowed = maint.post("/users/approve/bob", data={"_csrf_token": token})
    assert allowed.status_code == 200
    assert UsersRepository(engine).get_by_username("bob")["status"] == "active"
    rows = _audit_rows(engine)
    assert any(row["action"] == "maintenance_access_denied" for row in rows)
    assert any(row["action"] == "account_operation_completed" for row in rows)


def test_llm_maintenance_endpoints_reject_business_accounts(app, engine):
    _insert_user(engine, username="alice", role=BUSINESS_USER)
    client = app.test_client()
    assert _login(client, "alice").status_code == 302

    # The current delivery hides local-AI routes entirely before role checks.
    assert client.get("/api/llm/status").status_code == 404
    token = _csrf(client, "/users/change-password")
    assert client.post(
        "/api/llm/reload",
        json={"name": "corrector", "_csrf_token": token},
        headers={"X-CSRF-Token": token},
    ).status_code == 404


def test_business_templates_do_not_restore_legacy_role_or_leader_permissions():
    template_root = Path(__file__).parents[1] / "templates"
    guarded_templates = (
        "standards/index.html",
        "equipment/_unit_modal.html",
        "equipment/detail.html",
        "equipment/partial_table.html",
        "equipment/index.html",
        "equipment/index_table.html",
        "projects/detail.html",
        "projects/partial_table.html",
        "projects/index.html",
        "index.html",
    )

    for relative_path in guarded_templates:
        source = (template_root / relative_path).read_text(encoding="utf-8")
        assert "管理员" not in source, relative_path
        assert "project['leader'] == session.get('user')" not in source, relative_path
        assert "p['leader'] == session.get('user')" not in source, relative_path


def test_audit_contract_validates_fields_and_removes_sensitive_metadata(engine):
    service = AuditService(AuditRepository(engine), app_version="test-v1")
    with engine.begin() as connection:
        event_id = service.record(
            connection,
            event_name="login_completed",
            user_id=None,
            object_type="ACCOUNT",
            object_id="opaque-user-id",
            result="FAILURE",
            request_id="req-test",
            duration_ms=3,
            error_code="INVALID_CREDENTIALS",
            properties={"reason": "/private/secret/customer.pdf", "password": "must-disappear"},
        )

    row = _audit_rows(engine)[0]
    assert event_id == row["id"]
    assert row["metadata"]["duration_ms"] == 3
    assert row["metadata"]["error_code"] == "INVALID_CREDENTIALS"
    assert row["metadata"]["app_version"] == "test-v1"
    assert "password" not in row["metadata"]
    assert "/private/secret/customer.pdf" not in json.dumps(row["metadata"])
    with engine.begin() as connection:
        with pytest.raises(AuditEventError):
            service.record(
                connection,
                event_name="invented_event",
                user_id=None,
                object_type="ACCOUNT",
                result="SUCCESS",
                request_id="req-test-2",
                duration_ms=0,
            )


def test_high_risk_account_action_rolls_back_when_audit_fails(app, engine):
    _insert_user(engine, username="bob", status="disabled")
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)
    client = app.test_client()
    assert _login(client, "maint").status_code == 302

    class FailingAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    app.extensions["audit_service"] = FailingAudit()
    token = _csrf(client, "/users/change-password")
    response = client.post("/users/approve/bob", data={"_csrf_token": token})

    assert response.status_code == 503
    assert UsersRepository(engine).get_by_username("bob")["status"] == "disabled"
    assert "audit unavailable" not in response.get_data(as_text=True)


def test_password_change_rolls_back_when_its_audit_cannot_be_written(app, engine):
    _insert_user(engine, must_change_password=True)
    client = app.test_client()
    assert _login(client).status_code == 302

    class FailingAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    app.extensions["audit_service"] = FailingAudit()
    token = _csrf(client, "/users/change-password")
    response = client.post(
        "/users/change-password",
        data={
            "old_password": "ValidPassword123",
            "new_password": "NewValidPassword456",
            "confirm_password": "NewValidPassword456",
            "_csrf_token": token,
        },
    )

    assert response.status_code == 503
    repository = UsersRepository(engine)
    assert repository.authenticate("alice", "ValidPassword123") is not None
    assert repository.authenticate("alice", "NewValidPassword456") is None


def test_maintenance_password_reset_uses_bcrypt_forces_change_and_audits(app, engine):
    _insert_user(engine, username="bob")
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)
    client = app.test_client()
    assert _login(client, "maint").status_code == 302
    token = _csrf(client, "/users/change-password")

    response = client.post(
        "/users/reset-password/bob",
        data={"new_password": "TemporaryPass789", "_csrf_token": token},
    )

    assert response.status_code == 200
    bob = UsersRepository(engine).get_by_username("bob")
    assert bob["password"].startswith("$2")
    assert bob["must_change_password"] is True
    reset_events = [
        row for row in _audit_rows(engine)
        if row["action"] == "account_operation_completed"
        and row["metadata"].get("operation") == "reset_password"
    ]
    assert len(reset_events) == 1


def test_failed_account_operation_writes_controlled_failure_event(app, engine):
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)
    client = app.test_client()
    assert _login(client, "maint").status_code == 302
    token = _csrf(client, "/users/change-password")

    response = client.post(
        "/users/approve/missing-account", data={"_csrf_token": token}
    )

    assert response.status_code == 404
    failures = [
        row for row in _audit_rows(engine)
        if row["action"] == "account_operation_completed" and row["result"] == "FAILURE"
    ]
    assert len(failures) == 1
    assert failures[0]["metadata"]["error_code"] == "ACCOUNT_NOT_FOUND"


@pytest.mark.parametrize("route", ["reject", "delete"])
def test_maintainer_cannot_disable_self_and_attempt_is_audited(app, engine, route):
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)
    client = app.test_client()
    assert _login(client, "maint").status_code == 302
    token = _csrf(client, "/users/change-password")

    response = client.post(f"/users/{route}/maint", data={"_csrf_token": token})

    assert response.status_code == 409
    assert UsersRepository(engine).get_by_username("maint")["status"] == "active"
    failures = [
        row for row in _audit_rows(engine)
        if row["action"] == "account_operation_completed"
        and row["result"] == "FAILURE"
        and row["metadata"].get("operation") == "disable"
    ]
    assert len(failures) == 1
    assert failures[0]["metadata"]["error_code"] == "SELF_DISABLE_REJECTED"


@pytest.mark.parametrize(
    ("username", "new_password", "expected_status", "error_code"),
    [
        ("missing-account", "TemporaryPass789", 404, "ACCOUNT_NOT_FOUND"),
        ("bob", "weak", 422, "PASSWORD_POLICY_REJECTED"),
    ],
)
def test_failed_password_reset_writes_one_failure_event(
    app, engine, username, new_password, expected_status, error_code
):
    _insert_user(engine, username="bob")
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)
    client = app.test_client()
    assert _login(client, "maint").status_code == 302
    token = _csrf(client, "/users/change-password")

    response = client.post(
        f"/users/reset-password/{username}",
        data={"new_password": new_password, "_csrf_token": token},
    )

    assert response.status_code == expected_status
    failures = [
        row for row in _audit_rows(engine)
        if row["action"] == "account_operation_completed"
        and row["result"] == "FAILURE"
        and row["metadata"].get("operation") == "reset_password"
    ]
    assert len(failures) == 1
    assert failures[0]["metadata"]["error_code"] == error_code


def test_failed_login_remains_controlled_when_failure_audit_is_unavailable(app, engine):
    _insert_user(engine)

    class FailingAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError("internal audit detail")

    app.extensions["audit_service"] = FailingAudit()
    response = _login(app.test_client(), password="WrongPassword123")

    assert response.status_code == 200
    assert "用户名或密码错误" in response.get_data(as_text=True)
    assert "internal audit detail" not in response.get_data(as_text=True)


def test_database_failure_does_not_authenticate_or_leak_details(app):
    class FailingUsers:
        def authenticate(self, username, password):
            raise sa.exc.OperationalError("secret sql", {}, Exception("db down"))

    app.extensions["users_repository"] = FailingUsers()
    client = app.test_client()
    response = _login(client)

    assert response.status_code == 503
    assert "secret sql" not in response.get_data(as_text=True)
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_database_failure_invalidates_existing_session_without_leaking(app, engine):
    _insert_user(engine)
    client = app.test_client()
    assert _login(client).status_code == 302

    class FailingUsers:
        def get_by_id(self, user_id):
            raise sa.exc.OperationalError("private sql", {}, Exception("db down"))

    app.extensions["users_repository"] = FailingUsers()
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")
    assert "private sql" not in response.get_data(as_text=True)


def test_request_id_is_server_generated_even_when_client_supplies_header(app):
    first = app.test_client().get("/healthz", headers={"X-Request-ID": "attacker-value"})
    second = app.test_client().get("/healthz", headers={"X-Request-ID": "attacker-value"})

    assert first.headers["X-Request-ID"].startswith("req_")
    assert first.headers["X-Request-ID"] != "attacker-value"
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


def test_json_logging_redacts_sensitive_keys_and_values():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter(app_version="test-v1"))
    logger = logging.getLogger("t03-redaction-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "request failed at /private/customer.pdf",
        extra={
            "request_id": "req-1",
            "user_id": 7,
            "app_module": "auth",
            "action": "login",
            "duration_ms": 4,
            "result": "FAILURE",
            "error_code": "INVALID_CREDENTIALS",
            "password": "VisiblePassword123",
            "absolute_path": "/private/secret/customer.pdf",
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["request_id"] == "req-1"
    assert payload["app_version"] == "test-v1"
    rendered = stream.getvalue()
    assert "VisiblePassword123" not in rendered
    assert "/private/secret/customer.pdf" not in rendered
    assert "/private/customer.pdf" not in rendered
    assert "password" not in payload


def test_json_logging_does_not_emit_arbitrary_message_values():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter(app_version="test-v1"))
    logger = logging.getLogger("t03-message-allowlist-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.error("unexpected credential value 9841-unmarked")

    payload = json.loads(stream.getvalue())
    assert payload["message"] == "event"
    assert "9841-unmarked" not in stream.getvalue()


def test_configured_logging_blocks_app_root_and_module_message_bypass(app):
    distinctive_values = (
        "app-credential-9841",
        "root-credential-9842",
        "module-credential-9843",
    )

    app.logger.error(distinctive_values[0])
    logging.getLogger().error(distinctive_values[1])
    logging.getLogger("app.routes.utils").error(distinctive_values[2])
    for active_logger in (app.logger, logging.getLogger()):
        for handler in active_logger.handlers:
            handler.flush()

    rendered = Path(app.config["LOG_FILE"]).read_text(encoding="utf-8")
    assert all(value not in rendered for value in distinctive_values)
    payloads = [json.loads(line) for line in rendered.splitlines()]
    assert sum(payload["message"] == "event" for payload in payloads) >= 3
