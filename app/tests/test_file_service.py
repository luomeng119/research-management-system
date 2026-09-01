from __future__ import annotations

import io
import os
from pathlib import Path
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from flask import Flask
from sqlalchemy.pool import StaticPool

from app.repositories.files import FilesRepository
from app.services.files import FileService, FileServiceError


def _schema(engine):
    metadata = sa.MetaData()
    common = lambda: (
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    sa.Table(
        "stored_files", metadata, *common(),
        sa.Column("business_id", sa.Text, nullable=False, unique=True),
        sa.Column("original_name", sa.Text, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="ACTIVE"),
    )
    sa.Table(
        "stored_file_versions", metadata, *common(),
        sa.Column("file_id", sa.String(36), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.UniqueConstraint("file_id", "version_no"),
    )
    sa.Table(
        "object_files", metadata, *common(),
        sa.Column("object_type", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("file_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.Text),
        sa.UniqueConstraint("object_type", "object_id", "file_id"),
    )
    sa.Table("expense_reimbursement", metadata, sa.Column("id", sa.Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(metadata.tables["expense_reimbursement"].insert().values(id=7))


class Audit:
    def __init__(self, fail=False):
        self.fail = fail
        self.events = []

    def record(self, connection, **event):
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)
        return "audit-id"


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
def service(tmp_path, engine):
    return FileService(
        FilesRepository(engine),
        Audit(),
        storage_root=tmp_path / "files",
        max_bytes=1024,
        preview_max_bytes=512,
    )


def _pdf(payload=b"document"):
    return io.BytesIO(b"%PDF-1.7\n" + payload)


def _rows(engine, table_name):
    table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table)).mappings()]


def test_upload_creates_uuid_path_hash_version_link_and_low_sensitivity_audit(service, engine):
    result = service.upload(
        _pdf(), original_name="研究报告.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-test",
    )

    assert result["versionNo"] == 1
    assert result["storagePath"].endswith(".pdf")
    assert "研究报告" not in result["storagePath"]
    stored_path = service.storage_root / result["storagePath"]
    assert stored_path.read_bytes().startswith(b"%PDF-")
    assert len(result["sha256"]) == 64
    assert len(_rows(engine, "stored_files")) == 1
    assert len(_rows(engine, "stored_file_versions")) == 1
    assert len(_rows(engine, "object_files")) == 1
    event = service.audit_service.events[-1]
    assert event["event_name"] == "file_operation_completed"
    assert set(event["properties"]) == {"operation", "file_type", "size_bucket"}
    assert "研究报告.pdf" not in repr(event)


def test_same_original_name_never_overwrites(service):
    first = service.upload(
        _pdf(b"one"), original_name="报告.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-1",
    )
    second = service.upload(
        _pdf(b"two"), original_name="报告.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-2",
    )

    assert first["fileId"] != second["fileId"]
    assert first["storagePath"] != second["storagePath"]
    assert (service.storage_root / first["storagePath"]).read_bytes().endswith(b"one")
    assert (service.storage_root / second["storagePath"]).read_bytes().endswith(b"two")


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("../escape.pdf", b"%PDF-1.7", "INVALID_FILENAME"),
        ("folder\\escape.pdf", b"%PDF-1.7", "INVALID_FILENAME"),
        ("bad\nname.pdf", b"%PDF-1.7", "INVALID_FILENAME"),
        ("report.exe.pdf", b"%PDF-1.7", "INVALID_FILENAME"),
        ("fake.pdf", b"not a pdf", "UNSUPPORTED_MEDIA_TYPE"),
        ("empty.pdf", b"", "UNSUPPORTED_MEDIA_TYPE"),
        ("empty.txt", b"", "UNSUPPORTED_MEDIA_TYPE"),
        ("nul.txt", b"hello\x00world", "UNSUPPORTED_MEDIA_TYPE"),
        ("too-large.pdf", b"%PDF-" + b"x" * 2048, "FILE_TOO_LARGE"),
    ],
)
def test_invalid_uploads_are_rejected_without_database_or_disk_residue(
    service, engine, name, content, code
):
    with pytest.raises(FileServiceError) as error:
        service.upload(
            io.BytesIO(content), original_name=name, object_type="EXPENSE", object_id="7",
            actor_user_id=1, request_id="req-bad",
        )

    assert error.value.code == code
    assert _rows(engine, "stored_files") == []
    assert not any(path.is_file() for path in service.storage_root.rglob("*"))


