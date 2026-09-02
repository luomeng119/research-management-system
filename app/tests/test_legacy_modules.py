from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import uuid

from flask import Flask, request
import openpyxl
import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app.repositories.resources import ResearchResourcesRepository
from app.services.resources import ResearchResourcesService, ResourceServiceError


ROOT = Path(__file__).resolve().parents[2]


class AuditRecorder:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    def record(self, connection, **event):
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)
        return "audit-id"


def _expert_schema(engine):
    metadata = sa.MetaData()
    sa.Table(
        "experts", metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("expert_id", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("unit", sa.Text),
        sa.Column("position", sa.Text),
        sa.Column("expertise", sa.Text),
        sa.Column("bank_card", sa.Text),
        sa.Column("bank_name", sa.Text),
        sa.Column("uploader", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("phone", sa.Text),
        sa.Column("id_card", sa.Text),
    )
    sa.Table(
        "expert_groups", metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Text, nullable=False, unique=True),
        sa.Column("meeting_name", sa.Text, nullable=False),
        sa.Column("creator", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Table(
        "expert_group_members", metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Text, nullable=False),
        sa.Column("expert_id", sa.Text, nullable=False),
        sa.Column("selected_by", sa.Text, nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("group_id", "expert_id"),
    )
    sa.Table(
        "expert_import_batches", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.Integer, nullable=False),
        sa.Column("source_name", sa.Text, nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("rows", sa.JSON, nullable=False),
        sa.Column("valid_count", sa.Integer, nullable=False),
        sa.Column("error_count", sa.Integer, nullable=False),
        sa.Column("duplicate_count", sa.Integer, nullable=False),
        sa.Column("result", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)
    return metadata


@pytest.fixture
def expert_engine():
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata = _expert_schema(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(metadata.tables["experts"].insert(), [
            {
                "expert_id": "EXP-001", "name": "张老师", "unit": "第一研究室",
                "position": "研究员", "expertise": "材料", "phone": "13812345678",
                "id_card": "110101199001011234", "bank_card": "6222021234567890",
                "bank_name": "本地银行", "uploader": "tester", "created_at": now, "updated_at": now,
            },
            {
                "expert_id": "EXP-002", "name": "李老师", "unit": "第二研究室",
                "position": "高级工程师", "expertise": "通信", "phone": "13987654321",
                "id_card": "110101198802022345", "bank_card": "6222029876543210",
                "bank_name": "本地银行", "uploader": "tester", "created_at": now, "updated_at": now,
            },
        ])
    yield engine
    engine.dispose()


@pytest.fixture
def expert_service(expert_engine):
    audit = AuditRecorder()
    return ResearchResourcesService(ResearchResourcesRepository(expert_engine), audit), audit


@pytest.fixture
def expert_routes(expert_engine, expert_service):
    from app.routes.expert_groups import bp as groups_bp
    from app.routes.experts import bp as experts_bp

    service, audit = expert_service
    app = Flask(
        __name__, template_folder=str(ROOT / "app/templates"),
        static_folder=str(ROOT / "app/static"),
    )
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
    app.extensions["database_engine"] = expert_engine
    app.extensions["resources_service"] = service
    app.add_url_rule("/test/users", endpoint="users.index", view_func=lambda: "")
    app.add_url_rule(
        "/test/change-password", endpoint="users.change_password", view_func=lambda: ""
    )
    app.add_url_rule("/test/logout", endpoint="auth.logout", view_func=lambda: "")
    app.add_url_rule("/test/login", endpoint="auth.login", view_func=lambda: "")
    app.register_blueprint(experts_bp)
    app.register_blueprint(groups_bp)

    @app.before_request
    def request_id():
        request.request_id = "req-route"

    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(
            user_id=7, user="zhang", name="张老师", role="BUSINESS_USER"
        )
    return client, service, audit


def test_expert_list_uses_server_pagination_and_masks_sensitive_fields(expert_service):
    service, _audit = expert_service

    result = service.list_experts(page=1, page_size=1, keyword="老师")

    assert result["total"] == 2
    assert result["page"] == 1
    assert result["pageSize"] == 1
    assert [item["name"] for item in result["items"]] == ["张老师"]
    assert result["items"][0]["phone"] == "138****5678"
    assert result["items"][0]["idCard"] == "110***********1234"
    assert result["items"][0]["bankCard"] == "6222********7890"


def test_sensitive_expert_view_returns_full_values_and_writes_redacted_audit(expert_service):
    service, audit = expert_service

    result = service.get_expert_sensitive(
        "EXP-001", actor_user_id=7, request_id="req-view"
    )

    assert result["phone"] == "13812345678"
    assert result["idCard"] == "110101199001011234"
    assert result["bankCard"] == "6222021234567890"
    assert audit.events == [{
        "event_name": "expert_sensitive_accessed",
        "user_id": 7,
        "object_type": "EXPERT",
        "object_id": "EXP-001",
        "result": "SUCCESS",
        "request_id": "req-view",
        "duration_ms": 0,
        "properties": {"operation": "VIEW", "record_count": 1},
    }]


def test_missing_sensitive_expert_does_not_write_success_audit(expert_service):
    service, audit = expert_service

    with pytest.raises(ResourceServiceError) as caught:
        service.get_expert_sensitive(
            "EXP-404", actor_user_id=7, request_id="req-missing"
        )

    assert caught.value.code == "EXPERT_NOT_FOUND"
    assert audit.events == []


def test_expert_create_and_update_preserve_the_original_business_fields(expert_service):
    service, _audit = expert_service

    created = service.create_expert(
        {
            "name": "王老师", "unit": "第三研究室", "position": "研究员",
            "expertise": "软件", "phone": "13711112222", "idCard": "110101198703033456",
            "bankCard": "6222021111222233", "bankName": "本地银行",
        },
        uploader="operator", actor_user_id=7, request_id="req-create",
    )
    updated = service.update_expert(
        created["expertId"],
        {
            "name": "王老师", "unit": "联合实验室", "position": "研究员",
            "expertise": "软件工程", "phone": "13711112222",
            "idCard": "110101198703033456", "bankCard": "6222021111222233",
            "bankName": "本地银行",
        },
        actor_user_id=7, request_id="req-update",
    )

    assert created["expertId"].startswith("EXP")
    assert updated["unit"] == "联合实验室"
    assert updated["expertise"] == "软件工程"
    assert updated["phone"] == "137****2222"


def test_expert_group_is_a_reusable_group_not_a_meeting_workflow(expert_service):
    service, _audit = expert_service

    group = service.create_expert_group(
        {"groupName": "通信与材料专家组"}, creator="operator"
    )
    service.add_expert_group_member(
        group["groupId"], "EXP-001", selected_by="operator"
    )
    detail = service.get_expert_group(group["groupId"])

    assert detail["groupName"] == "通信与材料专家组"
    assert "meetingName" not in detail
    assert [member["name"] for member in detail["members"]] == ["张老师"]
    assert detail["members"][0]["phone"] == "138****5678"
    with pytest.raises(ResourceServiceError) as caught:
        service.add_expert_group_member(
            group["groupId"], "EXP-001", selected_by="operator"
        )
    assert caught.value.code == "GROUP_MEMBER_EXISTS"


def test_expert_groups_support_paged_list_remove_and_delete(expert_service):
    service, _audit = expert_service
    group = service.create_expert_group(
        {"groupName": "通信专家组"}, creator="operator"
    )
    service.add_expert_group_member(
        group["groupId"], "EXP-001", selected_by="operator"
    )

    page = service.list_expert_groups(page=1, page_size=10)
    assert page["total"] == 1
    assert page["items"][0]["groupName"] == "通信专家组"
    assert page["items"][0]["memberCount"] == 1

    service.remove_expert_group_member(group["groupId"], "EXP-001")
    assert service.get_expert_group(group["groupId"])["members"] == []
    service.delete_expert_group(group["groupId"])
    assert service.list_expert_groups(page=1, page_size=10)["total"] == 0


def test_expert_group_sensitive_export_is_explicitly_audited(expert_service):
    service, audit = expert_service
    group = service.create_expert_group(
        {"groupName": "材料专家组"}, creator="operator"
    )
    service.add_expert_group_member(
        group["groupId"], "EXP-001", selected_by="operator"
    )

    exported = service.export_expert_group_sensitive(
        group["groupId"], actor_user_id=7, request_id="req-group-export"
    )

    assert exported["members"][0]["phone"] == "13812345678"
    assert audit.events[-1]["properties"] == {
        "operation": "EXPORT", "record_count": 1,
    }
    assert "name" not in audit.events[-1]["properties"]


def test_referenced_expert_cannot_be_deleted(expert_service):
    service, _audit = expert_service
    group = service.create_expert_group(
        {"groupName": "科研专家组"}, creator="operator"
    )
    service.add_expert_group_member(
        group["groupId"], "EXP-002", selected_by="operator"
    )

    with pytest.raises(ResourceServiceError) as caught:
        service.delete_expert("EXP-002")

    assert caught.value.code == "EXPERT_IN_USE"


def test_expert_routes_use_postgres_service_and_mask_list(expert_routes):
    client, service, audit = expert_routes

    listing = client.get("/experts/")
    assert listing.status_code == 200
    html = listing.get_data(as_text=True)
    assert "138****5678" in html
    assert "13812345678" not in html
    assert "6222021234567890" not in html

    detail = client.get("/experts/edit/EXP-001")
    assert detail.status_code == 200
    assert "13812345678" in detail.get_data(as_text=True)
    assert audit.events[-1]["event_name"] == "expert_sensitive_accessed"

    created = client.post("/experts/add", data={
        "name": "王老师", "unit": "第三研究室", "expertise": "软件",
    })
    assert created.status_code == 302
    assert service.list_experts(keyword="王老师")["total"] == 1

    expert_id = service.list_experts(keyword="王老师")["items"][0]["expertId"]
    deleted = client.post(f"/experts/delete/{expert_id}")
    assert deleted.status_code == 200
    assert deleted.get_json()["success"] is True


def test_expert_import_route_session_holds_only_batch_reference(expert_routes):
    client, service, _audit = expert_routes
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["姓名", "单位"])
    sheet.append(["王老师", "第三研究室"])
    payload = BytesIO()
    workbook.save(payload)

    preview = client.post(
        "/experts/import/preview",
        data={
            "file": (BytesIO(payload.getvalue()), "experts.xlsx"),
            "column_mapping_json": '{"0":"name","1":"unit"}',
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    with client.session_transaction() as active_session:
        batch_id = active_session["expert_import_task_id"]
        assert isinstance(batch_id, str)
        assert "processed" not in active_session

    page = client.get("/experts/import/preview")
    assert page.status_code == 200
    assert "王老师" in page.get_data(as_text=True)

    cancelled = client.post("/experts/import/cancel")
    assert cancelled.status_code == 200
    assert service.get_expert_import_batch(batch_id, owner_user_id=7)["status"] == "CANCELLED"
    assert service.list_experts(keyword="王老师")["total"] == 0


def test_import_rejects_duplicate_row_selection(expert_service):
    service, _audit = expert_service
    batch = service.create_expert_import_preview(
        source_name="duplicate.xlsx", source_bytes=b"test",
        rows=[{"_row_idx": 9, "name": "王老师", "unit": "第三研究室"}],
        owner_user_id=7,
    )

    with pytest.raises(ResourceServiceError) as caught:
        service.commit_expert_import(
            batch["batchId"],
            confirmed_rows=[
                {"_row_idx": 9, "name": "王老师"},
                {"_row_idx": 9, "name": "王老师"},
            ],
            owner_user_id=7, uploader="张老师", request_id="req-duplicate",
        )
    assert caught.value.code == "VALIDATION_ERROR"


def test_import_rechecks_edited_rows_against_existing_experts(expert_service):
    service, _audit = expert_service
    batch = service.create_expert_import_preview(
        source_name="edited-after-preview.xlsx", source_bytes=b"test",
        rows=[{
            "_row_idx": 5, "name": "王老师", "unit": "第三研究室",
            "phone": "13700000000",
        }],
        owner_user_id=7,
    )

    with pytest.raises(ResourceServiceError) as caught:
        service.commit_expert_import(
            batch["batchId"],
            confirmed_rows=[{
                "_row_idx": 5, "name": "张老师", "unit": "第一研究室",
                "phone": "13812345678",
            }],
            owner_user_id=7, uploader="李老师", request_id="req-edited-duplicate",
        )

    assert caught.value.code == "EXPERT_DUPLICATE"
    assert service.list_experts(page=1, page_size=20)["total"] == 2
    assert service.get_expert_import_batch(
        batch["batchId"], owner_user_id=7
    )["status"] == "PREVIEW"


def test_legacy_null_identity_is_normalized_for_create_and_import(expert_service):
    service, _audit = expert_service
    now = datetime.now(timezone.utc)
    with service.repository.engine.begin() as connection:
        connection.execute(service.repository.experts.insert().values(
            expert_id="EXP-LEGACY-NULL", name="王老师", unit=None, phone=None,
            position=None, expertise=None, bank_card=None, bank_name=None,
            id_card=None, uploader="legacy", created_at=now, updated_at=now,
        ))

    with pytest.raises(ResourceServiceError) as caught:
        service.create_expert(
            {"name": "王老师", "unit": "", "phone": ""},
            uploader="张老师", actor_user_id=7, request_id="req-null-create",
        )
    assert caught.value.code == "EXPERT_DUPLICATE"

    preview = service.create_expert_import_preview(
        source_name="legacy-null.xlsx", source_bytes=b"test",
        rows=[{"_row_idx": 0, "name": "王老师", "unit": "", "phone": ""}],
        owner_user_id=7,
    )
    assert preview["duplicateCount"] == 1
    assert preview["rows"][0]["_duplicate"] is True


def test_import_rejects_non_object_confirmed_row(expert_service):
    service, _audit = expert_service
    batch = service.create_expert_import_preview(
        source_name="invalid-row.xlsx", source_bytes=b"test",
        rows=[{"_row_idx": 1, "name": "王老师"}], owner_user_id=7,
    )

    with pytest.raises(ResourceServiceError) as caught:
        service.commit_expert_import(
            batch["batchId"], confirmed_rows=[1],
            owner_user_id=7, uploader="李老师", request_id="req-invalid-row",
        )

    assert caught.value.code == "VALIDATION_ERROR"


def test_expert_export_neutralizes_formulas_and_group_search_uses_text_content(expert_routes):
    client, service, _audit = expert_routes
    service.create_expert(
        {"name": '=HYPERLINK("https://example.invalid","x")', "unit": "=1+1"},
        uploader="张老师", actor_user_id=7, request_id="req-formula",
    )

    exported = client.get("/experts/export")
    workbook = openpyxl.load_workbook(BytesIO(exported.data), data_only=False)
    formula_row = next(
        row for row in workbook.active.iter_rows(min_row=2)
        if "HYPERLINK" in str(row[2].value)
    )
    assert formula_row[2].data_type == "s"
    assert formula_row[2].value.startswith("'=")
    assert formula_row[3].data_type == "s"

    template = (ROOT / "app/templates/experts/group_edit.html").read_text(encoding="utf-8")
    assert "cell.textContent = e[field] || '';" in template
    assert "<td>${e.name" not in template


def test_expert_group_routes_preserve_grouping_without_meeting_scope(expert_routes):
    client, service, audit = expert_routes

    created = client.post(
        "/experts/groups/new", data={"group_name": "材料与制造专家组"}
    )
    assert created.status_code == 302
    group_id = service.list_expert_groups()["items"][0]["groupId"]

    added = client.post(
        f"/experts/groups/edit/{group_id}",
        data={"action": "add", "expert_id": "EXP-001"},
    )
    assert added.status_code == 302
    page = client.get(f"/experts/groups/edit/{group_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "张老师" in html
    assert "138****5678" in html
    assert "会议流程" not in html

    exported = client.get(f"/experts/groups/export/{group_id}")
    assert exported.status_code == 200
    assert audit.events[-1]["event_name"] == "expert_sensitive_accessed"


@pytest.mark.skipif(
    not os.environ.get("T10_TEST_DATABASE_URL"),
    reason="requires isolated PostgreSQL",
)
def test_postgresql_expert_runtime_contract():
    engine = sa.create_engine(os.environ["T10_TEST_DATABASE_URL"])
    repository = ResearchResourcesRepository(engine)
    service = ResearchResourcesService(repository, AuditRecorder())
    suffix = uuid.uuid4().hex[:10]
    username = f"t10_{suffix}"
    keyword = f"PG专家{suffix}"
    users = sa.Table("users", sa.MetaData(), autoload_with=engine)

    with engine.begin() as connection:
        owner_id = connection.execute(users.insert().values(
            username=username, password="test-only", role="BUSINESS_USER",
            name="王老师", status="active", directory_permissions={},
            must_change_password=False, version=1,
        ).returning(users.c.id)).scalar_one()
    try:
        created = service.create_expert(
            {"name": keyword, "unit": "第三研究室", "phone": "13612345678"},
            uploader="王老师", actor_user_id=owner_id, request_id="req-pg-create",
        )
        assert service.list_experts(keyword=keyword)["items"][0]["phone"] == "136****5678"

        batch = service.create_expert_import_preview(
            source_name="pg.xlsx", source_bytes=b"pg-test",
            rows=[{"_row_idx": 0, "name": f"{keyword}导入", "unit": "第三研究室"}],
            owner_user_id=owner_id,
        )
        committed = service.commit_expert_import(
            batch["batchId"], confirmed_rows=[{"_row_idx": 0, "name": f"{keyword}导入"}],
            owner_user_id=owner_id, uploader="王老师", request_id="req-pg-import",
        )
        assert committed["successCount"] == 1
        assert service.list_experts(keyword=keyword)["total"] == 2
    finally:
        with engine.begin() as connection:
            connection.execute(repository.expert_import_batches.delete().where(
                repository.expert_import_batches.c.owner_user_id == owner_id
            ))
            connection.execute(repository.experts.delete().where(
                repository.experts.c.name.like(f"{keyword}%")
            ))
            connection.execute(users.delete().where(users.c.id == owner_id))
        engine.dispose()


def test_expert_import_preview_and_commit_are_database_backed_and_atomic(expert_service):
    service, audit = expert_service
    preview = service.create_expert_import_preview(
        source_name="专家导入.xlsx",
        source_bytes=b"workbook",
        rows=[
            {"_row_idx": 0, "name": "王老师", "unit": "第三研究室", "expertise": "软件"},
            {"_row_idx": 1, "name": "陈老师", "unit": "第四研究室", "expertise": "测试"},
        ],
        owner_user_id=7,
    )

    assert preview["batchId"]
    assert preview["validCount"] == 2
    committed = service.commit_expert_import(
        preview["batchId"],
        confirmed_rows=preview["rows"],
        owner_user_id=7,
        uploader="operator",
        request_id="req-import",
    )

    assert committed["status"] == "COMMITTED"
    assert committed["successCount"] == 2
    listed = service.list_experts(page=1, page_size=20)
    assert listed["total"] == 4
    assert audit.events[-1]["event_name"] == "import_batch_completed"
    assert audit.events[-1]["properties"] == {
        "module": "experts", "valid_count": 2,
        "error_count": 0, "duplicate_count": 0,
    }


def test_expert_import_rolls_back_rows_and_batch_status_when_audit_fails(expert_engine):
    service = ResearchResourcesService(
        ResearchResourcesRepository(expert_engine), AuditRecorder(fail=True)
    )
    preview = service.create_expert_import_preview(
        source_name="专家导入.xlsx",
        source_bytes=b"workbook",
        rows=[{"_row_idx": 0, "name": "王老师", "unit": "第三研究室"}],
        owner_user_id=7,
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.commit_expert_import(
            preview["batchId"], confirmed_rows=preview["rows"],
            owner_user_id=7, uploader="operator", request_id="req-import",
        )

    assert service.list_experts(page=1, page_size=20)["total"] == 2
    assert service.get_expert_import_batch(
        preview["batchId"], owner_user_id=7
    )["status"] == "PREVIEW"


def test_cancelled_expert_import_cannot_be_committed(expert_service):
    service, _audit = expert_service
    preview = service.create_expert_import_preview(
        source_name="专家导入.xlsx", source_bytes=b"workbook",
        rows=[{"_row_idx": 0, "name": "王老师"}], owner_user_id=7,
    )

    cancelled = service.cancel_expert_import(
        preview["batchId"], owner_user_id=7
    )

    assert cancelled["status"] == "CANCELLED"
    with pytest.raises(ResourceServiceError) as caught:
        service.commit_expert_import(
            preview["batchId"], confirmed_rows=preview["rows"],
            owner_user_id=7, uploader="operator", request_id="req-import",
        )
    assert caught.value.code == "IMPORT_BATCH_CLOSED"


def test_sensitive_export_is_audited_once_without_sensitive_metadata(expert_service):
    service, audit = expert_service

    rows = service.export_experts_sensitive(
        expert_ids=["EXP-001", "EXP-002"], actor_user_id=7,
        request_id="req-export",
    )

    assert [row["phone"] for row in rows] == ["13812345678", "13987654321"]
    assert audit.events == [{
        "event_name": "expert_sensitive_accessed",
        "user_id": 7,
        "object_type": "EXPERT",
        "object_id": None,
        "result": "SUCCESS",
        "request_id": "req-export",
        "duration_ms": 0,
        "properties": {"operation": "EXPORT", "record_count": 2},
    }]
