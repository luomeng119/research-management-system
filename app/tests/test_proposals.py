from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import io
import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app import create_app
from app.repositories.proposals import ProposalsRepository
from app.repositories.audit import AuditRepository
from app.repositories.files import FilesRepository
from app.services.audit import AuditService
from app.services.files import FileService, FileServiceError
from app.services.proposals import ProposalService, ProposalServiceError


SOURCE = {
    "title": "便携式保障设备适配研究",
    "sourceType": "IDEA",
    "sourceSummary": "现有设备在特定场景存在适配问题",
    "researchProblem": "如何降低环境对稳定性的影响",
    "objectives": "形成可验证的适配方案",
    "researchContent": "环境试验与结构优化",
    "expectedOutcomes": "研究报告和样机",
}


def _schema(engine):
    metadata = sa.MetaData()
    common = lambda: (
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    sa.Table(
        "proposals", metadata, *common(),
        sa.Column("business_id", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("source_type", sa.Text),
        sa.Column("source_summary", sa.Text),
        sa.Column("research_problem", sa.Text),
        sa.Column("objectives", sa.Text),
        sa.Column("research_content", sa.Text),
        sa.Column("expected_outcomes", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="DRAFT"),
    )
    sa.Table(
        "proposal_argumentations", metadata, *common(),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("facts", sa.JSON, nullable=False),
        sa.Column("conclusion", sa.Text),
        sa.Column("basis", sa.Text),
    )
    sa.Table(
        "proposal_decisions", metadata, *common(),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("decision_date", sa.Date, nullable=False),
        sa.Column("conclusion", sa.Text, nullable=False),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
    )
    sa.Table(
        "proposal_ai_drafts", metadata, *common(),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="READY"),
        sa.Column("provider_kind", sa.Text, nullable=False),
        sa.Column("model_version", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("source_proposal_version", sa.Integer, nullable=False),
        sa.Column("input_hash", sa.Text),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("accepted_fields", sa.JSON, nullable=False, server_default="[]"),
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
    metadata.create_all(engine)


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
def service(engine):
    return ProposalService(ProposalsRepository(engine), Audit())


def _create(service, **changes):
    payload = {**SOURCE, **changes}
    return service.create(payload, actor_user_id=7, request_id="req-create")


def _rows(engine, name):
    table = sa.Table(name, sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table)).mappings()]


def test_create_uses_server_identity_and_list_is_paginated(service):
    first = _create(service, status="REJECTED", creator="spoofed")
    _create(service, title="第二个提案", sourceType="CREATIVE")

    assert first["businessId"].startswith("TP-")
    assert first["status"] == "DRAFT"
    assert first["version"] == 1
    page = service.list(page=1, page_size=1, status="DRAFT", keyword="提案")
    assert page["total"] == 1
    assert len(page["items"]) == 1
    assert page["items"][0]["title"] == "第二个提案"
    assert "sourceSummary" not in page["items"][0]
    creative = service.list(page=1, page_size=20, source_type="CREATIVE")
    assert [item["title"] for item in creative["items"]] == ["第二个提案"]

    with pytest.raises(ProposalServiceError) as naive_time:
        service.list(updated_after="2026-09-02T10:00:00")
    assert naive_time.value.code == "VALIDATION_ERROR"
    assert "时区" in naive_time.value.message


def test_validation_reports_fields_without_writing(service, engine):
    invalid = {**SOURCE, "title": " ", "sourceType": "MEETING"}
    with pytest.raises(ProposalServiceError) as captured:
        service.create(invalid, actor_user_id=7, request_id="req-invalid")

    assert captured.value.status_code == 422
    assert set(captured.value.fields) == {"title", "sourceType"}
    assert _rows(engine, "proposals") == []

    for marker in ("\u200b\ufeff", "\ufe0f", "\u034f"):
        invisible = {**SOURCE, "title": marker}
        with pytest.raises(ProposalServiceError) as invisible_error:
            service.create(invisible, actor_user_id=7, request_id="req-invisible")
        assert invisible_error.value.code == "VALIDATION_ERROR"
        assert "title" in invisible_error.value.fields
        assert _rows(engine, "proposals") == []

    with pytest.raises(ProposalServiceError) as oversized:
        _create(service, researchContent="x" * 50_001)
    assert oversized.value.code == "VALIDATION_ERROR"
    assert "researchContent" in oversized.value.fields


def test_patch_is_allowlisted_draft_only_and_uses_optimistic_lock(service):
    created = _create(service)
    edited = service.update(
        created["businessId"], {"title": "更新名称"},
        expected_version=1, actor_user_id=7, request_id="req-edit",
    )
    assert edited["title"] == "更新名称"
    assert edited["status"] == "DRAFT"
    assert edited["version"] == 2
    assert service.audit_service.events[-1]["event_name"] == "proposal_updated"
    assert service.audit_service.events[-1]["properties"] == {"field_count": 1}

    with pytest.raises(ProposalServiceError) as captured:
        service.update(
            created["businessId"], {"title": "过期修改"}, expected_version=1,
            actor_user_id=7, request_id="req-stale",
        )
    assert captured.value.code == "VERSION_CONFLICT"
    assert captured.value.status_code == 409

    with pytest.raises(ProposalServiceError) as controlled:
        service.update(
            created["businessId"], {"status": "REJECTED"}, expected_version=2,
            actor_user_id=7, request_id="req-status-patch",
        )
    assert controlled.value.code == "SYSTEM_FIELD_NOT_WRITABLE"

    with pytest.raises(ProposalServiceError) as empty:
        service.update(
            created["businessId"], {}, expected_version=2,
            actor_user_id=7, request_id="req-empty-patch",
        )
    assert empty.value.code == "VALIDATION_ERROR"


def test_argumentation_starts_once_and_duplicate_version_rolls_back(service, engine):
    created = _create(service)
    result = service.add_argumentation(
        created["businessId"],
        {"summary": "必要性和可行性成立", "argumentationDate": "2026-09-02"},
        conclusion="继续论证", basis="已有试验证据", expected_version=1,
        actor_user_id=7, request_id="req-argument",
    )
    assert result["proposal"]["status"] == "ARGUMENTATION"
    assert result["proposal"]["version"] == 2

    with pytest.raises(ProposalServiceError) as captured:
        service.add_argumentation(
            created["businessId"],
            {"summary": "重复", "argumentationDate": "2026-09-02"},
            conclusion="重复", basis="重复", expected_version=1,
            actor_user_id=7, request_id="req-duplicate",
        )
    assert captured.value.code == "VERSION_CONFLICT"
    assert len(_rows(engine, "proposal_argumentations")) == 1

    with pytest.raises(ProposalServiceError) as unknown_fact:
        service.add_argumentation(
            created["businessId"],
            {"summary": "摘要", "argumentationDate": "2026-09-02", "meetingAttendees": ["不应建模"]},
            conclusion="结论", basis="依据", expected_version=2,
            actor_user_id=7, request_id="req-extra-fact",
        )
    assert unknown_fact.value.code == "VALIDATION_ERROR"

    with pytest.raises(ProposalServiceError) as oversized_fact:
        service.add_argumentation(
            created["businessId"],
            {"summary": "x" * 20_001, "argumentationDate": "2026-09-02"},
            conclusion="结论", basis="依据", expected_version=2,
            actor_user_id=7, request_id="req-large-fact",
        )
    assert oversized_fact.value.code == "VALIDATION_ERROR"

    with pytest.raises(ProposalServiceError) as non_text:
        service.add_argumentation(
            created["businessId"],
            {"summary": "摘要", "argumentationDate": "2026-09-02"},
            conclusion=["非文本"], basis="依据", expected_version=2,
            actor_user_id=7, request_id="req-non-text",
        )
    assert non_text.value.code == "VALIDATION_ERROR"


def test_defer_is_idempotent_and_reopen_returns_to_argumentation(service, engine):
    created = _create(service)
    started = service.add_argumentation(
        created["businessId"],
        {"summary": "待补证", "argumentationDate": "2026-09-02"},
        conclusion="待补证", basis="证据不足", expected_version=1,
        actor_user_id=7, request_id="req-start",
    )["proposal"]
    payload = {
        "decision": "DEFER", "decisionDate": "2026-09-02",
        "conclusion": "暂缓", "basis": "需要补充试验",
    }
    first = service.decide(
        created["businessId"], payload, idempotency_key="decision-001",
        expected_version=started["version"], actor_user_id=7, request_id="req-defer",
    )
    second = service.decide(
        created["businessId"], payload, idempotency_key="decision-001",
        expected_version=started["version"], actor_user_id=7, request_id="req-retry",
    )
    assert service.get(created["businessId"])["status"] == "DEFERRED"
    assert first["decision"] == second["decision"]
    assert second["_reused"] is True
    assert len(_rows(engine, "proposal_decisions")) == 1

    reopened = service.reopen(
        created["businessId"], expected_version=3,
        actor_user_id=7, request_id="req-reopen",
    )
    assert reopened["status"] == "ARGUMENTATION"
    replayed = service.decide(
        created["businessId"], payload, idempotency_key="decision-001",
        expected_version=started["version"], actor_user_id=7, request_id="req-late-retry",
    )
    assert replayed["decision"] == first["decision"]
    history = service.list_decisions(created["businessId"], page=1, page_size=1)
    assert history["total"] == 1
    assert len(history["items"]) == 1
    with pytest.raises(ProposalServiceError) as unbounded:
        service.list_decisions(created["businessId"], page=1, page_size=101)
    assert unbounded.value.code == "VALIDATION_ERROR"


def test_idempotency_key_cannot_be_reused_for_another_decision(service):
    created = _create(service)
    started = service.add_argumentation(
        created["businessId"],
        {"summary": "已论证", "argumentationDate": "2026-09-02"},
        conclusion="可决定", basis="证据", expected_version=1,
        actor_user_id=7, request_id="req-start",
    )["proposal"]
    service.decide(
        created["businessId"],
        {"decision": "DEFER", "decisionDate": "2026-09-02", "conclusion": "暂缓", "basis": "补证"},
        idempotency_key="decision-001", expected_version=started["version"],
        actor_user_id=7, request_id="req-one",
    )
    with pytest.raises(ProposalServiceError) as captured:
        service.decide(
            created["businessId"],
            {"decision": "REJECT", "decisionDate": "2026-09-02", "conclusion": "不立项", "basis": "不符合方向"},
            idempotency_key="decision-001", expected_version=2,
            actor_user_id=7, request_id="req-two",
        )
    assert captured.value.code == "DUPLICATE_OPERATION"


def test_reject_is_terminal_and_establish_waits_for_atomic_project_slice(service):
    created = _create(service)
    started = service.add_argumentation(
        created["businessId"],
        {"summary": "已论证", "argumentationDate": "2026-09-02"},
        conclusion="可决定", basis="证据", expected_version=1,
        actor_user_id=7, request_id="req-start",
    )["proposal"]
    with pytest.raises(ProposalServiceError) as establish:
        service.decide(
            created["businessId"],
            {"decision": "ESTABLISH", "decisionDate": "2026-09-02", "conclusion": "立项", "basis": "通过"},
            idempotency_key="decision-establish", expected_version=started["version"],
            actor_user_id=7, request_id="req-establish",
        )
    assert establish.value.code == "STATE_CONFLICT"
    assert establish.value.status_code == 409

    with pytest.raises(ProposalServiceError) as non_text:
        service.decide(
            created["businessId"],
            {"decision": "REJECT", "decisionDate": "2026-09-02", "conclusion": ["非文本"], "basis": "依据"},
            idempotency_key="decision-non-text", expected_version=started["version"],
            actor_user_id=7, request_id="req-non-text",
        )
    assert non_text.value.code == "VALIDATION_ERROR"

    service.decide(
        created["businessId"],
        {"decision": "REJECT", "decisionDate": "2026-09-02", "conclusion": "不立项", "basis": "不符合方向"},
        idempotency_key="decision-reject", expected_version=started["version"],
        actor_user_id=7, request_id="req-reject",
    )
    rejected = service.get(created["businessId"])
    assert rejected["status"] == "REJECTED"
    with pytest.raises(ProposalServiceError) as terminal:
        service.reopen(
            created["businessId"], expected_version=rejected["version"],
            actor_user_id=7, request_id="req-invalid-reopen",
        )
    assert terminal.value.code == "STATE_CONFLICT"


def test_argumentation_can_return_to_draft_for_supplement(service):
    created = _create(service)
    started = service.add_argumentation(
        created["businessId"],
        {"summary": "需补充", "argumentationDate": "2026-09-02"},
        conclusion="退回补充", basis="证据不足", expected_version=1,
        actor_user_id=7, request_id="req-start",
    )["proposal"]
    returned = service.return_to_draft(
        created["businessId"], expected_version=started["version"],
        actor_user_id=7, request_id="req-return",
    )
    assert returned["status"] == "DRAFT"
    edited = service.update(
        created["businessId"], {"researchContent": "补充后的研究内容"},
        expected_version=returned["version"], actor_user_id=7, request_id="req-supplement",
    )
    assert edited["researchContent"] == "补充后的研究内容"


def test_audit_failure_rolls_back_state_and_decision(engine):
    service = ProposalService(ProposalsRepository(engine), Audit(fail=True))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        _create(service)
    assert _rows(engine, "proposals") == []


def test_decision_audit_failure_rolls_back_decision_status_and_version(engine):
    audit = Audit()
    service = ProposalService(ProposalsRepository(engine), audit)
    created = _create(service)
    started = service.add_argumentation(
        created["businessId"],
        {"summary": "已论证", "argumentationDate": "2026-09-02"},
        conclusion="可决定", basis="证据", expected_version=1,
        actor_user_id=7, request_id="req-start",
    )["proposal"]
    audit.fail = True

    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.decide(
            created["businessId"],
            {"decision": "DEFER", "decisionDate": "2026-09-02", "conclusion": "暂缓", "basis": "补证"},
            idempotency_key="rollback-key", expected_version=started["version"],
            actor_user_id=7, request_id="req-rollback",
        )

    current = service.get(created["businessId"])
    assert (current["status"], current["version"]) == ("ARGUMENTATION", 2)
    assert _rows(engine, "proposal_decisions") == []


def test_proposal_attachments_reuse_file_service_with_business_id(
    tmp_path, engine, service
):
    created = _create(service)
    files = FileService(
        FilesRepository(engine), Audit(), storage_root=tmp_path / "files",
        max_bytes=1024, preview_max_bytes=512,
    )
    uploaded = files.upload(
        io.BytesIO(b"%PDF-1.7\nevidence"), original_name="依据.pdf",
        object_type="PROPOSAL", object_id=created["businessId"],
        actor_user_id=7, request_id="req-file",
    )
    assert files.list_for_object(
        object_type="PROPOSAL", object_id=created["businessId"]
    )[0]["fileId"] == uploaded["fileId"]

    with pytest.raises(FileServiceError) as wrong_identifier:
        files.upload(
            io.BytesIO(b"%PDF-1.7\nwrong"), original_name="错误.pdf",
            object_type="PROPOSAL", object_id=created["id"],
            actor_user_id=7, request_id="req-wrong-id",
        )
    assert wrong_identifier.value.code == "OBJECT_NOT_FOUND"
    assert service.get(created["businessId"])["status"] == "DRAFT"

    started = service.add_argumentation(
        created["businessId"],
        {"summary": "已论证", "argumentationDate": "2026-09-02"},
        conclusion="可处理", basis="依据", expected_version=1,
        actor_user_id=7, request_id="req-file-terminal-start",
    )["proposal"]
    service.decide(
        created["businessId"],
        {"decision": "REJECT", "decisionDate": "2026-09-02", "conclusion": "不立项", "basis": "不符合方向"},
        idempotency_key="file-terminal-reject", expected_version=started["version"],
        actor_user_id=7, request_id="req-file-terminal-reject",
    )
    with pytest.raises(FileServiceError) as read_only:
        files.upload(
            io.BytesIO(b"late"), original_name="late.txt",
            object_type="PROPOSAL", object_id=created["businessId"],
            actor_user_id=7, request_id="req-file-terminal-upload",
        )
    assert read_only.value.code == "OBJECT_READ_ONLY"


class Files:
    def list_for_object(self, *, object_type, object_id):
        assert object_type == "PROPOSAL"
        return [{"fileId": "file-1", "objectId": object_id}]


def test_api_detail_includes_existing_file_links_and_form_error_preserves_input(
    tmp_path, engine, service
):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATA_DIR": str(tmp_path / "data"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "DATABASE_ENGINE": engine,
        "PROPOSAL_SERVICE": service,
        "FILE_SERVICE": Files(),
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "LOG_FILE": None,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=7, user="alice", name="Alice", role="BUSINESS_USER",
            account_version=1,
        )

    assert client.get("/proposals").status_code == 200
    list_api = client.get("/api/proposals").get_json()
    assert set(list_api) == {"data", "pagination"}
    assert set(list_api["pagination"]) == {"page", "pageSize", "totalItems", "totalPages"}

    invalid = client.post("/proposals/new", data={**SOURCE, "title": ""})
    assert invalid.status_code == 422
    assert SOURCE["sourceSummary"] in invalid.get_data(as_text=True)
    assert "请填写提案名称" in invalid.get_data(as_text=True)

    created = _create(service)
    _create(service, title="分页验证提案")
    first_page = client.get("/proposals?page=1&pageSize=1")
    assert first_page.status_code == 200
    assert "page=2" in first_page.get_data(as_text=True)
    second_page = client.get("/proposals?page=2&pageSize=1")
    assert second_page.status_code == 200
    assert "page=1" in second_page.get_data(as_text=True)
    response = client.get(f"/api/proposals/{created['businessId']}")
    assert response.status_code == 200
    assert response.get_json()["files"] == [
        {"fileId": "file-1", "objectId": created["businessId"]}
    ]
    updated = client.patch(
        f"/api/proposals/{created['businessId']}",
        json={"title": "API 修改", "version": 1},
    )
    assert updated.status_code == 200
    assert updated.get_json()["title"] == "API 修改"


