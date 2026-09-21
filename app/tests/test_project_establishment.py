from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import uuid

import pytest
import sqlalchemy as sa

from app import create_app
from app.repositories.projects import ProjectsRepository
from app.repositories.audit import AuditRepository
from app.services.audit import AuditService
from app.services.projects import ProjectService, ProjectServiceError


CATEGORY_TABLES = {
    "GENERAL_RESEARCH": "projects",
    "SECURITY_CONFIDENTIALITY": "security_projects",
    "CRYPTO_APPLICATION": "crypto_projects",
}


class AuditRecorder:
    def record(self, connection, **values):
        connection.execute(
            sa.text(
                "INSERT INTO audit_events (action, object_id) VALUES (:action, :object_id)"
            ),
            {
                "action": values["event_name"],
                "object_id": values.get("object_id"),
            },
        )


def _schema(engine):
    metadata = sa.MetaData()
    sa.Table(
        "proposals",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by", sa.Integer),
    )
    sa.Table(
        "proposal_decisions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("decision_date", sa.Date, nullable=False),
        sa.Column("conclusion", sa.Text, nullable=False),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text),
        sa.Column("result_snapshot", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False),
    )
    sa.Table(
        "project_registry",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("business_id", sa.Text, nullable=False),
        sa.Column("proposal_id", sa.String(36), unique=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False),
        sa.UniqueConstraint("category", "business_id"),
        sa.UniqueConstraint("business_id"),
    )
    for name in CATEGORY_TABLES.values():
        sa.Table(
            name,
            metadata,
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
    def lifecycle_common():
        return (
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_registry_id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("created_by", sa.Integer),
            sa.Column("updated_by", sa.Integer),
            sa.Column("version", sa.Integer, nullable=False, default=1),
        )
    sa.Table(
        "project_progress", metadata, *lifecycle_common(),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("risk_level", sa.Text),
        sa.Column("issues", sa.Text),
        sa.Column("next_actions", sa.Text),
    )
    sa.Table(
        "project_changes", metadata, *lifecycle_common(),
        sa.Column("change_type", sa.Text, nullable=False),
        sa.Column("before_summary", sa.Text),
        sa.Column("after_summary", sa.Text, nullable=False),
        sa.Column("basis", sa.Text),
        sa.Column("decision", sa.Text),
        sa.Column("decision_date", sa.Date),
    )
    sa.Table(
        "project_outputs", metadata, *lifecycle_common(),
        sa.Column("output_type", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("formed_date", sa.Date),
        sa.Column("contributors", sa.Text),
    )
    sa.Table(
        "project_closures", metadata, *lifecycle_common(),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conclusion", sa.Text),
        sa.Column("remaining_issues", sa.Text),
        sa.Column("no_output_reason", sa.Text),
        sa.UniqueConstraint("project_registry_id"),
    )
    sa.Table(
        "audit_events",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text),
    )
    metadata.create_all(engine)
    return metadata


@pytest.fixture
def engine():
    value = sa.create_engine("sqlite:///:memory:")
    metadata = _schema(value)
    proposal_id = str(uuid.uuid4())
    with value.begin() as connection:
        connection.execute(
            metadata.tables["proposals"].insert().values(
                id=proposal_id,
                business_id="TA-2026-0001",
                title="高寒环境便携供电研究",
                status="ARGUMENTATION",
                version=2,
                updated_at=datetime.now(timezone.utc),
                updated_by=7,
            )
        )
    yield value
    value.dispose()


@pytest.fixture
def service(engine):
    return ProjectService(ProjectsRepository(engine), AuditRecorder())


def _payload(category="GENERAL_RESEARCH", name="高寒环境便携供电研究"):
    return {
        "decision": "ESTABLISH",
        "decisionDate": "2026-09-02",
        "conclusion": "同意立项",
        "basis": "论证材料完整",
        "project": {
            "category": category,
            "name": name,
            "leader": "张老师",
            "plannedEndDate": "2027-09-01",
        },
    }


@pytest.mark.parametrize("category,table_name", CATEGORY_TABLES.items())
def test_establish_creates_one_category_project_atomically(
    service, engine, category, table_name
):
    result = service.establish_from_proposal(
        "TA-2026-0001",
        _payload(category),
        idempotency_key=f"establish-{category}",
        expected_version=2,
        actor_user_id=7,
        request_id="req-establish",
    )

    assert result["project"]["category"] == category
    assert result["project"]["sourceProposalId"]
    assert result["project"]["status"] == "PENDING"
    assert result["decision"]["decision"] == "ESTABLISH"
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT status FROM proposals")) == "ESTABLISHED"
        assert connection.scalar(sa.text("SELECT count(*) FROM proposal_decisions")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM project_registry")) == 1
        assert connection.scalar(sa.text(f"SELECT count(*) FROM {table_name}")) == 1
        assert sum(
            connection.scalar(sa.text(f"SELECT count(*) FROM {name}"))
            for name in CATEGORY_TABLES.values()
        ) == 1


def test_establish_same_idempotency_key_reuses_first_result(service, engine):
    first = service.establish_from_proposal(
        "TA-2026-0001", _payload(), idempotency_key="same-key",
        expected_version=2, actor_user_id=7, request_id="req-1",
    )
    second = service.establish_from_proposal(
        "TA-2026-0001", _payload(), idempotency_key="same-key",
        expected_version=2, actor_user_id=7, request_id="req-2",
    )

    assert second["_reused"] is True
    assert second["project"] == first["project"]
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM proposal_decisions")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM project_registry")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM projects")) == 1


def test_idempotent_replay_does_not_depend_on_later_project_edits(service, engine):
    first = service.establish_from_proposal(
        "TA-2026-0001", _payload(), idempotency_key="stable-key",
        expected_version=2, actor_user_id=7, request_id="req-1",
    )
    with engine.begin() as connection:
        connection.execute(sa.text(
            "UPDATE projects SET name = '后续修改的名称', leader = '李老师' "
            "WHERE project_id = :project_id"
        ), {"project_id": first["project"]["businessId"]})

    replay = service.establish_from_proposal(
        "TA-2026-0001", _payload(), idempotency_key="stable-key",
        expected_version=2, actor_user_id=7, request_id="req-2",
    )
    assert replay["_reused"] is True
    assert replay["project"] == first["project"]


def test_establish_rejects_reusing_key_with_different_payload(service):
    service.establish_from_proposal(
        "TA-2026-0001", _payload(), idempotency_key="same-key",
        expected_version=2, actor_user_id=7, request_id="req-1",
    )
    with pytest.raises(ProjectServiceError) as caught:
        service.establish_from_proposal(
            "TA-2026-0001", _payload(name="另一个项目"),
            idempotency_key="same-key", expected_version=2,
            actor_user_id=7, request_id="req-2",
        )
    assert caught.value.code == "DUPLICATE_OPERATION"
    assert caught.value.status_code == 409


@pytest.mark.parametrize(
    "path,value",
    (("conclusion", None), ("basis", []), ("name", None), ("leader", {"name": "张老师"})),
)
def test_establish_rejects_non_text_fields_as_validation_errors(service, path, value):
    payload = _payload()
    if path in {"name", "leader"}:
        payload["project"][path] = value
    else:
        payload[path] = value
    with pytest.raises(ProjectServiceError) as caught:
        service.establish_from_proposal(
            "TA-2026-0001", payload, idempotency_key=f"invalid-{path}",
            expected_version=2, actor_user_id=7, request_id="req-invalid",
        )
    assert caught.value.code == "VALIDATION_ERROR"
    assert caught.value.status_code == 422


def test_planned_end_date_is_optional(service):
    payload = _payload()
    payload["project"]["plannedEndDate"] = ""
    result = service.establish_from_proposal(
        "TA-2026-0001", payload, idempotency_key="optional-end",
        expected_version=2, actor_user_id=7, request_id="req-optional",
    )
    assert result["project"]["plannedEndDate"] is None


@pytest.mark.parametrize(
    "failure_point",
    ("insert_decision", "insert_registry", "insert_category_project", "update_proposal"),
)
def test_establish_rolls_back_every_write_when_any_step_fails(engine, failure_point):
    repository = ProjectsRepository(engine)
    original = getattr(repository, failure_point)

    def fail(*args, **kwargs):
        raise RuntimeError(f"injected-{failure_point}")

    setattr(repository, failure_point, fail)
    service = ProjectService(repository, AuditRecorder())
    with pytest.raises(RuntimeError, match=failure_point):
        service.establish_from_proposal(
            "TA-2026-0001", _payload(), idempotency_key="rollback-key",
            expected_version=2, actor_user_id=7, request_id="req-fail",
        )
    setattr(repository, failure_point, original)

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT status FROM proposals")) == "ARGUMENTATION"
        assert connection.scalar(sa.text("SELECT count(*) FROM proposal_decisions")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM project_registry")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM projects")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM audit_events")) == 0


def test_establish_rolls_back_when_audit_write_fails(engine):
    class FailingAudit:
        def record(self, connection, **values):
            raise RuntimeError("injected-audit")

    service = ProjectService(ProjectsRepository(engine), FailingAudit())
    with pytest.raises(RuntimeError, match="injected-audit"):
        service.establish_from_proposal(
            "TA-2026-0001", _payload(), idempotency_key="audit-failure",
            expected_version=2, actor_user_id=7, request_id="req-audit-fail",
        )
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT status FROM proposals")) == "ARGUMENTATION"
        assert connection.scalar(sa.text("SELECT count(*) FROM proposal_decisions")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM project_registry")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM projects")) == 0


