from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
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
    try:
        parsed = int(value)
    except (TypeError, ValueError):
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

    def __init__(self, repository) -> None:
        self.repository = repository

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