def test_api_decision_replay_has_identical_status_and_json(tmp_path, engine, service):
    client = _web_client(tmp_path, engine, service)
    invalid = client.post("/api/proposals", json={**SOURCE, "title": ""})
    assert invalid.status_code == 422
    assert invalid.get_json()["error"]["details"]["title"] == ["请填写提案名称"]

    created = _create(service)
    started = service.add_argumentation(
        created["businessId"],
        {"summary": "API 幂等验证", "argumentationDate": "2026-09-02"},
        conclusion="可处理", basis="证据完整", expected_version=1,
        actor_user_id=7, request_id="req-api-idempotency-start",
    )["proposal"]
    payload = {
        "decision": "DEFER", "decisionDate": "2026-09-02",
        "conclusion": "暂缓", "basis": "补充试验", "version": started["version"],
    }
    url = f"/api/proposals/{created['businessId']}/decisions"
    first = client.post(url, json=payload, headers={"Idempotency-Key": "api-same-key"})
    replay = client.post(url, json=payload, headers={"Idempotency-Key": "api-same-key"})
    assert first.status_code == replay.status_code == 201
    assert first.get_json() == replay.get_json()


def _web_client(tmp_path, engine, service, *, csrf=False, file_service=None):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATA_DIR": str(tmp_path / "data"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "DATABASE_ENGINE": engine,
        "PROPOSAL_SERVICE": service,
        "FILE_SERVICE": file_service or Files(),
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": csrf,
        "LOG_FILE": None,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=7, user="alice", name="Alice", role="BUSINESS_USER",
            account_version=1,
        )
    return client


