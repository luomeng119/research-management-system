from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app import create_app
from app.repositories.audit import AuditRepository
from app.repositories.projects import ProjectsRepository
from app.services.audit import AuditService
from app.services.projects import ProjectService, ProjectServiceError


class AuditRecorder:
    def __init__(self):
        self.events = []

    def record(self, connection, **values):
        self.events.append(values)


def _common_columns():
    return (
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False, default=1),
    )


def _schema(engine):
    metadata = sa.MetaData()
    sa.Table(
        "proposals", metadata, *_common_columns(),
        sa.Column("business_id", sa.Text, unique=True),
        sa.Column("status", sa.Text),
    )
    sa.Table(
        "proposal_decisions", metadata, *_common_columns(),
        sa.Column("proposal_id", sa.String(36)),
        sa.Column("decision", sa.Text),
        sa.Column("decision_date", sa.Date),
        sa.Column("conclusion", sa.Text),
        sa.Column("basis", sa.Text),
        sa.Column("idempotency_key", sa.Text, unique=True),
    )
    sa.Table(
        "project_registry", metadata, *_common_columns(),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("business_id", sa.Text, nullable=False, unique=True),
        sa.Column("proposal_id", sa.String(36)),
        sa.Column("status", sa.Text, nullable=False),
    )
    for table_name in ("projects", "security_projects", "crypto_projects"):
        sa.Table(
            table_name, metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.Text, nullable=False, unique=True),
            sa.Column("registry_id", sa.String(36), unique=True),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("leader", sa.Text),
            sa.Column("start_date", sa.Date),
            sa.Column("planned_end_date", sa.Date),
            sa.Column("actual_end_date", sa.Date),
            sa.Column("status", sa.Text),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("task_number", sa.Text),
        )
    sa.Table(
        "project_progress", metadata, *_common_columns(),
        sa.Column("project_registry_id", sa.String(36), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("risk_level", sa.Text),
        sa.Column("issues", sa.Text),
        sa.Column("next_actions", sa.Text),
    )
    sa.Table(
        "project_changes", metadata, *_common_columns(),
        sa.Column("project_registry_id", sa.String(36), nullable=False),
        sa.Column("change_type", sa.Text, nullable=False),
        sa.Column("before_summary", sa.Text),
        sa.Column("after_summary", sa.Text, nullable=False),
        sa.Column("basis", sa.Text),
        sa.Column("decision", sa.Text),
        sa.Column("decision_date", sa.Date),
    )
    sa.Table(
        "project_outputs", metadata, *_common_columns(),
        sa.Column("project_registry_id", sa.String(36), nullable=False),
        sa.Column("output_type", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("formed_date", sa.Date),
        sa.Column("contributors", sa.Text),
    )
    sa.Table(
        "project_closures", metadata, *_common_columns(),
        sa.Column("project_registry_id", sa.String(36), nullable=False, unique=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conclusion", sa.Text),
        sa.Column("remaining_issues", sa.Text),
        sa.Column("no_output_reason", sa.Text),
    )
    metadata.create_all(engine)
    return metadata


@pytest.fixture()
def lifecycle():
    engine = sa.create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata = _schema(engine)
    registry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(metadata.tables["project_registry"].insert().values(
            id=registry_id,
            category="GENERAL_RESEARCH",
            business_id="KY-2026-001",
            proposal_id=None,
            status="PENDING",
            created_at=now,
            updated_at=now,
            created_by=7,
            updated_by=7,
            version=1,
        ))
        connection.execute(metadata.tables["projects"].insert().values(
            project_id="KY-2026-001",
            registry_id=registry_id,
            name="便携式保障设备适配研究",
            leader="张老师",
            status="任务下达",
            created_at=now,
        ))
    audit = AuditRecorder()
    service = ProjectService(ProjectsRepository(engine), audit)
    yield service, engine, audit, registry_id
    engine.dispose()


def test_progress_supports_all_read_only_path_states_and_lists_newest_first(lifecycle):
    service, _, audit, registry_id = lifecycle
    service.transition_status(
        registry_id, {"toStatus": "ACTIVE", "reason": "启动", "version": 1},
        actor_user_id=7, request_id="req-start",
    )
    for index, status in enumerate(("NORMAL", "RISK", "BLOCKED"), 1):
        created = service.add_progress(
            registry_id,
            {
                "recordedAt": f"2026-09-0{index}T09:00:00+08:00",
                "status": status,
                "summary": f"第 {index} 条过程记录",
                "riskLevel": "HIGH" if status in {"RISK", "BLOCKED"} else None,
                "issues": "存在风险" if status in {"RISK", "BLOCKED"} else "",
                "nextActions": "继续核对数据",
                "version": index + 1,
            },
            actor_user_id=7,
            request_id=f"req-progress-{index}",
        )
        assert created["status"] == status
        assert created["createdBy"] == 7

    rows = service.list_progress(registry_id)
    assert [row["status"] for row in rows] == ["BLOCKED", "RISK", "NORMAL"]
    page = service.page_process_records(
        registry_id, record_type="PROGRESS", page=2, page_size=2
    )
    assert page["total"] == 3
    assert [row["status"] for row in page["items"]] == ["NORMAL"]
    assert [event["event_name"] for event in audit.events].count("project_record_added") == 3


def test_invalid_progress_does_not_write(lifecycle):
    service, engine, _, registry_id = lifecycle
    with pytest.raises(ProjectServiceError) as caught:
        service.add_progress(
            registry_id,
            {"recordedAt": "2026-09-02T09:00:00+08:00", "status": "PENDING", "summary": "非法任务"},
            actor_user_id=7,
            request_id="req-invalid",
        )
    assert caught.value.code == "VALIDATION_ERROR"
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM project_progress")) == 0


def test_status_transitions_are_controlled_versioned_and_sync_legacy_status(lifecycle):
    service, engine, audit, registry_id = lifecycle
    active = service.transition_status(
        registry_id,
        {"toStatus": "ACTIVE", "reason": "启动执行", "version": 1},
        actor_user_id=7,
        request_id="req-active",
    )
    assert active["status"] == "ACTIVE"
    assert active["version"] == 2

    with pytest.raises(ProjectServiceError) as illegal:
        service.transition_status(
            registry_id,
            {"toStatus": "CLOSED", "reason": "跳过结题", "version": 2},
            actor_user_id=7,
            request_id="req-illegal",
        )
    assert illegal.value.code == "STATE_CONFLICT"

    with pytest.raises(ProjectServiceError) as stale:
        service.transition_status(
            registry_id,
            {"toStatus": "PAUSED", "reason": "版本过期", "version": 1},
            actor_user_id=7,
            request_id="req-stale",
        )
    assert stale.value.code == "VERSION_CONFLICT"

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT status FROM projects")) == "执行中"
        assert connection.scalar(sa.text("SELECT count(*) FROM project_changes")) == 1
    assert audit.events[-1]["event_name"] == "project_status_changed"
    assert audit.events[-1]["properties"] == {"from_status": "PENDING", "to_status": "ACTIVE"}


def test_change_output_and_closure_complete_the_project_without_tasks(lifecycle):
    service, engine, _, registry_id = lifecycle
    service.add_change(
        registry_id,
        {
            "changeType": "PERIOD", "beforeSummary": "原计划",
            "afterSummary": "延长一个月", "basis": "试验条件调整",
            "decision": "AGREED", "decisionDate": "2026-09-02",
            "version": 1,
        },
        actor_user_id=7,
        request_id="req-change",
    )
    service.add_output(
        registry_id,
        {
            "outputType": "REPORT", "title": "适配研究报告",
            "description": "V1 成果", "formedDate": "2026-12-19",
            "contributors": "张老师、李老师",
            "version": 2,
        },
        actor_user_id=7,
        request_id="req-output",
    )
    service.transition_status(
        registry_id,
        {"toStatus": "ACTIVE", "reason": "启动", "version": 3},
        actor_user_id=7,
        request_id="req-start",
    )
    service.transition_status(
        registry_id,
        {"toStatus": "CLOSING", "reason": "进入结题", "version": 4},
        actor_user_id=7,
        request_id="req-closing",
    )
    closure = service.close_project(
        registry_id,
        {
            "closedAt": "2026-12-20T09:00:00+08:00",
            "conclusion": "PASS", "summary": "完成研究目标并形成报告",
            "remainingIssues": "无", "version": 5,
        },
        actor_user_id=7,
        request_id="req-close",
    )
    assert closure["projectStatus"] == "CLOSED"
    assert service.get_closure(registry_id)["summary"] == "完成研究目标并形成报告"

    with pytest.raises(ProjectServiceError) as duplicate:
        service.close_project(
            registry_id,
            {
                "closedAt": "2026-12-21T09:00:00+08:00",
                "conclusion": "PASS", "summary": "重复", "version": 6,
            },
            actor_user_id=7,
            request_id="req-duplicate",
        )
    assert duplicate.value.code == "STATE_CONFLICT"

    assert not sa.inspect(engine).has_table("tasks")
    assert not sa.inspect(engine).has_table("task_assignments")


def test_research_path_is_derived_read_only_and_contains_traceable_records(lifecycle):
    service, _, _, registry_id = lifecycle
    service.transition_status(
        registry_id, {"toStatus": "ACTIVE", "reason": "启动", "version": 1},
        actor_user_id=7, request_id="req-start",
    )
    for index, (status, summary) in enumerate(
        (("NORMAL", "依据确认"), ("RISK", "数据格式有风险"), ("BLOCKED", "样本尚未取得")),
        2,
    ):
        service.add_progress(
            registry_id,
            {
                "recordedAt": "2026-09-02T09:00:00+08:00",
                "status": status, "summary": summary, "version": index,
            },
            actor_user_id=7,
            request_id=f"req-{status.lower()}",
        )

    path = service.research_path(registry_id)
    assert path["readOnly"] is True
    assert path["project"]["businessId"] == "KY-2026-001"
    assert path["tree"]["data"]["owner"] == "张老师"
    progress_nodes = path["tree"]["children"][1]["children"]
    assert {node["data"]["status"] for node in progress_nodes} == {"done", "risk", "blocked"}
    assert all(node["data"]["source"].startswith("进展记录") for node in progress_nodes)
    assert "tasks" not in path


def test_project_detail_and_path_api_work_without_ai_or_graph_runtime(lifecycle, tmp_path):
    service, _, _, registry_id = lifecycle
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "lifecycle-test-secret",
        "DATA_DIR": str(tmp_path),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=7,
            user="zhang",
            name="张老师",
            role="BUSINESS_USER",
            account_version=1,
        )

    page = client.get(f"/projects/{registry_id}/overview")
    assert page.status_code == 200
    assert "便携式保障设备适配研究" in page.text
    assert "只呈现关系，不创建或派发任务" in page.text
    assert "/static/vendor/g6.min.js" in page.text
    assert "DeepSeek" not in page.text

    path = client.get(f"/api/projects/{registry_id}/research-path")
    assert path.status_code == 200
    assert path.json["readOnly"] is True


def test_project_detail_escapes_user_supplied_process_text(lifecycle, tmp_path):
    service, _, _, registry_id = lifecycle
    service.add_progress(
        registry_id,
        {
            "recordedAt": "2026-09-02T09:00:00+08:00",
            "status": "NORMAL",
            "summary": '<script>alert("process")</script>',
            "version": 1,
        },
        actor_user_id=7,
        request_id="req-escaped-process-text",
    )
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "lifecycle-escape-test-secret",
        "DATA_DIR": str(tmp_path),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=7,
            user="zhang",
            name="张老师",
            role="BUSINESS_USER",
            account_version=1,
        )

    page = client.get(f"/projects/{registry_id}/overview")
    assert page.status_code == 200
    assert '<script>alert("process")</script>' not in page.text
    assert "&lt;script&gt;alert" in page.text


def test_lifecycle_write_api_keeps_global_csrf_protection(lifecycle, tmp_path):
    service, _, _, registry_id = lifecycle
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "lifecycle-csrf-test-secret",
        "DATA_DIR": str(tmp_path),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": True,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=7,
            user="zhang",
            name="张老师",
            role="BUSINESS_USER",
            account_version=1,
        )
    assert client.get(f"/projects/{registry_id}/overview").status_code == 200
    with client.session_transaction() as session:
        token = session["csrf_token"]

    payload = {"toStatus": "ACTIVE", "reason": "正式启动", "version": 1}
    rejected = client.post(
        f"/api/projects/{registry_id}/status-transitions", json=payload
    )
    assert rejected.status_code == 403
    accepted = client.post(
        f"/api/projects/{registry_id}/status-transitions",
        json=payload,
        headers={"X-CSRF-Token": token},
    )
    assert accepted.status_code == 200
    assert accepted.json["status"] == "ACTIVE"


