from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import re
import threading
import time
import uuid
from pathlib import Path

from app.ai.contract import (
    AssistantContractError, PROMPT_VERSION, build_messages,
    finalize_assistant_content,
)
from app.repositories.base import OptimisticLockConflict
from app.services.proposals import BODY_FIELDS, _proposal, _validate_body
from app.services.files import FileServiceError
from app.text import has_meaningful_text


APPLY_FIELDS = frozenset(
    {"title", "researchProblem", "objectives", "researchContent", "expectedOutcomes"}
)
ARRAY_APPLY_FIELDS = frozenset({"objectives", "researchContent", "expectedOutcomes"})
AI_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".json", ".xml", ".yml", ".yaml", ".log"})
MAX_SELECTED_FILES = 3
MAX_SELECTED_FILE_BYTES = 64 * 1024


class AssistantServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class RunRegistry:
    """Bounded process-local cancellation registry for the single-node V1."""

    def __init__(self, *, max_entries: int = 128) -> None:
        self.max_entries = max(8, int(max_entries))
        self._lock = threading.RLock()
        self._states: OrderedDict[str, dict] = OrderedDict()

    def start(self, run_id: str, actor_user_id: int) -> None:
        with self._lock:
            if run_id in self._states:
                state = self._states[run_id]
                if state["actor"] == actor_user_id and state["status"] == "CANCELLED":
                    raise AssistantServiceError("AI_CANCELLED", "助手运行已取消", 409)
                raise AssistantServiceError("DUPLICATE_RUN", "运行标识已使用", 409)
            while len(self._states) >= self.max_entries:
                key, state = next(iter(self._states.items()))
                if state["status"] == "RUNNING":
                    raise AssistantServiceError("TOO_MANY_RUNS", "助手运行过多，请稍后再试", 429)
                self._states.pop(key)
            self._states[run_id] = {"status": "RUNNING", "actor": actor_user_id}

    def cancel(self, run_id: str, actor_user_id: int) -> tuple[str, bool]:
        with self._lock:
            state = self._states.get(run_id)
            if state is None:
                while len(self._states) >= self.max_entries:
                    key, old_state = next(iter(self._states.items()))
                    if old_state["status"] == "RUNNING":
                        raise AssistantServiceError("TOO_MANY_RUNS", "助手运行过多，请稍后再试", 429)
                    self._states.pop(key)
                self._states[run_id] = {"status": "CANCELLED", "actor": actor_user_id}
                return "CANCELLED", True
            if state["actor"] != actor_user_id:
                raise AssistantServiceError("RUN_NOT_FOUND", "助手运行不存在", 404)
            if state["status"] == "RUNNING":
                state["status"] = "CANCELLED"
                return "CANCELLED", True
            return state["status"], False

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            state = self._states.get(run_id)
            return state is not None and state["status"] == "CANCELLED"

    def finish(self, run_id: str, status: str) -> None:
        with self._lock:
            state = self._states.get(run_id)
            if state is not None and state["status"] != "CANCELLED":
                state["status"] = status
            self._states.move_to_end(run_id)

    @contextmanager
    def completing(self, run_id: str):
        """Atomically exclude cancellation while the READY draft transaction commits."""
        with self._lock:
            state = self._states.get(run_id)
            if state is not None and state["status"] == "CANCELLED":
                raise AssistantServiceError("AI_CANCELLED", "助手运行已取消", 409)
            if state is None or state["status"] != "RUNNING":
                raise AssistantServiceError("DUPLICATE_RUN", "助手运行状态无效", 409)
            yield
            state["status"] = "COMPLETED"
            self._states.move_to_end(run_id)


def _draft(row) -> dict:
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "content": row["content"],
        "providerKind": row["provider_kind"],
        "modelVersion": row["model_version"],
        "promptVersion": row["prompt_version"],
        "createdAt": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else row["created_at"],
        "acceptedFields": row["accepted_fields"] or [],
    }