def test_detail_page_exposes_complete_proposal_workflow(tmp_path, engine, service):
    files = FileService(
        FilesRepository(engine), service.audit_service,
        storage_root=tmp_path / "web-files", max_bytes=1024, preview_max_bytes=512,
    )
    client = _web_client(tmp_path, engine, service, file_service=files)
    created = _create(service)
    detail_url = f"/proposals/{created['businessId']}"

    draft = client.get(detail_url)
    assert draft.status_code == 200
    draft_html = draft.get_data(as_text=True)
    assert "编辑提案" in draft_html
    assert "上传附件" in draft_html
    assert "新增论证" in draft_html

    edited = client.post(
        f"{detail_url}/edit",
        data={**SOURCE, "title": "已编辑的提案", "version": "1"},
        follow_redirects=True,
    )
    assert edited.status_code == 200
    assert "已编辑的提案" in edited.get_data(as_text=True)
    uploaded = client.post(
        f"{detail_url}/attachments",
        data={"file": (io.BytesIO(b"web evidence"), "evidence.txt")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert uploaded.status_code == 200
    assert "evidence.txt" in uploaded.get_data(as_text=True)

    started = client.post(
        f"{detail_url}/argumentations",
        data={
            "version": "2", "summary": "必要性和可行性成立",
            "argumentationDate": "2026-09-02", "conclusion": "可处理",
            "basis": "已有试验证据",
        },
        follow_redirects=True,
    )
    assert started.status_code == 200
    started_html = started.get_data(as_text=True)
    assert "暂缓" in started_html
    assert "不立项" in started_html
    assert "退回补充" in started_html

    returned = client.post(
        f"{detail_url}/return-to-draft", data={"version": "3"},
        follow_redirects=True,
    )
    assert returned.status_code == 200
    assert "编辑提案" in returned.get_data(as_text=True)
    restarted = client.post(
        f"{detail_url}/argumentations",
        data={
            "version": "4", "summary": "补充后重新论证",
            "argumentationDate": "2026-09-02", "conclusion": "可处理",
            "basis": "补充证据已完成",
        },
        follow_redirects=True,
    )
    assert restarted.status_code == 200
    assert "补充后重新论证" in restarted.get_data(as_text=True)

    deferred = client.post(
        f"{detail_url}/decisions",
        data={
            "version": "5", "decision": "DEFER", "decisionDate": "2026-09-02",
            "conclusion": "暂缓", "basis": "需补充试验", "idempotencyKey": "html-defer-1",
        },
        follow_redirects=True,
    )
    assert deferred.status_code == 200
    assert "重新论证" in deferred.get_data(as_text=True)

    reopened = client.post(
        f"{detail_url}/reopen", data={"version": "6"}, follow_redirects=True
    )
    assert reopened.status_code == 200
    assert "论证处理" in reopened.get_data(as_text=True)

    rejected = client.post(
        f"{detail_url}/decisions",
        data={
            "version": "7", "decision": "REJECT", "decisionDate": "2026-09-02",
            "conclusion": "不立项", "basis": "不符合当前方向", "idempotencyKey": "html-reject-1",
        },
        follow_redirects=True,
    )
    assert rejected.status_code == 200
    assert "不立项" in rejected.get_data(as_text=True)
    blocked_upload = client.post(
        f"{detail_url}/attachments",
        data={"file": (io.BytesIO(b"late"), "late.txt")},
        content_type="multipart/form-data",
    )
    assert blocked_upload.status_code == 409
    assert "终态提案不允许新增附件" in blocked_upload.get_data(as_text=True)


def test_new_proposal_form_works_with_csrf_enabled(tmp_path, engine, service):
    client = _web_client(tmp_path, engine, service, csrf=True)
    form = client.get("/proposals/new")
    form_html = form.get_data(as_text=True)
    assert re.search(r'<option value="IDEA"\s+selected>\s*想法\s*</option>', form_html)
    token = re.search(
        r'name="_csrf_token" value="([^"]+)"', form_html
    ).group(1)
    created = client.post(
        "/proposals/new", data={**SOURCE, "_csrf_token": token},
        follow_redirects=False,
    )
    assert created.status_code == 302
    assert "/proposals/TP-" in created.headers["Location"]


def test_new_proposal_validation_does_not_replace_explicit_empty_source(
    tmp_path, engine, service,
):
    client = _web_client(tmp_path, engine, service, csrf=False)
    response = client.post("/proposals/new", data={**SOURCE, "sourceType": ""})
    html = response.get_data(as_text=True)

    assert response.status_code == 422
    assert not re.search(r'<option value="IDEA"\s+selected>', html)


def test_api_internal_failure_is_fixed_json(tmp_path, engine):
    failing = ProposalService(ProposalsRepository(engine), Audit(fail=True))
    app = create_app({
        "TESTING": True, "SECRET_KEY": "test-secret",
        "DATA_DIR": str(tmp_path / "data"), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "DATABASE_ENGINE": engine, "PROPOSAL_SERVICE": failing,
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False, "LOG_FILE": None,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7, user="alice", name="Alice", role="BUSINESS_USER", account_version=1)
    response = client.post("/api/proposals", json=SOURCE)
    assert response.status_code == 500
    assert response.is_json
    assert response.get_json()["error"]["code"] == "INTERNAL_ERROR"


def test_api_request_too_large_preserves_413_json(tmp_path, engine, service):
    app = create_app({
        "TESTING": True, "SECRET_KEY": "test-secret", "MAX_CONTENT_LENGTH": 20,
        "DATA_DIR": str(tmp_path / "data"), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "DATABASE_ENGINE": engine, "PROPOSAL_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False, "LOG_FILE": None,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7, user="alice", name="Alice", role="BUSINESS_USER", account_version=1)
    response = client.post("/api/proposals", json=SOURCE)
    assert response.status_code == 413
    assert response.is_json
    assert response.get_json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.skipif(
    not os.environ.get("T06_TEST_DATABASE_URL"),
    reason="requires isolated migrated PostgreSQL",
)
def test_postgresql_concurrent_version_and_decision_idempotency():
    engine = sa.create_engine(os.environ["T06_TEST_DATABASE_URL"], pool_pre_ping=True)
    users = sa.Table("users", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        actor_id = connection.execute(
            users.insert().values(
                username="t06-actor", password="disabled", role="BUSINESS_USER",
                name="T06", status="active", directory_permissions={},
                must_change_password=True, version=1,
            ).returning(users.c.id)
        ).scalar_one()
    service = ProposalService(
        ProposalsRepository(engine), AuditService(AuditRepository(engine), "test-v1")
    )
    created = service.create(SOURCE, actor_user_id=actor_id, request_id="req-pg-create")

    edit_barrier = Barrier(2)

    def edit(title):
        edit_barrier.wait()
        try:
            return service.update(
                created["businessId"], {"title": title}, expected_version=1,
                actor_user_id=actor_id, request_id=f"req-{title}",
            )
        except ProposalServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        edit_results = list(pool.map(edit, ("并发修改 A", "并发修改 B")))
    assert sum(isinstance(item, dict) for item in edit_results) == 1
    assert edit_results.count("VERSION_CONFLICT") == 1
    current = service.get(created["businessId"])
    started = service.add_argumentation(
        created["businessId"],
        {"summary": "并发决定验证", "argumentationDate": "2026-09-02"},
        conclusion="可决定", basis="证据", expected_version=current["version"],
        actor_user_id=actor_id, request_id="req-pg-start",
    )["proposal"]
    decision = {
        "decision": "DEFER", "decisionDate": "2026-09-02",
        "conclusion": "暂缓", "basis": "补充试验",
    }

    decision_barrier = Barrier(2)

    def decide(index):
        decision_barrier.wait()
        return service.decide(
            created["businessId"], decision, idempotency_key="pg-same-key",
            expected_version=started["version"], actor_user_id=actor_id,
            request_id=f"req-pg-decision-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(decide, (1, 2)))
    assert sorted(item["_reused"] for item in decisions) == [False, True]
    assert decisions[0]["decision"] == decisions[1]["decision"]
    table = sa.Table("proposal_decisions", sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 1
    engine.dispose()
