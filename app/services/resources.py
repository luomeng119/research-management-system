from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import uuid

import sqlalchemy as sa


class ResourceServiceError(RuntimeError):
    def __init__(self, code, message, status_code=400, *, fields=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.fields = fields or {}


def _positive_int(value, field, *, maximum=None):
    if isinstance(value, bool):
        raise ResourceServiceError(
            "VALIDATION_ERROR", f"{field} 必须是整数", 422,
            fields={field: "请填写有效整数"},
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ResourceServiceError(
            "VALIDATION_ERROR", f"{field} 必须是整数", 422,
            fields={field: "请填写有效整数"},
        )
    if isinstance(value, float) and not value.is_integer():
        raise ResourceServiceError(
            "VALIDATION_ERROR", f"{field} 必须是整数", 422,
            fields={field: "请填写有效整数"},
        )
    if isinstance(value, str) and value.strip() != str(parsed):
        raise ResourceServiceError(
            "VALIDATION_ERROR", f"{field} 必须是整数", 422,
            fields={field: "请填写有效整数"},
        )
    if parsed < 1 or (maximum is not None and parsed > maximum):
        raise ResourceServiceError(
            "VALIDATION_ERROR", f"{field} 超出范围", 422,
            fields={field: f"范围为 1-{maximum}" if maximum else "必须大于 0"},
        )
    return parsed


def _mask_phone(value):
    value = str(value or "")
    return value[:3] + "****" + value[-4:] if len(value) >= 7 else ("*" * len(value))


def _mask_id_card(value):
    value = str(value or "")
    return value[:3] + ("*" * max(0, len(value) - 7)) + value[-4:] if value else ""


def _mask_bank_card(value):
    value = str(value or "")
    return value[:4] + ("*" * max(0, len(value) - 8)) + value[-4:] if value else ""


def _expert(row, *, sensitive=False):
    return {
        "id": row["id"],
        "expertId": row["expert_id"],
        "name": row["name"],
        "unit": row.get("unit"),
        "position": row.get("position"),
        "expertise": row.get("expertise"),
        "phone": row.get("phone") if sensitive else _mask_phone(row.get("phone")),
        "idCard": row.get("id_card") if sensitive else _mask_id_card(row.get("id_card")),
        "bankCard": row.get("bank_card") if sensitive else _mask_bank_card(row.get("bank_card")),
        "bankName": row.get("bank_name"),
        "uploader": row.get("uploader"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def _expert_values(payload):
    payload = payload or {}
    values = {
        "name": str(payload.get("name") or "").strip(),
        "unit": str(payload.get("unit") or "").strip(),
        "position": str(payload.get("position") or "").strip(),
        "expertise": str(payload.get("expertise") or "").strip(),
        "phone": str(payload.get("phone") or "").strip(),
        "id_card": str(payload.get("idCard") or payload.get("id_card") or "").strip(),
        "bank_card": str(payload.get("bankCard") or payload.get("bank_card") or "").strip(),
        "bank_name": str(payload.get("bankName") or payload.get("bank_name") or "").strip(),
    }
    if not values["name"]:
        raise ResourceServiceError(
            "VALIDATION_ERROR", "姓名不能为空", 422,
            fields={"name": "请填写姓名"},
        )
    if len(values["name"]) > 100:
        raise ResourceServiceError(
            "VALIDATION_ERROR", "姓名过长", 422,
            fields={"name": "最多 100 个字符"},
        )
    limits = {
        "unit": 200, "position": 100, "expertise": 500, "phone": 32,
        "id_card": 64, "bank_card": 64, "bank_name": 200,
    }
    too_long = [field for field, limit in limits.items() if len(values[field]) > limit]
    if too_long or sum(len(value) for value in values.values()) > 1500:
        raise ResourceServiceError(
            "VALIDATION_ERROR", "专家字段过长", 422,
            fields={field: "内容过长" for field in too_long},
        )
    return values


class ResearchResourcesService:
    def __init__(self, repository, audit_service) -> None:
        self.repository = repository
        self.audit_service = audit_service

    def list_experts(self, *, page=1, page_size=20, keyword=None):
        page = _positive_int(page, "page")
        page_size = _positive_int(page_size, "pageSize", maximum=100)
        keyword = str(keyword or "").strip() or None
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_experts(
                connection, page=page, page_size=page_size, keyword=keyword
            )
        return {
            "items": [_expert(row) for row in rows],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    def get_expert_sensitive(
        self, expert_id, *, actor_user_id, request_id, operation="VIEW"
    ):
        expert_id = str(expert_id or "").strip()
        with self.repository.engine.begin() as connection:
            row = self.repository.get_expert(connection, expert_id)
            if row is None:
                raise ResourceServiceError(
                    "EXPERT_NOT_FOUND", "专家不存在", 404
                )
            self.audit_service.record(
                connection,
                event_name="expert_sensitive_accessed",
                user_id=actor_user_id,
                object_type="EXPERT",
                object_id=expert_id,
                result="SUCCESS",
                request_id=request_id,
                duration_ms=0,
                properties={"operation": operation, "record_count": 1},
            )
            return _expert(dict(row), sensitive=True)

    def export_experts_sensitive(
        self, *, expert_ids=None, actor_user_id, request_id
    ):
        normalized_ids = None
        if expert_ids is not None:
            normalized_ids = [str(value).strip() for value in expert_ids if str(value).strip()]
        with self.repository.engine.begin() as connection:
            rows = self.repository.export_experts(connection, normalized_ids)
            self.audit_service.record(
                connection,
                event_name="expert_sensitive_accessed",
                user_id=actor_user_id,
                object_type="EXPERT",
                object_id=None,
                result="SUCCESS",
                request_id=request_id,
                duration_ms=0,
                properties={"operation": "EXPORT", "record_count": len(rows)},
            )
            return [_expert(row, sensitive=True) for row in rows]

    def create_expert(
        self, payload, *, uploader, actor_user_id, request_id
    ):
        values = _expert_values(payload)
        now = datetime.now(timezone.utc)
        expert_id = "EXP" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()
        values.update(
            expert_id=expert_id,
            uploader=str(uploader or "").strip(),
            created_at=now,
            updated_at=now,
        )
        with self.repository.engine.begin() as connection:
            self.repository.lock_expert_identity_space(connection)
            key = (values["name"], values["unit"], values["phone"])
            if self.repository.expert_with_key(connection, key):
                raise ResourceServiceError(
                    "EXPERT_DUPLICATE", "相同专家记录已存在", 409
                )
            self.repository.insert_expert(connection, values)
            row = self.repository.get_expert(connection, expert_id)
        return _expert(dict(row))

    def update_expert(
        self, expert_id, payload, *, actor_user_id, request_id
    ):
        expert_id = str(expert_id or "").strip()
        values = _expert_values(payload)
        values["updated_at"] = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            self.repository.lock_expert_identity_space(connection)
            key = (values["name"], values["unit"], values["phone"])
            if self.repository.expert_with_key(
                connection, key, exclude_expert_id=expert_id
            ):
                raise ResourceServiceError(
                    "EXPERT_DUPLICATE", "相同专家记录已存在", 409
                )
            if not self.repository.update_expert(connection, expert_id, values):
                raise ResourceServiceError(
                    "EXPERT_NOT_FOUND", "专家不存在", 404
                )
            row = self.repository.get_expert(connection, expert_id)
        return _expert(dict(row))

    def delete_expert(self, expert_id):
        expert_id = str(expert_id or "").strip()
        with self.repository.engine.begin() as connection:
            if self.repository.expert_membership_count(connection, expert_id):
                raise ResourceServiceError(
                    "EXPERT_IN_USE", "专家已在专家组中，请先移出后再删除", 409
                )
            if not self.repository.delete_expert(connection, expert_id):
                raise ResourceServiceError("EXPERT_NOT_FOUND", "专家不存在", 404)

    def create_expert_group(self, payload, *, creator):
        group_name = str((payload or {}).get("groupName") or "").strip()
        if not group_name:
            raise ResourceServiceError(
                "VALIDATION_ERROR", "专家组名称不能为空", 422,
                fields={"groupName": "请填写专家组名称"},
            )
        now = datetime.now(timezone.utc)
        group_id = "EG" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()
        values = {
            "group_id": group_id,
            "meeting_name": group_name,
            "creator": str(creator or "").strip(),
            "created_at": now,
            "updated_at": now,
        }
        with self.repository.engine.begin() as connection:
            self.repository.insert_expert_group(connection, values)
        return {"groupId": group_id, "groupName": group_name}

    def list_expert_groups(self, *, page=1, page_size=20):
        page = _positive_int(page, "page")
        page_size = _positive_int(page_size, "pageSize", maximum=100)
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_expert_groups(
                connection, page=page, page_size=page_size
            )
        return {
            "items": [{
                "groupId": row["group_id"],
                "groupName": row["meeting_name"],
                "creator": row["creator"],
                "createdAt": row["created_at"],
                "memberCount": int(row["member_count"] or 0),
            } for row in rows],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    def add_expert_group_member(self, group_id, expert_id, *, selected_by):
        group_id = str(group_id or "").strip()
        expert_id = str(expert_id or "").strip()
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            if self.repository.get_expert_group(connection, group_id) is None:
                raise ResourceServiceError("EXPERT_GROUP_NOT_FOUND", "专家组不存在", 404)
            if self.repository.get_expert(connection, expert_id) is None:
                raise ResourceServiceError("EXPERT_NOT_FOUND", "专家不存在", 404)
            try:
                self.repository.insert_expert_group_member(connection, {
                    "group_id": group_id,
                    "expert_id": expert_id,
                    "selected_by": str(selected_by or "").strip(),
                    "selected_at": now,
                })
            except sa.exc.IntegrityError as error:
                raise ResourceServiceError(
                    "GROUP_MEMBER_EXISTS", "该专家已在专家组中", 409
                ) from error

    def get_expert_group(self, group_id):
        with self.repository.engine.connect() as connection:
            found = self.repository.get_expert_group(connection, str(group_id or "").strip())
        if found is None:
            raise ResourceServiceError("EXPERT_GROUP_NOT_FOUND", "专家组不存在", 404)
        group, members = found
        member_items = []
        for member in members:
            item = _expert(member)
            item.update(
                selectedBy=member.get("selected_by"),
                selectedAt=member.get("selected_at"),
            )
            member_items.append(item)
        return {
            "groupId": group["group_id"],
            "groupName": group["meeting_name"],
            "creator": group["creator"],
            "createdAt": group["created_at"],
            "members": member_items,
        }

    def list_available_experts(
        self, group_id, *, keyword=None, unit=None, position=None,
        expertise=None, limit=20,
    ):
        limit = _positive_int(limit, "limit", maximum=100)
        with self.repository.engine.connect() as connection:
            if self.repository.get_expert_group(connection, group_id) is None:
                raise ResourceServiceError(
                    "EXPERT_GROUP_NOT_FOUND", "专家组不存在", 404
                )
            rows = self.repository.list_available_experts(
                connection, group_id, keyword=str(keyword or "").strip() or None,
                unit=str(unit or "").strip() or None,
                position=str(position or "").strip() or None,
                expertise=str(expertise or "").strip() or None,
                limit=limit,
            )
        return [_expert(row) for row in rows]

    def expert_facets(self):
        with self.repository.engine.connect() as connection:
            return self.repository.expert_facets(connection)

    def remove_expert_group_member(self, group_id, expert_id):
        group_id = str(group_id or "").strip()
        expert_id = str(expert_id or "").strip()
        with self.repository.engine.begin() as connection:
            if self.repository.get_expert_group(connection, group_id) is None:
                raise ResourceServiceError("EXPERT_GROUP_NOT_FOUND", "专家组不存在", 404)
            if not self.repository.delete_expert_group_member(
                connection, group_id, expert_id
            ):
                raise ResourceServiceError("GROUP_MEMBER_NOT_FOUND", "专家组成员不存在", 404)

    def delete_expert_group(self, group_id):
        group_id = str(group_id or "").strip()
        with self.repository.engine.begin() as connection:
            if not self.repository.delete_expert_group(connection, group_id):
                raise ResourceServiceError("EXPERT_GROUP_NOT_FOUND", "专家组不存在", 404)

    def export_expert_group_sensitive(
        self, group_id, *, actor_user_id, request_id
    ):
        group_id = str(group_id or "").strip()
        with self.repository.engine.begin() as connection:
            found = self.repository.get_expert_group(connection, group_id)
            if found is None:
                raise ResourceServiceError("EXPERT_GROUP_NOT_FOUND", "专家组不存在", 404)
            group, members = found
            self.audit_service.record(
                connection,
                event_name="expert_sensitive_accessed",
                user_id=actor_user_id,
                object_type="EXPERT",
                object_id=group_id,
                result="SUCCESS",
                request_id=request_id,
                duration_ms=0,
                properties={"operation": "EXPORT", "record_count": len(members)},
            )
        member_items = []
        for member in members:
            item = _expert(member, sensitive=True)
            item.update(
                selectedBy=member.get("selected_by"),
                selectedAt=member.get("selected_at"),
            )
            member_items.append(item)
        return {
            "groupId": group["group_id"],
            "groupName": group["meeting_name"],
            "creator": group["creator"],
            "createdAt": group["created_at"],
            "members": member_items,
        }

    @staticmethod
    def _import_batch(row):
        return {
            "batchId": str(row["id"]),
            "sourceName": row["source_name"],
            "status": row["status"],
            "rows": row["rows"],
            "validCount": row["valid_count"],
            "errorCount": row["error_count"],
            "duplicateCount": row["duplicate_count"],
            "result": row.get("result"),
        }

    def create_expert_import_preview(
        self, *, source_name, source_bytes, rows, owner_user_id
    ):
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ResourceServiceError(
                "VALIDATION_ERROR", "单次导入不能超过 1000 条", 422
            )
        processed = []
        error_count = 0
        for index, row in enumerate(rows):
            raw = dict(row or {})
            try:
                values = _expert_values(raw)
                processed.append({"_row_idx": int(raw.get("_row_idx", index)), **values})
            except ResourceServiceError as error:
                processed.append({
                    "_row_idx": int(raw.get("_row_idx", index)),
                    "name": str(raw.get("name") or "").strip(),
                    "_error": error.message,
                })
                error_count += 1
        candidate_keys = {
            (row.get("name", ""), row.get("unit", ""), row.get("phone", ""))
            for row in processed if not row.get("_error")
        }
        with self.repository.engine.connect() as connection:
            existing_keys = self.repository.existing_expert_keys(
                connection, candidate_keys
            )
        seen = set()
        duplicate_count = 0
        for row in processed:
            if row.get("_error"):
                continue
            key = (row.get("name", ""), row.get("unit", ""), row.get("phone", ""))
            if key in existing_keys or key in seen:
                row["_error"] = "重复记录，已跳过"
                row["_duplicate"] = True
                duplicate_count += 1
            else:
                seen.add(key)
        valid_count = len(processed) - error_count - duplicate_count
        now = datetime.now(timezone.utc)
        batch_id = uuid.uuid4()
        values = {
            "id": batch_id,
            "owner_user_id": int(owner_user_id),
            "source_name": str(source_name or "").strip() or "unnamed.xlsx",
            "source_sha256": hashlib.sha256(bytes(source_bytes)).hexdigest(),
            "status": "PREVIEW",
            "rows": processed,
            "valid_count": valid_count,
            "error_count": error_count,
            "duplicate_count": duplicate_count,
            "result": None,
            "created_at": now,
            "updated_at": now,
            "committed_at": None,
            "version": 1,
        }
        with self.repository.engine.begin() as connection:
            self.repository.insert_expert_import_batch(connection, values)
            row = self.repository.get_expert_import_batch(connection, batch_id)
        return self._import_batch(dict(row))

    def get_expert_import_batch(self, batch_id, *, owner_user_id):
        with self.repository.engine.connect() as connection:
            row = self.repository.get_expert_import_batch(connection, batch_id)
        if row is None or int(row["owner_user_id"]) != int(owner_user_id):
            raise ResourceServiceError("IMPORT_BATCH_NOT_FOUND", "导入批次不存在", 404)
        return self._import_batch(dict(row))

    def commit_expert_import(
        self, batch_id, *, confirmed_rows, owner_user_id, uploader, request_id
    ):
        if not isinstance(confirmed_rows, list) or not confirmed_rows:
            raise ResourceServiceError("VALIDATION_ERROR", "没有勾选任何行", 422)
        if len(confirmed_rows) > 1000:
            raise ResourceServiceError("VALIDATION_ERROR", "单次提交不能超过 1000 条", 422)
        if any(not isinstance(row, Mapping) for row in confirmed_rows):
            raise ResourceServiceError("VALIDATION_ERROR", "导入行格式无效", 422)
        try:
            selected_indexes = [
                int(row.get("_row_idx", position))
                for position, row in enumerate(confirmed_rows)
            ]
        except (TypeError, ValueError):
            raise ResourceServiceError("VALIDATION_ERROR", "导入行索引无效", 422)
        if len(selected_indexes) != len(set(selected_indexes)):
            raise ResourceServiceError("VALIDATION_ERROR", "不能重复提交同一行", 422)
        with self.repository.engine.begin() as connection:
            batch = self.repository.get_expert_import_batch(
                connection, batch_id, lock=True
            )
            if batch is None or int(batch["owner_user_id"]) != int(owner_user_id):
                raise ResourceServiceError("IMPORT_BATCH_NOT_FOUND", "导入批次不存在", 404)
            if batch["status"] != "PREVIEW":
                raise ResourceServiceError("IMPORT_BATCH_CLOSED", "导入批次已结束", 409)
            allowed_indexes = {
                int(row["_row_idx"])
                for row in batch["rows"]
                if not row.get("_error")
            }
            values_list = []
            created = []
            now = datetime.now(timezone.utc)
            for row_index, row in zip(selected_indexes, confirmed_rows):
                if row_index not in allowed_indexes:
                    raise ResourceServiceError("VALIDATION_ERROR", "导入行不属于当前批次", 422)
                values = _expert_values(row)
                values.update(
                    expert_id="EXP" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper(),
                    uploader=str(uploader or "").strip(),
                    created_at=now,
                    updated_at=now,
                )
                values_list.append(values)
                created.append({
                    "expert_id": values["expert_id"],
                    "name": values["name"],
                    "unit": values["unit"],
                })
            self.repository.lock_expert_identity_space(connection)
            final_keys = [
                (values["name"], values["unit"], values["phone"])
                for values in values_list
            ]
            if len(final_keys) != len(set(final_keys)):
                raise ResourceServiceError(
                    "EXPERT_DUPLICATE", "提交内容包含重复专家记录", 409
                )
            if self.repository.existing_expert_keys(connection, final_keys):
                raise ResourceServiceError(
                    "EXPERT_DUPLICATE", "提交内容与已有专家记录重复", 409
                )
            for values in values_list:
                self.repository.insert_expert(connection, values)
            result = {"successCount": len(values_list), "failureCount": 0}
            self.repository.update_expert_import_batch(connection, batch_id, {
                "status": "COMMITTED",
                "result": result,
                "committed_at": now,
                "updated_at": now,
                "version": int(batch["version"]) + 1,
            })
            self.audit_service.record(
                connection,
                event_name="import_batch_completed",
                user_id=owner_user_id,
                object_type="IMPORT_BATCH",
                object_id=str(batch_id),
                result="SUCCESS",
                request_id=request_id,
                duration_ms=0,
                properties={
                    "module": "experts",
                    "valid_count": len(values_list),
                    "error_count": int(batch["error_count"]),
                    "duplicate_count": int(batch["duplicate_count"]),
                },
            )
        return {
            "batchId": str(batch_id), "status": "COMMITTED",
            "sourceName": batch["source_name"],
            "skipCount": int(batch["error_count"]) + int(batch["duplicate_count"]),
            "created": created,
            **result,
        }

    def cancel_expert_import(self, batch_id, *, owner_user_id):
        with self.repository.engine.begin() as connection:
            batch = self.repository.get_expert_import_batch(
                connection, batch_id, lock=True
            )
            if batch is None or int(batch["owner_user_id"]) != int(owner_user_id):
                raise ResourceServiceError("IMPORT_BATCH_NOT_FOUND", "导入批次不存在", 404)
            if batch["status"] != "PREVIEW":
                raise ResourceServiceError("IMPORT_BATCH_CLOSED", "导入批次已结束", 409)
            self.repository.update_expert_import_batch(connection, batch_id, {
                "status": "CANCELLED",
                "updated_at": datetime.now(timezone.utc),
                "version": int(batch["version"]) + 1,
            })
        return {"batchId": str(batch_id), "status": "CANCELLED"}


class EquipmentResourcesService:
    """Equipment resource registration; deliberately not an asset-management system."""

    FIELD_MAP = {
        "name": "name", "model": "model", "category": "category",
        "subclass": "subclass", "form": "form", "price": "price",
        "techIndex": "tech_index", "tech_index": "tech_index",
        "techStatus": "tech_status", "tech_status": "tech_status",
        "installationRequirements": "installation_requirements",
        "installation_requirements": "installation_requirements",
        "manufacturer": "manufacturer", "equipmentImage": "equipment_image",
        "equipment_image": "equipment_image", "relatedFiles": "related_files",
        "related_files": "related_files", "mainPurpose": "main_purpose",
        "main_purpose": "main_purpose", "formerName": "former_name",
        "former_name": "former_name", "resourceGuarantee": "resource_guarantee",
        "resource_guarantee": "resource_guarantee",
    }

    def __init__(self, repository, audit_service=None) -> None:
        self.repository = repository
        self.audit_service = audit_service

    def _values(self, payload, *, require_name=False):
        payload = payload or {}
        values = {}
        for source, target in self.FIELD_MAP.items():
            if source in payload:
                value = payload[source]
                if target == "related_files":
                    value = value if isinstance(value, list) else []
                elif target == "price" and value in (None, ""):
                    value = None
                elif target == "price":
                    try:
                        value = Decimal(str(value))
                    except InvalidOperation:
                        raise ResourceServiceError("VALIDATION_ERROR", "设备单价格式无效", 422)
                elif value is not None:
                    value = str(value).strip()
                values[target] = value
        if require_name and not values.get("name"):
            raise ResourceServiceError("VALIDATION_ERROR", "设备名称不能为空", 422)
        if "name" in values and not values["name"]:
            raise ResourceServiceError("VALIDATION_ERROR", "设备名称不能为空", 422)
        return values

    def list_equipment(self, *, page=1, page_size=20, **filters):
        page = _positive_int(page, "page")
        page_size = _positive_int(page_size, "pageSize", maximum=100)
        normalized = {
            key: str(filters.get(key) or "").strip() or None
            for key in ("category", "form", "tech_status", "keyword", "subclass")
        }
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_equipment(
                connection, page=page, page_size=page_size, **normalized
            )
        return {"items": rows, "page": page, "pageSize": page_size, "total": total}

    def equipment_stats(self):
        with self.repository.engine.connect() as connection:
            categories, statuses = self.repository.equipment_stats(connection)
        by_category = {name: {"count": 0, "value": 0} for name in (
            "安全设备", "密码设备", "通用设备"
        )}
        total = 0
        for row in categories:
            category = row["category"] or "通用设备"
            bucket = by_category.setdefault(category, {"count": 0, "value": 0})
            row_count = int(row["count"] or 0)
            bucket["count"] += row_count
            bucket["value"] += float(row["value"] or 0)
            total += row_count
        return {
            "by_category": by_category,
            "by_status": {row["tech_status"] or "未知": int(row["count"] or 0)
                          for row in statuses},
            "total_value": sum(item["value"] for item in by_category.values()),
            "total": total,
        }

    def find_import_duplicate(self, *, equipment_id="", name="", model=""):
        with self.repository.engine.connect() as connection:
            row = self.repository.import_duplicate(
                connection, str(equipment_id or "").strip(),
                str(name or "").strip(), str(model or "").strip(),
            )
        return dict(row) if row else None

    def match_host_device_text(self, text):
        from app.utils.fuzzy_match import _levenshtein, _normalize, split_device_names

        result = {"original": str(text or ""), "devices": []}
        with self.repository.engine.connect() as connection:
            for name in split_device_names(text):
                exact, candidates = self.repository.host_match_candidates(
                    connection, name, limit=100
                )
                if exact:
                    result["devices"].append({
                        "name": name, "exact": exact, "fuzzy": [],
                        "status": "exact", "selected": exact[0],
                    })
                    continue
                cleaned = _normalize(name)
                fuzzy = []
                for row in candidates:
                    row_name = str(row.get("name") or "")
                    row_model = str(row.get("model") or "")
                    if cleaned and (
                        cleaned in row_name or cleaned in row_model
                        or row_name in cleaned or row_model in cleaned
                    ):
                        fuzzy.append(row)
                    elif len(name) > 5 and len(cleaned) > 5 and (
                        _levenshtein(name, row_name) < 3
                        or _levenshtein(cleaned, row_name) < 3
                        or _levenshtein(name, row_model) < 3
                    ):
                        fuzzy.append(row)
                result["devices"].append({
                    "name": name, "exact": [], "fuzzy": fuzzy,
                    "status": "fuzzy" if fuzzy else "unmatched", "selected": None,
                })
        return result

    def match_research_unit_name(self, name):
        from app.utils.fuzzy_match import match_research_unit

        class Rows:
            def __init__(self, values):
                self.values = values

            def get_all(self):
                return self.values

        with self.repository.engine.connect() as connection:
            rows = self.repository.import_match_units(connection, limit=100)
        return match_research_unit(name, Rows(rows))

    def match_subclass_name(self, name, parent_category=None):
        from app.utils.fuzzy_match import match_subclass

        class Rows:
            def __init__(self, values):
                self.values = values

            def get_all(self):
                return self.values

        with self.repository.engine.connect() as connection:
            rows = self.repository.import_match_subclasses(connection, limit=100)
        return match_subclass(name, Rows(rows), parent_category)

    @staticmethod
    def _equipment_import_batch(row):
        return {
            "batchId": str(row["id"]), "sourceName": row["source_name"],
            "status": row["status"], "rows": row["rows"],
            "statistics": row["statistics"], "result": row.get("result"),
            "version": int(row["version"]),
        }

    def create_equipment_import_preview(
        self, *, source_name, source_bytes, rows, statistics, owner_user_id
    ):
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ResourceServiceError(
                "VALIDATION_ERROR", "单次导入不能超过 1000 条", 422
            )
        if not isinstance(statistics, Mapping):
            raise ResourceServiceError("VALIDATION_ERROR", "导入统计格式无效", 422)
        now = datetime.now(timezone.utc)
        batch_id = uuid.uuid4()
        values = {
            "id": batch_id, "owner_user_id": int(owner_user_id),
            "source_name": str(source_name or "").strip() or "unnamed.xlsx",
            "source_sha256": hashlib.sha256(bytes(source_bytes)).hexdigest(),
            "status": "PREVIEW", "rows": rows, "statistics": dict(statistics),
            "result": None, "created_at": now, "updated_at": now,
            "committed_at": None, "version": 1,
        }
        with self.repository.engine.begin() as connection:
            self.repository.insert_equipment_import_batch(connection, values)
            row = self.repository.get_equipment_import_batch(connection, batch_id)
        return self._equipment_import_batch(dict(row))

    def get_equipment_import_batch(self, batch_id, *, owner_user_id):
        with self.repository.engine.connect() as connection:
            row = self.repository.get_equipment_import_batch(connection, batch_id)
        if row is None or int(row["owner_user_id"]) != int(owner_user_id):
            raise ResourceServiceError("IMPORT_BATCH_NOT_FOUND", "导入批次不存在", 404)
        return self._equipment_import_batch(dict(row))

    def update_equipment_import_row(
        self, batch_id, *, owner_user_id, row_idx, field, value
    ):
        allowed = {
            "name", "model", "category", "form", "price", "tech_index",
            "tech_status", "manufacturer", "main_purpose", "former_name",
            "resource_guarantee", "installation_requirements", "subclass",
            "subclass_action", "dup_action",
        }
        relation_match = re.fullmatch(r"related_select_(\d+)", field)
        if field not in allowed and relation_match is None:
            raise ResourceServiceError("VALIDATION_ERROR", f"不允许的字段: {field}", 422)
        with self.repository.engine.begin() as connection:
            batch = self.repository.get_equipment_import_batch(
                connection, batch_id, lock=True
            )
            if batch is None or int(batch["owner_user_id"]) != int(owner_user_id):
                raise ResourceServiceError("IMPORT_BATCH_NOT_FOUND", "导入批次不存在", 404)
            if batch["status"] != "PREVIEW":
                raise ResourceServiceError("IMPORT_BATCH_CLOSED", "导入批次已结束", 409)
            rows = [dict(item) for item in batch["rows"]]
            target = next((item for item in rows if int(item.get("row_idx", -1)) == int(row_idx)), None)
            if target is None:
                raise ResourceServiceError("IMPORT_ROW_NOT_FOUND", "导入行不存在", 404)
            if relation_match is not None:
                relation_index = int(relation_match.group(1))
                devices = (target.get("related_result") or {}).get("devices", [])
                if relation_index >= len(devices):
                    raise ResourceServiceError(
                        "VALIDATION_ERROR", "关联设备序号无效", 422
                    )
                selected_id = str(value or "").strip()
                if (
                    selected_id not in {"", "_skip_"}
                    and self.repository.get_host_device(connection, selected_id) is None
                ):
                    raise ResourceServiceError(
                        "RESOURCE_NOT_FOUND", "关联宿主设备不存在", 409
                    )
                target.setdefault("_user_actions", {})[field] = selected_id
            elif field in {"subclass_action", "dup_action"}:
                target.setdefault("_user_actions", {})[field] = str(value or "").strip()
            elif field == "price":
                try:
                    target["price"] = float(value) if str(value or "").strip() else None
                    target["price_error"] = ""
                except (TypeError, ValueError):
                    target["price_error"] = "价格格式错误"
            else:
                target[field] = str(value or "").strip()
            self.repository.update_equipment_import_batch(connection, batch_id, {
                "rows": rows, "updated_at": datetime.now(timezone.utc),
                "version": int(batch["version"]) + 1,
            })
        return {"batchId": str(batch_id), "rowIdx": int(row_idx), "field": field}

    def commit_equipment_import(
        self, batch_id, *, form_data, owner_user_id,
        request_id, cancel_check=None, progress_callback=None,
        begin_commit_callback=None,
    ):
        if not isinstance(form_data, Mapping):
            raise ResourceServiceError("VALIDATION_ERROR", "导入提交格式无效", 422)
        now = datetime.now(timezone.utc)
        result = {
            "new": 0, "overwrite": 0, "skip": 0,
            "relations": 0, "skipped_relations": [],
        }
        try:
            with self.repository.engine.begin() as connection:
                batch = self.repository.get_equipment_import_batch(
                    connection, batch_id, lock=True
                )
                if batch is None or int(batch["owner_user_id"]) != int(owner_user_id):
                    raise ResourceServiceError("IMPORT_BATCH_NOT_FOUND", "导入批次不存在", 404)
                if batch["status"] != "PREVIEW":
                    raise ResourceServiceError("IMPORT_BATCH_CLOSED", "导入批次已结束", 409)
                for position, row in enumerate(batch["rows"]):
                    if cancel_check and cancel_check():
                        raise ResourceServiceError("IMPORT_CANCELLED", "导入已取消", 409)
                    row_idx = int(row["row_idx"])

                    def value(field, default=""):
                        raw = form_data.get(f"{field}_{row_idx}", row.get(field, default))
                        return str(raw or "").strip()

                    name = value("name")
                    if not name:
                        result["skip"] += 1
                        if progress_callback:
                            progress_callback(position + 1, "<跳过（无名称）>")
                        continue
                    category = value("category")
                    if category not in {"安全设备", "密码设备", "通用设备", "其他设备"}:
                        category = "其他设备"
                    try:
                        price = Decimal(value("price") or "0")
                    except InvalidOperation:
                        price = Decimal("0")
                    subclass = value("subclass")
                    actions = row.get("_user_actions") or {}
                    subclass_action = value("subclass_action", actions.get("subclass_action", ""))
                    dup_action = value("dup_action", actions.get("dup_action", ""))
                    if subclass_action == "confirm" and subclass and not self.repository.get_subclass(
                        connection, category, subclass
                    ):
                        self.repository.insert_subclass(connection, {
                            "parent_category": category, "subclass_name": subclass,
                            "created_at": now,
                        })
                    values = {
                        "name": name, "model": value("model"), "category": category,
                        "form": value("form"), "price": price,
                        "tech_index": value("tech_index"),
                        "tech_status": value("tech_status") or "货架产品",
                        "manufacturer": value("manufacturer"),
                        "main_purpose": value("main_purpose"),
                        "former_name": value("former_name"),
                        "resource_guarantee": value("resource_guarantee"),
                        "installation_requirements": value("installation_requirements"),
                        "subclass": subclass, "updated_at": now,
                    }
                    existing = row.get("existing_equipment") or {}
                    saved_id = None
                    if row.get("duplicate_status") == "exists" and dup_action == "skip":
                        result["skip"] += 1
                    elif row.get("duplicate_status") == "exists" and dup_action == "overwrite":
                        saved_id = str(existing.get("equipment_id") or "").strip()
                        if not saved_id or not self.repository.update_equipment(
                            connection, saved_id, values
                        ):
                            raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "待覆盖设备不存在", 409)
                        result["overwrite"] += 1
                    else:
                        saved_id = "EQP" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:10].upper()
                        self.repository.insert_equipment(connection, {
                            **values, "equipment_id": saved_id, "related_files": [],
                            "created_at": now,
                        })
                        result["new"] += 1
                    for relation_index, matched in enumerate(
                        (row.get("related_result") or {}).get("devices", [])
                    ):
                        form_key = f"related_select_{row_idx}_{relation_index}"
                        saved_key = f"related_select_{relation_index}"
                        selected_id = str(
                            form_data.get(form_key, actions.get(saved_key, "")) or ""
                        ).strip()
                        if not selected_id:
                            selected = matched.get("selected")
                            exact = matched.get("exact") or []
                            fuzzy = matched.get("fuzzy") or []
                            selected_id = str((selected or (exact[0] if exact else None)
                                               or (fuzzy[0] if fuzzy else None) or {}).get("host_id") or "")
                        if selected_id == "_skip_":
                            result["skipped_relations"].append({
                                "name": name, "device": matched.get("name", ""),
                            })
                            continue
                        if selected_id and saved_id:
                            if self.repository.get_host_device(connection, selected_id) is None:
                                raise ResourceServiceError("RESOURCE_NOT_FOUND", "关联宿主设备不存在", 409)
                            if self.repository.add_import_relation(
                                connection, saved_id, selected_id, now
                            ):
                                result["relations"] += 1
                    if progress_callback:
                        progress_callback(position + 1, name)
                if cancel_check and cancel_check():
                    raise ResourceServiceError("IMPORT_CANCELLED", "导入已取消", 409)
                if begin_commit_callback is not None and not begin_commit_callback():
                    raise ResourceServiceError("IMPORT_CANCELLED", "导入已取消", 409)
                result_payload = {
                    "saved_new": result["new"],
                    "saved_overwrite": result["overwrite"],
                    "skipped": result["skip"],
                    "relations_created": result["relations"],
                    "skipped_relations": result["skipped_relations"],
                }
                self.repository.update_equipment_import_batch(connection, batch_id, {
                    "status": "COMMITTED", "result": result_payload,
                    "committed_at": now, "updated_at": now,
                    "version": int(batch["version"]) + 1,
                })
                if self.audit_service is not None:
                    self.audit_service.record(
                        connection, event_name="import_batch_completed",
                        user_id=owner_user_id, object_type="IMPORT_BATCH",
                        object_id=str(batch_id), result="SUCCESS", request_id=request_id,
                        duration_ms=0, properties={
                            "module": "equipment",
                            "saved_new": result_payload["saved_new"],
                            "saved_overwrite": result_payload["saved_overwrite"],
                            "skipped": result_payload["skipped"],
                            "relations_created": result_payload["relations_created"],
                            "skipped_relation_count": len(result_payload["skipped_relations"]),
                        },
                    )
            return {"batchId": str(batch_id), "status": "COMMITTED", **result_payload}
        except ResourceServiceError as error:
            target_status = "CANCELLED" if error.code == "IMPORT_CANCELLED" else "FAILED"
            with self.repository.engine.begin() as connection:
                batch = self.repository.get_equipment_import_batch(connection, batch_id, lock=True)
                if (
                    batch and int(batch["owner_user_id"]) == int(owner_user_id)
                    and batch["status"] == "PREVIEW"
                ):
                    self.repository.update_equipment_import_batch(connection, batch_id, {
                        "status": target_status,
                        "result": {**result, "error_code": error.code},
                        "updated_at": datetime.now(timezone.utc),
                        "version": int(batch["version"]) + 1,
                    })
            raise
        except Exception:
            with self.repository.engine.begin() as connection:
                batch = self.repository.get_equipment_import_batch(connection, batch_id, lock=True)
                if (
                    batch and int(batch["owner_user_id"]) == int(owner_user_id)
                    and batch["status"] == "PREVIEW"
                ):
                    self.repository.update_equipment_import_batch(connection, batch_id, {
                        "status": "FAILED", "result": result,
                        "updated_at": datetime.now(timezone.utc),
                        "version": int(batch["version"]) + 1,
                    })
            raise

    def get_equipment(self, equipment_id):
        with self.repository.engine.connect() as connection:
            row = self.repository.get_equipment(connection, str(equipment_id).strip())
        return dict(row) if row else None

    def create_equipment(self, payload):
        values = self._values(payload, require_name=True)
        now = datetime.now(timezone.utc)
        equipment_id = "EQP" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:10].upper()
        values.update(equipment_id=equipment_id, created_at=now, updated_at=now)
        values.setdefault("related_files", [])
        with self.repository.engine.begin() as connection:
            self.repository.insert_equipment(connection, values)
            row = self.repository.get_equipment(connection, equipment_id)
        return dict(row)

    def import_equipment_rows(self, rows):
        """Compatibility CSV import backed by the same atomic resource store."""
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ResourceServiceError(
                "VALIDATION_ERROR", "单次导入不能超过 1000 条", 422
            )
        now = datetime.now(timezone.utc)
        values_list = []
        for payload in rows:
            if not str((payload or {}).get("name") or "").strip():
                continue
            values = self._values(payload, require_name=True)
            values.setdefault("category", "通用设备")
            values.setdefault("tech_status", "货架产品")
            values.setdefault("related_files", [])
            values.update(
                equipment_id="EQP" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:10].upper(),
                created_at=now, updated_at=now,
            )
            values_list.append(values)
        with self.repository.engine.begin() as connection:
            for values in values_list:
                self.repository.insert_equipment(connection, values)
        return len(values_list)

    def update_equipment(self, equipment_id, payload):
        equipment_id = str(equipment_id or "").strip()
        values = self._values(payload)
        values["updated_at"] = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            if not self.repository.update_equipment(connection, equipment_id, values):
                raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "设备不存在", 404)
            row = self.repository.get_equipment(connection, equipment_id)
        return dict(row)

    def delete_equipment(self, equipment_id):
        with self.repository.engine.begin() as connection:
            if not self.repository.delete_equipment(connection, str(equipment_id).strip()):
                raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "设备不存在", 404)

    def list_research_units(self):
        with self.repository.engine.connect() as connection:
            return self.repository.list_research_units(connection)

    def create_research_unit(self, name, alias=""):
        name, alias = str(name or "").strip(), str(alias or "").strip()
        if not name:
            raise ResourceServiceError("VALIDATION_ERROR", "研制单位名称不能为空", 422)
        now = datetime.now(timezone.utc)
        unit_id = "RU" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()
        try:
            with self.repository.engine.begin() as connection:
                if self.repository.get_research_unit_by_name(connection, name):
                    raise ResourceServiceError("RESOURCE_DUPLICATE", "研制单位已存在", 409)
                self.repository.insert_research_unit(connection, {
                    "unit_id": unit_id, "name": name, "alias": alias,
                    "created_at": now, "updated_at": now,
                })
                row = self.repository.get_research_unit_by_name(connection, name)
        except sa.exc.IntegrityError as error:
            raise ResourceServiceError("RESOURCE_DUPLICATE", "研制单位已存在", 409) from error
        return dict(row)

    def update_research_unit(self, unit_id, name, alias=""):
        unit_id = str(unit_id or "").strip()
        name, alias = str(name or "").strip(), str(alias or "").strip()
        if not name:
            raise ResourceServiceError("VALIDATION_ERROR", "研制单位名称不能为空", 422)
        try:
            with self.repository.engine.begin() as connection:
                duplicate = self.repository.get_research_unit_by_name(connection, name)
                if duplicate and duplicate["unit_id"] != unit_id:
                    raise ResourceServiceError("RESOURCE_DUPLICATE", "研制单位已存在", 409)
                if not self.repository.update_research_unit(connection, unit_id, {
                    "name": name, "alias": alias, "updated_at": datetime.now(timezone.utc),
                }):
                    raise ResourceServiceError("RESOURCE_NOT_FOUND", "研制单位不存在", 404)
                row = self.repository.get_research_unit(connection, unit_id)
        except sa.exc.IntegrityError as error:
            raise ResourceServiceError("RESOURCE_DUPLICATE", "研制单位已存在", 409) from error
        return dict(row)

    def delete_research_unit(self, unit_id):
        with self.repository.engine.begin() as connection:
            if not self.repository.delete_research_unit(connection, str(unit_id).strip()):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "研制单位不存在", 404)

    def list_subclasses(self, parent_category=None):
        with self.repository.engine.connect() as connection:
            return self.repository.list_subclasses(
                connection, str(parent_category or "").strip() or None
            )

    def create_subclass(self, parent_category, subclass_name):
        parent_category = str(parent_category or "").strip()
        subclass_name = str(subclass_name or "").strip()
        if not parent_category or not subclass_name:
            raise ResourceServiceError("VALIDATION_ERROR", "分类和子类不能为空", 422)
        try:
            with self.repository.engine.begin() as connection:
                if self.repository.get_subclass(connection, parent_category, subclass_name):
                    raise ResourceServiceError("RESOURCE_DUPLICATE", "设备子类已存在", 409)
                row_id = self.repository.insert_subclass(connection, {
                    "parent_category": parent_category,
                    "subclass_name": subclass_name,
                    "created_at": datetime.now(timezone.utc),
                })
        except sa.exc.IntegrityError as error:
            raise ResourceServiceError("RESOURCE_DUPLICATE", "设备子类已存在", 409) from error
        return {"id": row_id, "parent_category": parent_category,
                "subclass_name": subclass_name}

    def delete_subclass(self, row_id):
        with self.repository.engine.begin() as connection:
            if not self.repository.delete_subclass(connection, int(row_id)):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "设备子类不存在", 404)

    def get_or_create_project_group(self, project_id, project_name, creator):
        project_id = str(project_id or "").strip()
        project_name = str(project_name or "").strip()
        if not project_id or not project_name:
            raise ResourceServiceError("VALIDATION_ERROR", "项目和设备组名称不能为空", 422)
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            self.repository.lock_project_group(connection, project_id)
            row = self.repository.get_group_by_project(connection, project_id)
            if row is None:
                group_id = "FG" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()
                self.repository.insert_equipment_group(connection, {
                    "group_id": group_id, "project_name": project_name,
                    "project_id": project_id, "creator": str(creator or "").strip(),
                    "created_at": now, "updated_at": now,
                })
                row = self.repository.get_group_by_project(connection, project_id)
        return dict(row)

    def create_equipment_group(self, project_name, creator, project_id=None):
        project_id = str(project_id or "").strip() or None
        project_name = str(project_name or "").strip()
        if not project_name:
            raise ResourceServiceError("VALIDATION_ERROR", "设备组名称不能为空", 422)
        now = datetime.now(timezone.utc)
        group_id = "FG" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()
        values = {"group_id": group_id, "project_name": project_name,
                  "project_id": project_id, "creator": str(creator or "").strip(),
                  "created_at": now, "updated_at": now}
        try:
            with self.repository.engine.begin() as connection:
                if project_id:
                    self.repository.lock_project_group(connection, project_id)
                    if self.repository.get_group_by_project(connection, project_id):
                        raise ResourceServiceError(
                            "RESOURCE_DUPLICATE", "该项目已有关联设备组", 409
                        )
                self.repository.insert_equipment_group(connection, values)
        except sa.exc.IntegrityError as error:
            raise ResourceServiceError(
                "RESOURCE_DUPLICATE", "该项目已有关联设备组", 409
            ) from error
        return values

    def list_equipment_groups(self, *, page=1, page_size=100, project_id=None):
        page = _positive_int(page, "page")
        page_size = _positive_int(page_size, "pageSize", maximum=100)
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_equipment_groups(
                connection, page=page, page_size=page_size,
                project_id=str(project_id or "").strip() or None,
            )
        return {"items": rows, "page": page, "pageSize": page_size, "total": total}

    def add_group_member(self, group_id, equipment_id, *, quantity=1,
                         location="", selected_by=""):
        quantity = _positive_int(quantity, "quantity")
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            self.repository.lock_equipment_group(connection, str(group_id).strip())
            group, _ = self.repository.get_equipment_group(connection, str(group_id).strip())
            if group is None:
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "设备组不存在", 404)
            if self.repository.get_equipment(connection, str(equipment_id).strip()) is None:
                raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "设备不存在", 404)
            self.repository.upsert_group_member(connection, {
                "group_id": str(group_id).strip(),
                "equipment_id": str(equipment_id).strip(),
                "quantity": quantity, "location": str(location or "").strip(),
                "selected_by": str(selected_by or "").strip(), "selected_at": now,
            })

    def add_group_members(self, group_id, equipment_ids, *, quantity=1,
                          location="", selected_by=""):
        quantity = _positive_int(quantity, "quantity")
        normalized_ids = []
        seen = set()
        for value in equipment_ids or []:
            equipment_id = str(value or "").strip()
            if equipment_id and equipment_id not in seen:
                seen.add(equipment_id)
                normalized_ids.append(equipment_id)
        if not normalized_ids:
            raise ResourceServiceError("VALIDATION_ERROR", "请选择设备", 422)
        group_id = str(group_id or "").strip()
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            self.repository.lock_equipment_group(connection, group_id)
            group, _ = self.repository.get_equipment_group(connection, group_id)
            if group is None:
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "设备组不存在", 404)
            for equipment_id in normalized_ids:
                if self.repository.get_equipment(connection, equipment_id) is None:
                    raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "设备不存在", 404)
            for equipment_id in normalized_ids:
                self.repository.upsert_group_member(connection, {
                    "group_id": group_id, "equipment_id": equipment_id,
                    "quantity": quantity, "location": str(location or "").strip(),
                    "selected_by": str(selected_by or "").strip(), "selected_at": now,
                })
        return len(normalized_ids)

    def get_equipment_group(self, group_id):
        with self.repository.engine.connect() as connection:
            group, members = self.repository.get_equipment_group(
                connection, str(group_id).strip()
            )
        if group is None:
            raise ResourceServiceError("RESOURCE_NOT_FOUND", "设备组不存在", 404)
        group["members"] = members
        return group

    def remove_group_member(self, group_id, equipment_id):
        with self.repository.engine.begin() as connection:
            self.repository.delete_group_member(
                connection, str(group_id).strip(), str(equipment_id).strip()
            )

    def delete_equipment_group(self, group_id):
        with self.repository.engine.begin() as connection:
            if not self.repository.delete_equipment_group(connection, str(group_id).strip()):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "设备组不存在", 404)

    def available_equipment(self, group_id, *, keyword=None, category=None, form=None):
        with self.repository.engine.connect() as connection:
            items = self.repository.list_available_equipment(
                connection, str(group_id).strip(), keyword=str(keyword or "").strip() or None,
                category=str(category or "").strip() or None,
                form=str(form or "").strip() or None, limit=20,
            )
            facets = self.repository.equipment_facets(connection)
        return {"items": items, **facets}

    @staticmethod
    def _project_candidate(row):
        item = dict(row)
        item["id"] = item["equipment_id"]
        return item

    def search_equipment_candidates(self, *, keyword=None, category=None,
                                    page=1, page_size=20):
        page = _positive_int(page, "page")
        page_size = _positive_int(page_size, "pageSize", maximum=50)
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_equipment(
                connection, page=page, page_size=page_size,
                keyword=str(keyword or "").strip() or None,
                category=str(category or "").strip() or None,
            )
        return {
            "items": [self._project_candidate(row) for row in rows],
            "page": page, "pageSize": page_size, "total": total,
        }

    def project_equipment(self, project_id, *, available_page_size=20):
        project_id = str(project_id or "").strip()
        with self.repository.engine.connect() as connection:
            groups, _ = self.repository.list_equipment_groups(
                connection, page=1, page_size=100, project_id=project_id
            )
            items = []
            for group in groups:
                _, members = self.repository.get_equipment_group(
                    connection, group["group_id"]
                )
                for member in members:
                    member["id"] = member["equipment_id"]
                    member["group_id"] = group["group_id"]
                    member["related_files"] = ""
                    items.append(member)
            available, available_total = self.repository.list_equipment(
                connection, page=1,
                page_size=_positive_int(
                    available_page_size, "pageSize", maximum=100
                ),
            )
        return {
            "groups": groups, "items": items,
            "available": [self._project_candidate(row) for row in available],
            "available_total": available_total,
        }

    def link_project_equipment(self, project_id, project_name, creator,
                               equipment_id, *, quantity=1, location=""):
        project_id = str(project_id or "").strip()
        project_name = str(project_name or "").strip()
        equipment_id = str(equipment_id or "").strip()
        quantity = _positive_int(quantity, "quantity")
        if not project_id or not project_name or not equipment_id:
            raise ResourceServiceError("VALIDATION_ERROR", "项目和设备不能为空", 422)
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            self.repository.lock_project_group(connection, project_id)
            group = self.repository.get_group_by_project(connection, project_id)
            if group is None:
                group_id = "FG" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()
                self.repository.insert_equipment_group(connection, {
                    "group_id": group_id, "project_name": project_name,
                    "project_id": project_id, "creator": str(creator or "").strip(),
                    "created_at": now, "updated_at": now,
                })
                group = self.repository.get_group_by_project(connection, project_id)
            if self.repository.get_equipment(connection, equipment_id) is None:
                raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "设备不存在", 404)
            self.repository.lock_equipment_group(connection, group["group_id"])
            self.repository.upsert_group_member(connection, {
                "group_id": group["group_id"], "equipment_id": equipment_id,
                "quantity": quantity, "location": str(location or "").strip(),
                "selected_by": str(creator or "").strip(), "selected_at": now,
            })
        return {"group_id": group["group_id"], "equipment_id": equipment_id}

    def unlink_project_equipment(self, project_id, group_id, equipment_id):
        with self.repository.engine.begin() as connection:
            self.repository.lock_equipment_group(connection, str(group_id).strip())
            group, _ = self.repository.get_equipment_group(connection, str(group_id).strip())
            if group is None or group.get("project_id") != str(project_id).strip():
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "项目设备关联不存在", 404)
            if not self.repository.delete_group_member(
                connection, str(group_id).strip(), str(equipment_id).strip()
            ):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "项目设备关联不存在", 404)

    def update_project_equipment(self, project_id, group_id, equipment_id, *,
                                 quantity=1, location=""):
        quantity = _positive_int(quantity, "quantity")
        with self.repository.engine.begin() as connection:
            self.repository.lock_equipment_group(connection, str(group_id).strip())
            group, _ = self.repository.get_equipment_group(connection, str(group_id).strip())
            if group is None or group.get("project_id") != str(project_id).strip():
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "项目设备关联不存在", 404)
            if not self.repository.update_group_member(
                connection, str(group_id).strip(), str(equipment_id).strip(), {
                    "quantity": quantity, "location": str(location or "").strip(),
                    "selected_at": datetime.now(timezone.utc),
                }
            ):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "项目设备关联不存在", 404)

    @staticmethod
    def _relations(values):
        normalized = []
        seen = set()
        for row in values or []:
            if not isinstance(row, Mapping):
                raise ResourceServiceError("VALIDATION_ERROR", "关联设备格式无效", 422)
            device_id = str((row or {}).get("device_id") or "").strip()
            if not device_id or device_id in seen:
                raise ResourceServiceError("VALIDATION_ERROR", "关联设备不能重复或为空", 422)
            seen.add(device_id)
            normalized.append({
                "device_id": device_id,
                "quantity": _positive_int(row.get("quantity", 1), "quantity"),
            })
        return normalized

    def create_host_device(self, payload, *, relations=None):
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ResourceServiceError("VALIDATION_ERROR", "宿主设备名称不能为空", 422)
        normalized = self._relations(relations)
        now = datetime.now(timezone.utc)
        host_id = "HD" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:10].upper()
        with self.repository.engine.begin() as connection:
            for relation in normalized:
                if self.repository.get_equipment(connection, relation["device_id"]) is None:
                    raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "关联设备不存在", 404)
            self.repository.insert_host_device(connection, {
                "host_id": host_id, "name": name,
                "model": str(payload.get("model") or "").strip(),
                "category": str(payload.get("category") or "").strip(),
                "form": str(payload.get("form") or "").strip(),
                "created_at": now, "updated_at": now,
            })
            self.repository.replace_host_relations(connection, host_id, normalized, now)
            row = self.repository.get_host_device(connection, host_id)
        return dict(row)

    def match_equipment_text(self, text):
        from app.utils.fuzzy_match import _levenshtein, _normalize, split_device_names

        result = {"original": str(text or ""), "devices": []}
        with self.repository.engine.connect() as connection:
            for name in split_device_names(text):
                exact, candidates = self.repository.equipment_match_candidates(
                    connection, name, limit=100
                )
                if exact:
                    result["devices"].append({
                        "name": name, "exact": exact, "fuzzy": [],
                        "status": "exact", "selected": exact[0],
                    })
                    continue
                cleaned = _normalize(name)
                fuzzy = []
                for row in candidates:
                    row_name = str(row.get("name") or "")
                    row_model = str(row.get("model") or "")
                    if cleaned and (
                        cleaned in row_name or cleaned in row_model
                        or row_name in cleaned or row_model in cleaned
                    ):
                        fuzzy.append(row)
                    elif len(name) > 5 and len(cleaned) > 5 and (
                        _levenshtein(name, row_name) < 3
                        or _levenshtein(cleaned, row_name) < 3
                        or _levenshtein(name, row_model) < 3
                    ):
                        fuzzy.append(row)
                result["devices"].append({
                    "name": name, "exact": [], "fuzzy": fuzzy,
                    "status": "fuzzy" if fuzzy else "unmatched", "selected": None,
                })
        return result

    def import_host_devices(self, rows):
        now = datetime.now(timezone.utc)
        result = {"new": 0, "overwrite": 0, "skip": 0,
                  "relations": 0, "skipped_relations": []}
        with self.repository.engine.begin() as connection:
            for row in rows or []:
                name = str(row.get("name") or "").strip()
                if not name or row.get("action") == "skip":
                    result["skip"] += 1
                    continue
                category = str(row.get("category") or "").strip()
                if category and self.repository.get_host_category(connection, category) is None:
                    self.repository.insert_host_category(connection, category, now)
                existing_id = str(row.get("existing_host_id") or "").strip()
                if existing_id and row.get("action") == "overwrite":
                    if not self.repository.update_host_device(connection, existing_id, {
                        "name": name, "model": str(row.get("model") or "").strip(),
                        "category": category, "form": str(row.get("form") or "").strip(),
                        "updated_at": now,
                    }):
                        raise ResourceServiceError("RESOURCE_NOT_FOUND", "宿主设备不存在", 404)
                    host_id = existing_id
                    current = {
                        item["device_id"]: {
                            "device_id": item["device_id"], "quantity": item["quantity"]
                        }
                        for item in self.repository.get_devices_by_host(connection, host_id)
                    }
                    result["overwrite"] += 1
                else:
                    host_id = "HD" + now.strftime("%Y%m%d") + uuid.uuid4().hex[:10].upper()
                    self.repository.insert_host_device(connection, {
                        "host_id": host_id, "name": name,
                        "model": str(row.get("model") or "").strip(),
                        "category": category, "form": str(row.get("form") or "").strip(),
                        "created_at": now, "updated_at": now,
                    })
                    current = {}
                    result["new"] += 1
                for device_id in row.get("device_ids") or []:
                    device_id = str(device_id or "").strip()
                    if not device_id:
                        continue
                    if self.repository.get_equipment(connection, device_id) is None:
                        raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "关联设备不存在", 404)
                    if device_id not in current:
                        result["relations"] += 1
                    current[device_id] = {"device_id": device_id, "quantity": 1}
                self.repository.replace_host_relations(
                    connection, host_id, list(current.values()), now
                )
        return result

    def replace_host_relations(self, host_id, relations):
        host_id = str(host_id or "").strip()
        normalized = self._relations(relations)
        with self.repository.engine.begin() as connection:
            if self.repository.get_host_device(connection, host_id) is None:
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "宿主设备不存在", 404)
            for relation in normalized:
                if self.repository.get_equipment(connection, relation["device_id"]) is None:
                    raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "关联设备不存在", 404)
            self.repository.replace_host_relations(
                connection, host_id, normalized, datetime.now(timezone.utc)
            )

    def upsert_host_relations(self, host_id, relations):
        host_id = str(host_id or "").strip()
        incoming = self._relations(relations)
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            if self.repository.lock_host_device(connection, host_id) is None:
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "宿主设备不存在", 404)
            current = {
                row["device_id"]: {"device_id": row["device_id"], "quantity": row["quantity"]}
                for row in self.repository.get_devices_by_host(connection, host_id)
            }
            for row in incoming:
                if self.repository.get_equipment(connection, row["device_id"]) is None:
                    raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "关联设备不存在", 404)
                current[row["device_id"]] = row
            self.repository.replace_host_relations(connection, host_id, list(current.values()), now)

    def update_host_relation(self, host_id, device_id, quantity):
        host_id, device_id = str(host_id or "").strip(), str(device_id or "").strip()
        quantity = _positive_int(quantity, "quantity")
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            if self.repository.lock_host_device(connection, host_id) is None:
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "宿主设备不存在", 404)
            current = self.repository.get_devices_by_host(connection, host_id)
            found = False
            normalized = []
            for row in current:
                item = {"device_id": row["device_id"], "quantity": row["quantity"]}
                if row["device_id"] == device_id:
                    item["quantity"] = quantity
                    found = True
                normalized.append(item)
            if not found:
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "设备关联不存在", 404)
            self.repository.replace_host_relations(connection, host_id, normalized, now)

    def remove_host_relation(self, host_id, device_id):
        host_id, device_id = str(host_id or "").strip(), str(device_id or "").strip()
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            if self.repository.lock_host_device(connection, host_id) is None:
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "宿主设备不存在", 404)
            current = self.repository.get_devices_by_host(connection, host_id)
            normalized = [
                {"device_id": row["device_id"], "quantity": row["quantity"]}
                for row in current if row["device_id"] != device_id
            ]
            if len(normalized) == len(current):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "设备关联不存在", 404)
            self.repository.replace_host_relations(connection, host_id, normalized, now)

    def get_devices_by_host(self, host_id):
        with self.repository.engine.connect() as connection:
            return self.repository.get_devices_by_host(connection, str(host_id).strip())

    def get_hosts_by_device(self, device_id):
        with self.repository.engine.connect() as connection:
            if self.repository.get_equipment(connection, str(device_id).strip()) is None:
                raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "设备不存在", 404)
            return self.repository.get_hosts_by_device(connection, str(device_id).strip())

    def add_device_host_relation(self, device_id, host_id):
        self.upsert_host_relations(
            str(host_id or "").strip(),
            [{"device_id": str(device_id or "").strip(), "quantity": 1}],
        )

    def remove_device_host_relation(self, device_id, host_id):
        self.remove_host_relation(
            str(host_id or "").strip(), str(device_id or "").strip()
        )

    def export_equipment(self):
        with self.repository.engine.connect() as connection:
            rows = self.repository.export_equipment(connection)
            hosts_by_equipment = {}
            for relation in self.repository.export_equipment_host_relations(connection):
                device_id = relation.pop("device_id")
                hosts_by_equipment.setdefault(device_id, []).append(relation)
            for row in rows:
                row["hosts"] = hosts_by_equipment.get(row["equipment_id"], [])
        return rows

    def list_host_devices(self, *, page=1, page_size=20, category=None,
                          form=None, keyword=None):
        page = _positive_int(page, "page")
        page_size = _positive_int(page_size, "pageSize", maximum=100)
        with self.repository.engine.connect() as connection:
            rows, total = self.repository.list_host_devices(
                connection, page=page, page_size=page_size,
                category=str(category or "").strip() or None,
                form=str(form or "").strip() or None,
                keyword=str(keyword or "").strip() or None,
            )
            for row in rows:
                devices = self.repository.get_devices_by_host(connection, row["host_id"])
                names = [item["name"] for item in devices]
                row["_device_summary"] = "无" if not names else (
                    "、".join(names) if len(names) <= 2 else f"{names[0]}、{names[1]}等{len(names)}台"
                )
        return {"items": rows, "page": page, "pageSize": page_size, "total": total}

    def get_host_device(self, host_id):
        with self.repository.engine.connect() as connection:
            row = self.repository.get_host_device(connection, str(host_id).strip())
        return dict(row) if row else None

    def update_host_device(self, host_id, payload, *, relations=None):
        host_id = str(host_id or "").strip()
        payload = payload or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ResourceServiceError("VALIDATION_ERROR", "宿主设备名称不能为空", 422)
        normalized = self._relations(relations)
        now = datetime.now(timezone.utc)
        with self.repository.engine.begin() as connection:
            if not self.repository.update_host_device(connection, host_id, {
                "name": name, "model": str(payload.get("model") or "").strip(),
                "category": str(payload.get("category") or "").strip(),
                "form": str(payload.get("form") or "").strip(), "updated_at": now,
            }):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "宿主设备不存在", 404)
            for relation in normalized:
                if self.repository.get_equipment(connection, relation["device_id"]) is None:
                    raise ResourceServiceError("EQUIPMENT_NOT_FOUND", "关联设备不存在", 404)
            self.repository.replace_host_relations(connection, host_id, normalized, now)
        return self.get_host_device(host_id)

    def delete_host_device(self, host_id):
        with self.repository.engine.begin() as connection:
            if not self.repository.delete_host_device(connection, str(host_id).strip()):
                raise ResourceServiceError("RESOURCE_NOT_FOUND", "宿主设备不存在", 404)

    def list_host_categories(self):
        with self.repository.engine.connect() as connection:
            return self.repository.list_host_categories(connection)

    def export_host_devices(self):
        with self.repository.engine.connect() as connection:
            rows = self.repository.export_host_devices(connection)
            for row in rows:
                row["devices"] = self.repository.get_devices_by_host(
                    connection, row["host_id"]
                )
        return rows

    def create_host_category(self, name):
        name = str(name or "").strip()
        if not name:
            raise ResourceServiceError("VALIDATION_ERROR", "类型名称不能为空", 422)
        try:
            with self.repository.engine.begin() as connection:
                if self.repository.get_host_category(connection, name):
                    raise ResourceServiceError("RESOURCE_DUPLICATE", "类型已存在", 409)
                self.repository.insert_host_category(connection, name, datetime.now(timezone.utc))
        except sa.exc.IntegrityError as error:
            raise ResourceServiceError("RESOURCE_DUPLICATE", "类型已存在", 409) from error

    def merge_host_category(self, old_name, new_name):
        old_name, new_name = str(old_name or "").strip(), str(new_name or "").strip()
        if not old_name or not new_name or old_name == new_name:
            raise ResourceServiceError("VALIDATION_ERROR", "类型合并参数无效", 422)
        with self.repository.engine.begin() as connection:
            if self.repository.get_host_category(connection, new_name) is None:
                self.repository.insert_host_category(connection, new_name, datetime.now(timezone.utc))
            self.repository.merge_host_category(connection, old_name, new_name, datetime.now(timezone.utc))

    def delete_host_category(self, name):
        with self.repository.engine.begin() as connection:
            if not self.repository.delete_host_category(connection, str(name or "").strip()):
                raise ResourceServiceError("RESOURCE_IN_USE", "该类型有关联设备，无法删除", 409)
