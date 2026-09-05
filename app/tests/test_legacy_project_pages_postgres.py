"""Retained project pages against PostgreSQL, real authentication and services."""

import json
import os
from pathlib import Path
import re
import uuid

import pytest
import sqlalchemy as sa

from app import create_app
from app.security.auth import hash_password


@pytest.mark.parametrize("category,prefix", [
    ("GENERAL_RESEARCH", "projects"),
    ("SECURITY_CONFIDENTIALITY", "security_projects"),
    ("CRYPTO_APPLICATION", "crypto_projects"),
])
def test_retained_detail_reads_real_project_and_folders(tmp_path, category, prefix):
    if not os.environ.get("T02_ISOLATED_POSTGRES_ROOT"):
        pytest.skip("requires owned PostgreSQL harness")
    expected_data = Path(os.environ["T02_ISOLATED_POSTGRES_ROOT"]).resolve() / "data"
    owned_pid = (expected_data / "postmaster.pid").read_text().splitlines()
    expected_port = int(os.environ["T02_ISOLATED_POSTGRES_PORT"])
    expected_database = os.environ["T02_ISOLATED_POSTGRES_DATABASE"]
    assert Path(owned_pid[1]).resolve() == expected_data
    assert int(owned_pid[3]) == expected_port
    os.kill(int(owned_pid[0]), 0)
    url = sa.engine.make_url(os.environ["TEST_DATABASE_URL"])
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    assert url.port == expected_port and url.database == expected_database
    engine = sa.create_engine(url)
    username = "pages_" + uuid.uuid4().hex
    try:
        with engine.connect() as connection:
            database, port, address = connection.execute(sa.text(
                "SELECT current_database(), inet_server_port(), host(inet_server_addr())"
            )).one()
            assert database == expected_database and port == expected_port
            assert address in {"127.0.0.1", "::1"}
        users = sa.Table("users", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            user_id = connection.execute(users.insert().values(
                username=username, password=hash_password("TestPassword123!"),
                name="王老师", role="BUSINESS_USER", status="active",
                must_change_password=False, version=1,
            ).returning(users.c.id)).scalar_one()
        app = create_app({
            "TESTING": True, "SECRET_KEY": "retained-pages-test-only",
            "DATABASE_ENGINE": engine,
            "DATA_DIR": str(tmp_path / "data"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
            "DOCUMENTS_DIR": str(tmp_path / "documents"),
            "SESSION_FILE_DIR": str(tmp_path / "sessions"),
            "SECURITY_AUTH_ENABLED": True, "CSRF_ENABLED": True,
        })
        project = app.extensions["project_service"].create_standalone(
            category, {"name": "演练-三类旧项目详情", "leader": "王老师"},
            actor_user_id=user_id,
        )
        client = app.test_client()
        login = client.get("/auth/login")
        token = re.search(r'name="_csrf_token" value="([^"]+)"', login.text)[1]
        assert client.post("/auth/login", data={
            "username": username, "password": "TestPassword123!", "_csrf_token": token,
        }).status_code == 302
        response = client.get(f"/{prefix}/detail/{project['businessId']}")
        assert response.status_code == 200
        assert "演练-三类旧项目详情" in response.text
        assert project["businessId"] in response.text
        folders = json.loads(re.search(r"var folderData = (.*);", response.text)[1])
        assert [folder["name"] for folder in folders] == [
            "任务输入文件", "研究成果文件", "院内审查文件", "机关审查文件", "成果上报文件",
        ]
        stored = app.extensions["project_service"].get(project["id"])
        assert stored["category"] == category
        assert stored["businessId"] == project["businessId"]
    finally:
        engine.dispose()
