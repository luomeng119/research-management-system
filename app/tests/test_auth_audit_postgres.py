from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import timedelta

import sqlalchemy as sa
import pytest

from app import create_app
from app.repositories.audit import AuditRepository
from app.repositories.users import UsersRepository
from app.security.auth import BUSINESS_USER, SYSTEM_MAINTAINER, hash_password
from app.services.audit import AuditService


def _csrf(client, path="/auth/login") -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="_csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def _login(client, username: str, password: str):
    return client.post(
        "/auth/login",
        data={
            "username": username,
            "password": password,
            "_csrf_token": _csrf(client),
        },
    )


def test_postgres_login_upgrade_session_revocation_and_append_only_audit(tmp_path):
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("requires the isolated PostgreSQL contract runner")
    engine = sa.create_engine(os.environ["TEST_DATABASE_URL"])
    users = sa.Table("users", sa.MetaData(), autoload_with=engine)
    audit_events = sa.Table("audit_events", sa.MetaData(), autoload_with=engine)
    suffix = uuid.uuid4().hex[:10]
    username = f"legacy_{suffix}"
    maintainer = f"maint_{suffix}"
    password = "LegacyPassword123"
    salt = "a" * 32
    legacy_hash = salt + hashlib.sha256((salt + password).encode()).hexdigest()

    with engine.begin() as connection:
        connection.execute(
            users.insert(),
            [
                {
                    "username": username,
                    "password": legacy_hash,
                    "role": BUSINESS_USER,
                    "name": "PostgreSQL business user",
                    "status": "active",
                    "must_change_password": False,
                    "version": 1,
                },
                {
                    "username": maintainer,
                    "password": hash_password("MaintainerPass123"),
                    "role": SYSTEM_MAINTAINER,
                    "name": "PostgreSQL maintainer",
                    "status": "active",
                    "must_change_password": False,
                    "version": 1,
                },
            ],
        )

    repository = UsersRepository(engine)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "postgres-contract-secret",
            "DATA_DIR": str(tmp_path / "data"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
            "DOCUMENTS_DIR": str(tmp_path / "documents"),
            "SESSION_FILE_DIR": str(tmp_path / "sessions"),
            "DATABASE_ENGINE": engine,
            "USERS_REPOSITORY": repository,
            "AUDIT_SERVICE": AuditService(AuditRepository(engine), app_version="test-v1"),
            "SECURITY_AUTH_ENABLED": True,
            "CSRF_ENABLED": True,
            "LOGIN_RATE_LIMIT_ATTEMPTS": 5,
            "LOGIN_RATE_LIMIT_WINDOW_SECONDS": 900,
            "PERMANENT_SESSION_LIFETIME": timedelta(minutes=30),
            "SESSION_COOKIE_SECURE": False,
            "LOG_FILE": str(tmp_path / "app.jsonl"),
        }
    )
    business_client = app.test_client()
    maintainer_client = app.test_client()

    assert _login(business_client, username, password).status_code == 302
    upgraded = repository.get_by_username(username)
    assert upgraded["password"].startswith("$2")
    with business_client.session_transaction() as active_session:
        assert active_session["account_version"] == upgraded["version"]

    assert _login(maintainer_client, maintainer, "MaintainerPass123").status_code == 302
    token = _csrf(maintainer_client, "/users/change-password")
    assert maintainer_client.post(
        f"/users/reject/{username}", data={"_csrf_token": token}
    ).status_code == 200
    revoked = business_client.get("/", follow_redirects=False)
    assert revoked.status_code == 302
    assert revoked.headers["Location"].endswith("/auth/login")

    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(audit_events.c.action, audit_events.c.result)
            .where(audit_events.c.actor_user_id.in_([upgraded["id"], repository.get_by_username(maintainer)["id"]]))
        ).all()
    assert ("login_completed", "SUCCESS") in rows
    assert ("account_operation_completed", "SUCCESS") in rows

    with pytest.raises(sa.exc.DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                audit_events.update()
                .where(audit_events.c.actor_user_id == upgraded["id"])
                .values(result="FAILURE")
            )
