from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone

from app.repositories.base import OptimisticLockConflict
from app.text import has_meaningful_text, normalize_text


SOURCE_TYPES = frozenset(
    {"IDEA", "CREATIVE", "MEETING_CONCLUSION", "FINISHED_MATERIAL", "OTHER"}
)
STATUSES = frozenset({"DRAFT", "ARGUMENTATION", "ESTABLISHED", "DEFERRED", "REJECTED"})
EDITABLE_STATUSES = frozenset({"DRAFT"})
ARGUMENTATION_STATUSES = frozenset({"DRAFT", "ARGUMENTATION"})
BODY_FIELDS = {
    "title": "title",
    "sourceType": "source_type",
    "sourceSummary": "source_summary",
    "researchProblem": "research_problem",
    "objectives": "objectives",
    "researchContent": "research_content",
    "expectedOutcomes": "expected_outcomes",
}
FIELD_MESSAGES = {
    "title": "请填写提案名称",
    "sourceType": "请选择有效的来源类型",
    "sourceSummary": "请填写原始依据摘要",
    "researchProblem": "请填写拟解决的研究问题",
    "objectives": "请填写研究目标",
    "researchContent": "请填写主要研究内容",
    "expectedOutcomes": "请填写预期成果",
}
FIELD_LIMITS = {
    "title": 200,
    "sourceSummary": 10_000,
    "researchProblem": 10_000,
    "objectives": 10_000,
    "researchContent": 50_000,
    "expectedOutcomes": 10_000,
}
ARGUMENTATION_LIMITS = {"summary": 20_000, "conclusion": 10_000, "basis": 20_000}
DECISION_LIMITS = {"conclusion": 5_000, "basis": 20_000}


class ProposalServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.fields = fields or {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _proposal(row) -> dict:
    mapping = {
        "id": "id",
        "business_id": "businessId",
        "title": "title",
        "source_type": "sourceType",
        "source_summary": "sourceSummary",
        "research_problem": "researchProblem",
        "objectives": "objectives",
        "research_content": "researchContent",
        "expected_outcomes": "expectedOutcomes",
        "status": "status",
        "created_by": "createdBy",
        "updated_by": "updatedBy",
        "created_at": "createdAt",
        "updated_at": "updatedAt",
        "version": "version",
    }
    return {
        target: _serialize_value(row[source])
        for source, target in mapping.items()
        if source in row
    }


def _proposal_summary(row) -> dict:
    allowed = {
        "id", "business_id", "title", "source_type", "status",
        "updated_by", "updated_at", "version",
    }
    return _proposal({key: value for key, value in row.items() if key in allowed})


def _decision(row) -> dict:
    mapping = {
        "id": "id",
        "proposal_id": "proposalId",
        "decision": "decision",
        "decision_date": "decisionDate",
        "conclusion": "conclusion",
        "basis": "basis",
        "created_by": "createdBy",
        "created_at": "createdAt",
    }
    return {
        target: _serialize_value(row[source])
        for source, target in mapping.items()
        if source in row
    }


def _validate_body(payload: dict, *, merged: dict | None = None) -> dict:
    values = dict(merged or {})
    for external, internal in BODY_FIELDS.items():
        if external in payload:
            raw = payload.get(external)
            values[internal] = normalize_text(raw) if isinstance(raw, str) else raw
    errors: dict[str, str] = {}
    for external, internal in BODY_FIELDS.items():
        value = values.get(internal)
        if external == "sourceType":
            if value not in SOURCE_TYPES:
                errors[external] = FIELD_MESSAGES[external]
        elif not has_meaningful_text(value):
            errors[external] = FIELD_MESSAGES[external]
    if isinstance(values.get("title"), str) and len(values["title"]) > 200:
        errors["title"] = "提案名称不能超过 200 个字符"
    for external, limit in FIELD_LIMITS.items():
        internal = BODY_FIELDS[external]
        value = values.get(internal)
        if isinstance(value, str) and len(value) > limit:
            errors[external] = f"字段不能超过 {limit} 个字符"
    if errors:
        raise ProposalServiceError(
            "VALIDATION_ERROR", "请检查提案字段", 422, fields=errors
        )
    return {internal: values[internal] for internal in BODY_FIELDS.values()}


def _parse_date(value, field: str) -> date:
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ProposalServiceError(
            "VALIDATION_ERROR", f"{field} 必须是 YYYY-MM-DD", 422,
            fields={field: "请填写有效日期"},
        )


class ProposalService:
    def __init__(self, repository, audit_service) -> None:
        self.repository = repository
        self.audit_service = audit_service

