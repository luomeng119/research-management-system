import builtins
import sqlite3

import pytest

from app import create_app


@pytest.fixture()
def app_factory(tmp_path, monkeypatch):
    def database_access_during_startup(*args, **kwargs):
        raise AssertionError("application startup must not access a database")

    real_import = builtins.__import__

    def import_without_optional_ocr(name, *args, **kwargs):
        if name == "rapidocr_onnxruntime":
            raise ImportError("optional OCR dependency is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", database_access_during_startup)
    monkeypatch.setattr(builtins, "__import__", import_without_optional_ocr)

    def factory():
        return create_app({
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "DATA_DIR": str(tmp_path / "data"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
            "DOCUMENTS_DIR": str(tmp_path / "documents"),
            "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        })

    return factory


def test_factory_starts_without_database_access_when_optional_ocr_is_missing(app_factory):
    app = app_factory()
    assert app.config["TESTING"] is True


def test_only_login_and_health_are_public(app_factory):
    app = app_factory()
    client = app.test_client()

    login_response = client.get("/auth/login")
    health_response = client.get("/healthz")

    assert login_response.status_code == 200
    assert health_response.status_code == 200
    assert health_response.get_json() == {"status": "ok"}

    protected_urls = (
        "/auth/register",
        "/projects/",
        "/security_projects/",
        "/crypto_projects/",
        "/experts/",
        "/utils/",
        "/expense/",
        "/api/tree",
        "/uploads/private.txt",
    )
    for url in protected_urls:
        response = client.get(url)
        assert response.status_code == 302, url
        assert response.headers["Location"].endswith("/auth/login"), url


def test_test_and_bare_upload_routes_are_not_registered(app_factory):
    app = app_factory()
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert "/test/document_preview" not in rules
    assert "/test/tree" not in rules
    assert "/uploads/<path:filename>" not in rules


def test_critical_existing_business_urls_remain_registered(app_factory):
    app = app_factory()
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert {
        "/projects/",
        "/security_projects/",
        "/crypto_projects/",
        "/experts/",
        "/utils/",
        "/expense/",
        "/tables",
        "/argumentation/<category>",
    } <= rules


def test_login_does_not_write_credentials_to_stdout(app_factory, monkeypatch, capsys):
    app = app_factory()
    class FakeUserModel:
        def get_by_username(self, username):
            return {
                "username": username,
                "name": "测试用户",
                "role": "用户",
                "status": "active",
            }

        def verify(self, username, password):
            return False

    from app.routes import auth

    monkeypatch.setattr(auth, "UserModel", FakeUserModel)
    secret = "credential-that-must-not-be-logged"

    response = app.test_client().post(
        "/auth/login",
        data={"username": "tester", "password": secret},
    )

    assert response.status_code == 200
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
