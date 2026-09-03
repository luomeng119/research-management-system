from __future__ import annotations

from io import BytesIO
from pathlib import Path

from flask import Flask, request
import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool


ROOT = Path(__file__).resolve().parents[2]


class AuditRecorder:
    def __init__(self, *, fail: bool = False):
        self.events = []
        self.fail = fail

    def record(self, connection, **event):
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)
        return "audit-id"


def _schema(engine):
    metadata = sa.MetaData()
    sa.Table(
        "standards", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("doc_id", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("file_type", sa.Text),
        sa.Column("uploader", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="ACTIVE"),
    )
    sa.Table(
        "reference_template_folders", metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("parent_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.Integer), sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    sa.Table(
        "reference_template_items", metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("template_id", sa.Text, nullable=False, unique=True),
        sa.Column("folder_id", sa.Text),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.Integer), sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    sa.Table(
        "stored_files", metadata,
        sa.Column("id", sa.Text, primary_key=True), sa.Column("business_id", sa.Text),
        sa.Column("original_name", sa.Text), sa.Column("media_type", sa.Text),
        sa.Column("status", sa.Text), sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer), sa.Column("version", sa.Integer),
    )
    sa.Table(
        "stored_file_versions", metadata,
        sa.Column("id", sa.Text, primary_key=True), sa.Column("file_id", sa.Text),
        sa.Column("version_no", sa.Integer), sa.Column("storage_path", sa.Text),
        sa.Column("sha256", sa.Text), sa.Column("size_bytes", sa.Integer),
        sa.Column("media_type", sa.Text), sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer), sa.Column("version", sa.Integer),
    )
    sa.Table(
        "object_files", metadata,
        sa.Column("id", sa.Text, primary_key=True), sa.Column("object_type", sa.Text),
        sa.Column("object_id", sa.Text), sa.Column("file_id", sa.Text),
        sa.Column("created_by", sa.Integer), sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer),
    )
    sa.Table(
        "audit_events", metadata,
        sa.Column("id", sa.Text, primary_key=True), sa.Column("actor_user_id", sa.Integer),
        sa.Column("action", sa.Text), sa.Column("object_type", sa.Text), sa.Column("object_id", sa.Text),
        sa.Column("result", sa.Text), sa.Column("request_id", sa.Text), sa.Column("metadata", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    sa.Table(
        "users", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.Text, nullable=False),
    )
    metadata.create_all(engine)
    return metadata


@pytest.fixture
def reference_engine():
    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    _schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def reference_service(reference_engine, tmp_path):
    from app.repositories.files import FilesRepository
    from app.repositories.reference_library import ReferenceLibraryRepository
    from app.services.files import FileService
    from app.services.reference_library import ReferenceLibraryService

    file_audit = AuditRecorder()
    service = ReferenceLibraryService(
        ReferenceLibraryRepository(reference_engine),
        AuditRecorder(),
        FileService(
            FilesRepository(reference_engine), file_audit, storage_root=tmp_path / "files",
            max_bytes=1024 * 1024, preview_max_bytes=1024,
        ),
    )
    return service


def _pdf(payload: bytes = b"reference") -> BytesIO:
    return BytesIO(b"%PDF-1.4\n" + payload)


def _seed_folder(engine, folder_id: str, name: str, parent_id: str | None = None, status="ACTIVE"):
    table = sa.Table("reference_template_folders", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=folder_id, parent_id=parent_id, name=name, status=status, version=1,
        ))


def test_standard_upload_and_archive_use_controlled_file_metadata(reference_service):
    """Removing the FileService call or archive status update must break this contract."""
    created = reference_service.upload_standard(
        _pdf(), original_name="GB.pdf", name="国标", category="国家标准",
        actor_user_id=7, request_id="req-standard",
    )

    assert created["docId"].startswith("STD-")
    assert created["file"]["originalName"] == "GB.pdf"
    assert reference_service.list_standards(category="国家标准")[0]["name"] == "国标"
    reference_service.archive_standard(created["docId"], actor_user_id=7, request_id="req-archive")
    assert reference_service.list_standards() == []
    with pytest.raises(Exception) as caught:
        reference_service.open_standard_download(created["docId"])
    assert getattr(caught.value, "code", None) == "STANDARD_NOT_FOUND"