def test_unknown_object_is_rejected_before_staging(service, engine):
    with pytest.raises(FileServiceError) as error:
        service.upload(
            _pdf(), original_name="report.pdf", object_type="EXPENSE", object_id="999",
            actor_user_id=1, request_id="req-missing",
        )
    assert error.value.code == "OBJECT_NOT_FOUND"
    assert _rows(engine, "stored_files") == []
    assert not any(path.is_file() for path in service.storage_root.rglob("*"))


def test_audit_failure_rolls_back_database_and_removes_final_file(tmp_path, engine):
    service = FileService(
        FilesRepository(engine), Audit(fail=True), storage_root=tmp_path / "files",
        max_bytes=1024, preview_max_bytes=512,
    )
    with pytest.raises(FileServiceError) as error:
        service.upload(
            _pdf(), original_name="report.pdf", object_type="EXPENSE", object_id="7",
            actor_user_id=1, request_id="req-audit",
        )
    assert error.value.code == "FILE_OPERATION_FAILED"
    assert _rows(engine, "stored_files") == []
    assert not any(path.is_file() for path in service.storage_root.rglob("*"))


def test_new_version_preserves_v1_detects_conflict_and_archive_preserves_history(service, engine):
    first = service.upload(
        _pdf(b"v1"), original_name="report.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-v1",
    )
    v1_bytes = (service.storage_root / first["storagePath"]).read_bytes()
    second = service.add_version(
        first["fileId"], _pdf(b"v2"), original_name="report.pdf",
        object_type="EXPENSE", object_id="7", expected_version=1,
        actor_user_id=1, request_id="req-v2",
    )

    assert second["versionNo"] == 2
    assert second["storagePath"] != first["storagePath"]
    assert (service.storage_root / first["storagePath"]).read_bytes() == v1_bytes
    with pytest.raises(FileServiceError) as conflict:
        service.add_version(
            first["fileId"], _pdf(b"stale"), original_name="report.pdf",
            object_type="EXPENSE", object_id="7", expected_version=1,
            actor_user_id=1, request_id="req-stale",
        )
    assert conflict.value.code == "VERSION_CONFLICT"

    service.archive(
        first["fileId"], object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-archive",
    )
    assert len(_rows(engine, "stored_file_versions")) == 2
    assert all((service.storage_root / row["storage_path"]).exists() for row in _rows(engine, "stored_file_versions"))
    with pytest.raises(FileServiceError) as archived:
        service.open_version(first["fileId"], 2, object_type="EXPENSE", object_id="7")
    assert archived.value.code == "FILE_ARCHIVED"


def test_new_version_must_keep_the_original_file_type(service):
    uploaded = service.upload(
        _pdf(), original_name="report.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-original",
    )
    with pytest.raises(FileServiceError) as error:
        service.add_version(
            uploaded["fileId"], io.BytesIO(b"plain text"), original_name="report.txt",
            object_type="EXPENSE", object_id="7", expected_version=1,
            actor_user_id=1, request_id="req-wrong-type",
        )
    assert error.value.code == "FILE_TYPE_MISMATCH"
    assert not any(path.suffix == ".txt" for path in service.storage_root.rglob("*"))


@pytest.mark.parametrize("storage_path", ["../../outside.pdf", "/tmp/outside.pdf", "%252e%252e/outside.pdf"])
def test_database_path_tampering_is_rejected(service, engine, storage_path):
    uploaded = service.upload(
        _pdf(), original_name="report.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-path",
    )
    versions = sa.Table("stored_file_versions", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            versions.update().where(versions.c.file_id == uploaded["fileId"]).values(storage_path=storage_path)
        )
    with pytest.raises(FileServiceError) as error:
        service.open_version(uploaded["fileId"], 1, object_type="EXPENSE", object_id="7")
    assert error.value.code == "FILE_NOT_FOUND"