    def _audit(
        self,
        connection,
        *,
        event_name: str,
        actor_user_id: int,
        request_id: str,
        proposal: dict,
        properties: dict,
    ) -> None:
        self.audit_service.record(
            connection,
            event_name=event_name,
            user_id=actor_user_id,
            object_type="PROPOSAL",
            object_id=str(proposal["business_id"]),
            result="SUCCESS",
            request_id=request_id,
            duration_ms=0,
            properties=properties,
        )

    def _get_or_error(self, connection, business_id: str, *, lock: bool = False):
        row = self.repository.get(connection, business_id, lock=lock)
        if row is None:
            raise ProposalServiceError("PROPOSAL_NOT_FOUND", "科研提案不存在", 404)
        return row

    def create(
        self, payload: dict, *, actor_user_id: int, request_id: str
    ) -> dict:
        values = _validate_body(payload)
        proposal_id = uuid.uuid4()
        now = _now()
        values.update(
            id=proposal_id,
            business_id=f"TP-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            status="DRAFT",
            created_at=now,
            updated_at=now,
            created_by=actor_user_id,
            updated_by=actor_user_id,
            version=1,
        )
        with self.repository.engine.begin() as connection:
            self.repository.insert_proposal(connection, values)
            row = self._get_or_error(connection, values["business_id"])
            self._audit(
                connection,
                event_name="proposal_created",
                actor_user_id=actor_user_id,
                request_id=request_id,
                proposal=row,
                properties={"source_type": values["source_type"]},
            )
        return _proposal(row)

