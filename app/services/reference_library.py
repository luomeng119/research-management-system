from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote
import uuid

import sqlalchemy as sa

from app.services.files import FileServiceError


RETAINED_TEMPLATE_CATEGORIES = ("财务模板", "会务模板", "公文模板", "方案模板", "其他模板")


class ReferenceLibraryError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ReferenceLibraryService:
    """Retained resource operations backed by metadata and controlled files only."""

    def __init__(self, repository, audit_service, file_service) -> None:
        self.repository = repository
        self.audit_service = audit_service
        self.file_service = file_service

    def _require_file_service(self):
        if self.file_service is None:
            raise ReferenceLibraryError("REFERENCE_LIBRARY_UNAVAILABLE", "文件服务未就绪", 503)
        return self.file_service

    @staticmethod
    def _name(value: str, *, kind: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ReferenceLibraryError("INVALID_REFERENCE_NAME", f"{kind}名称不能为空")
        name = value.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise ReferenceLibraryError("INVALID_REFERENCE_NAME", f"{kind}名称无效")
        return name

    @classmethod
    def normalize_logical_path(cls, raw: str) -> str:
        if not isinstance(raw, str) or not raw or raw != raw.strip() or raw.startswith(("/", "\\")):
            raise ReferenceLibraryError("INVALID_TEMPLATE_PATH", "模板路径无效")
        decoded = raw
        for _ in range(4):
            candidate = unquote(decoded)
            if candidate == decoded:
                break
            decoded = candidate
        if decoded != raw or "\\" in decoded:
            raise ReferenceLibraryError("INVALID_TEMPLATE_PATH", "模板路径无效")
        parts = decoded.split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise ReferenceLibraryError("INVALID_TEMPLATE_PATH", "模板路径无效")
        for part in parts:
            cls._name(part, kind="模板")
        return "/".join(parts)

    def _audit(self, *, operation: str, actor_user_id: int, request_id: str, object_type: str, object_id: str) -> None:
        try:
            with self.repository.engine.begin() as connection:
                self.audit_service.record(
                    connection, event_name="reference_library_operation", user_id=actor_user_id,
                    object_type=object_type, object_id=object_id, result="SUCCESS",
                    request_id=request_id, duration_ms=0, properties={"operation": operation},
                )
        except Exception as exc:
            raise ReferenceLibraryError("REFERENCE_LIBRARY_OPERATION_FAILED", "参考库操作失败", 500) from exc

    def list_standards(self, category: str | None = None) -> list[dict]:
        return self.repository.list_standards(category=category or None)

    def upload_standard(self, stream, original_name: str, name: str, category: str, actor_user_id: int, request_id: str) -> dict:
        service = self._require_file_service()
        name = self._name(name, kind="标准")
        category = self._name(category, kind="分类")
        if self.repository.has_active_standard_name(name):
            raise ReferenceLibraryError("DUPLICATE_STANDARD_NAME", "标准名称已存在", 409)
        doc_id = f"STD-{uuid.uuid4().hex.upper()}"
        extension = Path(original_name or "").suffix.lstrip(".").upper()
        try:
            uploaded = service.upload_new_object(
                stream, original_name=original_name, object_type="STANDARD", object_id=doc_id,
                create_metadata=lambda writer: self.repository.insert_standard(
                    writer, doc_id=doc_id, name=name, category=category,
                    file_type=extension, actor_user_id=actor_user_id,
                ), actor_user_id=actor_user_id, request_id=request_id,
            )
        except FileServiceError as exc:
            raise ReferenceLibraryError(exc.code, exc.message, exc.status_code) from exc
        return {"docId": doc_id, "name": name, "file": uploaded}

    def archive_standard(self, doc_id: str, *, actor_user_id: int, request_id: str) -> None:
        if not self.repository.archive_standard(doc_id):
            raise ReferenceLibraryError("STANDARD_NOT_FOUND", "标准不存在", 404)
        self._audit(operation="ARCHIVE", actor_user_id=actor_user_id, request_id=request_id, object_type="STANDARD", object_id=doc_id)

    def _active_standard(self, doc_id: str) -> dict:
        standard = self.repository.get_standard(doc_id)
        if not standard:
            raise ReferenceLibraryError("STANDARD_NOT_FOUND", "标准不存在", 404)
        return standard

    def _first_file(self, object_type: str, object_id: str) -> dict:
        files = self._require_file_service().list_for_object(object_type=object_type, object_id=object_id)
        if not files:
            raise ReferenceLibraryError("REFERENCE_FILE_NOT_FOUND", "文件不存在", 404)
        return files[0]

    def open_standard_download(self, doc_id: str) -> dict:
        self._active_standard(doc_id)
        file_row = self._first_file("STANDARD", doc_id)
        try:
            return self._require_file_service().open_version_stream(
                file_row["fileId"], file_row["versionNo"], object_type="STANDARD", object_id=doc_id,
            )
        except FileServiceError as exc:
            raise ReferenceLibraryError(exc.code, exc.message, exc.status_code) from exc

    def add_standard_version(self, doc_id: str, file_id: str, stream, original_name: str, expected_version: int, *, actor_user_id: int, request_id: str) -> dict:
        if not self.repository.get_standard(doc_id, active_only=False):
            raise ReferenceLibraryError("STANDARD_NOT_FOUND", "标准不存在", 404)
        if not self.repository.get_standard(doc_id):
            raise ReferenceLibraryError("STANDARD_NOT_WRITABLE", "已归档标准不允许修改附件", 409)
        try:
            return self._require_file_service().add_version(
                file_id, stream, original_name=original_name, object_type="STANDARD", object_id=doc_id,
                expected_version=expected_version, actor_user_id=actor_user_id, request_id=request_id,
            )
        except FileServiceError as exc:
            code = "STANDARD_NOT_WRITABLE" if exc.code == "OBJECT_READ_ONLY" else exc.code
            raise ReferenceLibraryError(code, exc.message, exc.status_code) from exc

    def _folder_paths(self) -> tuple[dict[str, dict], dict[str, str]]:
        folders = {str(row["id"]): row for row in self.repository.active_folders()}
        paths: dict[str, str] = {}

        def path_for(folder_id: str, seen: set[str]) -> str | None:
            if folder_id in paths:
                return paths[folder_id]
            folder = folders.get(folder_id)
            if folder is None or folder_id in seen:
                return None
            parent_id = folder.get("parent_id")
            if parent_id is None:
                result = str(folder["name"])
            else:
                parent = path_for(str(parent_id), seen | {folder_id})
                if parent is None:
                    return None
                result = f"{parent}/{folder['name']}"
            paths[folder_id] = result
            return result

        for folder_id in folders:
            path_for(folder_id, set())
        return folders, paths

    def _folder_by_path(self, folder_path: str) -> dict:
        normalized = self.normalize_logical_path(folder_path)
        folders, paths = self._folder_paths()
        matches = [folders[folder_id] for folder_id, path in paths.items() if path.casefold() == normalized.casefold()]
        if len(matches) != 1:
            raise ReferenceLibraryError("TEMPLATE_FOLDER_NOT_FOUND", "模板文件夹不存在", 404)
        return matches[0]

    def build_template_tree(self, current_category: str | None = None) -> list[dict]:
        folders, paths = self._folder_paths()
        items = self.repository.active_items()
        nodes = {
            folder_id: {"name": row["name"], "path": paths[folder_id], "level": paths[folder_id].count("/"), "fileCount": 0, "files": [], "children": []}
            for folder_id, row in folders.items() if folder_id in paths
        }
        for folder_id, row in folders.items():
            if folder_id not in nodes:
                continue
            parent_id = row.get("parent_id")
            if parent_id is not None and str(parent_id) in nodes:
                nodes[str(parent_id)]["children"].append(nodes[folder_id])
        for item in items:
            folder_id = item.get("folder_id")
            if folder_id is None or str(folder_id) not in nodes:
                continue
            node = nodes[str(folder_id)]
            file_row = self._first_file("TEMPLATE", str(item["template_id"]))
            node["files"].append({
                "name": item["display_name"], "path": f"{node['path']}/{item['display_name']}",
                "size": int(file_row.get("sizeBytes", 0)), "templateId": str(item["template_id"]),
            })
            node["fileCount"] += 1
        roots = [nodes[folder_id] for folder_id, row in folders.items() if folder_id in nodes and row.get("parent_id") is None]
        if current_category:
            normalized = self.normalize_logical_path(current_category)
            return [node for node in nodes.values() if node["path"].casefold() == normalized.casefold()]
        order = {name: index for index, name in enumerate(RETAINED_TEMPLATE_CATEGORIES)}
        return sorted(roots, key=lambda node: (order.get(node["name"], len(order)), node["name"]))

    def create_folder(self, name: str, parent_path: str | None, *, actor_user_id: int, request_id: str) -> dict:
        name = self._name(name, kind="文件夹")
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ReferenceLibraryError("INVALID_REFERENCE_NAME", "文件夹名称无效")
        parent_id = None
        if parent_path:
            parent_id = str(self._folder_by_path(parent_path)["id"])
        try:
            folder_id = self.repository.new_id()
            self.repository.create_folder(folder_id=folder_id, parent_id=parent_id, name=name, actor_user_id=actor_user_id)
        except sa.exc.IntegrityError as exc:
            raise ReferenceLibraryError("DUPLICATE_TEMPLATE_NAME", "文件夹名称已存在", 409) from exc
        self._audit(operation="CREATE_FOLDER", actor_user_id=actor_user_id, request_id=request_id, object_type="TEMPLATE_FOLDER", object_id=folder_id)
        return {"folderId": folder_id, "name": name}

    def archive_folder(self, folder_path: str, *, actor_user_id: int, request_id: str) -> None:
        folder = self._folder_by_path(folder_path)
        if not self.repository.archive_folder(str(folder["id"]), actor_user_id=actor_user_id):
            raise ReferenceLibraryError("TEMPLATE_FOLDER_NOT_FOUND", "模板文件夹不存在", 404)
        self._audit(operation="ARCHIVE_FOLDER", actor_user_id=actor_user_id, request_id=request_id, object_type="TEMPLATE_FOLDER", object_id=str(folder["id"]))

    def _has_active_item_name(self, folder_id: str, display_name: str) -> bool:
        return any(
            str(row.get("folder_id")) == folder_id and str(row["display_name"]).casefold() == display_name.casefold()
            for row in self.repository.active_items()
        )

    def upload_template(self, stream, original_name: str, folder_path: str, display_name: str, *, actor_user_id: int, request_id: str) -> dict:
        service = self._require_file_service()
        folder = self._folder_by_path(folder_path or "其他模板")
        display_name = self._name(display_name, kind="模板")
        if "/" in display_name or "\\" in display_name or display_name in {".", ".."}:
            raise ReferenceLibraryError("INVALID_REFERENCE_NAME", "模板名称无效")
        folder_id = str(folder["id"])
        if self._has_active_item_name(folder_id, display_name):
            raise ReferenceLibraryError("DUPLICATE_TEMPLATE_NAME", "模板名称已存在", 409)
        template_id = f"TPL-{uuid.uuid4().hex.upper()}"
        try:
            uploaded = service.upload_new_object(
                stream, original_name=original_name, object_type="TEMPLATE", object_id=template_id,
                create_metadata=lambda writer: self.repository.insert_template(
                    writer, template_id=template_id, item_id=self.repository.new_id(), folder_id=folder_id,
                    display_name=display_name, actor_user_id=actor_user_id,
                ), actor_user_id=actor_user_id, request_id=request_id,
            )
        except FileServiceError as exc:
            raise ReferenceLibraryError(exc.code, exc.message, exc.status_code) from exc
        return {"templateId": template_id, "displayName": display_name, "file": uploaded}

    def resolve_template_path(self, filepath: str) -> dict:
        normalized = self.normalize_logical_path(filepath)
        _folders, paths = self._folder_paths()
        matches = []
        for item in self.repository.active_items():
            folder_id = item.get("folder_id")
            if folder_id is None or str(folder_id) not in paths:
                continue
            logical = f"{paths[str(folder_id)]}/{item['display_name']}"
            if logical.casefold() == normalized.casefold():
                matches.append(item)
        if len(matches) == 0:
            raise ReferenceLibraryError("TEMPLATE_NOT_FOUND", "模板文件不存在", 404)
        if len(matches) > 1:
            raise ReferenceLibraryError("AMBIGUOUS_TEMPLATE_PATH", "模板路径不唯一", 400)
        item = matches[0]
        return {"templateId": str(item["template_id"]), "displayName": item["display_name"]}

    def open_template_download(self, filepath: str) -> dict:
        item = self.resolve_template_path(filepath)
        file_row = self._first_file("TEMPLATE", item["templateId"])
        try:
            return self._require_file_service().open_version_stream(
                file_row["fileId"], file_row["versionNo"], object_type="TEMPLATE", object_id=item["templateId"],
            )
        except FileServiceError as exc:
            raise ReferenceLibraryError(exc.code, exc.message, exc.status_code) from exc

    def rename_template(self, filepath: str, new_name: str, *, actor_user_id: int, request_id: str) -> None:
        item = self.resolve_template_path(filepath)
        name = self._name(new_name, kind="模板")
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ReferenceLibraryError("INVALID_REFERENCE_NAME", "模板名称无效")
        try:
            if not self.repository.update_template_name(item["templateId"], display_name=name, actor_user_id=actor_user_id):
                raise ReferenceLibraryError("TEMPLATE_NOT_FOUND", "模板文件不存在", 404)
        except sa.exc.IntegrityError as exc:
            raise ReferenceLibraryError("DUPLICATE_TEMPLATE_NAME", "模板名称已存在", 409) from exc
        self._audit(operation="RENAME", actor_user_id=actor_user_id, request_id=request_id, object_type="TEMPLATE", object_id=item["templateId"])

    def archive_template(self, filepath: str, *, actor_user_id: int, request_id: str) -> None:
        item = self.resolve_template_path(filepath)
        if not self.repository.archive_template(item["templateId"], actor_user_id=actor_user_id):
            raise ReferenceLibraryError("TEMPLATE_NOT_FOUND", "模板文件不存在", 404)
        self._audit(operation="ARCHIVE", actor_user_id=actor_user_id, request_id=request_id, object_type="TEMPLATE", object_id=item["templateId"])