def test_failed_closure_can_be_completed_later_without_creating_attempt_tables(lifecycle):
    service, engine, _, registry_id = lifecycle
    service.transition_status(
        registry_id, {"toStatus": "ACTIVE", "reason": "启动", "version": 1},
        actor_user_id=7, request_id="req-start",
    )
    service.transition_status(
        registry_id, {"toStatus": "CLOSING", "reason": "首次结题", "version": 2},
        actor_user_id=7, request_id="req-closing-1",
    )
    with pytest.raises(ProjectServiceError) as missing_reason:
        service.close_project(
            registry_id,
            {
                "closedAt": "2026-12-01T09:00:00+08:00",
                "conclusion": "FAIL", "summary": "验证材料仍需补充",
                "version": 3,
            },
            actor_user_id=7,
            request_id="req-missing-output-reason",
        )
    assert missing_reason.value.code == "VALIDATION_ERROR"
    failed = service.close_project(
        registry_id,
        {
            "closedAt": "2026-12-01T09:00:00+08:00",
            "conclusion": "FAIL",
            "summary": "验证材料仍需补充",
            "remainingIssues": "缺少极端环境验证",
            "noOutputReason": "验证尚未完成",
            "version": 3,
        },
        actor_user_id=7,
        request_id="req-fail",
    )
    assert failed["projectStatus"] == "ACTIVE"

    service.transition_status(
        registry_id, {"toStatus": "CLOSING", "reason": "补充完成", "version": 4},
        actor_user_id=7, request_id="req-closing-2",
    )
    passed = service.close_project(
        registry_id,
        {
            "closedAt": "2026-12-20T09:00:00+08:00",
            "conclusion": "PASS",
            "summary": "补充验证后完成研究目标",
            "remainingIssues": "无",
            "noOutputReason": "本项目形成方法记录，未单独登记成果",
            "version": 5,
        },
        actor_user_id=7,
        request_id="req-pass",
    )
    assert passed["projectStatus"] == "CLOSED"
    assert passed["version"] == 2
    assert passed["createdAt"] == failed["createdAt"]
    assert passed["createdBy"] == failed["createdBy"]
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM project_closures")) == 1
        assert connection.scalar(sa.text("SELECT conclusion FROM project_closures")) == "PASS"