def test_historical_project_without_proposal_remains_readable(service, engine):
    registry_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO project_registry "
            "(id, category, business_id, proposal_id, status, version) "
            "VALUES (:id, 'GENERAL_RESEARCH', 'KY-HISTORY-001', NULL, 'ACTIVE', 1)"
        ), {"id": registry_id})
        connection.execute(sa.text(
            "INSERT INTO projects (project_id, registry_id, name, leader, status) "
            "VALUES ('KY-HISTORY-001', :id, '历史科研项目', '王老师', '执行中')"
        ), {"id": registry_id})

    result = service.get(registry_id)
    assert result["businessId"] == "KY-HISTORY-001"
    assert result["sourceProposalId"] is None
    assert result["name"] == "历史科研项目"


def test_business_id_must_be_unique_across_categories_for_legacy_compatibility(service):
    general = service.create_standalone("GENERAL_RESEARCH", {
        "projectId": "SHARED-001", "name": "一般科研", "leader": "张老师"
    }, actor_user_id=7)
    with pytest.raises(ProjectServiceError) as caught:
        service.create_standalone("SECURITY_CONFIDENTIALITY", {
            "projectId": "SHARED-001", "name": "安全保密", "leader": "李老师"
        }, actor_user_id=7)

    assert caught.value.code == "DUPLICATE_PROJECT"
    assert service.get(general["id"])["name"] == "一般科研"
    assert service.get_legacy(
        category="GENERAL_RESEARCH", business_id="SHARED-001"
    )["name"] == "一般科研"


