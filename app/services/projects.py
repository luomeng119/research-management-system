from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa

from app.repositories.base import OptimisticLockConflict
from app.repositories.projects import CATEGORY_TABLES
from app.text import has_meaningful_text, normalize_text


CATEGORIES = frozenset(CATEGORY_TABLES)
CATEGORY_PREFIXES = {
    "GENERAL_RESEARCH": "KY",
    "SECURITY_CONFIDENTIALITY": "AB",
    "CRYPTO_APPLICATION": "MM",
}
PROJECT_STATUSES = frozenset(
    {"PENDING", "ACTIVE", "PAUSED", "CLOSING", "CLOSED", "TERMINATED"}
)
LEGACY_PROJECT_STATUSES = frozenset({
    "任务下达", "启动", "执行中", "通过院内评审", "通过机关评审",
    "结题上报", "已结题", "已终止",
})
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class ProjectServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400, *, fields=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.fields = fields or {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _date(value, field: str) -> date:
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ProjectServiceError(
            "VALIDATION_ERROR", f"{field} 必须是 YYYY-MM-DD", 422,
            fields={field: "请填写有效日期"},
        )


def _serialize(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _decision(row: dict) -> dict:
    return {
        "id": _serialize(row["id"]),
        "proposalId": _serialize(row["proposal_id"]),
        "decision": row["decision"],
        "decisionDate": _serialize(row["decision_date"]),
        "conclusion": row["conclusion"],
        "basis": row["basis"],
    }


def _project_ref(registry: dict, project: dict) -> dict:
    return {
        "id": _serialize(registry["id"]),
        "businessId": registry["business_id"],
        "category": registry["category"],
        "name": project["name"],
        "leader": project.get("leader"),
        "plannedEndDate": _serialize(project.get("planned_end_date")),
        "status": registry["status"],
        "sourceProposalId": _serialize(registry.get("proposal_id")),
        "version": registry.get("version", 1),
    }


class ProjectService:
    def __init__(self, repository, audit_service) -> None:
        self.repository = repository
        self.audit_service = audit_service

    @staticmethod
    def _operation_key(actor_user_id: int, business_id: str, key: str) -> str:
        return hashlib.sha256(
            f"{actor_user_id}\0/api/proposals/{business_id}/decisions\0{key}".encode()
        ).hexdigest()

    @staticmethod
    def _validate(payload: dict, idempotency_key: str):
        if payload.get("decision") != "ESTABLISH":
            raise ProjectServiceError("VALIDATION_ERROR", "只能通过此操作记录立项", 422)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ProjectServiceError("IDEMPOTENCY_KEY_REQUIRED", "必须提供幂等键", 422)
        if len(idempotency_key) > 256:
            raise ProjectServiceError("VALIDATION_ERROR", "幂等键过长", 422)
        decision_date = _date(payload.get("decisionDate"), "decisionDate")
        conclusion_raw = payload.get("conclusion")
        basis_raw = payload.get("basis")
        project = payload.get("project")
        if not isinstance(project, dict):
            raise ProjectServiceError("VALIDATION_ERROR", "请填写立项项目信息", 422)
        category = project.get("category")
        name_raw = project.get("name")
        leader_raw = project.get("leader")
        typed = {
            "conclusion": conclusion_raw,
            "basis": basis_raw,
            "project.name": name_raw,
            "project.leader": leader_raw,
        }
        invalid_types = {key: "必须是文本" for key, value in typed.items() if not isinstance(value, str)}
        if invalid_types:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "立项字段类型无效", 422, fields=invalid_types
            )
        conclusion = normalize_text(conclusion_raw)
        basis = normalize_text(basis_raw)
        name = normalize_text(name_raw)
        leader = normalize_text(leader_raw)
        if category not in CATEGORIES:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "请选择有效项目类别", 422,
                fields={"project.category": "项目类别无效"},
            )
        required = {
            "conclusion": conclusion,
            "basis": basis,
            "project.name": name,
            "project.leader": leader,
        }
        missing = {key: "请填写" for key, value in required.items() if not has_meaningful_text(value)}
        if missing:
            raise ProjectServiceError("VALIDATION_ERROR", "请补充立项信息", 422, fields=missing)
        if len(name) > 200 or len(leader) > 200 or len(conclusion) > 5000 or len(basis) > 20000:
            raise ProjectServiceError("VALIDATION_ERROR", "立项字段过长", 422)
        planned_end_raw = project.get("plannedEndDate")
        planned_end_date = (
            None if planned_end_raw in (None, "")
            else _date(planned_end_raw, "project.plannedEndDate")
        )
        return {
            "decision_date": decision_date,
            "conclusion": conclusion,
            "basis": basis,
            "category": category,
            "name": name,
            "leader": leader,
            "planned_end_date": planned_end_date,
        }

    def _audit(self, connection, *, event_name, actor_user_id, request_id, object_id, properties):
        self.audit_service.record(
            connection,
            event_name=event_name,
            user_id=actor_user_id,
            object_type="PROJECT" if event_name == "project_created_from_proposal" else "PROPOSAL",
            object_id=str(object_id),
            result="SUCCESS",
            request_id=request_id,
            duration_ms=0,
            properties=properties,
        )

    def establish_from_proposal(
        self,
        business_id: str,
        payload: dict,
        *,
        idempotency_key: str,
        expected_version: int,
        actor_user_id: int,
        request_id: str,
    ) -> dict:
        values = self._validate(payload, idempotency_key)
        request_fingerprint = hashlib.sha256(json.dumps({
            "decision": "ESTABLISH",
            "decisionDate": values["decision_date"].isoformat(),
            "conclusion": values["conclusion"],
            "basis": values["basis"],
            "project": {
                "category": values["category"],
                "name": values["name"],
                "leader": values["leader"],
                "plannedEndDate": (
                    values["planned_end_date"].isoformat()
                    if values["planned_end_date"] else None
                ),
            },
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        stored_key = self._operation_key(actor_user_id, business_id, idempotency_key.strip())
        with self.repository.engine.begin() as connection:
            proposal = self.repository.get_proposal(connection, business_id, lock=True)
            if proposal is None:
                raise ProjectServiceError("NOT_FOUND", "科研提案不存在", 404)
            existing = self.repository.get_decision_by_key(connection, stored_key)
            if existing is not None:
                same = (
                    existing["decision"] == "ESTABLISH"
                    and existing.get("request_fingerprint") == request_fingerprint
                    and isinstance(existing.get("result_snapshot"), dict)
                )
                if not same:
                    raise ProjectServiceError(
                        "DUPLICATE_OPERATION", "该幂等键已用于其他立项请求", 409
                    )
                return {**existing["result_snapshot"], "_reused": True}
            if proposal["status"] != "ARGUMENTATION":
                raise ProjectServiceError("STATE_CONFLICT", "仅论证中提案可立项", 409)
            try:
                expected_version = int(expected_version)
            except (TypeError, ValueError):
                raise ProjectServiceError("VERSION_REQUIRED", "必须提供当前版本", 422)

            now = _now()
            decision_id = uuid.uuid4()
            registry_id = uuid.uuid4()
            project_business_id = (
                f"{CATEGORY_PREFIXES[values['category']]}-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
            )
            decision_values = {
                "id": decision_id,
                "proposal_id": proposal["id"],
                "decision": "ESTABLISH",
                "decision_date": values["decision_date"],
                "conclusion": values["conclusion"],
                "basis": values["basis"],
                "idempotency_key": stored_key,
                "request_fingerprint": request_fingerprint,
                "created_at": now,
                "updated_at": now,
                "created_by": actor_user_id,
                "updated_by": actor_user_id,
                "version": 1,
            }
            registry_values = {
                "id": registry_id,
                "category": values["category"],
                "business_id": project_business_id,
                "proposal_id": proposal["id"],
                "status": "PENDING",
                "created_at": now,
                "updated_at": now,
                "created_by": actor_user_id,
                "updated_by": actor_user_id,
                "version": 1,
            }
            project_values = {
                "project_id": project_business_id,
                "registry_id": registry_id,
                "name": values["name"],
                "leader": values["leader"],
                "start_date": values["decision_date"],
                "planned_end_date": values["planned_end_date"],
                "actual_end_date": None,
                "status": "任务下达",
                "created_at": now,
                "task_number": "",
            }
            first_result = {
                "decision": _decision(decision_values),
                "project": _project_ref(registry_values, project_values),
            }
            decision_values["result_snapshot"] = first_result
            self.repository.insert_decision(connection, decision_values)
            self.repository.insert_registry(connection, registry_values)
            self.repository.insert_category_project(
                connection, category=values["category"], values=project_values
            )
            try:
                self.repository.update_proposal(
                    connection,
                    proposal_id=proposal["id"],
                    expected_version=expected_version,
                    values={"status": "ESTABLISHED", "updated_by": actor_user_id},
                )
            except OptimisticLockConflict as error:
                raise ProjectServiceError(
                    "VERSION_CONFLICT", "提案已被其他操作修改", 409
                ) from error
            self._audit(
                connection,
                event_name="proposal_decision_recorded",
                actor_user_id=actor_user_id,
                request_id=request_id,
                object_id=proposal["business_id"],
                properties={"decision": "ESTABLISH", "project_category": values["category"]},
            )
            self._audit(
                connection,
                event_name="proposal_status_changed",
                actor_user_id=actor_user_id,
                request_id=request_id,
                object_id=proposal["business_id"],
                properties={"from_status": "ARGUMENTATION", "to_status": "ESTABLISHED"},
            )
            self._audit(
                connection,
                event_name="project_created_from_proposal",
                actor_user_id=actor_user_id,
                request_id=request_id,
                object_id=project_business_id,
                properties={"project_category": values["category"], "idempotency_reused": False},
            )
        return {**first_result, "_reused": False}

    def get(self, registry_id) -> dict:
        with self.repository.engine.connect() as connection:
            try:
                registry = self.repository.get_registry(connection, registry_id)
            except (TypeError, ValueError):
                registry = None
            if registry is None:
                raise ProjectServiceError("NOT_FOUND", "科研项目不存在", 404)
            project = self.repository.get_category_project(connection, registry)
            if project is None:
                raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
            return _project_ref(dict(registry), dict(project))

    def get_legacy(self, *, category: str, business_id: str) -> dict:
        if category not in CATEGORIES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目类别无效", 422)
        with self.repository.engine.connect() as connection:
            registry = self.repository.get_registry_by_category_business_id(
                connection, category=category, business_id=business_id
            )
            if registry is None:
                raise ProjectServiceError("NOT_FOUND", "科研项目不存在", 404)
            project = self.repository.get_category_project(connection, registry)
            if project is None:
                raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
            return dict(project)

    def list_legacy(
        self, *, category: str, page=1, page_size=50, status=None, keyword=None
    ) -> dict:
        if category not in CATEGORIES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目类别无效", 422)
        try:
            page, page_size = int(page), int(page_size)
        except (TypeError, ValueError):
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422)
        if page < 1 or page_size < 1 or page_size > 100:
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422)
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_category_projects(
                connection, category=category, page=page, page_size=page_size,
                status=status or None, keyword=keyword or None,
            )
        return {"items": rows, "page": page, "pageSize": page_size, "total": total}

    def create_standalone(
        self, category: str, payload: dict, *, actor_user_id: int
    ) -> dict:
        if category not in CATEGORIES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目类别无效", 422)
        project_id_raw = payload.get("projectId")
        name_raw = payload.get("name")
        leader_raw = payload.get("leader")
        if project_id_raw is not None and not isinstance(project_id_raw, str):
            raise ProjectServiceError("VALIDATION_ERROR", "项目编号必须是文本", 422)
        if not isinstance(name_raw, str) or not isinstance(leader_raw, str):
            raise ProjectServiceError("VALIDATION_ERROR", "项目名称和负责人必须是文本", 422)
        project_id = normalize_text(project_id_raw or "")
        name = normalize_text(name_raw)
        leader = normalize_text(leader_raw)
        if not has_meaningful_text(name) or not has_meaningful_text(leader):
            raise ProjectServiceError("VALIDATION_ERROR", "请填写项目名称和负责人", 422)
        if len(project_id) > 100 or len(name) > 200 or len(leader) > 200:
            raise ProjectServiceError("VALIDATION_ERROR", "项目字段过长", 422)
        now = _now()
        if not project_id:
            project_id = f"{CATEGORY_PREFIXES[category]}-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
        elif not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ProjectServiceError(
                "VALIDATION_ERROR",
                "项目编号只能包含字母、数字、下划线和连字符",
                422,
                fields={"projectId": "项目编号格式无效"},
            )
        def optional_date(key):
            raw = payload.get(key)
            return None if raw in (None, "") else _date(raw, key)
        start_date = optional_date("startDate")
        planned_end_date = optional_date("plannedEndDate")
        actual_end_date = optional_date("actualEndDate")
        legacy_status = payload.get("status") or "任务下达"
        if not isinstance(legacy_status, str) or legacy_status not in LEGACY_PROJECT_STATUSES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目状态无效", 422)
        canonical_status = self._canonical_status(legacy_status)
        task_number_raw = payload.get("taskNumber")
        if task_number_raw is not None and not isinstance(task_number_raw, str):
            raise ProjectServiceError("VALIDATION_ERROR", "任务书编号必须是文本", 422)
        task_number = normalize_text(task_number_raw or "")
        if len(task_number) > 200:
            raise ProjectServiceError("VALIDATION_ERROR", "任务书编号过长", 422)
        registry_id = uuid.uuid4()
        registry_values = {
            "id": registry_id, "category": category, "business_id": project_id,
            "proposal_id": None, "status": canonical_status, "created_at": now,
            "updated_at": now, "created_by": actor_user_id, "updated_by": actor_user_id,
            "version": 1,
        }
        project_values = {
            "project_id": project_id, "registry_id": registry_id, "name": name,
            "leader": leader, "start_date": start_date,
            "planned_end_date": planned_end_date, "actual_end_date": actual_end_date,
            "status": legacy_status, "created_at": now,
            "task_number": task_number,
        }
        try:
            with self.repository.engine.begin() as connection:
                self.repository.insert_registry(connection, registry_values)
                self.repository.insert_category_project(
                    connection, category=category, values=project_values
                )
        except sa.exc.IntegrityError as error:
            raise ProjectServiceError("DUPLICATE_PROJECT", "项目编号已存在", 409) from error
        return _project_ref(registry_values, project_values)

    @staticmethod
    def _canonical_status(legacy_status: str) -> str:
        return {
            "任务下达": "PENDING", "启动": "ACTIVE", "执行中": "ACTIVE",
            "通过院内评审": "ACTIVE", "通过机关评审": "ACTIVE",
            "结题上报": "CLOSING", "已结题": "CLOSED", "已终止": "TERMINATED",
        }.get(legacy_status, "PENDING")

    def update_legacy_field(
        self, *, category: str, business_id: str, field: str, value, actor_user_id: int
    ) -> dict:
        if category not in CATEGORIES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目类别无效", 422)
        if field not in {"actual_end_date", "status", "task_number"}:
            raise ProjectServiceError("VALIDATION_ERROR", "不允许修改该字段", 422)
        if not isinstance(value, str):
            raise ProjectServiceError("VALIDATION_ERROR", "字段值必须是文本", 422)
        normalized = normalize_text(value)
        if len(normalized) > 200:
            raise ProjectServiceError("VALIDATION_ERROR", "字段值过长", 422)
        if field == "status" and normalized not in LEGACY_PROJECT_STATUSES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目状态无效", 422)
        field_value = (
            None if field == "actual_end_date" and not normalized
            else _date(normalized, "actualEndDate") if field == "actual_end_date"
            else normalized
        )
        with self.repository.engine.begin() as connection:
            registry = self.repository.get_registry_by_category_business_id(
                connection, category=category, business_id=business_id, lock=True
            )
            if registry is None:
                raise ProjectServiceError("NOT_FOUND", "科研项目不存在", 404)
            updated = self.repository.update_category_project(
                connection, category=category, registry_id=registry["id"],
                values={field: field_value},
            )
            if updated != 1:
                raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
            registry_values = {"updated_at": _now(), "updated_by": actor_user_id}
            if field == "status":
                registry_values["status"] = self._canonical_status(normalized)
            registry_updated = self.repository.update_registry(
                connection, registry_id=registry["id"], values=registry_values
            )
            if registry_updated != 1:
                raise ProjectServiceError("STATE_CONFLICT", "项目已被其他操作修改", 409)
            project = self.repository.get_category_project(connection, registry)
        return dict(project)

    def delete_legacy_standalone(self, *, category: str, business_id: str) -> None:
        if category not in CATEGORIES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目类别无效", 422)
        with self.repository.engine.begin() as connection:
            registry = self.repository.get_registry_by_category_business_id(
                connection, category=category, business_id=business_id, lock=True
            )
            if registry is None:
                raise ProjectServiceError("NOT_FOUND", "科研项目不存在", 404)
            if registry.get("proposal_id") is not None:
                raise ProjectServiceError(
                    "STATE_CONFLICT", "由提案立项的项目不允许物理删除，请使用受控终止", 409
                )
            deleted = self.repository.delete_category_project(
                connection, category=category, registry_id=registry["id"]
            )
            if deleted != 1:
                raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
            registry_deleted = self.repository.delete_registry(
                connection, registry_id=registry["id"]
            )
            if registry_deleted != 1:
                raise ProjectServiceError("STATE_CONFLICT", "项目已被其他操作修改", 409)

    def list(self, *, page=1, page_size=20, category=None, status=None) -> dict:
        try:
            page = int(page)
            page_size = int(page_size)
        except (TypeError, ValueError):
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422)
        if page < 1 or page_size < 1 or page_size > 100:
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422)
        if category and category not in CATEGORIES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目类别无效", 422)
        if status and status not in PROJECT_STATUSES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目状态无效", 422)
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_registry(
                connection, page=page, page_size=page_size, category=category, status=status
            )
            projects = self.repository.get_category_projects(connection, rows)
            items = [
                _project_ref(registry, projects[registry["id"]])
                for registry in rows if registry["id"] in projects
            ]
        return {"items": items, "page": page, "pageSize": page_size, "total": total}