def test_no_output_closure_requires_reason_and_process_history_blocks_delete(lifecycle):
    service, engine, _, registry_id = lifecycle
    service.add_change(
        registry_id,
        {
            "changeType": "OTHER", "afterSummary": "记录前期调整",
            "basis": "项目事实", "decision": "FILED",
            "decisionDate": "2026-09-02", "version": 1,
        },
        actor_user_id=7,
        request_id="req-change",
    )
    with pytest.raises(ProjectServiceError) as deletion:
        service.delete_legacy_standalone(
            category="GENERAL_RESEARCH", business_id="KY-2026-001"
        )
    assert deletion.value.code == "STATE_CONFLICT"
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM project_registry")) == 1


@pytest.mark.skipif(
    not os.environ.get("T09_TEST_DATABASE_URL"),
    reason="requires isolated migrated PostgreSQL",
)
def test_postgresql_concurrent_record_and_termination_have_one_consistent_winner():
    engine = sa.create_engine(
        os.environ["T09_TEST_DATABASE_URL"], pool_size=4, max_overflow=0
    )
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=engine)
    registry = sa.Table("project_registry", metadata, autoload_with=engine)
    projects = sa.Table("projects", metadata, autoload_with=engine)
    progress = sa.Table("project_progress", metadata, autoload_with=engine)
    marker = uuid.uuid4().hex
    registry_id = uuid.uuid4()
    business_id = f"T09-PG-{marker}"
    with engine.begin() as connection:
        actor_id = connection.execute(
            users.insert().values(
                username=f"t09-{marker}", password="disabled",
                role="BUSINESS_USER", name="张老师", status="active",
                directory_permissions={}, must_change_password=True, version=1,
            ).returning(users.c.id)
        ).scalar_one()
        connection.execute(registry.insert().values(
            id=registry_id, category="GENERAL_RESEARCH", business_id=business_id,
            status="ACTIVE", created_by=actor_id, updated_by=actor_id, version=1,
        ))
        connection.execute(projects.insert().values(
            project_id=business_id, registry_id=registry_id,
            name="并发记录一致性测试", leader="张老师", status="执行中",
        ))

    service = ProjectService(
        ProjectsRepository(engine), AuditService(AuditRepository(engine), "test-v1")
    )
    barrier = Barrier(2)

    def add_record():
        barrier.wait()
        try:
            service.add_progress(
                registry_id,
                {
                    "recordedAt": "2026-09-02T09:00:00+08:00",
                    "status": "NORMAL", "summary": "并发进展", "version": 1,
                },
                actor_user_id=actor_id, request_id="req-t09-pg-progress",
            )
            return "PROGRESS"
        except ProjectServiceError as error:
            return error.code

    def terminate():
        barrier.wait()
        try:
            service.transition_status(
                registry_id,
                {"toStatus": "TERMINATED", "reason": "并发终止", "version": 1},
                actor_user_id=actor_id, request_id="req-t09-pg-terminate",
            )
            return "TERMINATED"
        except ProjectServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(add_record), pool.submit(terminate)]
        outcomes = [future.result() for future in results]
    assert sorted(outcomes) in (
        ["PROGRESS", "VERSION_CONFLICT"],
        ["STATE_CONFLICT", "TERMINATED"],
        ["TERMINATED", "VERSION_CONFLICT"],
    )
    with engine.connect() as connection:
        status = connection.scalar(
            sa.select(registry.c.status).where(registry.c.id == registry_id)
        )
        count = connection.scalar(
            sa.select(sa.func.count()).select_from(progress).where(
                progress.c.project_registry_id == registry_id
            )
        )
    assert (status, count) in (("ACTIVE", 1), ("TERMINATED", 0))
    engine.dispose()
