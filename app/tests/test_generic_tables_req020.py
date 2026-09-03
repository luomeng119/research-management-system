from __future__ import annotations

import io

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app import create_app
from app.tests.test_generic_tables_postgres import _schema


def _client(tmp_path):
    engine = sa.create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    _schema(engine)
    app = create_app({
        "TESTING": True, "SECRET_KEY": "generic-test", "DATABASE_ENGINE": engine,
        "DATA_DIR": str(tmp_path), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED", "LOG_FILE": None,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "张老师"
        session["user_id"] = 7
    return client


def test_req020_snapshot_current_and_history_readonly(tmp_path):
    client = _client(tmp_path)
    created = client.post("/api/generic-tables", json={"name": "科研台账"})
    assert created.status_code == 200
    table_id = created.get_json()["table_id"]
    versions = client.get(f"/api/generic-tables/{table_id}/versions").get_json()["versions"]
    current = versions[0]["version_id"]
    saved = client.post(
        f"/api/generic-tables/{table_id}/versions",
        json={"method": "snapshot", "source_version_id": current, "label": "初版"},
    )
    assert saved.status_code == 200
    body = saved.get_json()
    assert body["snapshot_id"] != body["new_current_id"]
    readonly = client.get(f"/tables/{table_id}/version/{body['snapshot_id']}/")
    assert readonly.status_code == 200
    assert "只读快照" in readonly.get_data(as_text=True)


def test_req020_locked_write_and_legacy_xls_are_rejected(tmp_path):
    client = _client(tmp_path)
    table_id = client.post("/api/generic-tables", json={"name": "科研台账"}).get_json()["table_id"]
    current = client.get(f"/api/generic-tables/{table_id}/versions").get_json()["versions"][0]["version_id"]
    snapshot = client.post(f"/api/generic-tables/{table_id}/versions", json={"method": "snapshot", "source_version_id": current}).get_json()["snapshot_id"]
    write = client.post(f"/api/generic-tables/versions/{snapshot}/rows", json={"row_data": {"name": "x"}})
    assert write.status_code == 409
    legacy = client.post(
        f"/api/generic-tables/versions/{snapshot}/rows/import",
        data={"file": (io.BytesIO(b"legacy"), "legacy.xls")},
        content_type="multipart/form-data",
    )
    assert legacy.status_code == 400
    assert ".xlsx" in legacy.get_json()["error"]