def test_duplicate_active_names_and_archived_object_writes_are_rejected(reference_service):
    """Dropping active-name checks or write-state checks must make this test fail."""
    first = reference_service.upload_standard(
        _pdf(b"one"), original_name="one.pdf", name="重复", category="国家标准",
        actor_user_id=7, request_id="req-one",
    )
    with pytest.raises(Exception) as duplicate:
        reference_service.upload_standard(
            _pdf(b"two"), original_name="two.pdf", name="重复", category="国家标准",
            actor_user_id=7, request_id="req-two",
        )
    assert getattr(duplicate.value, "code", None) == "DUPLICATE_STANDARD_NAME"
    reference_service.archive_standard(first["docId"], actor_user_id=7, request_id="req-archive")
    with pytest.raises(Exception) as archived:
        reference_service.add_standard_version(
            first["docId"], first["file"]["fileId"], _pdf(b"three"), "one.pdf", 1,
            actor_user_id=7, request_id="req-version",
        )
    assert getattr(archived.value, "code", None) == "STANDARD_NOT_WRITABLE"


def test_template_logical_paths_reject_plain_and_encoded_traversal(reference_service, reference_engine):
    """Permitting dot segments, encoded separators, or inactive ancestors is a security bug."""
    _seed_folder(reference_engine, "finance", "财务模板")
    uploaded = reference_service.upload_template(
        _pdf(), original_name="budget.pdf", folder_path="财务模板", display_name="预算.pdf",
        actor_user_id=7, request_id="req-template",
    )
    assert reference_service.resolve_template_path("财务模板/预算.pdf")["templateId"] == uploaded["templateId"]
    for unsafe in ("../预算.pdf", "%2e%2e/预算.pdf", "财务模板%2f预算.pdf", "财务模板/%252e%252e/预算.pdf", "/etc/passwd"):
        with pytest.raises(Exception) as caught:
            reference_service.resolve_template_path(unsafe)
        assert getattr(caught.value, "code", None) == "INVALID_TEMPLATE_PATH"
    reference_service.archive_folder("财务模板", actor_user_id=7, request_id="req-folder-archive")
    with pytest.raises(Exception) as inactive:
        reference_service.resolve_template_path("财务模板/预算.pdf")
    assert getattr(inactive.value, "code", None) == "TEMPLATE_NOT_FOUND"


def test_template_tree_escapes_stored_names_and_does_not_use_argumentation_schema(reference_service, reference_engine, reference_routes):
    """Rendering file names as markup or inserting them in doc_templates must fail this behavior."""
    _seed_folder(reference_engine, "other", "其他模板")
    reference_service.upload_template(
        _pdf(), original_name="safe.pdf", folder_path="其他模板", display_name="<img src=x onerror=alert(1)>.pdf",
        actor_user_id=7, request_id="req-xss",
    )
    tree = reference_service.build_template_tree()
    assert tree[0]["files"][0]["name"] == "<img src=x onerror=alert(1)>.pdf"
    assert "doc_templates" not in reference_service.repository.table_names
    page = reference_routes.get("/templates/")
    assert page.status_code == 200
    assert b"<img src=x onerror=alert(1)>" not in page.data
    assert b"\\u003cimg src=x onerror=alert(1)\\u003e" in page.data


@pytest.fixture
def reference_routes(reference_service):
    from app.routes.standards import bp as standards_bp
    from app.routes.templates import bp as templates_bp

    app = Flask(__name__, template_folder=str(ROOT / "app/templates"), static_folder=str(ROOT / "app/static"))
    app.config.update(TESTING=True, SECRET_KEY="reference-test")
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
    app.extensions["reference_library_service"] = reference_service
    app.add_url_rule("/test/login", endpoint="auth.login", view_func=lambda: "")
    app.add_url_rule("/test/logout", endpoint="auth.logout", view_func=lambda: "")
    app.add_url_rule("/test/change-password", endpoint="users.change_password", view_func=lambda: "")
    app.add_url_rule("/test/users", endpoint="users.index", view_func=lambda: "")
    app.register_blueprint(standards_bp)
    app.register_blueprint(templates_bp)

    @app.before_request
    def request_context():
        request.request_id = "req-route"

    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(user="operator", user_id=7)
    return client


