from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import uuid

import sqlalchemy as sa
from flask import redirect, session
from sqlalchemy.pool import StaticPool

import app as app_module
from app import create_app
from app.repositories.projects import ProjectsRepository
from app.services.projects import ProjectService
from app.tests.test_project_lifecycle import AuditRecorder, _schema


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
        start_date=now.date(),
        planned_end_date=now.date(),
        status="任务下达",
        created_at=now,
    ))

service = ProjectService(ProjectsRepository(engine), AuditRecorder())
service.transition_status(
    registry_id, {"toStatus": "ACTIVE", "reason": "E2E 启动", "version": 1},
    actor_user_id=7, request_id="e2e-start",
)
for version, status, summary, issues in (
    (2, "NORMAL", "前期依据确认", ""),
    (3, "RISK", "历史数据格式核对", "部分字段格式不一致"),
    (4, "BLOCKED", "验证样本补充", "两类样本尚未取得"),
):
    service.add_progress(
        registry_id,
        {
            "recordedAt": now.isoformat(),
            "status": status,
            "summary": summary,
            "issues": issues,
            "nextActions": "回到进展记录表单补充事实",
            "version": version,
        },
        actor_user_id=7,
        request_id=f"e2e-{status.lower()}",
    )

data_dir = tempfile.mkdtemp(prefix="research-v1-e2e-")
import app.models as legacy_models

legacy_models.DB_PATH = f"{data_dir}/research.db"
legacy_models.EquipmentGroupModel().create_tables()
application = create_app({
    "TESTING": True,
    "SECRET_KEY": "research-v1-e2e-secret",
    "DATA_DIR": data_dir,
    "SESSION_FILE_DIR": f"{data_dir}/sessions",
    "PROJECT_SERVICE": service,
    "SECURITY_AUTH_ENABLED": False,
    "CSRF_ENABLED": False,
    "AI_PROVIDER": "DISABLED",
    "LOG_FILE": None,
})


@application.get("/__e2e_login", endpoint="e2e_login")
def e2e_login():
    session.update(
        user_id=7,
        user="zhang",
        name="张老师",
        role="BUSINESS_USER",
        account_version=1,
    )
    return redirect(f"/projects/{registry_id}/overview")


app_module.PUBLIC_ENDPOINTS = frozenset({*app_module.PUBLIC_ENDPOINTS, "e2e_login"})


if __name__ == "__main__":
    application.run(host="127.0.0.1", port=8877, debug=False, use_reloader=False)
