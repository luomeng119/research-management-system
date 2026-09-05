"""Retained project pages against PostgreSQL, real authentication and services."""

import json
import io
import os
from pathlib import Path
import re
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, get_ident
import subprocess
from html.parser import HTMLParser

import pytest
import sqlalchemy as sa

from app import create_app
from app.security.auth import hash_password
from app.services.files import FileServiceError


@pytest.mark.parametrize("category,prefix", [
    ("GENERAL_RESEARCH", "projects"),
    ("SECURITY_CONFIDENTIALITY", "security_projects"),
    ("CRYPTO_APPLICATION", "crypto_projects"),
])
@pytest.mark.parametrize("operation", ["detail", "upload", "path_versions", "folder_upload", "file_rename", "file_archive"])
def test_retained_project_reads_and_uploads(tmp_path, category, prefix, operation, monkeypatch):
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
        if operation == "folder_upload":
            csrf_page = client.get("/users/change-password")
            csrf = re.search(r'name="_csrf_token" value="([^"]+)"', csrf_page.text)[1]
            response = client.post(f"/{prefix}/upload_folder/{project['businessId']}", data={
                "files": [(io.BytesIO(b"direction a"), "资料包/方向甲/input.txt"),
                          (io.BytesIO(b"direction b"), "资料包/方向乙/input.txt")],
            }, headers={"X-CSRF-Token": csrf})
            assert response.status_code == 200 and response.json["success"] is True
            service = app.extensions["file_service"]
            paths = service.list_project_paths(project["businessId"])
            assert {item["path"] for item in paths} == {
                "任务输入文件/资料包/方向甲/input.txt", "任务输入文件/资料包/方向乙/input.txt",
            }
            for path, content in [("任务输入文件/资料包/方向甲/input.txt", b"direction a"),
                                  ("任务输入文件/资料包/方向乙/input.txt", b"direction b")]:
                downloaded = client.get(f"/{prefix}/download/{project['businessId']}/{path}", follow_redirects=True)
                assert downloaded.status_code == 200 and downloaded.data == content
            refreshed = client.get(f"/{prefix}/detail/{project['businessId']}")
            tree = json.loads(re.search(r"var folderData = (.*);", refreshed.text)[1])
            package = next(node for node in tree[0]["children"] if node["name"] == "资料包")
            assert {node["name"] for node in package["children"]} == {"方向甲", "方向乙"}
            mixed = client.post(f"/{prefix}/upload_folder/{project['businessId']}", data={
                "files": [(io.BytesIO(b"updated"), "资料包/方向甲/input.txt"),
                          (io.BytesIO(b"invalid"), "../outside.txt"),
                          (io.BytesIO(b"new valid"), "资料包/方向丙/input.txt")],
            }, headers={"X-CSRF-Token": csrf})
            assert mixed.status_code == 207 and mixed.json["success"] is False
            assert mixed.json["uploadedCount"] == 2 and mixed.json["failedCount"] == 1
            assert mixed.json["errors"][0]["path"] == "../outside.txt"
            paths = service.list_project_paths(project["businessId"])
            assert len(paths) == 3
            assert next(item for item in paths if item["path"] == "任务输入文件/资料包/方向甲/input.txt")["versionNo"] == 2
            assert not (tmp_path / "outside.txt").exists()
            submit_code = re.search(
                r"document.getElementById\('uploadForm'\).onsubmit = function\(e\).*?\n};",
                refreshed.text, re.S,
            )[0]
            submitted = subprocess.run(["node", "-e", """
const fs = require('fs'); const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const form = {}; const sent = {}; const state = {reload: false};
const elements = {uploadForm: form, folderInput: {files: [
  {name:'input.txt', webkitRelativePath:'资料包/方向甲/input.txt'},
  {name:'input.txt', webkitRelativePath:'资料包/方向乙/input.txt'}]}, uploadFolder: {value:'任务输入文件'}};
const context = {document:{getElementById:id=>elements[id]}, projectUpdateUrl:input.prefix,
  FormData:class {constructor(){this.entries=[];} append(...args){this.entries.push(args);}},
  fetch:(url, options)=>{sent.url=url; sent.entries=options.body.entries;
    return Promise.resolve({json:()=>Promise.resolve({success:false,uploadedCount:1,message:'一项成功、一项失败'})});},
  alert:message=>{state.message=message;}, location:{reload:()=>{state.reload=true;}}};
vm.createContext(context); vm.runInContext(input.code, context);
form.onsubmit.call(form,{preventDefault(){}});
setImmediate(()=>process.stdout.write(JSON.stringify({sent,state})));
"""], input=json.dumps({"code": submit_code, "prefix": "/" + prefix}), text=True,
                                      capture_output=True, check=True)
            browser_contract = json.loads(submitted.stdout)
            assert browser_contract["sent"]["url"] == f"/{prefix}/upload_folder/{project['businessId']}"
            assert [entry[2] for entry in browser_contract["sent"]["entries"] if entry[0] == "files"] == [
                "资料包/方向甲/input.txt", "资料包/方向乙/input.txt",
            ]
            assert browser_contract["state"] == {"reload": True, "message": "一项成功、一项失败"}
            real_upload = service.upload_project_path
            for failure_type in (OSError, sa.exc.SQLAlchemyError):
                def faulty_upload(stream, *, original_name, **kwargs):
                    if original_name == "fault.txt":
                        raise failure_type("private diagnostic sentinel")
                    return real_upload(stream, original_name=original_name, **kwargs)
                # Inject a lower-level failure; successful neighbors still use real PG and storage.
                with monkeypatch.context() as fault:
                    fault.setattr(service, "upload_project_path", faulty_upload)
                    result = client.post(f"/{prefix}/upload_folder/{project['businessId']}", data={
                        "files": [(io.BytesIO(b"before"), "failure-test/before.txt"),
                                  (io.BytesIO(b"fault"), "failure-test/fault.txt"),
                                  (io.BytesIO(b"after"), "failure-test/after.txt")],
                    }, headers={"X-CSRF-Token": csrf})
                assert result.status_code == 207
                assert result.json["uploadedCount"] == 2 and result.json["failedCount"] == 1
                assert result.json["errors"][0]["code"] == "FILE_OPERATION_FAILED"
                assert "private diagnostic sentinel" not in result.text
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
        if operation in {"upload", "file_rename", "file_archive"}:
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
                    self.rename_buttons = []
                def handle_starttag(self, tag, attrs):
                    self.tags.append(tag)
                    if "btn-rename" in dict(attrs).get("class", "").split():
                        self.rename_buttons.append(dict(attrs))
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
            if operation == "file_archive":
                service = app.extensions["file_service"]
                def reject_archive_audit(*args, **kwargs):
                    raise RuntimeError("injected archive audit failure")
                with monkeypatch.context() as failure:
                    failure.setattr(service.audit_service, "record", reject_archive_audit)
                    with pytest.raises(FileServiceError) as failed:
                        service.archive(files[0]["fileId"], object_type="PROJECT", object_id=project["businessId"],
                                        actor_user_id=user_id, request_id="archive-audit-failure")
                    assert failed.value.code == "FILE_OPERATION_FAILED"
                active = service.list_project_paths(project["businessId"])
                assert [(row["fileId"], row["versionNo"]) for row in active] == [(files[0]["fileId"], 2)]
                for attempt in range(2):
                    service.archive(files[0]["fileId"], object_type="PROJECT", object_id=project["businessId"],
                                    actor_user_id=user_id, request_id=f"archive-version-{attempt}")
                    with engine.connect() as connection:
                        row = service.repository.get_linked_file(
                            connection, file_id=files[0]["fileId"], object_type="PROJECT",
                            object_id=project["businessId"],
                        )
                        assert row["status"] == "ARCHIVED" and row["version"] == 2
                        for number, expected_bytes in [(1, b"research input"), (2, b"second revision")]:
                            version = service.repository.get_version(
                                connection, file_id=files[0]["fileId"], version_no=number,
                            )
                            assert (service.storage_root / version["storage_path"]).read_bytes() == expected_bytes
                        assert service.repository.get_version(
                            connection, file_id=files[0]["fileId"], version_no=3,
                        ) is None
                assert service.list_project_paths(project["businessId"]) == []
            if operation == "file_rename":
                assert links.rename_buttons[0]["data-file-id"] == files[0]["fileId"]
                submit_code = re.search(
                    r"document.getElementById\('renameForm'\).onsubmit = function\(e\).*?\n    };",
                    refreshed.text, re.S,
                )[0]
                submitted = subprocess.run(["node", "-e", """
const fs = require('fs'); const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fields = {renameForm: {}, renameFilePath: {value: ''},
  renameFileId: {value: ''}, newFileName: {value: ''}, renameModal: {}};
const button = {dataset: {path: input.button['data-path'], name: input.button['data-name'],
  fileId: input.button['data-file-id']}, addEventListener: function(event, handler) {
    this.click = handler;
  }};
const context = {document: {getElementById: id => fields[id],
  querySelectorAll: () => [button]}, FormData,
  bootstrap: {Modal: class {show() {}}},
  projectUpdateUrl: input.prefix,
  fetch: (url, options) => {
    process.stdout.write(JSON.stringify({url, data: Object.fromEntries(options.body)}));
    return Promise.resolve({json: () => ({success: false})});
  }, alert: () => {}};
vm.createContext(context); vm.runInContext(input.clickCode, context);
button.click({stopPropagation() {}});
fields.newFileName.value = 'renamed.txt';
vm.runInContext(input.code, context);
fields.renameForm.onsubmit({preventDefault() {}});
"""], input=json.dumps({"code": submit_code, "prefix": "/" + prefix,
                         "button": links.rename_buttons[0],
                         "clickCode": re.search(
                             r"document.querySelectorAll\('\.btn-rename'\).*?\n    \}\);",
                             refreshed.text, re.S,
                         )[0]}),
                                           text=True, capture_output=True, check=True)
                request_data = json.loads(submitted.stdout)
                assert request_data["data"].get("expectedFileId") == files[0]["fileId"]
                assert request_data["url"] == f"/{prefix}/rename_file/{project['businessId']}"
                renamed = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                    "file_path": "任务输入文件/input.txt", "new_name": "renamed.txt",
                    "expectedFileId": files[0]["fileId"],
                }, headers={"X-CSRF-Token": csrf})
                assert renamed.status_code == 200 and renamed.json["success"] is True
                path_rows = app.extensions["file_service"].list_project_paths(project["businessId"])
                assert [(row["fileId"], row["path"], row["versionNo"]) for row in path_rows] == [
                    (files[0]["fileId"], "任务输入文件/renamed.txt", 2),
                ]
                downloaded = client.get(f"/{prefix}/download/{project['businessId']}/任务输入文件/renamed.txt",
                                        follow_redirects=True)
                assert downloaded.status_code == 200 and downloaded.data == b"second revision"
                original = client.get(f"/api/files/{files[0]['fileId']}/versions/1/download",
                                      query_string={"objectType": "PROJECT", "objectId": project["businessId"]})
                assert original.status_code == 200 and original.data == b"research input"
                legacy_target = tmp_path / "uploads" / project["businessId"] / "任务输入文件" / "occupied.txt"
                legacy_target.parent.mkdir(parents=True, exist_ok=True)
                legacy_target.write_bytes(b"retained legacy file")
                for name, status in [("occupied.txt", 409), ("renamed.pdf", 415), ("../escape.txt", 400)]:
                    rejected = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                        "file_path": "任务输入文件/renamed.txt", "new_name": name,
                        "expectedFileId": files[0]["fileId"],
                    }, headers={"X-CSRF-Token": csrf})
                    assert rejected.status_code == status and rejected.json["success"] is False
                assert legacy_target.read_bytes() == b"retained legacy file"
                service = app.extensions["file_service"]
                def fail_audit(*args, **kwargs):
                    raise RuntimeError("injected rename audit failure")
                with monkeypatch.context() as failure:
                    failure.setattr(service.audit_service, "record", fail_audit)
                    failed = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                        "file_path": "任务输入文件/renamed.txt", "new_name": "must-not-persist.txt",
                        "expectedFileId": files[0]["fileId"],
                    }, headers={"X-CSRF-Token": csrf})
                assert failed.status_code == 500
                assert [(row["path"], row["versionNo"]) for row in service.list_project_paths(project["businessId"])] == [
                    ("任务输入文件/renamed.txt", 2),
                ]
                reverse_conflict = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                    "file_path": "任务输入文件/occupied.txt", "new_name": "renamed.txt",
                }, headers={"X-CSRF-Token": csrf})
                assert reverse_conflict.status_code == 409 and reverse_conflict.json["success"] is False
                assert legacy_target.read_bytes() == b"retained legacy file"
                for source, target in [
                    ("任务输入文件/occupied.txt", "./renamed.txt"),
                    ("任务输入文件/occupied.txt", "../任务输入文件/renamed.txt"),
                    ("任务输入文件/./occupied.txt", "renamed.txt"),
                    ("任务输入文件//occupied.txt", "renamed.txt"),
                ]:
                    aliased = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                        "file_path": source, "new_name": target,
                    }, headers={"X-CSRF-Token": csrf})
                    assert aliased.status_code == 400 and aliased.json["success"] is False
                    assert legacy_target.read_bytes() == b"retained legacy file"
                legacy_renamed = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                    "file_path": "任务输入文件/occupied.txt", "new_name": "保留旧格式.doc",
                }, headers={"X-CSRF-Token": csrf})
                assert legacy_renamed.status_code == 200 and legacy_renamed.json["success"] is True
                assert not legacy_target.exists()
                assert legacy_target.with_name("保留旧格式.doc").read_bytes() == b"retained legacy file"
                for stale_id in (str(uuid.uuid4()), ""):
                    stale = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                        "file_path": "任务输入文件/renamed.txt", "new_name": "stale-must-not-change.txt",
                        "expectedFileId": stale_id,
                    }, headers={"X-CSRF-Token": csrf})
                    assert stale.status_code == 409
                assert service.list_project_paths(project["businessId"])[0]["path"] == "任务输入文件/renamed.txt"
                other = app.extensions["project_service"].create_standalone(
                    category, {"name": "演练-共享附件保留", "leader": "李老师"}, actor_user_id=user_id,
                )
                rename_marker = "rename-test-" + uuid.uuid4().hex
                def rename_concurrently():
                    worker_id = get_ident()
                    def mark_transaction(connection):
                        if get_ident() == worker_id:
                            connection.execute(sa.text("select set_config('application_name', :name, true)"),
                                               {"name": rename_marker})
                    sa.event.listen(engine, "begin", mark_transaction)
                    try:
                        return service.rename_project_path(
                            project["businessId"], "任务输入文件/renamed.txt", "concurrent-must-not-change.txt",
                            expected_file_id=files[0]["fileId"], actor_user_id=user_id,
                            request_id="shared-link-concurrency",
                        )
                    finally:
                        sa.event.remove(engine, "begin", mark_transaction)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with engine.begin() as connection:
                        blocker_pid = connection.execute(sa.text("select pg_backend_pid()")).scalar_one()
                        service.repository.link_object(
                            connection, link_id=uuid.uuid4(), object_type="PROJECT",
                            object_id=other["businessId"], file_id=files[0]["fileId"],
                            actor_user_id=user_id, purpose="PROJECT_TREE:任务输入文件/renamed.txt",
                        )
                        pending = executor.submit(rename_concurrently)
                        deadline = time.monotonic() + 5
                        blocked = False
                        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                            while time.monotonic() < deadline and not pending.done():
                                blocked = observer.execute(sa.text(
                                    "select exists (select 1 from pg_stat_activity "
                                    "where application_name = :name and :pid = any(pg_blocking_pids(pid)))"
                                ), {"pid": blocker_pid, "name": rename_marker}).scalar_one()
                                if blocked:
                                    break
                                time.sleep(0.01)
                        assert blocked, "rename must wait for the uncommitted shared-link transaction"
                    with pytest.raises(FileServiceError) as conflict:
                        pending.result(timeout=5)
                    assert conflict.value.code == "FILE_SHARED" and conflict.value.status_code == 409
                shared = client.post(f"/{prefix}/rename_file/{project['businessId']}", data={
                    "file_path": "任务输入文件/renamed.txt", "new_name": "shared-must-not-change.txt",
                    "expectedFileId": files[0]["fileId"],
                }, headers={"X-CSRF-Token": csrf})
                assert shared.status_code == 409 and shared.json["code"] == "FILE_SHARED"
                for business_id in (project["businessId"], other["businessId"]):
                    retained = service.list_project_paths(business_id)
                    assert [(row["path"], row["name"], row["versionNo"]) for row in retained] == [
                        ("任务输入文件/renamed.txt", "renamed.txt", 2),
                    ]
                reverse_file = service.upload_project_path(
                    io.BytesIO(b"reverse ordering bytes"), original_name="before.txt", folder="任务输入文件",
                    project_id=project["businessId"], actor_user_id=user_id, request_id="reverse-upload",
                )
                rename_ready, release_rename = Event(), Event()
                rename_pid = []
                original_audit = service.audit_service.record
                def pause_after_real_audit(connection, **kwargs):
                    result = original_audit(connection, **kwargs)
                    if kwargs["request_id"] == "reverse-rename":
                        rename_pid.append(connection.execute(sa.text("select pg_backend_pid()")).scalar_one())
                        rename_ready.set()
                        assert release_rename.wait(10), "test must release the rename transaction"
                    return result
                link_marker = "link-test-" + uuid.uuid4().hex
                def insert_after_rename():
                    with engine.begin() as connection:
                        connection.execute(sa.text("select set_config('application_name', :name, true)"),
                                           {"name": link_marker})
                        service.repository.link_object(
                            connection, link_id=uuid.uuid4(), object_type="PROJECT",
                            object_id=other["businessId"], file_id=reverse_file["fileId"],
                            actor_user_id=user_id, purpose="PROJECT_TREE:任务输入文件/after.txt",
                        )
                with monkeypatch.context() as scheduling:
                    scheduling.setattr(service.audit_service, "record", pause_after_real_audit)
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        renaming = executor.submit(
                            service.rename_project_path, project["businessId"], "任务输入文件/before.txt",
                            "after.txt", expected_file_id=reverse_file["fileId"],
                            actor_user_id=user_id, request_id="reverse-rename",
                        )
                        try:
                            assert rename_ready.wait(5), "rename must reach its real audit before commit"
                            linking = executor.submit(insert_after_rename)
                            blocked = False
                            deadline = time.monotonic() + 5
                            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                                while time.monotonic() < deadline and not linking.done():
                                    blocked = observer.execute(sa.text(
                                        "select exists (select 1 from pg_stat_activity where "
                                        "application_name = :name and :pid = any(pg_blocking_pids(pid)))"
                                    ), {"name": link_marker, "pid": rename_pid[0]}).scalar_one()
                                    if blocked:
                                        break
                                    time.sleep(0.01)
                            assert blocked, "new link must wait for the rename transaction"
                        finally:
                            release_rename.set()
                        assert renaming.result(timeout=5)["path"] == "任务输入文件/after.txt"
                        linking.result(timeout=5)
                for business_id in (project["businessId"], other["businessId"]):
                    retained = [row for row in service.list_project_paths(business_id)
                                if row["fileId"] == reverse_file["fileId"]]
                    assert [(row["path"], row["name"], row["versionNo"]) for row in retained] == [
                        ("任务输入文件/after.txt", "after.txt", 1),
                    ]
                    content = client.get(f"/api/files/{reverse_file['fileId']}/versions/1/download",
                                         query_string={"objectType": "PROJECT", "objectId": business_id})
                    assert content.status_code == 200 and content.data == b"reverse ordering bytes"
    finally:
        engine.dispose()
