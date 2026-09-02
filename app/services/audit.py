from __future__ import annotations

from dataclasses import dataclass
import re

SENSITIVE_KEY_PARTS = frozenset(
    {
        "password", "passwd", "session", "token", "cookie", "api_key", "apikey",
        "id_card", "bank_card", "phone", "contact", "content", "body", "path",
    }
)
RESULTS = frozenset({"SUCCESS", "FAILURE", "CANCELLED"})
OBJECT_TYPES = frozenset(
    {
        "ACCOUNT", "SESSION", "REQUEST", "PROPOSAL", "PROJECT", "EXPERT",
        "EQUIPMENT", "STANDARD", "TEMPLATE", "GENERIC_TABLE", "EXPENSE",
        "INVOICE", "PAYMENT", "DOCUMENT", "FILE", "IMPORT_BATCH", "MIGRATION_BATCH",
        "BACKUP", "DEPENDENCY", "ASSISTANT_DRAFT",
    }
)
EVENT_PROPERTIES = {
    "login_completed": {"reason", "password_upgraded"},
    "logout_completed": set(),
    "password_change_completed": {"reason"},
    "account_operation_completed": {"operation", "target_user_id"},
    "maintenance_access_denied": {"endpoint"},
    "csrf_rejected": {"method", "endpoint"},
    "proposal_created": {"source_type"},
    "proposal_updated": {"field_count"},
    "proposal_status_changed": {"from_status", "to_status"},
    "proposal_decision_recorded": {"decision", "project_category"},
    "project_created_from_proposal": {"project_category", "idempotency_reused"},
    "project_status_changed": {"from_status", "to_status"},
    "project_record_added": {"record_type", "project_status"},
    "file_operation_completed": {"operation", "file_type", "size_bucket"},
    "import_batch_completed": {"module", "valid_count", "error_count", "duplicate_count"},
    "assistant_generation_completed": {"adapter_kind", "model_version", "prompt_version", "input_token_count", "output_token_count", "schema_valid", "remote_input_confirmed"},
    "assistant_draft_applied": {"selected_field_count", "available_field_count"},
    "migration_batch_completed": {"source_name", "record_count", "anomaly_count"},
    "backup_restore_completed": {"operation", "object_count", "file_count", "hash_mismatch_count"},
    "system_dependency_checked": {"dependency", "status"},
}


class AuditEventError(ValueError):
    pass


def _sensitive(key: str) -> bool:
    lowered = key.casefold()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _low_sensitivity_value(value) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if not isinstance(value, str) or len(value) > 128:
        return False
    lowered = value.casefold().strip()
    if lowered.startswith(("/", "\\")) or re.match(r"^[a-z]:[\\/]", lowered):
        return False
    return not any(
        marker in lowered
        for marker in ("password=", "password:", "token=", "token:", "api_key=", "api_key:")
    )


@dataclass
class AuditService:
    repository: object
    app_version: str

    def record(
        self,
        connection,
        *,
        event_name: str,
        user_id,
        object_type: str,
        result: str,
        request_id: str,
        duration_ms: int,
        object_id: str | None = None,
        error_code: str | None = None,
        properties: dict | None = None,
    ):
        if event_name not in EVENT_PROPERTIES:
            raise AuditEventError("event name is not allowed")
        if object_type not in OBJECT_TYPES:
            raise AuditEventError("object type is not allowed")
        if result not in RESULTS:
            raise AuditEventError("result is not allowed")
        if duration_ms < 0:
            raise AuditEventError("duration_ms must be non-negative")
        if result == "FAILURE" and not error_code:
            raise AuditEventError("failure events require error_code")
        allowed = EVENT_PROPERTIES[event_name]
        cleaned = {
            key: value
            for key, value in (properties or {}).items()
            if key in allowed and not _sensitive(key) and _low_sensitivity_value(value)
        }
        metadata = {
            "event_name": event_name,
            "duration_ms": int(duration_ms),
            "app_version": self.app_version,
            **cleaned,
        }
        if error_code:
            metadata["error_code"] = error_code
        return self.repository.insert(
            connection,
            {
                "actor_user_id": user_id,
                "action": event_name,
                "object_type": object_type,
                "object_id": object_id,
                "result": result,
                "request_id": request_id,
                "metadata": metadata,
            },
        )