def test_legacy_download_urls_stream_only_controlled_files_and_hide_debug_routes(reference_routes, reference_service):
    """Restoring raw send_file paths or debug routes makes these compatibility checks fail."""
    standard = reference_service.upload_standard(
        _pdf(b"std"), original_name="standard.pdf", name="标准", category="国家标准",
        actor_user_id=7, request_id="req-standard",
    )
    _seed_folder(reference_service.repository.engine, "official", "公文模板")
    reference_service.upload_template(
        _pdf(b"template"), original_name="notice.pdf", folder_path="公文模板", display_name="通知.pdf",
        actor_user_id=7, request_id="req-template",
    )

    assert reference_routes.get(f"/standards/download/{standard['docId']}").data.startswith(b"%PDF-")
    assert reference_routes.get("/templates/download/%E5%85%AC%E6%96%87%E6%A8%A1%E6%9D%BF/%E9%80%9A%E7%9F%A5.pdf").data.startswith(b"%PDF-")
    assert reference_routes.get("/templates/download/%252e%252e%252fsecret.pdf").status_code == 400
    assert reference_routes.get("/templates/debug_tree").status_code == 404
    assert reference_routes.get("/templates/test_tree").status_code == 404


def test_unready_file_service_and_atomic_upload_failure_leave_no_standard(reference_engine, tmp_path):
    """A missing service must be controlled and a failing first upload must not leave metadata."""
    from app.repositories.files import FilesRepository
    from app.repositories.reference_library import ReferenceLibraryRepository
    from app.services.files import FileService
    from app.services.reference_library import ReferenceLibraryService

    service = ReferenceLibraryService(ReferenceLibraryRepository(reference_engine), AuditRecorder(), None)
    with pytest.raises(Exception) as unready:
        service.upload_standard(_pdf(), "x.pdf", "X", "国家标准", 7, "req-unready")
    assert getattr(unready.value, "code", None) == "REFERENCE_LIBRARY_UNAVAILABLE"

    failing_file_service = FileService(
        FilesRepository(reference_engine), AuditRecorder(fail=True), storage_root=tmp_path / "atomic",
        max_bytes=1024 * 1024, preview_max_bytes=1024,
    )
    atomic_service = ReferenceLibraryService(
        ReferenceLibraryRepository(reference_engine), AuditRecorder(), failing_file_service,
    )
    with pytest.raises(Exception):
        atomic_service.upload_standard(_pdf(), "rollback.pdf", "回滚", "国家标准", 7, "req-rollback")
    assert atomic_service.list_standards() == []


def test_template_folder_and_item_crud_is_metadata_only(reference_service, reference_engine):
    """Replacing metadata archive/rename with filesystem operations must break this lifecycle."""
    _seed_folder(reference_engine, "root", "其他模板")
    reference_service.create_folder("子目录", "其他模板", actor_user_id=7, request_id="req-folder")
    created = reference_service.upload_template(
        _pdf(), "source.pdf", "其他模板/子目录", "原名.pdf", actor_user_id=7, request_id="req-upload",
    )
    reference_service.rename_template("其他模板/子目录/原名.pdf", "新名.pdf", actor_user_id=7, request_id="req-rename")
    assert reference_service.resolve_template_path("其他模板/子目录/新名.pdf")["templateId"] == created["templateId"]
    reference_service.archive_template("其他模板/子目录/新名.pdf", actor_user_id=7, request_id="req-archive")
    with pytest.raises(Exception) as archived:
        reference_service.resolve_template_path("其他模板/子目录/新名.pdf")
    assert getattr(archived.value, "code", None) == "TEMPLATE_NOT_FOUND"


def test_retained_routes_require_login_and_report_unready_service(reference_routes):
    """Removing the retained login boundary or leaking an extension KeyError is a regression."""
    with reference_routes.session_transaction() as active_session:
        active_session.clear()
    assert reference_routes.get("/standards/").status_code == 302
    assert reference_routes.post("/templates/upload").status_code == 401


def test_reference_tree_exposes_controlled_preview_reference(reference_service, reference_engine):
    """Dropping file identity/version from list rows would reintroduce path preview."""
    _seed_folder(reference_engine, "preview", "方案模板")
    reference_service.upload_template(
        _pdf(), "preview.pdf", "方案模板", "预览.pdf", actor_user_id=7, request_id="req-preview",
    )
    file_row = reference_service.build_template_tree()[0]["files"][0]
    assert file_row["fileId"]
    assert file_row["versionNo"] == 1
    assert file_row["objectType"] == "TEMPLATE"


