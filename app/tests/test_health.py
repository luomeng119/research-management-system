from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import sqlalchemy as sa
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app import create_app


HEAD_REVISION = "0013_report_redaction_snapshot"


def _runtime_paths_from_fresh_process(*, data_root: Path | None) -> dict[str, str]:
    environment = os.environ.copy()
    if data_root is None:
        environment.pop("APP_DATA_ROOT", None)
    else:
        environment["APP_DATA_ROOT"] = str(data_root)
    names = (
        "DATA_DIR",
        "UPLOAD_DIR",
        "DOCUMENTS_DIR",
        "BACKUP_DIR",
        "SESSION_FILE_DIR",
        "FILE_STORAGE_ROOT",
        "LOG_FILE",
    )
    code = (
        "import json, config; "
        f"print(json.dumps({{name: getattr(config, name) for name in {names!r}}}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _engine(*, revision: str = HEAD_REVISION) -> sa.Engine:
    engine = sa.create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )
    return engine


def _app(tmp_path, *, engine=None, **config):
    data_dir = tmp_path / "data"
    values = {
        "TESTING": True,
        "SECRET_KEY": "health-test",
        "DATABASE_ENGINE": engine,
        "DATA_DIR": str(data_dir),
        "SESSION_FILE_DIR": str(data_dir / "sessions"),
        "FILE_STORAGE_ROOT": str(data_dir / "files"),
        "SECURITY_AUTH_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    }
    values.update(config)
    return create_app(values)


def test_liveness_reports_process_without_database(tmp_path):
    client = _app(tmp_path).test_client()

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"status": "alive"}


def test_healthz_keeps_legacy_contract(tmp_path):
    client = _app(tmp_path).test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readiness_requires_database_engine(tmp_path):
    client = _app(tmp_path).test_client()

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "missing", "schema": "not_checked", "storage": "not_checked"},
    }


def test_readiness_accepts_database_schema_and_writable_storage(tmp_path):
    client = _app(tmp_path, engine=_engine()).test_client()

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ready",
        "checks": {"database": "ok", "schema": "ok", "storage": "ok"},
    }


def test_readiness_rejects_unavailable_database(tmp_path):
    engine = _engine()
    app = _app(tmp_path, engine=engine)

    @sa.event.listens_for(engine, "before_cursor_execute")
    def fail_database(*_args):
        raise OperationalError("SELECT 1", {}, RuntimeError("database down"))

    response = app.test_client().get("/health/ready")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable", "schema": "not_checked", "storage": "not_checked"},
    }


def test_readiness_rejects_schema_revision_mismatch(tmp_path):
    client = _app(tmp_path, engine=_engine(revision="old_revision")).test_client()

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "schema": "mismatch", "storage": "not_checked"},
    }


@pytest.mark.parametrize(
    "setting",
    (
        "DATA_DIR",
        "FILE_STORAGE_ROOT",
        "SESSION_FILE_DIR",
        "UPLOAD_DIR",
        "DOCUMENTS_DIR",
        "BACKUP_DIR",
    ),
)
def test_readiness_rejects_unwritable_storage_path(tmp_path, setting):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    app = _app(tmp_path, engine=_engine())
    app.config[setting] = str(blocked_parent / setting.lower())
    client = app.test_client()

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "schema": "ok", "storage": "unwritable"},
    }


class _ExplodingAssistant:
    def __getattribute__(self, _name):
        raise RuntimeError("AI must not be used by readiness checks")


def test_readiness_does_not_require_enabled_ai(tmp_path):
    client = _app(tmp_path, engine=_engine(), AI_PROVIDER="DISABLED").test_client()

    response = client.get("/health/ready")

    assert response.status_code == 200


def test_readiness_does_not_call_failing_ai_service(tmp_path):
    client = _app(
        tmp_path,
        engine=_engine(),
        AI_PROVIDER="LOCAL",
        ASSISTANT_SERVICE=_ExplodingAssistant(),
    ).test_client()

    response = client.get("/health/ready")

    assert response.status_code == 200