def test_deleted_business_object_makes_existing_link_inaccessible(service, engine):
    uploaded = service.upload(
        _pdf(), original_name="report.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-deleted-object",
    )
    expenses = sa.Table("expense_reimbursement", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(expenses.delete().where(expenses.c.id == 7))
    with pytest.raises(FileServiceError) as error:
        service.open_version(uploaded["fileId"], 1, object_type="EXPENSE", object_id="7")
    assert error.value.code == "FILE_NOT_FOUND"


def test_symlinked_storage_file_is_rejected(service, tmp_path, engine):
    uploaded = service.upload(
        _pdf(), original_name="report.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-link",
    )
    stored = service.storage_root / uploaded["storagePath"]
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-outside")
    stored.unlink()
    stored.symlink_to(outside)
    with pytest.raises(FileServiceError) as error:
        service.open_version(uploaded["fileId"], 1, object_type="EXPENSE", object_id="7")
    assert error.value.code == "FILE_NOT_FOUND"


def test_symlinked_ancestor_outside_storage_root_name_remains_readable(tmp_path, engine):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    aliased = FileService(
        FilesRepository(engine), Audit(), storage_root=alias_parent / "files",
        max_bytes=1024, preview_max_bytes=512,
    )
    uploaded = aliased.upload(
        _pdf(), original_name="report.pdf", object_type="EXPENSE", object_id="7",
        actor_user_id=1, request_id="req-ancestor-link",
    )
    opened = aliased.open_version_stream(
        uploaded["fileId"], 1, object_type="EXPENSE", object_id="7"
    )
    try:
        assert opened["stream"].read().startswith(b"%PDF-")
    finally:
        opened["stream"].close()


@pytest.fixture()
def web_app(service):
    from app.routes.preview import bp as preview_bp
    from app.web.files import bp as files_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="file-test", SECURITY_AUTH_ENABLED=False)
    app.extensions["file_service"] = service
    app.register_blueprint(files_bp)
    app.register_blueprint(preview_bp)
    return app


def _business_client(app, role="BUSINESS_USER"):
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(
            user_id=1,
            user="alice",
            name="Alice",
            role=role,
            account_version=1,
        )
    return client