def test_folder_archive_cascades_to_descendants_and_rejects_direct_file_write(reference_service, reference_engine):
    """Archiving only the selected folder leaves ACTIVE unreachable template objects."""
    _seed_folder(reference_engine, "root-archive", "其他模板")
    reference_service.create_folder("子目录", "其他模板", actor_user_id=7, request_id="req-create")
    uploaded = reference_service.upload_template(
        _pdf(), "child.pdf", "其他模板/子目录", "子文件.pdf", actor_user_id=7, request_id="req-upload",
    )
    reference_service.archive_folder("其他模板", actor_user_id=7, request_id="req-archive")
    with pytest.raises(Exception) as hidden:
        reference_service.resolve_template_path("其他模板/子目录/子文件.pdf")
    assert getattr(hidden.value, "code", None) == "TEMPLATE_NOT_FOUND"
    with pytest.raises(Exception) as denied:
        reference_service.file_service.add_version(
            uploaded["file"]["fileId"], _pdf(b"v2"), original_name="child.pdf",
            object_type="TEMPLATE", object_id=uploaded["templateId"], expected_version=1,
            actor_user_id=7, request_id="req-direct-write",
        )
    assert getattr(denied.value, "code", None) == "OBJECT_READ_ONLY"


def test_folder_rename_and_audit_failure_roll_back_business_change(reference_engine, tmp_path):
    """Committing before the reference audit makes an audit outage silently mutate records."""
    from app.repositories.files import FilesRepository
    from app.repositories.reference_library import ReferenceLibraryRepository
    from app.services.files import FileService
    from app.services.reference_library import ReferenceLibraryService

    _seed_folder(reference_engine, "rename-root", "其他模板")
    service = ReferenceLibraryService(
        ReferenceLibraryRepository(reference_engine), AuditRecorder(fail=True),
        FileService(FilesRepository(reference_engine), AuditRecorder(), storage_root=tmp_path / "rename-files", max_bytes=1024 * 1024, preview_max_bytes=1024),
    )
    with pytest.raises(Exception) as failed:
        service.create_folder("不应提交", "其他模板", actor_user_id=7, request_id="req-audit-fail")
    assert getattr(failed.value, "code", None) == "REFERENCE_LIBRARY_OPERATION_FAILED"
    assert service.build_template_tree()[0]["children"] == []


def test_template_routes_preserve_nested_multipart_filename_and_reject_encoded_separator(reference_routes, reference_service, reference_engine):
    """Browser webkitRelativePath must survive multipart while URI separators are never decoded."""
    _seed_folder(reference_engine, "nested-root", "其他模板")
    response = reference_routes.post("/templates/upload_folder", data={
        "folder": "其他模板",
        "files": [(BytesIO(b"%PDF-1.4\nnested"), "一级/二级/材料.pdf")],
    }, content_type="multipart/form-data")
    assert response.status_code == 200
    tree = reference_service.build_template_tree()
    assert tree[0]["children"][0]["children"][0]["files"][0]["name"].endswith(".pdf")
    # A WSGI proxy may preserve the raw URI while Flask hands the view an
    # already-decoded path.  The route must examine that boundary value.
    assert reference_routes.get(
        "/templates/download/safe.pdf",
        environ_overrides={"RAW_URI": "/templates/download/%2fetc%2fpasswd"},
    ).status_code == 400
    assert reference_routes.get(
        "/templates/download/safe.pdf",
        environ_overrides={"RAW_URI": "/templates/download/%5cwindows%5csystem32"},
    ).status_code == 400
    assert reference_routes.get("/templates/download/%252fetc%252fpasswd").status_code == 400