def test_legacy_update_uses_registry_and_source_project_cannot_be_deleted(service):
    established = service.establish_from_proposal(
        "TA-2026-0001", _payload(), idempotency_key="guard-delete",
        expected_version=2, actor_user_id=7, request_id="req-guard",
    )["project"]
    updated = service.update_legacy_field(
        category="GENERAL_RESEARCH", business_id=established["businessId"],
        field="status", value="通过院内评审", actor_user_id=7,
    )
    assert updated["status"] == "通过院内评审"
    assert service.get(established["id"])["status"] == "ACTIVE"
    with pytest.raises(ProjectServiceError) as caught:
        service.delete_legacy_standalone(
            category="GENERAL_RESEARCH", business_id=established["businessId"]
        )
    assert caught.value.code == "STATE_CONFLICT"


def test_legacy_standalone_delete_removes_registry_and_category_row(service):
    created = service.create_standalone("CRYPTO_APPLICATION", {
        "projectId": "MANUAL-DELETE", "name": "可删除的手工项目", "leader": "王老师"
    }, actor_user_id=7)
    service.delete_legacy_standalone(
        category="CRYPTO_APPLICATION", business_id=created["businessId"]
    )
    with pytest.raises(ProjectServiceError) as caught:
        service.get(created["id"])
    assert caught.value.code == "NOT_FOUND"