class AssistantService:
    def __init__(
        self, repository, audit_service, provider, *, run_registry=None,
        file_service=None,
    ) -> None:
        self.repository = repository
        self.audit_service = audit_service
        self.provider = provider
        self.run_registry = run_registry or RunRegistry()
        self.file_service = file_service

    def _source_with_selected_files(
        self, business_id: str, source_text: str, selected_file_ids: list
    ) -> str:
        if not isinstance(selected_file_ids, list):
            raise AssistantServiceError("AI_INPUT_INVALID", "selectedFileIds 必须是数组", 422)
        if not selected_file_ids:
            return source_text
        if (
            len(selected_file_ids) > MAX_SELECTED_FILES
            or any(
                not isinstance(value, str)
                or len(value) > 36
                or not value
                for value in selected_file_ids
            )
            or len(selected_file_ids) != len(set(selected_file_ids))
        ):
            raise AssistantServiceError("AI_FILE_INPUT_INVALID", "选定附件无效或超过 3 个", 422)
        try:
            selected_file_ids = [str(uuid.UUID(value)) for value in selected_file_ids]
        except (ValueError, AttributeError) as error:
            raise AssistantServiceError("AI_FILE_INPUT_INVALID", "附件标识必须是 UUID", 422) from error
        if self.file_service is None:
            raise AssistantServiceError(
                "AI_FILE_INPUT_UNSUPPORTED", "当前环境未启用附件文字整理", 422
            )
        available = {
            item["fileId"]: item
            for item in self.file_service.list_for_object(
                object_type="PROPOSAL", object_id=business_id
            )
        }
        sections = [source_text.strip()]
        for file_id in selected_file_ids:
            metadata = available.get(file_id)
            if metadata is None:
                raise AssistantServiceError(
                    "AI_FILE_NOT_FOUND", "选定附件不属于当前提案或已归档", 404
                )
            if Path(metadata["originalName"]).suffix.lower() not in AI_TEXT_EXTENSIONS:
                raise AssistantServiceError(
                    "AI_FILE_TYPE_UNSUPPORTED", "助手仅支持已选定的 UTF-8 纯文本附件", 415
                )
            opened = self.file_service.open_version_stream(
                file_id,
                int(metadata["versionNo"]),
                object_type="PROPOSAL",
                object_id=business_id,
            )
            if int(opened["sizeBytes"]) > MAX_SELECTED_FILE_BYTES:
                opened["stream"].close()
                raise AssistantServiceError("AI_FILE_TOO_LARGE", "选定附件超过 64KB", 413)
            try:
                raw = opened["stream"].read(MAX_SELECTED_FILE_BYTES + 1)
            finally:
                opened["stream"].close()
            if len(raw) > MAX_SELECTED_FILE_BYTES:
                raise AssistantServiceError("AI_FILE_TOO_LARGE", "选定附件超过 64KB", 413)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise AssistantServiceError(
                    "AI_FILE_TYPE_UNSUPPORTED", "选定附件不是 UTF-8 纯文本", 415
                ) from error
            if any(
                (ord(character) < 32 and character not in "\t\n\r")
                or 127 <= ord(character) <= 159
                for character in text
            ):
                raise AssistantServiceError(
                    "AI_FILE_TYPE_UNSUPPORTED", "选定附件包含二进制控制字符", 415
                )
            sections.append("[用户明确选定的附件文字]\n" + text)
        return "\n\n".join(sections)

    def _proposal(self, connection, business_id: str, *, lock: bool = False):
        row = self.repository.get_proposal(connection, business_id, lock=lock)
        if row is None:
            raise AssistantServiceError("PROPOSAL_NOT_FOUND", "科研提案不存在", 404)
        return row

    def _audit_generation(
        self, *, run_id, actor_user_id, request_id, result, error_code=None,
        schema_valid=False, duration_ms=0, remote_input_confirmed=False,
        input_token_count=None, output_token_count=None,
    ) -> None:
        with self.repository.engine.begin() as connection:
            self.audit_service.record(
                connection,
                event_name="assistant_generation_completed",
                user_id=actor_user_id,
                object_type="ASSISTANT_DRAFT",
                object_id=run_id,
                result=result,
                request_id=request_id,
                duration_ms=duration_ms,
                error_code=error_code,
                properties={
                    "adapter_kind": self.provider.provider_kind,
                    "model_version": self.provider.model_version,
                    "prompt_version": PROMPT_VERSION,
                    "input_token_count": input_token_count,
                    "output_token_count": output_token_count,
                    "schema_valid": schema_valid,
                    "remote_input_confirmed": remote_input_confirmed,
                },
            )

    def _best_effort_failure_audit(self, **values) -> None:
        try:
            self._audit_generation(**values)
        except Exception:
            # The original operation already failed. Do not let a second audit
            # outage replace its controlled error; successful writes still
            # require audit in the same transaction and therefore roll back.
            return

    def generate(
        self, business_id: str, *, source_text: str, selected_file_ids: list,
        proposal_version: int, run_id: str, actor_user_id: int, request_id: str,
        remote_input_confirmed: bool = False,
    ) -> dict:
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id)
        ):
            raise AssistantServiceError("AI_INPUT_INVALID", "runId 无效", 422)
        if self.provider.provider_kind == "DEEPSEEK" and remote_input_confirmed is not True:
            raise AssistantServiceError(
                "AI_REMOTE_CONFIRMATION_REQUIRED",
                "使用 DeepSeek 前必须确认本次材料已经脱敏",
                422,
            )
        try:
            build_messages(source_text)
            try:
                combined_source = self._source_with_selected_files(
                    business_id, source_text, selected_file_ids
                )
            except FileServiceError as error:
                raise AssistantServiceError(
                    error.code, error.message, error.status_code
                ) from error
            build_messages(combined_source)
        except AssistantContractError as error:
            raise AssistantServiceError(error.code, str(error), 422) from error
        try:
            proposal_version = int(proposal_version)
        except (TypeError, ValueError) as error:
            raise AssistantServiceError("VERSION_REQUIRED", "必须提供当前提案版本", 422) from error
        with self.repository.engine.connect() as connection:
            proposal = self._proposal(connection, business_id)
        if proposal["status"] != "DRAFT":
            raise AssistantServiceError("STATE_CONFLICT", "仅草稿提案可使用助手", 409)
        if int(proposal["version"]) != proposal_version:
            raise AssistantServiceError("VERSION_CONFLICT", "提案版本已变化", 409)

        self.run_registry.start(run_id, actor_user_id)
        started = time.monotonic()
        # Keep counts local to this invocation; a provider exception must not
        # reuse metadata from a previous call on the same thread.
        token_counts = {"inputTokens": None, "outputTokens": None}
        try:
            raw = self.provider.generate(
                combined_source,
                deadline_seconds=60,
                cancel_check=lambda: self.run_registry.is_cancelled(run_id),
            )
            # Production adapters provide thread-local usage; fixture providers
            # keep unknown counts rather than audit shared state.
            metadata = (
                getattr(self.provider, "last_metadata", {})
                if self.provider.provider_kind in {"DEEPSEEK", "LOCAL"} else {}
            )
            metadata = metadata if isinstance(metadata, dict) else {}
            token_counts = {
                key: value if type(value) is int and value >= 0 else None
                for key in ("inputTokens", "outputTokens")
                for value in (metadata.get(key),)
            }
            if self.run_registry.is_cancelled(run_id):
                raise AssistantServiceError("AI_CANCELLED", "助手运行已取消", 409)
            # Attachment text may contain quoted or hostile instructions.
            # Deterministic completion therefore inspects only the explicit
            # text box; the full model output still passes strict parsing.
            content = finalize_assistant_content(source_text, raw)
            now = datetime.now(timezone.utc)
            draft_id = uuid.uuid4()
            with self.run_registry.completing(run_id):
                with self.repository.engine.begin() as connection:
                    current = self._proposal(connection, business_id, lock=True)
                    if current["status"] != "DRAFT":
                        raise AssistantServiceError("STATE_CONFLICT", "仅草稿提案可使用助手", 409)
                    if int(current["version"]) != proposal_version:
                        raise AssistantServiceError("VERSION_CONFLICT", "提案版本已变化", 409)
                    self.repository.insert_draft(
                        connection,
                        {
                            "id": draft_id,
                            "proposal_id": current["id"],
                            "status": "READY",
                            "provider_kind": self.provider.provider_kind,
                            "model_version": self.provider.model_version,
                            "prompt_version": PROMPT_VERSION,
                            "source_proposal_version": proposal_version,
                            "input_hash": hashlib.sha256(combined_source.encode("utf-8")).hexdigest(),
                            "content": content,
                            "accepted_fields": [],
                            "created_at": now,
                            "updated_at": now,
                            "created_by": actor_user_id,
                            "updated_by": actor_user_id,
                            "version": 1,
                        },
                    )
                    row = self.repository.get_draft(connection, draft_id, current["id"])
                    self.audit_service.record(
                        connection,
                        event_name="assistant_generation_completed",
                        user_id=actor_user_id,
                        object_type="ASSISTANT_DRAFT",
                        object_id=str(draft_id),
                        result="SUCCESS",
                        request_id=request_id,
                        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                        properties={
                            "adapter_kind": self.provider.provider_kind,
                            "model_version": self.provider.model_version,
                            "prompt_version": PROMPT_VERSION,
                            "input_token_count": token_counts["inputTokens"],
                            "output_token_count": token_counts["outputTokens"],
                            "schema_valid": True,
                            "remote_input_confirmed": remote_input_confirmed,
                        },
                    )
            return _draft(row)
        except AssistantServiceError as error:
            if error.code == "AI_CANCELLED":
                self.run_registry.finish(run_id, "CANCELLED")
            else:
                self.run_registry.finish(run_id, "FAILED")
                self._best_effort_failure_audit(
                    run_id=run_id, actor_user_id=actor_user_id, request_id=request_id,
                    result="FAILURE", error_code=error.code,
                    input_token_count=token_counts["inputTokens"],
                    output_token_count=token_counts["outputTokens"],
                    remote_input_confirmed=remote_input_confirmed,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
            raise
        except AssistantContractError as error:
            self.run_registry.finish(run_id, "FAILED")
            self._best_effort_failure_audit(
                run_id=run_id, actor_user_id=actor_user_id, request_id=request_id,
                result="FAILURE", error_code=error.code,
                input_token_count=token_counts["inputTokens"],
                output_token_count=token_counts["outputTokens"],
                remote_input_confirmed=remote_input_confirmed,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            raise AssistantServiceError(error.code, str(error), 422) from error
        except (TimeoutError, InterruptedError) as error:
            cancelled = self.run_registry.is_cancelled(run_id)
            code = "AI_CANCELLED" if cancelled else "AI_TIMEOUT"
            self.run_registry.finish(run_id, "CANCELLED" if cancelled else "FAILED")
            if not cancelled:
                self._best_effort_failure_audit(
                    run_id=run_id, actor_user_id=actor_user_id, request_id=request_id,
                    result="FAILURE", error_code=code,
                    input_token_count=token_counts["inputTokens"],
                    output_token_count=token_counts["outputTokens"],
                    remote_input_confirmed=remote_input_confirmed,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
            raise AssistantServiceError(code, "助手运行已取消" if cancelled else "模型响应超时", 409 if cancelled else 504) from error
        except Exception as error:
            self.run_registry.finish(run_id, "FAILED")
            self._best_effort_failure_audit(
                run_id=run_id, actor_user_id=actor_user_id, request_id=request_id,
                result="FAILURE", error_code="AI_UNAVAILABLE",
                input_token_count=token_counts["inputTokens"],
                output_token_count=token_counts["outputTokens"],
                remote_input_confirmed=remote_input_confirmed,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            raise AssistantServiceError("AI_UNAVAILABLE", "助手暂不可用，请继续手工填写", 503) from error

    def cancel(self, run_id: str, *, actor_user_id: int, request_id: str) -> dict:
        status, changed = self.run_registry.cancel(run_id, actor_user_id)
        if changed:
            self._audit_generation(
                run_id=run_id, actor_user_id=actor_user_id, request_id=request_id,
                result="CANCELLED", duration_ms=0,
            )
        return {"runId": run_id, "status": status}

    def apply(
        self, business_id: str, draft_id: str, *, fields: list,
        proposal_version: int, actor_user_id: int, request_id: str,
    ) -> dict:
        if (
            not isinstance(fields, list) or not fields
            or len(fields) > len(APPLY_FIELDS)
            or any(not isinstance(field, str) for field in fields)
            or len(fields) != len(set(fields))
        ):
            raise AssistantServiceError("AI_FIELDS_INVALID", "请选择不重复的可采纳字段", 422)
        if any(field not in APPLY_FIELDS for field in fields):
            raise AssistantServiceError("AI_FIELDS_INVALID", "包含不可采纳字段", 422)
        try:
            proposal_version = int(proposal_version)
        except (TypeError, ValueError) as error:
            raise AssistantServiceError("VERSION_REQUIRED", "必须提供当前提案版本", 422) from error
        with self.repository.engine.begin() as connection:
            proposal = self._proposal(connection, business_id, lock=True)
            draft = self.repository.get_draft(
                connection, draft_id, proposal["id"], lock=True
            )
            if draft is None:
                raise AssistantServiceError("AI_DRAFT_NOT_FOUND", "助手草稿不存在", 404)
            if draft["status"] != "READY":
                raise AssistantServiceError("AI_DRAFT_ALREADY_APPLIED", "助手草稿已采纳", 409)
            if proposal["status"] != "DRAFT":
                raise AssistantServiceError("STATE_CONFLICT", "仅草稿提案可采纳内容", 409)
            if (
                int(proposal["version"]) != proposal_version
                or int(draft["source_proposal_version"]) != proposal_version
            ):
                raise AssistantServiceError("VERSION_CONFLICT", "提案已变化，请重新生成建议", 409)
            external_values = {}
            for field in fields:
                value = draft["content"][field]
                if (
                    (isinstance(value, list) and (
                        not value or any(not has_meaningful_text(item) for item in value)
                    ))
                    or (isinstance(value, str) and not has_meaningful_text(value))
                ):
                    raise AssistantServiceError(
                        "AI_FIELD_EMPTY", "空建议不可采纳，请先补充材料", 422
                    )
                external_values[field] = "\n".join(value) if field in ARRAY_APPLY_FIELDS else value
            validated = _validate_body(external_values, merged=dict(proposal))
            update_values = {
                BODY_FIELDS[field]: validated[BODY_FIELDS[field]] for field in fields
            }
            update_values["updated_by"] = actor_user_id
            try:
                self.repository.update_proposal(
                    connection,
                    proposal_id=proposal["id"],
                    expected_version=proposal_version,
                    values=update_values,
                )
            except OptimisticLockConflict as error:
                raise AssistantServiceError("VERSION_CONFLICT", "提案已变化，请重新生成建议", 409) from error
            if not self.repository.mark_applied(
                connection, draft_id=draft["id"], accepted_fields=fields,
                actor_user_id=actor_user_id,
            ):
                raise AssistantServiceError("AI_DRAFT_ALREADY_APPLIED", "助手草稿已采纳", 409)
            updated = self._proposal(connection, business_id)
            updated_draft = self.repository.get_draft(connection, draft["id"], proposal["id"])
            self.audit_service.record(
                connection,
                event_name="assistant_draft_applied",
                user_id=actor_user_id,
                object_type="ASSISTANT_DRAFT",
                object_id=str(draft["id"]),
                result="SUCCESS",
                request_id=request_id,
                duration_ms=0,
                properties={
                    "selected_field_count": len(fields),
                    "available_field_count": len(APPLY_FIELDS),
                },
            )
        return {"proposal": _proposal(updated), "draft": _draft(updated_draft)}