    def get(self, business_id: str) -> dict:
        with self.repository.engine.connect() as connection:
            return _proposal(self._get_or_error(connection, business_id))

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
        updated_after=None,
    ) -> dict:
        if status and status not in STATUSES:
            raise ProposalServiceError("VALIDATION_ERROR", "提案状态无效", 422)
        if source_type and source_type not in SOURCE_TYPES:
            raise ProposalServiceError("VALIDATION_ERROR", "来源类型无效", 422)
        keyword = (keyword or "").strip()
        if len(keyword) > 200:
            raise ProposalServiceError("VALIDATION_ERROR", "关键词不能超过 200 个字符", 422)
        if updated_after:
            try:
                updated_after = datetime.fromisoformat(str(updated_after).replace("Z", "+00:00"))
            except ValueError:
                raise ProposalServiceError("VALIDATION_ERROR", "updatedAfter 无效", 422)
            if updated_after.tzinfo is None or updated_after.utcoffset() is None:
                raise ProposalServiceError(
                    "VALIDATION_ERROR", "updatedAfter 必须包含时区", 422
                )
        try:
            page = int(page)
            page_size = int(page_size)
            with self.repository.engine.connect() as connection:
                rows, total = self.repository.list(
                    connection,
                    page=page,
                    page_size=page_size,
                    status=status,
                    source_type=source_type,
                    keyword=keyword or None,
                    updated_after=updated_after,
                )
        except (TypeError, ValueError) as error:
            raise ProposalServiceError("VALIDATION_ERROR", str(error), 422) from error
        return {"items": [_proposal_summary(row) for row in rows], "page": page, "pageSize": page_size, "total": total}

    def update(
        self,
        business_id: str,
        payload: dict,
        *,
        expected_version: int,
        actor_user_id: int,
        request_id: str,
    ) -> dict:
        controlled = set(payload) - set(BODY_FIELDS)
        if controlled:
            raise ProposalServiceError(
                "SYSTEM_FIELD_NOT_WRITABLE", "系统字段不能通过正文修改", 422,
                fields={key: "该字段由系统控制" for key in sorted(controlled)},
            )
        if not payload:
            raise ProposalServiceError(
                "VALIDATION_ERROR", "至少提供一个可修改字段", 422
            )
        try:
            expected_version = int(expected_version)
        except (TypeError, ValueError):
            raise ProposalServiceError("VERSION_REQUIRED", "必须提供当前版本", 422)
        with self.repository.engine.begin() as connection:
            current = self._get_or_error(connection, business_id, lock=True)
            if current["status"] not in EDITABLE_STATUSES:
                raise ProposalServiceError("STATE_CONFLICT", "当前状态不允许修改正文", 409)
            values = _validate_body(payload, merged=dict(current))
            values["updated_by"] = actor_user_id
            try:
                self.repository.update(
                    connection,
                    proposal_id=current["id"],
                    expected_version=expected_version,
                    values=values,
                )
            except OptimisticLockConflict as error:
                raise ProposalServiceError("VERSION_CONFLICT", "提案已被其他操作修改", 409) from error
            updated = self._get_or_error(connection, business_id)
            self._audit(
                connection,
                event_name="proposal_updated",
                actor_user_id=actor_user_id,
                request_id=request_id,
                proposal=updated,
                properties={"field_count": len(payload)},
            )
        return _proposal(updated)

    def add_argumentation(
        self,
        business_id: str,
        facts: dict,
        *,
        conclusion: str,
        basis: str,
        expected_version: int,
        actor_user_id: int,
        request_id: str,
    ) -> dict:
        if (
            not isinstance(facts, dict)
            or not isinstance(facts.get("summary"), str)
            or not facts["summary"].strip()
        ):
            raise ProposalServiceError(
                "VALIDATION_ERROR", "请填写论证摘要", 422,
                fields={"summary": "请填写论证摘要"},
            )
        unknown_facts = set(facts) - {"summary", "argumentationDate"}
        if unknown_facts:
            raise ProposalServiceError(
                "VALIDATION_ERROR", "论证事实包含未支持字段", 422,
                fields={key: "该论证字段未开放" for key in sorted(unknown_facts)},
            )
        if len(str(facts["summary"])) > ARGUMENTATION_LIMITS["summary"]:
            raise ProposalServiceError(
                "VALIDATION_ERROR", "论证摘要过长", 422,
                fields={"summary": "论证摘要不能超过 20000 个字符"},
            )
        argumentation_date = _parse_date(
            facts.get("argumentationDate"), "argumentationDate"
        )
        facts = {
            "summary": str(facts["summary"]).strip(),
            "argumentationDate": argumentation_date.isoformat(),
        }
        if (
            not isinstance(conclusion, str)
            or not isinstance(basis, str)
            or not conclusion.strip()
            or not basis.strip()
        ):
            raise ProposalServiceError("VALIDATION_ERROR", "请填写论证结论和依据", 422)
        if len(str(conclusion)) > ARGUMENTATION_LIMITS["conclusion"] or len(str(basis)) > ARGUMENTATION_LIMITS["basis"]:
            raise ProposalServiceError("VALIDATION_ERROR", "论证结论或依据过长", 422)
        with self.repository.engine.begin() as connection:
            current = self._get_or_error(connection, business_id, lock=True)
            if current["status"] not in ARGUMENTATION_STATUSES:
                raise ProposalServiceError("STATE_CONFLICT", "当前状态不允许新增论证", 409)
            try:
                self.repository.update(
                    connection,
                    proposal_id=current["id"],
                    expected_version=int(expected_version),
                    values={"status": "ARGUMENTATION", "updated_by": actor_user_id},
                )
            except (TypeError, ValueError):
                raise ProposalServiceError("VERSION_REQUIRED", "必须提供当前版本", 422)
            except OptimisticLockConflict as error:
                raise ProposalServiceError("VERSION_CONFLICT", "提案已被其他操作修改", 409) from error
            now = _now()
            self.repository.insert_argumentation(
                connection,
                {
                    "id": uuid.uuid4(),
                    "proposal_id": current["id"],
                    "facts": facts,
                    "conclusion": conclusion.strip(),
                    "basis": basis.strip(),
                    "created_at": now,
                    "updated_at": now,
                    "created_by": actor_user_id,
                    "updated_by": actor_user_id,
                    "version": 1,
                },
            )
            updated = self._get_or_error(connection, business_id)
            if current["status"] != "ARGUMENTATION":
                self._audit(
                    connection,
                    event_name="proposal_status_changed",
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    proposal=updated,
                    properties={"from_status": current["status"], "to_status": "ARGUMENTATION"},
                )
        return {"proposal": _proposal(updated)}

    def list_argumentations(
        self, business_id: str, *, page: int = 1, page_size: int = 20
    ) -> dict:
        with self.repository.engine.connect() as connection:
            proposal = self._get_or_error(connection, business_id)
            try:
                rows, total = self.repository.list_argumentations(
                    connection,
                    proposal_id=proposal["id"],
                    page=int(page),
                    page_size=int(page_size),
                )
            except (TypeError, ValueError) as error:
                raise ProposalServiceError("VALIDATION_ERROR", str(error), 422) from error
        return {
            "items": [
                {key: _serialize_value(value) for key, value in row.items()} for row in rows
            ],
            "page": int(page),
            "pageSize": int(page_size),
            "total": total,
        }

    def list_decisions(
        self, business_id: str, *, page: int = 1, page_size: int = 20
    ) -> dict:
        with self.repository.engine.connect() as connection:
            proposal = self._get_or_error(connection, business_id)
            try:
                page = int(page)
                page_size = int(page_size)
                rows, total = self.repository.list_decisions(
                    connection,
                    proposal_id=proposal["id"],
                    page=page,
                    page_size=page_size,
                )
            except (TypeError, ValueError) as error:
                raise ProposalServiceError(
                    "VALIDATION_ERROR", str(error), 422
                ) from error
        return {
            "items": [_decision(row) for row in rows],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    @staticmethod
    def _operation_key(actor_user_id: int, business_id: str, key: str) -> str:
        return hashlib.sha256(
            f"{actor_user_id}\0/api/proposals/{business_id}/decisions\0{key}".encode()
        ).hexdigest()

    def decide(
        self,
        business_id: str,
        payload: dict,
        *,
        idempotency_key: str,
        expected_version: int,
        actor_user_id: int,
        request_id: str,
    ) -> dict:
        decision = str(payload.get("decision", "")).upper()
        if decision == "ESTABLISH":
            raise ProposalServiceError(
                "STATE_CONFLICT", "立项必须由项目原子创建流程完成", 409
            )
        if decision not in {"DEFER", "REJECT"}:
            raise ProposalServiceError("VALIDATION_ERROR", "决定类型无效", 422)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ProposalServiceError("IDEMPOTENCY_KEY_REQUIRED", "必须提供幂等键", 422)
        if len(idempotency_key) > 256:
            raise ProposalServiceError("VALIDATION_ERROR", "幂等键过长", 422)
        decision_date = _parse_date(payload.get("decisionDate"), "decisionDate")
        conclusion = payload.get("conclusion", "")
        basis = payload.get("basis", "")
        if (
            not isinstance(conclusion, str)
            or not isinstance(basis, str)
            or not conclusion.strip()
            or not basis.strip()
        ):
            raise ProposalServiceError("VALIDATION_ERROR", "请填写决定结论和依据", 422)
        conclusion = conclusion.strip()
        basis = basis.strip()
        if len(conclusion) > DECISION_LIMITS["conclusion"] or len(basis) > DECISION_LIMITS["basis"]:
            raise ProposalServiceError("VALIDATION_ERROR", "决定结论或依据过长", 422)
        stored_key = self._operation_key(actor_user_id, business_id, idempotency_key.strip())
        with self.repository.engine.begin() as connection:
            current = self._get_or_error(connection, business_id, lock=True)
            existing = self.repository.get_decision_by_key(connection, stored_key)
            if existing is not None:
                same = (
                    str(existing["proposal_id"]) == str(current["id"])
                    and existing["decision"] == decision
                    and existing["decision_date"] == decision_date
                    and existing["conclusion"] == conclusion
                    and existing["basis"] == basis
                )
                if not same:
                    raise ProposalServiceError(
                        "DUPLICATE_OPERATION", "该幂等键已用于其他决定", 409
                    )
                return {"decision": _decision(existing), "_reused": True}
            if current["status"] != "ARGUMENTATION":
                raise ProposalServiceError("STATE_CONFLICT", "仅论证中提案可记录决定", 409)
            target_status = "DEFERRED" if decision == "DEFER" else "REJECTED"
            try:
                self.repository.update(
                    connection,
                    proposal_id=current["id"],
                    expected_version=int(expected_version),
                    values={"status": target_status, "updated_by": actor_user_id},
                )
            except (TypeError, ValueError):
                raise ProposalServiceError("VERSION_REQUIRED", "必须提供当前版本", 422)
            except OptimisticLockConflict as error:
                raise ProposalServiceError("VERSION_CONFLICT", "提案已被其他操作修改", 409) from error
            now = _now()
            self.repository.insert_decision(
                connection,
                {
                    "id": uuid.uuid4(),
                    "proposal_id": current["id"],
                    "decision": decision,
                    "decision_date": decision_date,
                    "conclusion": conclusion,
                    "basis": basis,
                    "idempotency_key": stored_key,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": actor_user_id,
                    "updated_by": actor_user_id,
                    "version": 1,
                },
            )
            recorded = self.repository.get_decision_by_key(connection, stored_key)
            updated = self._get_or_error(connection, business_id)
            self._audit(
                connection,
                event_name="proposal_decision_recorded",
                actor_user_id=actor_user_id,
                request_id=request_id,
                proposal=updated,
                properties={"decision": decision, "project_category": None},
            )
            self._audit(
                connection,
                event_name="proposal_status_changed",
                actor_user_id=actor_user_id,
                request_id=request_id,
                proposal=updated,
                properties={"from_status": "ARGUMENTATION", "to_status": target_status},
            )
        return {"decision": _decision(recorded), "_reused": False}

    def return_to_draft(
        self,
        business_id: str,
        *,
        expected_version: int,
        actor_user_id: int,
        request_id: str,
    ) -> dict:
        with self.repository.engine.begin() as connection:
            current = self._get_or_error(connection, business_id, lock=True)
            if current["status"] != "ARGUMENTATION":
                raise ProposalServiceError("STATE_CONFLICT", "仅论证中提案可退回补充", 409)
            try:
                self.repository.update(
                    connection,
                    proposal_id=current["id"],
                    expected_version=int(expected_version),
                    values={"status": "DRAFT", "updated_by": actor_user_id},
                )
            except (TypeError, ValueError):
                raise ProposalServiceError("VERSION_REQUIRED", "必须提供当前版本", 422)
            except OptimisticLockConflict as error:
                raise ProposalServiceError("VERSION_CONFLICT", "提案已被其他操作修改", 409) from error
            updated = self._get_or_error(connection, business_id)
            self._audit(
                connection,
                event_name="proposal_status_changed",
                actor_user_id=actor_user_id,
                request_id=request_id,
                proposal=updated,
                properties={"from_status": "ARGUMENTATION", "to_status": "DRAFT"},
            )
        return _proposal(updated)

    def reopen(
        self,
        business_id: str,
        *,
        expected_version: int,
        actor_user_id: int,
        request_id: str,
    ) -> dict:
        with self.repository.engine.begin() as connection:
            current = self._get_or_error(connection, business_id, lock=True)
            if current["status"] != "DEFERRED":
                raise ProposalServiceError("STATE_CONFLICT", "仅暂缓提案可重新论证", 409)
            try:
                self.repository.update(
                    connection,
                    proposal_id=current["id"],
                    expected_version=int(expected_version),
                    values={"status": "ARGUMENTATION", "updated_by": actor_user_id},
                )
            except (TypeError, ValueError):
                raise ProposalServiceError("VERSION_REQUIRED", "必须提供当前版本", 422)
            except OptimisticLockConflict as error:
                raise ProposalServiceError("VERSION_CONFLICT", "提案已被其他操作修改", 409) from error
            updated = self._get_or_error(connection, business_id)
            self._audit(
                connection,
                event_name="proposal_status_changed",
                actor_user_id=actor_user_id,
                request_id=request_id,
                proposal=updated,
                properties={"from_status": "DEFERRED", "to_status": "ARGUMENTATION"},
            )
        return _proposal(updated)