def test_file_api_requires_auth_and_round_trips_upload_download(web_app):
    anonymous = web_app.test_client()
    assert anonymous.get("/api/files?objectType=EXPENSE&objectId=7").status_code == 401

    client = _business_client(web_app)
    uploaded = client.post(
        "/api/files",
        data={
            "objectType": "EXPENSE",
            "objectId": "7",
            "file": (_pdf(b"api"), "report.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 201
    payload = uploaded.get_json()
    assert "storagePath" not in payload

    listing = client.get("/api/files?objectType=EXPENSE&objectId=7")
    assert listing.status_code == 200
    assert listing.get_json()["files"][0]["fileId"] == payload["fileId"]

    downloaded = client.get(
        f"/api/files/{payload['fileId']}/versions/1/download?objectType=EXPENSE&objectId=7"
    )
    assert downloaded.status_code == 200
    assert downloaded.data.endswith(b"api")
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
    assert "attachment" in downloaded.headers["Content-Disposition"]


def test_maintenance_role_cannot_access_business_files(web_app):
    maintainer = _business_client(web_app, role="SYSTEM_MAINTAINER")
    assert maintainer.get("/api/files?objectType=EXPENSE&objectId=7").status_code == 403
    assert maintainer.post(
        "/api/files",
        data={
            "objectType": "EXPENSE", "objectId": "7",
            "file": (_pdf(), "report.pdf"),
        },
        content_type="multipart/form-data",
    ).status_code == 403


def test_preview_fallback_and_legacy_path_parameter_rejection(web_app, service):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("readme.txt", "hello")
    archive.seek(0)
    client = _business_client(web_app)
    uploaded = client.post(
        "/api/files",
        data={
            "objectType": "EXPENSE",
            "objectId": "7",
            "file": (archive, "materials.zip"),
        },
        content_type="multipart/form-data",
    ).get_json()

    previewed = client.get(
        f"/api/files/{uploaded['fileId']}/versions/1/preview?objectType=EXPENSE&objectId=7"
    )
    assert previewed.status_code == 409
    assert previewed.get_json()["error"]["code"] == "PREVIEW_UNAVAILABLE"
    assert "/download?" in previewed.get_json()["downloadUrl"]
    assert service.audit_service.events[-1]["result"] == "FAILURE"
    assert service.audit_service.events[-1]["properties"]["operation"] == "PREVIEW"

    legacy = client.get("/preview/file?path=/etc/passwd&type=other")
    assert legacy.status_code == 400
    assert legacy.get_json()["error"]["code"] == "CONTROLLED_FILE_REFERENCE_REQUIRED"


def test_invalid_file_id_and_object_mismatch_are_controlled_and_audited(web_app, service):
    client = _business_client(web_app)
    invalid = client.get(
        "/api/files/not-a-uuid/versions/1/download?objectType=EXPENSE&objectId=7"
    )
    assert invalid.status_code == 404
    assert invalid.get_json()["error"]["code"] == "FILE_NOT_FOUND"
    assert service.audit_service.events[-1]["result"] == "FAILURE"
    assert service.audit_service.events[-1]["properties"]["operation"] == "DOWNLOAD"


def test_legacy_attachment_resolution_rejects_escape_and_symlink(tmp_path):
    from app.routes.documents import _resolve_legacy_attachment

    root = tmp_path / "app"
    uploads = root / "uploads"
    uploads.mkdir(parents=True)
    stored = uploads / "invoice.pdf"
    stored.write_bytes(b"%PDF-safe")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-outside")
    linked = uploads / "linked.pdf"
    linked.symlink_to(outside)

    assert _resolve_legacy_attachment(root, "uploads/invoice.pdf") == stored
    assert _resolve_legacy_attachment(root, str(stored)) == stored
    assert _resolve_legacy_attachment(root, "../outside.pdf") is None
    assert _resolve_legacy_attachment(root, str(outside)) is None
    assert _resolve_legacy_attachment(root, "uploads/linked.pdf") is None


def test_postgres_file_version_and_audit_contract(tmp_path):
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("requires the isolated PostgreSQL contract runner")
    from app.repositories.audit import AuditRepository
    from app.services.audit import AuditService

    pg_engine = sa.create_engine(os.environ["TEST_DATABASE_URL"])
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=pg_engine)
    expenses = sa.Table("expense_reimbursement", metadata, autoload_with=pg_engine)
    audit_events = sa.Table("audit_events", metadata, autoload_with=pg_engine)
    suffix = uuid.uuid4().hex[:10]
    with pg_engine.begin() as connection:
        user_id = connection.execute(
            users.insert().values(
                username=f"file_{suffix}", password="unused", role="BUSINESS_USER",
                name="File contract", status="active", must_change_password=False, version=1,
            ).returning(users.c.id)
        ).scalar_one()
        expense_id = connection.execute(
            expenses.insert().values(reimbursement_no=f"FILE-{suffix}").returning(expenses.c.id)
        ).scalar_one()

    pg_service = FileService(
        FilesRepository(pg_engine),
        AuditService(AuditRepository(pg_engine), app_version="test-v1"),
        storage_root=tmp_path / "pg-files", max_bytes=1024, preview_max_bytes=512,
    )
    first = pg_service.upload(
        _pdf(b"pg-v1"), original_name="report.pdf", object_type="EXPENSE",
        object_id=str(expense_id), actor_user_id=user_id, request_id=f"req-{suffix}-1",
    )
    second = pg_service.add_version(
        first["fileId"], _pdf(b"pg-v2"), original_name="report.pdf",
        object_type="EXPENSE", object_id=str(expense_id), expected_version=1,
        actor_user_id=user_id, request_id=f"req-{suffix}-2",
    )

    assert second["versionNo"] == 2
    assert pg_service.open_version(
        first["fileId"], 1, object_type="EXPENSE", object_id=str(expense_id)
    )["path"].read_bytes().endswith(b"pg-v1")
    with pg_engine.connect() as connection:
        events = connection.execute(
            sa.select(audit_events.c.metadata)
            .where(audit_events.c.actor_user_id == user_id)
            .order_by(audit_events.c.created_at, audit_events.c.id)
        ).scalars().all()
    assert [event["operation"] for event in events] == ["UPLOAD", "ADD_VERSION"]
    assert all(set(event).isdisjoint({"path", "original_name", "content"}) for event in events)


def _postgres_entities(suffix):
    pg_engine = sa.create_engine(os.environ["TEST_DATABASE_URL"])
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=pg_engine)
    expenses = sa.Table("expense_reimbursement", metadata, autoload_with=pg_engine)
    with pg_engine.begin() as connection:
        user_id = connection.execute(
            users.insert().values(
                username=f"file_{suffix}", password="unused", role="BUSINESS_USER",
                name="File contract", status="active", must_change_password=False, version=1,
            ).returning(users.c.id)
        ).scalar_one()
        expense_id = connection.execute(
            expenses.insert().values(reimbursement_no=f"FILE-{suffix}").returning(expenses.c.id)
        ).scalar_one()
    return pg_engine, user_id, expense_id


def test_postgres_concurrent_version_writers_serialize(tmp_path):
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("requires the isolated PostgreSQL contract runner")
    from app.repositories.audit import AuditRepository
    from app.services.audit import AuditService

    suffix = uuid.uuid4().hex[:10]
    pg_engine, user_id, expense_id = _postgres_entities(suffix)
    pg_service = FileService(
        FilesRepository(pg_engine),
        AuditService(AuditRepository(pg_engine), app_version="test-v1"),
        storage_root=tmp_path / "concurrent-files", max_bytes=1024, preview_max_bytes=512,
    )
    first = pg_service.upload(
        _pdf(b"v1"), original_name="report.pdf", object_type="EXPENSE",
        object_id=str(expense_id), actor_user_id=user_id, request_id=f"req-{suffix}-v1",
    )
    barrier = Barrier(2)

    def add(label):
        barrier.wait(timeout=5)
        try:
            return pg_service.add_version(
                first["fileId"], _pdf(label.encode()), original_name="report.pdf",
                object_type="EXPENSE", object_id=str(expense_id), expected_version=1,
                actor_user_id=user_id, request_id=f"req-{suffix}-{label}",
            )
        except FileServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(add, ("writer-a", "writer-b")))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("VERSION_CONFLICT") == 1
    versions = sa.Table("stored_file_versions", sa.MetaData(), autoload_with=pg_engine)
    with pg_engine.connect() as connection:
        version_numbers = connection.execute(
            sa.select(versions.c.version_no)
            .where(versions.c.file_id == uuid.UUID(first["fileId"]))
            .order_by(versions.c.version_no)
        ).scalars().all()
    assert version_numbers == [1, 2]
    assert len([path for path in pg_service.storage_root.rglob("*") if path.is_file()]) == 2