def test_template_download_rejects_real_encoded_separators_before_router_redirect(reference_routes, reference_service, reference_engine):
    """A percent-encoded leading separator must not normalize into a valid logical URL."""
    _seed_folder(reference_engine, "encoded-etc", "etc")
    reference_service.upload_template(
        _pdf(b"reachable-only-after-redirect"), original_name="passwd.pdf",
        folder_path="etc", display_name="passwd.pdf", actor_user_id=7,
        request_id="req-encoded-separator",
    )
    for separator in ("%2f", "%2F", "%5c", "%252f"):
        response = reference_routes.get(
            f"/templates/download/{separator}etc{separator}passwd.pdf",
            follow_redirects=True,
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "INVALID_TEMPLATE_PATH"


def test_real_audit_service_accepts_reference_taxonomy_and_logs_operation(reference_engine, tmp_path):
    """A non-whitelisted event/object type previously made every real archive audit fail."""
    from app.repositories.audit import AuditRepository
    from app.repositories.files import FilesRepository
    from app.repositories.reference_library import ReferenceLibraryRepository
    from app.services.audit import AuditService
    from app.services.files import FileService
    from app.services.reference_library import ReferenceLibraryService

    _seed_folder(reference_engine, "audit-root", "其他模板")
    audit = AuditService(AuditRepository(reference_engine), app_version="test")
    service = ReferenceLibraryService(
        ReferenceLibraryRepository(reference_engine), audit,
        FileService(FilesRepository(reference_engine), audit, storage_root=tmp_path / "audit-files", max_bytes=1024 * 1024, preview_max_bytes=1024),
    )
    service.create_folder("审计目录", "其他模板", actor_user_id=7, request_id="req-audit")
    logs = service.list_logs("templates", operation="CREATE_FOLDER")
    assert logs[0]["operation_code"] == "CREATE_FOLDER"
    assert logs[0]["operation_type"] == "创建文件夹"


def test_reference_logs_include_file_uploads_and_filter_before_the_limit(reference_engine, tmp_path):
    """File upload events must be safely attributed to their reference object before filtering."""
    from app.repositories.audit import AuditRepository
    from app.repositories.files import FilesRepository
    from app.repositories.reference_library import ReferenceLibraryRepository
    from app.services.audit import AuditService
    from app.services.files import FileService
    from app.services.reference_library import ReferenceLibraryService

    users = sa.Table("users", sa.MetaData(), autoload_with=reference_engine)
    with reference_engine.begin() as connection:
        connection.execute(users.insert().values(id=7, username="operator"))
    _seed_folder(reference_engine, "log-template", "其他模板")
    audit = AuditService(AuditRepository(reference_engine), app_version="test")
    service = ReferenceLibraryService(
        ReferenceLibraryRepository(reference_engine), audit,
        FileService(FilesRepository(reference_engine), audit, storage_root=tmp_path / "log-files", max_bytes=1024 * 1024, preview_max_bytes=1024),
    )
    standard = service.upload_standard(_pdf(b"audit-standard"), "audit-standard.pdf", "审计标准", "国家标准", 7, "req-log-standard")
    template = service.upload_template(_pdf(b"audit-template"), "audit-template.pdf", "其他模板", "审计模板.pdf", actor_user_id=7, request_id="req-log-template")
    with reference_engine.begin() as connection:
        for index in range(101):
            audit.record(
                connection, event_name="reference_library_operation", user_id=7,
                object_type="TEMPLATE", object_id=template["templateId"], result="SUCCESS",
                request_id=f"req-noise-{index}", duration_ms=0, properties={"operation": "ARCHIVE"},
            )

    audit_statements = []

    def capture_audit_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "audit_events" in statement:
            audit_statements.append(statement)

    sa.event.listen(reference_engine, "before_cursor_execute", capture_audit_sql)
    try:
        standard_logs = service.list_logs("standards", operator="operator", file_name="审计标准", operation="UPLOAD")
        template_logs = service.list_logs("templates", operator="operator", file_name="审计模板", operation="UPLOAD")
        assert service.list_logs("standards", operation="ARCHIVE") == []
    finally:
        sa.event.remove(reference_engine, "before_cursor_execute", capture_audit_sql)
    assert [entry["file_name"] for entry in standard_logs] == ["审计标准"]
    assert [entry["file_name"] for entry in template_logs] == ["审计模板.pdf"]
    assert all("LIMIT" in statement.upper() for statement in audit_statements)
    assert service.list_logs("templates", operation="上传文件")[0]["file_name"] == "审计模板.pdf"

    from app.routes.templates import bp as templates_bp
    app = Flask(__name__, template_folder=str(ROOT / "app/templates"))
    app.config.update(TESTING=True, SECRET_KEY="log-route-test")
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
    app.extensions["reference_library_service"] = service
    app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "")
    app.add_url_rule("/logout", endpoint="auth.logout", view_func=lambda: "")
    app.add_url_rule("/change-password", endpoint="users.change_password", view_func=lambda: "")
    app.add_url_rule("/users", endpoint="users.index", view_func=lambda: "")
    app.register_blueprint(templates_bp)
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(user_id=7, user="operator")
    page = client.get("/templates/logs/templates?operation_type=上传文件")
    assert page.status_code == 200, page.get_json()
    rendered = page.get_data(as_text=True)
    assert "审计模板.pdf" in rendered
    assert "上传文件" in rendered
    assert "badge bg-success" in rendered
    assert ">UPLOAD<" not in rendered
    archive_page = client.get("/templates/logs/templates?operation_type=归档文件")
    archive_rendered = archive_page.get_data(as_text=True)
    assert archive_page.status_code == 200
    assert "归档文件" in archive_rendered
    assert "badge bg-danger" in archive_rendered