def test_non_testing_startup_requires_database_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "production-test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL must be configured"):
        create_app(
            {
                "TESTING": False,
                "DATA_DIR": str(tmp_path / "data"),
                "SESSION_FILE_DIR": str(tmp_path / "sessions"),
                "FILE_STORAGE_ROOT": str(tmp_path / "files"),
                "LOG_FILE": None,
                "AI_PROVIDER": "DISABLED",
            }
        )


def test_run_uses_single_process_waitress_with_bounded_threads(monkeypatch):
    import app as app_package

    monkeypatch.delenv("APP_BIND_HOST", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    application = object()
    monkeypatch.setattr(app_package, "create_app", lambda: application)
    sys.modules.pop("run", None)
    run_module = importlib.import_module("run")
    served = {}

    def capture_serve(app, **options):
        served["app"] = app
        served["options"] = options

    monkeypatch.setattr(run_module, "serve", capture_serve, raising=False)

    run_module.main()

    assert served == {
        "app": application,
        "options": {"host": "127.0.0.1", "port": 5001, "threads": 4},
    }


def test_run_allows_explicit_bind_host_and_port(monkeypatch):
    import app as app_package

    monkeypatch.setenv("APP_BIND_HOST", "10.10.0.5")
    monkeypatch.setenv("APP_PORT", "8080")
    application = object()
    monkeypatch.setattr(app_package, "create_app", lambda: application)
    sys.modules.pop("run", None)
    run_module = importlib.import_module("run")
    served = {}

    monkeypatch.setattr(
        run_module,
        "serve",
        lambda app, **options: served.update(app=app, options=options),
    )

    run_module.main()

    assert served == {
        "app": application,
        "options": {"host": "10.10.0.5", "port": 8080, "threads": 4},
    }


def test_app_data_root_contains_every_production_mutable_path(tmp_path):
    root = tmp_path / "runtime-data"

    paths = _runtime_paths_from_fresh_process(data_root=root)

    assert paths == {
        "DATA_DIR": str(root / "data"),
        "UPLOAD_DIR": str(root / "uploads"),
        "DOCUMENTS_DIR": str(root / "documents"),
        "BACKUP_DIR": str(root / "backups"),
        "SESSION_FILE_DIR": str(root / "data" / "flask_sessions"),
        "FILE_STORAGE_ROOT": str(root / "data" / "files"),
        "LOG_FILE": str(root / "data" / "logs" / "app.jsonl"),
    }


def test_unset_app_data_root_keeps_existing_project_defaults():
    import config

    paths = _runtime_paths_from_fresh_process(data_root=None)
    project_root = Path(config.__file__).resolve().parent

    assert paths == {
        "DATA_DIR": str(project_root / "data"),
        "UPLOAD_DIR": str(project_root / "uploads"),
        "DOCUMENTS_DIR": str(project_root / "documents"),
        "BACKUP_DIR": str(project_root / "backups"),
        "SESSION_FILE_DIR": str(project_root / "data" / "flask_sessions"),
        "FILE_STORAGE_ROOT": str(project_root / "data" / "files"),
        "LOG_FILE": str(project_root / "data" / "logs" / "app.jsonl"),
    }


def test_create_app_test_config_overrides_data_root_paths(tmp_path):
    overrides = {
        "DATA_DIR": str(tmp_path / "custom-data"),
        "UPLOAD_DIR": str(tmp_path / "custom-uploads"),
        "DOCUMENTS_DIR": str(tmp_path / "custom-documents"),
        "BACKUP_DIR": str(tmp_path / "custom-backups"),
        "SESSION_FILE_DIR": str(tmp_path / "custom-sessions"),
        "FILE_STORAGE_ROOT": str(tmp_path / "custom-files"),
        "LOG_FILE": None,
    }

    app = _app(tmp_path, **overrides)

    assert {name: app.config[name] for name in overrides} == overrides