def test_establishment_and_unified_project_api_share_service(service, tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "project-test-secret",
        "DATA_DIR": str(tmp_path),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update({
            "user_id": 7,
            "user": "zhang",
            "name": "张老师",
            "role": "BUSINESS_USER",
            "account_version": 1,
        })

    response = client.post(
        "/api/proposals/TA-2026-0001/decisions",
        json={**_payload(), "version": 2},
        headers={"Idempotency-Key": "api-establish-1"},
    )
    assert response.status_code == 201
    project = response.get_json()["project"]

    detail = client.get(f"/api/projects/{project['id']}")
    assert detail.status_code == 200
    assert detail.get_json()["sourceProposalId"] == project["sourceProposalId"]
    listing = client.get("/api/projects?category=GENERAL_RESEARCH")
    assert listing.status_code == 200
    assert listing.get_json()["pagination"]["totalItems"] == 1
    assert listing.get_json()["data"][0]["businessId"] == project["businessId"]


def test_project_api_requires_a_business_session(service, tmp_path):
    class UsersRepository:
        def get_by_id(self, user_id):
            return None

    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "project-test-secret",
        "DATA_DIR": str(tmp_path),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "USERS_REPOSITORY": UsersRepository(),
        "AUDIT_SERVICE": AuditRecorder(),
        "SECURITY_AUTH_ENABLED": True,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    response = app.test_client().get("/api/projects")
    assert response.status_code == 401


def test_legacy_lists_tree_and_equipment_selector_use_project_service(
    service, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.models.DB_PATH", str(tmp_path / "legacy.db"))
    created = {}
    for category in CATEGORY_TABLES:
        created[category] = service.create_standalone(category, {
            "projectId": f"SAME-{category[-3:]}",
            "name": f"{category} 项目",
            "leader": "王老师",
            "status": "任务下达",
        }, actor_user_id=7)
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "project-test-secret",
        "DATA_DIR": str(tmp_path),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update({
            "user_id": 7, "user": "wang", "name": "王老师",
            "role": "BUSINESS_USER", "account_version": 1,
        })
    paths = {
        "GENERAL_RESEARCH": "/projects/",
        "SECURITY_CONFIDENTIALITY": "/security_projects/",
        "CRYPTO_APPLICATION": "/crypto_projects/",
    }
    for category, path in paths.items():
        response = client.get(path)
        assert response.status_code == 200
        assert f"{category} 项目" in response.get_data(as_text=True)
    tree = client.get("/api/tree")
    assert tree.status_code == 200
    tree_text = json.dumps(tree.get_json(), ensure_ascii=False)
    assert all(f"{category} 项目" in tree_text for category in CATEGORY_TABLES)
    with app.test_request_context("/"):
        from app.routes.equipment_groups import get_all_projects

        options = get_all_projects()
    assert {item["id"] for item in options}.issuperset(
        {item["businessId"] for item in created.values()}
    )
    for model_path in (
        "app.routes.projects.ProjectModel.get_by_id",
        "app.routes.security_projects.SecurityProjectModel.get_by_id",
        "app.routes.crypto_projects.CryptoProjectModel.get_by_id",
    ):
        monkeypatch.setattr(
            model_path,
            lambda *args, **kwargs: pytest.fail("PostgreSQL project fell back to SQLite"),
        )
    project_paths = {
        "GENERAL_RESEARCH": "projects",
        "SECURITY_CONFIDENTIALITY": "security_projects",
        "CRYPTO_APPLICATION": "crypto_projects",
    }
    for category, prefix in project_paths.items():
        business_id = created[category]["businessId"]
        documents = client.get(f"/{prefix}/documents/{business_id}")
        assert documents.status_code == 200
        assert f"{category} 项目" in documents.get_data(as_text=True)
        assert f"/{prefix}/upload_doc/{business_id}" in documents.get_data(as_text=True)
        upload = client.post(
            f"/{prefix}/upload_doc/{business_id}",
            data={"document": (io.BytesIO(category.encode()), "evidence.txt")},
            content_type="multipart/form-data",
        )
        assert upload.status_code == 302
        assert f"/{prefix}/documents/{business_id}" in upload.headers["Location"]
        download = client.get(f"/{prefix}/download_doc/{business_id}/evidence.txt")
        assert download.status_code == 200
        assert download.data == category.encode()
        deleted = client.post(f"/{prefix}/delete_doc/{business_id}/evidence.txt")
        assert deleted.status_code == 200
        assert deleted.get_json()["success"] is True
        equipment = client.post(f"/{prefix}/link_equipment/{business_id}", data={})
        assert equipment.status_code == 200
        assert equipment.get_json()["message"] == "请选择设备"


def test_project_file_routes_reject_traversal_and_preserve_outside_file(
    service, tmp_path
):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "project-test-secret",
        "DATA_DIR": str(tmp_path),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update({
            "user_id": 7, "user": "zhang", "name": "张老师",
            "role": "BUSINESS_USER", "account_version": 1,
        })
    outside = tmp_path / "outside.txt"
    outside.write_text("不可删除", encoding="utf-8")
    for prefix in ("projects", "security_projects", "crypto_projects"):
        response = client.post(
            f"/{prefix}/delete_file/SAFE-001",
            data={"file_path": "../../outside.txt"},
        )
        assert response.status_code == 400
        assert outside.read_text(encoding="utf-8") == "不可删除"
        create = client.post(
            f"/{prefix}/create_folder/SAFE-001",
            data={"parent_folder": "../../", "folder_name": "outside-folder"},
        )
        assert create.status_code == 400
        document_upload = client.post(
            f"/{prefix}/upload_doc/%2E%2E",
            data={"document": (io.BytesIO(b"must-not-write"), "outside.txt")},
            content_type="multipart/form-data",
        )
        assert document_upload.status_code == 404
        assert outside.read_text(encoding="utf-8") == "不可删除"
    document_delete = client.post("/projects/delete_doc/%2E%2E/outside.txt")
    assert document_delete.status_code == 404
    assert outside.read_text(encoding="utf-8") == "不可删除"


def test_manual_project_id_cannot_escape_upload_root(service, tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "project-test-secret",
        "DATA_DIR": str(tmp_path),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update({
            "user_id": 7, "user": "li", "name": "李老师",
            "role": "BUSINESS_USER", "account_version": 1,
        })
    response = client.post("/projects/add", data={
        "project_id": "../../outside", "name": "路径测试", "leader": "李老师",
    })
    assert response.status_code == 422
    assert not (tmp_path / "outside").exists()


def test_directory_initialization_failure_does_not_rollback_project(
    service, tmp_path, monkeypatch
):
    upload_root = tmp_path / "uploads"
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "project-test-secret",
        "DATA_DIR": str(tmp_path),
        "UPLOAD_DIR": str(upload_root),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update({
            "user_id": 7, "user": "wang", "name": "王老师",
            "role": "BUSINESS_USER", "account_version": 1,
        })
    real_makedirs = os.makedirs

    def fail_upload_directory(path, *args, **kwargs):
        if os.path.commonpath((str(upload_root), os.path.realpath(path))) == str(upload_root):
            raise OSError("模拟目录不可写")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", fail_upload_directory)
    response = client.post("/projects/add", data={
        "project_id": "DIR-FAIL-001", "name": "目录降级测试", "leader": "王老师",
    })
    assert response.status_code == 302
    assert service.get_legacy(
        category="GENERAL_RESEARCH", business_id="DIR-FAIL-001"
    )["name"] == "目录降级测试"


def test_document_routes_work_with_csrf_enabled_for_all_categories(service, tmp_path):
    created = {}
    for category in CATEGORY_TABLES:
        created[category] = service.create_standalone(category, {
            "projectId": f"CSRF-{category[-3:]}",
            "name": f"{category} CSRF", "leader": "张老师",
        }, actor_user_id=7)
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "project-test-secret",
        "DATA_DIR": str(tmp_path),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "PROJECT_SERVICE": service,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": True,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    csrf = "document-csrf-token"
    with client.session_transaction() as session:
        session.update({
            "user_id": 7, "user": "zhang", "name": "张老师",
            "role": "BUSINESS_USER", "account_version": 1,
            "csrf_token": csrf,
        })
    prefixes = {
        "GENERAL_RESEARCH": "projects",
        "SECURITY_CONFIDENTIALITY": "security_projects",
        "CRYPTO_APPLICATION": "crypto_projects",
    }
    for category, prefix in prefixes.items():
        business_id = created[category]["businessId"]
        rejected = client.post(
            f"/{prefix}/upload_doc/{business_id}",
            data={"document": (io.BytesIO(b"blocked"), "csrf.txt")},
            content_type="multipart/form-data",
        )
        assert rejected.status_code == 403
        uploaded = client.post(
            f"/{prefix}/upload_doc/{business_id}",
            data={
                "_csrf_token": csrf,
                "document": (io.BytesIO(category.encode()), "csrf.txt"),
            },
            content_type="multipart/form-data",
        )
        assert uploaded.status_code == 302
        deleted = client.post(
            f"/{prefix}/delete_doc/{business_id}/csrf.txt",
            headers={"X-CSRF-Token": csrf},
        )
        assert deleted.status_code == 200
        assert deleted.get_json()["success"] is True


def test_postgresql_concurrent_establishment_is_atomic_and_idempotent():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("real PostgreSQL concurrency test requires TEST_DATABASE_URL")
    pg_engine = sa.create_engine(database_url, pool_size=4, max_overflow=0)
    marker = uuid.uuid4().hex
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=pg_engine)
    proposals = sa.Table("proposals", metadata, autoload_with=pg_engine)
    with pg_engine.begin() as connection:
        actor_id = connection.scalar(users.insert().values(
            username=f"project-test-{marker}", password="not-used-in-service",
            role="BUSINESS_USER", name="张老师", status="active",
        ).returning(users.c.id))
        for suffix in ("same", "race"):
            connection.execute(proposals.insert().values(
                business_id=f"TP-PG-{suffix}-{marker}", title=f"PG {suffix}",
                status="ARGUMENTATION", version=2, created_by=actor_id,
                updated_by=actor_id,
            ))
    service = ProjectService(
        ProjectsRepository(pg_engine), AuditService(AuditRepository(pg_engine), app_version="test")
    )

    def establish(business_id, key):
        return service.establish_from_proposal(
            business_id, _payload(), idempotency_key=key,
            expected_version=2, actor_user_id=actor_id, request_id=f"req-{uuid.uuid4().hex}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        same_results = list(executor.map(
            lambda _: establish(f"TP-PG-same-{marker}", "same-key"), range(2)
        ))
    assert sorted(item["_reused"] for item in same_results) == [False, True]
    assert same_results[0]["project"] == same_results[1]["project"]

    def competing(key):
        try:
            establish(f"TP-PG-race-{marker}", key)
            return "CREATED"
        except ProjectServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        race_results = list(executor.map(competing, ("key-a", "key-b")))
    assert sorted(race_results) == ["CREATED", "STATE_CONFLICT"]
    pg_engine.dispose()


def test_establishment_conclusion_and_basis_preserve_body_on_save_and_readback(service, engine):
    conclusion = '合成验证：立项开展终端样机改进与复测，不代表科研指标已验收。\n保留Ａ²、㎏和\t制表符'
    basis = '立项依据：资料原文（演练），面积10 m²。\r\n第二行；参数x₂。'
    payload = _payload()
    payload.update(conclusion='\u200b\u3000' + conclusion + '\ufeff ', basis='\u3000' + basis + '\u200b')
    result = service.establish_from_proposal(
        'TA-2026-0001', payload, idempotency_key='establishment-body-fidelity',
        expected_version=2, actor_user_id=7, request_id='req-fidelity',
    )
    assert result['decision']['conclusion'] == conclusion
    assert result['decision']['basis'] == basis
    with engine.connect() as connection:
        stored = connection.execute(sa.text('SELECT conclusion, basis FROM proposal_decisions')).mappings().one()
    assert stored['conclusion'] == conclusion
    assert stored['basis'] == basis