def test_standards_page_keeps_stored_name_as_dom_data_not_inline_script(reference_routes, reference_service):
    """Putting a stored standard name back in an onclick argument would execute markup on click."""
    reference_service.upload_standard(
        _pdf(), "safe.pdf", "<img src=x onerror=alert(1)>", "国家标准", 7, "req-standard-xss",
    )
    page = reference_routes.get("/standards/")
    assert page.status_code == 200
    assert b"onclick=\"openPreview" not in page.data
    assert b"<img src=x onerror=alert(1)>" not in page.data
    assert b"data-file-name=\"&lt;img src=x onerror=alert(1)&gt;\"" in page.data


def test_controlled_preview_adapter_accepts_standard_and_template_references(reference_service, reference_engine):
    """Preview must accept the four controlled references, never a stored path."""
    from app.routes.preview import bp as preview_bp
    from app.web.files import bp as files_bp

    standard = reference_service.upload_standard(_pdf(), "preview-standard.pdf", "预览标准", "国家标准", 7, "req-preview-standard")
    _seed_folder(reference_engine, "preview-template", "方案模板")
    template = reference_service.upload_template(_pdf(), "preview-template.pdf", "方案模板", "预览模板.pdf", actor_user_id=7, request_id="req-preview-template")
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="preview-test", SECURITY_AUTH_ENABLED=False)
    app.extensions["file_service"] = reference_service.file_service
    app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "")
    app.register_blueprint(files_bp)
    app.register_blueprint(preview_bp)
    @app.before_request
    def set_request_id():
        request.request_id = "req-preview"
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(user_id=7, user="operator", name="operator", role="BUSINESS_USER", account_version=1)
    for object_type, object_id, file_data in (("STANDARD", standard["docId"], standard["file"]), ("TEMPLATE", template["templateId"], template["file"])):
        response = client.get("/preview/file", query_string={"fileId": file_data["fileId"], "versionNo": file_data["versionNo"], "objectType": object_type, "objectId": object_id}, follow_redirects=True)
        assert response.status_code == 200, (object_type, response.get_json())
        assert response.data.startswith(b"%PDF-")


def test_controlled_preview_json_contract_supports_text_word_and_excel(reference_service, reference_engine):
    """The shared panel's success gate needs an explicit success flag for JSON previews."""
    from docx import Document
    import openpyxl
    from app.routes.preview import bp as preview_bp
    from app.web.files import bp as files_bp

    _seed_folder(reference_engine, "typed-preview", "其他模板")
    reference_service.file_service.preview_max_bytes = 1024 * 1024
    word = Document(); word.add_paragraph("Word preview")
    word_bytes = BytesIO(); word.save(word_bytes); word_bytes.seek(0)
    workbook = openpyxl.Workbook(); workbook.active.append(["Excel preview"])
    excel_bytes = BytesIO(); workbook.save(excel_bytes); excel_bytes.seek(0)
    uploaded = [
        ("text", reference_service.upload_template(BytesIO(b"text preview"), "preview.txt", "其他模板", "preview.txt", actor_user_id=7, request_id="req-preview-text")),
        ("word", reference_service.upload_template(word_bytes, "preview.docx", "其他模板", "preview.docx", actor_user_id=7, request_id="req-preview-word")),
        ("excel", reference_service.upload_template(excel_bytes, "preview.xlsx", "其他模板", "preview.xlsx", actor_user_id=7, request_id="req-preview-excel")),
    ]
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="typed-preview-test", SECURITY_AUTH_ENABLED=False)
    app.extensions["file_service"] = reference_service.file_service
    app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "")
    app.register_blueprint(files_bp)
    app.register_blueprint(preview_bp)
    @app.before_request
    def set_request_id():
        request.request_id = "req-typed-preview"
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(user_id=7, user="operator", name="operator", role="BUSINESS_USER", account_version=1)
    for expected_type, result in uploaded:
        response = client.get("/preview/file", query_string={
            "fileId": result["file"]["fileId"], "versionNo": result["file"]["versionNo"],
            "objectType": "TEMPLATE", "objectId": result["templateId"],
        }, follow_redirects=True)
        assert response.status_code == 200, (expected_type, response.get_json())
        assert response.get_json()["success"] is True
        assert response.get_json()["type"] == expected_type
    panel = (ROOT / "app/templates/components/preview_panel.html").read_text(encoding="utf-8")
    assert "if (data.success) renderPreviewContent(data)" in panel