def test_postgres_audit_failure_rolls_back_and_removes_moved_file(tmp_path):
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("requires the isolated PostgreSQL contract runner")
    suffix = uuid.uuid4().hex[:10]
    pg_engine, user_id, expense_id = _postgres_entities(suffix)
    files = sa.Table("stored_files", sa.MetaData(), autoload_with=pg_engine)
    with pg_engine.connect() as connection:
        before = connection.execute(sa.select(sa.func.count()).select_from(files)).scalar_one()
    pg_service = FileService(
        FilesRepository(pg_engine), Audit(fail=True),
        storage_root=tmp_path / "rollback-files", max_bytes=1024, preview_max_bytes=512,
    )

    with pytest.raises(FileServiceError) as error:
        pg_service.upload(
            _pdf(), original_name="report.pdf", object_type="EXPENSE",
            object_id=str(expense_id), actor_user_id=user_id, request_id=f"req-{suffix}-failure",
        )
    assert error.value.code == "FILE_OPERATION_FAILED"
    with pg_engine.connect() as connection:
        after = connection.execute(sa.select(sa.func.count()).select_from(files)).scalar_one()
    assert after == before
    assert not any(path.is_file() for path in pg_service.storage_root.rglob("*"))


def test_ooxml_zip_bomb_shape_is_rejected_without_residue(service, engine):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * 300_000)
    payload.seek(0)
    with pytest.raises(FileServiceError) as error:
        service.upload(
            payload, original_name="bomb.docx", object_type="EXPENSE", object_id="7",
            actor_user_id=1, request_id="req-bomb",
        )
    assert error.value.code == "UNSUPPORTED_MEDIA_TYPE"
    assert _rows(engine, "stored_files") == []


@pytest.mark.parametrize(
    ("extension", "required_member", "parser_module", "parser_attribute"),
    [
        ("docx", "word/document.xml", "docx", "Document"),
        ("xlsx", "xl/workbook.xml", "openpyxl", "load_workbook"),
    ],
)
def test_office_preview_rejects_excessive_xml_nodes_before_library_parse(
    web_app, service, monkeypatch, extension, required_member, parser_module, parser_attribute
):
    service.max_bytes = 10 * 1024 * 1024
    service.preview_max_bytes = 10 * 1024 * 1024
    payload = io.BytesIO()
    nodes = "<root>" + ("<n/>" * 50_001) + "</root>"
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(required_member, nodes)
    payload.seek(0)
    client = _business_client(web_app)
    uploaded = client.post(
        "/api/files",
        data={
            "objectType": "EXPENSE", "objectId": "7",
            "file": (payload, f"oversized.{extension}"),
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 201

    parser = __import__(parser_module)
    called = False

    def forbidden_parser(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("OOXML parser must not run before the preview budget gate")

    monkeypatch.setattr(parser, parser_attribute, forbidden_parser)
    file_id = uploaded.get_json()["fileId"]
    response = client.get(
        f"/api/files/{file_id}/versions/1/preview?objectType=EXPENSE&objectId=7"
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PREVIEW_UNAVAILABLE"
    assert called is False
