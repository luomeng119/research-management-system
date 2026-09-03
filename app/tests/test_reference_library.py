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
