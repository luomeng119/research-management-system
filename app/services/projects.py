from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

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
PROGRESS_STATUSES = frozenset({"NORMAL", "RISK", "BLOCKED"})
CHANGE_TYPES = frozenset({"GOAL", "PERIOD", "LEADER", "CONTENT", "OTHER"})
CHANGE_DECISIONS = frozenset({"AGREED", "REJECTED", "FILED"})
OUTPUT_TYPES = frozenset(
    {"REPORT", "PAPER", "PATENT", "SOFTWARE", "STANDARD", "PROTOTYPE", "DATA", "OTHER"}
)
RISK_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
PROJECT_TRANSITIONS = {
    "PENDING": frozenset({"ACTIVE", "TERMINATED"}),
    "ACTIVE": frozenset({"PAUSED", "CLOSING", "TERMINATED"}),
    "PAUSED": frozenset({"ACTIVE", "TERMINATED"}),
    "CLOSING": frozenset({"ACTIVE"}),
    "CLOSED": frozenset(),
    "TERMINATED": frozenset(),
}
LEGACY_PROJECT_STATUSES = frozenset({
    "任务下达", "启动", "执行中", "已暂停", "通过院内评审", "通过机关评审",
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


def _datetime(value, field: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ProjectServiceError(
            "VALIDATION_ERROR", f"{field} 必须是 ISO 8601 时间", 422,
            fields={field: "请填写有效时间"},
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectServiceError(
            "VALIDATION_ERROR", f"{field} 必须包含时区", 422,
            fields={field: "请填写带时区的时间"},
        )
    return parsed


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
        "updatedAt": _serialize(registry.get("updated_at")),
        "sourceProposalId": _serialize(registry.get("proposal_id")),
        "version": registry.get("version", 1),
    }


def _record_base(row: dict) -> dict:
    return {
        "id": _serialize(row["id"]),
        "createdAt": _serialize(row.get("created_at")),
        "updatedAt": _serialize(row.get("updated_at")),
        "createdBy": row.get("created_by"),
        "updatedBy": row.get("updated_by"),
        "version": row.get("version", 1),
    }


def _progress(row: dict) -> dict:
    return {
        **_record_base(row),
        "recordedAt": _serialize(row["recorded_at"]),
        "status": row["status"],
        "summary": row["summary"],
        "riskLevel": row.get("risk_level"),
        "issues": row.get("issues"),
        "nextActions": row.get("next_actions"),
    }


def _change(row: dict) -> dict:
    return {
        **_record_base(row),
        "changeType": row["change_type"],
        "beforeSummary": row.get("before_summary"),
        "afterSummary": row["after_summary"],
        "basis": row.get("basis"),
        "decision": row.get("decision"),
        "decisionDate": _serialize(row.get("decision_date")),
    }


def _output(row: dict) -> dict:
    return {
        **_record_base(row),
        "outputType": row["output_type"],
        "title": row["title"],
        "description": row.get("description"),
        "formedDate": _serialize(row.get("formed_date")),
        "contributors": row.get("contributors"),
    }


def _closure(row: dict) -> dict:
    return {
        **_record_base(row),
        "closedAt": _serialize(row["closed_at"]),
        "summary": row["summary"],
        "conclusion": row.get("conclusion"),
        "remainingIssues": row.get("remaining_issues"),
        "noOutputReason": row.get("no_output_reason"),
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
            object_type=(
                "PROJECT"
                if event_name in {
                    "project_created_from_proposal", "project_status_changed",
                    "project_record_added",
                }
                else "PROPOSAL"
            ),
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

    def list_logs(
        self, *, category: str, page=1, page_size=50, operator=None,
        project_name=None, start_date=None, end_date=None,
        newest_first=False,
    ) -> dict:
        if category not in CATEGORIES:
            raise ProjectServiceError("VALIDATION_ERROR", "项目类别无效", 422)
        try:
            page, page_size = int(page), int(page_size)
        except (TypeError, ValueError) as error:
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422) from error
        if page < 1 or page_size < 1 or page_size > 100:
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422)
        try:
            start_at = (
                datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
                if start_date else None
            )
            end_at = (
                datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
                + timedelta(days=1) if end_date else None
            )
        except ValueError as error:
            raise ProjectServiceError("VALIDATION_ERROR", "日志日期格式无效", 422) from error
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_project_logs(
                connection, category=category, page=page, page_size=page_size,
                operator=str(operator or "").strip() or None,
                project_name=str(project_name or "").strip() or None,
                start_at=start_at, end_at=end_at,
                newest_first=bool(newest_first),
            )
        labels = {
            "project_created_from_proposal": "立项",
            "project_status_changed": "状态变更",
            "project_record_added": "登记记录",
        }
        items = [{
            "timestamp": row["created_at"],
            "operator": str(row.get("operator_name") or ""),
            "operation_type": labels.get(row["action"], row["action"]),
            "file_name": str(row.get("project_name") or ""),
            "detail": "" if row.get("result") == "SUCCESS" else "操作失败",
        } for row in rows]
        return {
            "items": items, "page": page, "pageSize": page_size,
            "total": total,
        }

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
            "已暂停": "PAUSED",
            "通过院内评审": "ACTIVE", "通过机关评审": "ACTIVE",
            "结题上报": "CLOSING", "已结题": "CLOSED", "已终止": "TERMINATED",
        }.get(legacy_status, "PENDING")

    def update_legacy_field(
        self, *, category: str, business_id: str, field: str, value, actor_user_id: int,
        request_id: str = "legacy-project-update",
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
        if field == "status":
            with self.repository.engine.connect() as connection:
                registry = self.repository.get_registry_by_category_business_id(
                    connection, category=category, business_id=business_id
                )
                if registry is None:
                    raise ProjectServiceError("NOT_FOUND", "科研项目不存在", 404)
                current_project = self.repository.get_category_project(connection, registry)
                if current_project is None:
                    raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
            target_status = self._canonical_status(normalized)
            self.transition_status(
                registry["id"],
                {
                    "toStatus": target_status,
                    "reason": f"兼容入口状态更新：{current_project.get('status') or registry['status']} → {normalized}",
                    "version": registry["version"],
                },
                actor_user_id=actor_user_id,
                request_id=request_id,
                legacy_status_override=normalized,
                allow_same_status=True,
            )
            return self.get_legacy(category=category, business_id=business_id)
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
            if registry["status"] != "PENDING":
                raise ProjectServiceError(
                    "STATE_CONFLICT", "只有尚未启动的空白项目可以删除", 409
                )
            if self.repository.count_process_records(connection, registry["id"]):
                raise ProjectServiceError(
                    "STATE_CONFLICT", "项目已有过程记录，请使用受控终止", 409
                )
            if self.repository.count_project_files(connection, registry["business_id"]):
                raise ProjectServiceError(
                    "STATE_CONFLICT", "项目已有文件，请使用受控终止", 409
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

    @staticmethod
    def _legacy_status(status: str) -> str:
        return {
            "PENDING": "任务下达",
            "ACTIVE": "执行中",
            "PAUSED": "已暂停",
            "CLOSING": "结题上报",
            "CLOSED": "已结题",
            "TERMINATED": "已终止",
        }[status]

    @staticmethod
    def _required_text(payload: dict, key: str, *, max_length: int = 5000) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise ProjectServiceError(
                "VALIDATION_ERROR", f"{key} 必须是文本", 422,
                fields={key: "请填写文本"},
            )
        normalized = normalize_text(value)
        if not has_meaningful_text(normalized):
            raise ProjectServiceError(
                "VALIDATION_ERROR", f"请填写 {key}", 422,
                fields={key: "请填写"},
            )
        if len(normalized) > max_length:
            raise ProjectServiceError(
                "VALIDATION_ERROR", f"{key} 过长", 422,
                fields={key: f"不得超过 {max_length} 字"},
            )
        return normalized

    @staticmethod
    def _optional_text(payload: dict, key: str, *, max_length: int = 5000):
        value = payload.get(key)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ProjectServiceError(
                "VALIDATION_ERROR", f"{key} 必须是文本", 422,
                fields={key: "请填写文本"},
            )
        normalized = normalize_text(value)
        if len(normalized) > max_length:
            raise ProjectServiceError(
                "VALIDATION_ERROR", f"{key} 过长", 422,
                fields={key: f"不得超过 {max_length} 字"},
            )
        return normalized or None

    def _project_rows(self, connection, registry_id, *, lock=False):
        try:
            registry = (
                self.repository.get_registry_for_update(connection, registry_id)
                if lock else self.repository.get_registry(connection, registry_id)
            )
        except (TypeError, ValueError):
            registry = None
        if registry is None:
            raise ProjectServiceError("NOT_FOUND", "科研项目不存在", 404)
        project = self.repository.get_category_project(connection, registry)
        if project is None:
            raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
        return dict(registry), dict(project)

    def _record_event(
        self, connection, *, record_type: str, status: str,
        actor_user_id: int, request_id: str, business_id: str
    ) -> None:
        self._audit(
            connection,
            event_name="project_record_added",
            actor_user_id=actor_user_id,
            request_id=request_id,
            object_id=business_id,
            properties={"record_type": record_type, "project_status": status},
        )

    @staticmethod
    def _expected_version(payload: dict) -> int:
        try:
            version = int(payload.get("version"))
        except (TypeError, ValueError):
            raise ProjectServiceError("VERSION_REQUIRED", "必须提供当前版本", 422)
        if version < 1:
            raise ProjectServiceError("VERSION_REQUIRED", "项目版本无效", 422)
        return version

    def _append_record(
        self, connection, *, registry: dict, record_type: str, values: dict,
        expected_version: int, actor_user_id: int, request_id: str
    ) -> int:
        if registry["status"] in {"CLOSED", "TERMINATED"}:
            raise ProjectServiceError("STATE_CONFLICT", "终态项目不能新增过程记录", 409)
        if expected_version != registry["version"]:
            raise ProjectServiceError("VERSION_CONFLICT", "项目已被其他操作修改", 409)
        self.repository.insert_process_record(
            connection, record_type=record_type, values=values
        )
        try:
            new_version = self.repository.transition_registry(
                connection,
                registry_id=registry["id"],
                expected_version=expected_version,
                values={"updated_by": actor_user_id},
            )
        except OptimisticLockConflict as error:
            raise ProjectServiceError(
                "VERSION_CONFLICT", "项目已被其他操作修改", 409
            ) from error
        self._record_event(
            connection, record_type=record_type, status=registry["status"],
            actor_user_id=actor_user_id, request_id=request_id,
            business_id=registry["business_id"],
        )
        return new_version

    def add_progress(
        self, registry_id, payload: dict, *, actor_user_id: int, request_id: str
    ) -> dict:
        status = payload.get("status")
        if status not in PROGRESS_STATUSES:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "进展状态无效", 422,
                fields={"status": "请选择正常、有风险或受阻"},
            )
        summary = self._required_text(payload, "summary")
        risk_level = payload.get("riskLevel")
        if risk_level not in (None, "") and risk_level not in RISK_LEVELS:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "风险等级无效", 422,
                fields={"riskLevel": "请选择有效风险等级"},
            )
        recorded_at = _datetime(payload.get("recordedAt"), "recordedAt")
        expected_version = self._expected_version(payload)
        now = _now()
        values = {
            "id": uuid.uuid4(),
            "project_registry_id": registry_id,
            "recorded_at": recorded_at,
            "status": status,
            "summary": summary,
            "risk_level": risk_level or None,
            "issues": self._optional_text(payload, "issues", max_length=10000),
            "next_actions": self._optional_text(payload, "nextActions", max_length=10000),
            "created_at": now,
            "updated_at": now,
            "created_by": actor_user_id,
            "updated_by": actor_user_id,
            "version": 1,
        }
        with self.repository.engine.begin() as connection:
            registry, _ = self._project_rows(connection, registry_id, lock=True)
            new_version = self._append_record(
                connection, registry=registry, record_type="PROGRESS", values=values,
                expected_version=expected_version, actor_user_id=actor_user_id,
                request_id=request_id,
            )
        return {**_progress(values), "projectVersion": new_version}

    def list_progress(self, registry_id) -> list[dict]:
        with self.repository.engine.connect() as connection:
            self._project_rows(connection, registry_id)
            rows = self.repository.list_process_records(
                connection, record_type="PROGRESS", registry_id=registry_id,
                limit=200,
            )
        return [_progress(row) for row in rows]

    def page_process_records(
        self, registry_id, *, record_type: str, page=1, page_size=50
    ) -> dict:
        serializers = {
            "PROGRESS": _progress,
            "CHANGE": _change,
            "OUTPUT": _output,
        }
        if record_type not in serializers:
            raise ProjectServiceError("VALIDATION_ERROR", "过程记录类型无效", 422)
        try:
            page, page_size = int(page), int(page_size)
        except (TypeError, ValueError):
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422)
        if page < 1 or page_size < 1 or page_size > 100:
            raise ProjectServiceError("VALIDATION_ERROR", "分页参数无效", 422)
        with self.repository.engine.connect() as connection:
            self._project_rows(connection, registry_id)
            rows = self.repository.list_process_records(
                connection, record_type=record_type, registry_id=registry_id,
                limit=page_size, offset=(page - 1) * page_size,
            )
            total = self.repository.count_process_records_by_type(
                connection, record_type=record_type, registry_id=registry_id
            )
        return {
            "items": [serializers[record_type](row) for row in rows],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    def add_change(
        self, registry_id, payload: dict, *, actor_user_id: int, request_id: str
    ) -> dict:
        expected_version = self._expected_version(payload)
        values = {
            "id": uuid.uuid4(),
            "project_registry_id": registry_id,
            "change_type": self._required_text(payload, "changeType", max_length=100),
            "before_summary": self._optional_text(payload, "beforeSummary"),
            "after_summary": self._required_text(payload, "afterSummary"),
            "basis": self._required_text(payload, "basis", max_length=10000),
            "decision": payload.get("decision"),
            "decision_date": _date(payload.get("decisionDate"), "decisionDate"),
            "created_at": _now(),
            "updated_at": _now(),
            "created_by": actor_user_id,
            "updated_by": actor_user_id,
            "version": 1,
        }
        if values["change_type"] not in CHANGE_TYPES:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "变更类型无效", 422,
                fields={"changeType": "请选择目标、周期、负责人、内容或其他"},
            )
        if values["decision"] not in CHANGE_DECISIONS:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "变更决定无效", 422,
                fields={"decision": "请选择同意、不同意或备案"},
            )
        with self.repository.engine.begin() as connection:
            registry, _ = self._project_rows(connection, registry_id, lock=True)
            new_version = self._append_record(
                connection, registry=registry, record_type="CHANGE", values=values,
                expected_version=expected_version, actor_user_id=actor_user_id,
                request_id=request_id,
            )
        return {**_change(values), "projectVersion": new_version}

    def list_changes(self, registry_id) -> list[dict]:
        with self.repository.engine.connect() as connection:
            self._project_rows(connection, registry_id)
            rows = self.repository.list_process_records(
                connection, record_type="CHANGE", registry_id=registry_id,
                limit=200,
            )
        return [_change(row) for row in rows]

    def add_output(
        self, registry_id, payload: dict, *, actor_user_id: int, request_id: str
    ) -> dict:
        expected_version = self._expected_version(payload)
        values = {
            "id": uuid.uuid4(),
            "project_registry_id": registry_id,
            "output_type": self._required_text(payload, "outputType", max_length=100),
            "title": self._required_text(payload, "title", max_length=500),
            "description": self._optional_text(payload, "description", max_length=10000),
            "formed_date": _date(payload.get("formedDate"), "formedDate"),
            "contributors": self._required_text(payload, "contributors", max_length=1000),
            "created_at": _now(),
            "updated_at": _now(),
            "created_by": actor_user_id,
            "updated_by": actor_user_id,
            "version": 1,
        }
        if values["output_type"] not in OUTPUT_TYPES:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "成果类型无效", 422,
                fields={"outputType": "请选择有效成果类型"},
            )
        with self.repository.engine.begin() as connection:
            registry, _ = self._project_rows(connection, registry_id, lock=True)
            new_version = self._append_record(
                connection, registry=registry, record_type="OUTPUT", values=values,
                expected_version=expected_version, actor_user_id=actor_user_id,
                request_id=request_id,
            )
        return {**_output(values), "projectVersion": new_version}

    def list_outputs(self, registry_id) -> list[dict]:
        with self.repository.engine.connect() as connection:
            self._project_rows(connection, registry_id)
            rows = self.repository.list_process_records(
                connection, record_type="OUTPUT", registry_id=registry_id,
                limit=200,
            )
        return [_output(row) for row in rows]

    def transition_status(
        self, registry_id, payload: dict, *, actor_user_id: int, request_id: str,
        legacy_status_override: str | None = None,
        allow_same_status: bool = False,
    ) -> dict:
        to_status = payload.get("toStatus")
        reason = self._required_text(payload, "reason")
        expected_version = self._expected_version(payload)
        if to_status not in PROJECT_STATUSES:
            raise ProjectServiceError("VALIDATION_ERROR", "目标状态无效", 422)
        with self.repository.engine.begin() as connection:
            registry, project = self._project_rows(connection, registry_id, lock=True)
            if expected_version != registry["version"]:
                raise ProjectServiceError("VERSION_CONFLICT", "项目已被其他操作修改", 409)
            if to_status == registry["status"] and allow_same_status:
                pass
            elif to_status not in PROJECT_TRANSITIONS[registry["status"]]:
                raise ProjectServiceError(
                    "STATE_CONFLICT",
                    f"项目不能从 {registry['status']} 转换为 {to_status}",
                    409,
                )
            now = _now()
            change_values = {
                "id": uuid.uuid4(),
                "project_registry_id": registry["id"],
                "change_type": "STATUS_TRANSITION",
                "before_summary": registry["status"],
                "after_summary": to_status,
                "basis": reason,
                "decision": "FILED",
                "decision_date": now.date(),
                "created_at": now,
                "updated_at": now,
                "created_by": actor_user_id,
                "updated_by": actor_user_id,
                "version": 1,
            }
            self.repository.insert_process_record(
                connection, record_type="CHANGE", values=change_values
            )
            updated = self.repository.update_category_project(
                connection,
                category=registry["category"],
                registry_id=registry["id"],
                values={
                    "status": legacy_status_override or self._legacy_status(to_status),
                    **(
                        {"actual_end_date": now.date()}
                        if to_status == "TERMINATED"
                        else {}
                    ),
                },
            )
            if updated != 1:
                raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
            try:
                new_version = self.repository.transition_registry(
                    connection,
                    registry_id=registry["id"],
                    expected_version=expected_version,
                    values={"status": to_status, "updated_by": actor_user_id},
                )
            except OptimisticLockConflict as error:
                raise ProjectServiceError(
                    "VERSION_CONFLICT", "项目已被其他操作修改", 409
                ) from error
            self._audit(
                connection,
                event_name="project_status_changed",
                actor_user_id=actor_user_id,
                request_id=request_id,
                object_id=registry["business_id"],
                properties={"from_status": registry["status"], "to_status": to_status},
            )
        result = _project_ref({**registry, "status": to_status, "version": new_version}, project)
        result["status"] = to_status
        result["version"] = new_version
        return result

    def close_project(
        self, registry_id, payload: dict, *, actor_user_id: int, request_id: str
    ) -> dict:
        summary = self._required_text(payload, "summary", max_length=20000)
        closed_at = _datetime(payload.get("closedAt"), "closedAt")
        conclusion = payload.get("conclusion")
        if conclusion not in {"PASS", "FAIL", "TERMINATED"}:
            raise ProjectServiceError(
                "VALIDATION_ERROR", "结题结论无效", 422,
                fields={"conclusion": "请选择通过、未通过或终止"},
            )
        remaining_issues = self._optional_text(payload, "remainingIssues", max_length=10000)
        no_output_reason = self._optional_text(payload, "noOutputReason", max_length=5000)
        expected_version = self._expected_version(payload)
        with self.repository.engine.begin() as connection:
            registry, project = self._project_rows(connection, registry_id, lock=True)
            if expected_version != registry["version"]:
                raise ProjectServiceError("VERSION_CONFLICT", "项目已被其他操作修改", 409)
            if registry["status"] != "CLOSING":
                raise ProjectServiceError("STATE_CONFLICT", "仅结题中的项目可记录结题", 409)
            outputs = self.repository.list_process_records(
                connection, record_type="OUTPUT", registry_id=registry["id"],
                limit=1,
            )
            if not outputs and not no_output_reason:
                raise ProjectServiceError(
                    "VALIDATION_ERROR", "没有登记成果时必须说明原因", 422,
                    fields={"noOutputReason": "请说明无成果原因"},
                )
            existing_closure = self.repository.get_closure(connection, registry["id"])
            now = _now()
            closure_values = {
                "id": uuid.uuid4(),
                "project_registry_id": registry["id"],
                "summary": summary,
                "closed_at": closed_at,
                "conclusion": conclusion,
                "remaining_issues": remaining_issues,
                "no_output_reason": no_output_reason,
                "created_at": now,
                "updated_at": now,
                "created_by": actor_user_id,
                "updated_by": actor_user_id,
                "version": 1,
            }
            if existing_closure is None:
                self.repository.insert_process_record(
                    connection, record_type="CLOSURE", values=closure_values
                )
            else:
                closure_values = {
                    **closure_values,
                    "id": existing_closure["id"],
                    "created_at": existing_closure["created_at"],
                    "created_by": existing_closure["created_by"],
                }
                try:
                    closure_values["version"] = self.repository.update_closure(
                        connection,
                        closure_id=existing_closure["id"],
                        expected_version=existing_closure["version"],
                        values={
                            "summary": summary,
                            "closed_at": closed_at,
                            "conclusion": conclusion,
                            "remaining_issues": remaining_issues,
                            "no_output_reason": no_output_reason,
                            "updated_by": actor_user_id,
                        },
                    )
                except OptimisticLockConflict as error:
                    raise ProjectServiceError(
                        "VERSION_CONFLICT", "结题记录已被其他操作修改", 409
                    ) from error
            final_status = {
                "PASS": "CLOSED",
                "FAIL": "ACTIVE",
                "TERMINATED": "TERMINATED",
            }[conclusion]
            if self.repository.update_category_project(
                connection,
                category=registry["category"],
                registry_id=registry["id"],
                values={
                    "status": self._legacy_status(final_status),
                    "actual_end_date": closed_at.date() if final_status != "ACTIVE" else None,
                },
            ) != 1:
                raise ProjectServiceError("PROJECT_INCOMPLETE", "项目分类数据不完整", 409)
            try:
                new_version = self.repository.transition_registry(
                    connection,
                    registry_id=registry["id"],
                    expected_version=expected_version,
                    values={"status": final_status, "updated_by": actor_user_id},
                )
            except OptimisticLockConflict as error:
                raise ProjectServiceError(
                    "VERSION_CONFLICT", "项目已被其他操作修改", 409
                ) from error
            self._record_event(
                connection, record_type="CLOSURE", status=final_status,
                actor_user_id=actor_user_id, request_id=request_id,
                business_id=registry["business_id"],
            )
            persisted_closure = self.repository.get_closure(
                connection, registry["id"]
            )
        return {
            **_closure(dict(persisted_closure)),
            "projectStatus": final_status,
            "projectVersion": new_version,
        }

    def get_closure(self, registry_id):
        with self.repository.engine.connect() as connection:
            self._project_rows(connection, registry_id)
            row = self.repository.get_closure(connection, registry_id)
        return None if row is None else _closure(dict(row))

    def detail(self, registry_id) -> dict:
        with self.repository.engine.connect() as connection:
            registry, project = self._project_rows(connection, registry_id)
            progress = self.repository.list_process_records(
                connection, record_type="PROGRESS", registry_id=registry_id,
                limit=51,
            )
            changes = self.repository.list_process_records(
                connection, record_type="CHANGE", registry_id=registry_id,
                limit=51,
            )
            outputs = self.repository.list_process_records(
                connection, record_type="OUTPUT", registry_id=registry_id,
                limit=51,
            )
            closure = self.repository.get_closure(connection, registry_id)
        return {
            "project": _project_ref(registry, project),
            "progress": [_progress(row) for row in progress[:50]],
            "changes": [_change(row) for row in changes[:50]],
            "outputs": [_output(row) for row in outputs[:50]],
            "recordTruncated": {
                "progress": len(progress) > 50,
                "changes": len(changes) > 50,
                "outputs": len(outputs) > 50,
            },
            "closure": None if closure is None else _closure(dict(closure)),
        }

    def research_path(self, registry_id) -> dict:
        with self.repository.engine.connect() as connection:
            registry, project = self._project_rows(connection, registry_id)
            progress = self.repository.list_process_records(
                connection, record_type="PROGRESS", registry_id=registry_id,
                limit=191,
            )
        status_map = {
            "PENDING": "pending", "ACTIVE": "active", "PAUSED": "paused",
            "CLOSING": "active", "CLOSED": "done", "TERMINATED": "terminated",
        }
        stage_keys = ["PENDING", "ACTIVE", "CLOSING", "CLOSED"]
        current_index = {
            "PENDING": 0, "ACTIVE": 1, "PAUSED": 1,
            "CLOSING": 2, "CLOSED": 3, "TERMINATED": 0,
        }[registry["status"]]
        stage_labels = ["待启动", "执行中", "结题中", "已结题"]
        stage_nodes = []
        for index, (key, label) in enumerate(zip(stage_keys, stage_labels)):
            if registry["status"] == "TERMINATED" and index == current_index:
                node_status = "terminated"
            elif index < current_index or registry["status"] == "CLOSED":
                node_status = "done"
            elif index == current_index:
                node_status = status_map[registry["status"]]
            else:
                node_status = "pending"
            stage_nodes.append({
                "id": f"stage-{key.lower()}",
                "data": {
                    "title": label,
                    "status": node_status,
                    "owner": project.get("leader") or "未填写",
                    "period": "项目阶段",
                    "source": "项目状态记录",
                    "summary": f"当前项目状态：{registry['status']}",
                    "next": "状态仅通过项目详情中的受控动作变更。",
                },
            })
        visible_progress = progress[:190]
        progress_nodes = []
        for row in visible_progress:
            item = _progress(row)
            progress_nodes.append({
                "id": f"progress-{item['id']}",
                "data": {
                    "title": item["summary"][:24],
                    "status": {"NORMAL": "done", "RISK": "risk", "BLOCKED": "blocked"}[item["status"]],
                    "owner": f"记录人账号 {item['createdBy']}" if item["createdBy"] is not None else "未记录",
                    "period": item["recordedAt"],
                    "source": f"进展记录 {item['id']}",
                    "summary": item["summary"],
                    "issues": item.get("issues"),
                    "next": item.get("nextActions") or "如需调整，请回到进展记录表单登记新的业务事实。",
                },
            })
        project_ref = _project_ref(registry, project)
        tree = {
            "id": f"project-{project_ref['id']}",
            "data": {
                "title": project_ref["name"],
                "status": status_map[registry["status"]],
                "owner": project.get("leader") or "未填写",
                "period": (
                    f"{_serialize(project.get('start_date')) or '未填写'} — "
                    f"{_serialize(project.get('planned_end_date')) or '未填写'}"
                ),
                "source": (
                    f"来源提案 {project_ref['sourceProposalId']}"
                    if project_ref["sourceProposalId"] else "历史项目"
                ),
                "summary": f"{project_ref['category']} · {project_ref['status']}",
                "next": "查看阶段与进展记录。",
            },
            "children": [
                {
                    "id": "stages",
                    "data": {
                        "title": "项目阶段", "status": status_map[registry["status"]],
                        "owner": project.get("leader") or "未填写", "period": "全过程",
                        "source": "项目状态记录", "summary": "受控状态流转的只读结果。",
                        "next": "在项目详情中记录状态变化。",
                    },
                    "children": stage_nodes,
                },
                {
                    "id": "progress-records",
                    "data": {
                        "title": "进展与风险", "status": "active" if progress_nodes else "pending",
                        "owner": project.get("leader") or "未填写", "period": "按记录时间",
                        "source": "项目进展记录", "summary": "由现有进展和风险事实生成。",
                        "next": "通过普通表单新增进展记录。",
                    },
                    "children": progress_nodes,
                },
            ],
        }
        return {
            "project": project_ref,
            "readOnly": True,
            "tree": tree,
            "truncated": len(progress) > len(visible_progress),
            "visibleRecordCount": len(visible_progress),
        }
