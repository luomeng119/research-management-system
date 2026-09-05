"""Retained project pages against PostgreSQL, real authentication and services."""

import json
import io
import os
from pathlib import Path
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import subprocess
from html.parser import HTMLParser

import pytest
import sqlalchemy as sa

from app import create_app
from app.security.auth import hash_password


@pytest.mark.parametrize("category,prefix", [
    ("GENERAL_RESEARCH", "projects"),
    ("SECURITY_CONFIDENTIALITY", "security_projects"),
    ("CRYPTO_APPLICATION", "crypto_projects"),
])
@pytest.mark.parametrize("operation", ["detail", "upload", "path_versions"])
def test_retained_project_reads_and_uploads(tmp_path, category, prefix, operation):
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
        if operation == "path_versions":
            service = app.extensions["file_service"]
            barrier = Barrier(2)
            def write_path(payload):
                barrier.wait(timeout=10)
                return service.upload_project_path(
                    io.BytesIO(payload), original_name="input.txt", folder="任务输入文件",
                    project_id=project["businessId"], actor_user_id=user_id,
                    request_id="project-path-concurrent",
                )
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(write_path, [b"first upload", b"second upload"]))
            assert results[0]["fileId"] == results[1]["fileId"]
            assert sorted(result["versionNo"] for result in results) == [1, 2]
            assert {(service.storage_root / result["storagePath"]).read_bytes()
                    for result in results} == {b"first upload", b"second upload"}
            assert len(service.list_for_object(
                object_type="PROJECT", object_id=project["businessId"],
            )) == 1
        if operation == "upload":
            csrf_page = client.get("/users/change-password")
            csrf = re.search(r'name="_csrf_token" value="([^"]+)"', csrf_page.text)[1]
            uploaded = client.post(f"/{prefix}/upload/{project['businessId']}",
                                   data={"folder": "任务输入文件",
                                         "file": (io.BytesIO(b"research input"), "input.txt")},
                                   headers={"X-CSRF-Token": csrf})
            assert uploaded.status_code == 200
            assert uploaded.json["success"] is True
            files = app.extensions["file_service"].list_for_object(
                object_type="PROJECT", object_id=project["businessId"],
            )
            assert len(files) == 1
            assert files[0]["originalName"] == "input.txt"
            assert files[0]["versionNo"] == 1
            refreshed = client.get(f"/{prefix}/detail/{project['businessId']}")
            tree = json.loads(re.search(r"var folderData = (.*);", refreshed.text)[1])
            assert tree[0]["files"][0]["path"] == "任务输入文件/input.txt"
            downloaded = client.get(
                f"/{prefix}/download/{project['businessId']}/任务输入文件/input.txt",
                follow_redirects=True,
            )
            assert downloaded.status_code == 200
            assert downloaded.data == b"research input"
            render_code = re.search(r"function buildNode\(node\) \{.*?\n\}\n", refreshed.text, re.S)[0]
            escape_code = re.search(r"function escapeEq\(value\) \{.*?\n\}\n", refreshed.text, re.S)[0]
            rendered = subprocess.run(["node", "-e", """
const fs = require('fs'); const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = {projectUpdateUrl: input.prefix};
vm.createContext(context); vm.runInContext(input.code, context);
process.stdout.write(context.buildNode(input.tree));
"""], input=json.dumps({"code": escape_code + render_code, "prefix": "/" + prefix,
                         "tree": {**tree[0], "name": '<svg onload="bad()">',
                                  "files": [{**tree[0]["files"][0], "name": '<svg onload="bad()">.txt'}]}}),
                                      text=True, capture_output=True, check=True)
            class Links(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.hrefs = []
                    self.tags = []
                    self.preview_calls = []
                def handle_starttag(self, tag, attrs):
                    self.tags.append(tag)
                    if tag == "a":
                        self.hrefs.append(dict(attrs).get("href", ""))
                        if dict(attrs).get("onclick"):
                            self.preview_calls.append(dict(attrs)["onclick"])
            links = Links()
            links.feed(rendered.stdout)
            assert "svg" not in links.tags
            assert any(href.startswith(f"/{prefix}/download/") for href in links.hrefs)
            invoked = subprocess.run(["node", "-e", """
const fs = require('fs'); const vm = require('vm');
const callback = (...args) => process.stdout.write(JSON.stringify(args));
vm.runInNewContext(fs.readFileSync(0, 'utf8'), {openControlledPreview: callback});
"""], input=links.preview_calls[0], text=True, capture_output=True, check=True)
            assert json.loads(invoked.stdout)[:5] == [
                files[0]["fileId"], 1, "PROJECT", project["businessId"], '<svg onload="bad()">.txt',
            ]
            assert json.loads(invoked.stdout)[5] in links.hrefs
            preview = client.get("/preview/file", query_string={
                "fileId": files[0]["fileId"], "versionNo": 1,
                "objectType": "PROJECT", "objectId": project["businessId"],
            }, follow_redirects=True)
            assert preview.status_code == 200
            assert preview.json["type"] == "text" and preview.json["content"] == "research input"
            second = client.post(f"/{prefix}/upload/{project['businessId']}",
                                 data={"folder": "任务输入文件",
                                       "file": (io.BytesIO(b"second revision"), "input.txt")},
                                 headers={"X-CSRF-Token": csrf})
            assert second.status_code == 200 and second.json["versionNo"] == 2
            assert second.json["fileId"] == files[0]["fileId"]
            latest = client.get(f"/{prefix}/download/{project['businessId']}/任务输入文件/input.txt",
                                follow_redirects=True)
            assert latest.data == b"second revision"
            original = client.get(f"/api/files/{files[0]['fileId']}/versions/1/download",
                                  query_string={"objectType": "PROJECT", "objectId": project["businessId"]})
            assert original.status_code == 200 and original.data == b"research input"
            wrong_prefix = "crypto_projects" if prefix == "projects" else "projects"
            rejected = client.post(f"/{wrong_prefix}/upload/{project['businessId']}",
                                   data={"folder": "任务输入文件",
                                         "file": (io.BytesIO(b"wrong category"), "input.txt")},
                                   headers={"X-CSRF-Token": csrf})
            assert rejected.status_code == 404
            unsafe = client.post(f"/{prefix}/upload/{project['businessId']}",
                                 data={"folder": "../outside",
                                       "file": (io.BytesIO(b"invalid"), "input.txt")},
                                 headers={"X-CSRF-Token": csrf})
            assert unsafe.status_code == 400
            assert app.extensions["file_service"].list_for_object(
                object_type="PROJECT", object_id=project["businessId"],
            )[0]["versionNo"] == 2
    finally:
        engine.dispose()
