from __future__ import annotations

import os
import re

from flask import current_app, jsonify, redirect, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from app.security.auth import BUSINESS_USER, current_identity
from app.services.files import FileServiceError, validate_file_name
from app.services.projects import ProjectServiceError
from app.routes._shared import RESEARCH_FOLDER_TYPES


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def merge_project_files(tree, project_id):
    """Project controlled logical paths onto the retained physical folder tree."""
    service = current_app.extensions.get("file_service")
    if service is None:
        return tree
    for item in service.list_project_paths(project_id):
        parts = item["path"].split("/")[:-1]
        nodes = tree
        parent = None
        # Root files use the existing tree's folder-node shape as well.
        for level, part in enumerate(parts or [""]):
            path = "/".join(parts[:level + 1])
            parent = next((node for node in nodes if node["path"] == path), None)
            if parent is None:
                parent = {"name": part or "项目根目录", "path": path, "level": level,
                          "fileCount": 0, "files": [], "children": []}
                nodes.append(parent)
            nodes = parent["children"]
        parent["files"] = [entry for entry in parent["files"] if entry["path"] != item["path"]]
        parent["files"].append(item)
        parent["fileCount"] = len(parent["files"])
    return tree


def controlled_project_download(project_id, category, filepath):
    service = current_app.extensions.get("file_service")
    if service is None:
        return None
    identity = current_identity()
    if identity is None or identity.role != BUSINESS_USER:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        item = next((item for item in service.list_project_paths(project_id) if item["path"] == filepath), None)
        if item is not None:
            return redirect(url_for("files.download_file", file_id=item["fileId"],
                                    version_no=item["versionNo"], objectType="PROJECT", objectId=project_id))
        return None
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def rename_controlled_project_file(project_id, category):
    service = current_app.extensions.get("file_service")
    if service is None:
        return None
    identity = current_identity()
    if identity is None or identity.role != BUSINESS_USER:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        filepath = request.form.get("file_path", "")
        requested_name = request.form.get("new_name", "").strip()
        # Compare canonical logical paths before falling back to physical files.
        # Renaming is a leaf-name operation, never a move between directories.
        if (filepath != filepath.strip() or "\\" in filepath
                or any(part in {"", ".", ".."} for part in filepath.split("/"))
                or requested_name in {"", ".", ".."}
                or "/" in requested_name or "\\" in requested_name):
            raise ValueError("noncanonical rename path")
        paths = service.list_project_paths(project_id)
        folder, _, _old_name = filepath.rpartition("/")
        requested_target = (folder + "/" if folder else "") + requested_name
        if requested_target != filepath and any(item["path"] == requested_target for item in paths):
            return jsonify(success=False, message="文件名已存在"), 409
        # Unknown legacy paths remain with the existing adapter until adopted.
        if not any(item["path"] == filepath for item in paths):
            if request.form.get("expectedFileId"):
                return jsonify(success=False, message="文件已变化，请刷新后重试"), 409
            return None
        expected_file_id = request.form.get("expectedFileId", "").strip()
        if not expected_file_id:
            return jsonify(success=False, message="请刷新页面后重试"), 409
        name, _extension = validate_file_name(request.form.get("new_name", ""))
        folder, _, _old_name = filepath.rpartition("/")
        target = (folder + "/" if folder else "") + name
        if target != filepath and os.path.lexists(safe_project_path(current_app.config["UPLOAD_DIR"], project_id, target)):
            return jsonify(success=False, message="文件名已存在"), 409
        result = service.rename_project_path(project_id, filepath, name, actor_user_id=identity.user_id,
                                             expected_file_id=expected_file_id,
                                             request_id=getattr(request, "request_id", "unknown"))
        if result is None:
            return jsonify(success=False, message="文件路径已变化"), 409
        return jsonify(success=True, message="重命名成功", **result)
    except ValueError:
        return jsonify(success=False, message="非法路径"), 400
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def upload_project_file(project_id, category):
    """Keep the legacy upload URL while using controlled file/version storage."""
    identity = current_identity()
    if identity is None:
        return jsonify(success=False, message="未登录"), 401
    if identity.role != BUSINESS_USER:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(
            category=category, business_id=project_id,
        )
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify(success=False, message="请选择文件"), 400
        result = current_app.extensions["file_service"].upload_project_path(
            uploaded.stream, original_name=uploaded.filename,
            folder=request.form.get("folder", ""), project_id=project_id,
            actor_user_id=identity.user_id, request_id=getattr(request, "request_id", "unknown"),
        )
        return jsonify(success=True, message="上传成功", fileId=result["fileId"], versionNo=result["versionNo"])
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def upload_project_folder(project_id, category):
    """Keep folder-upload names and use the same controlled writer as single files."""
    identity = current_identity()
    if identity is None:
        return jsonify(success=False, message="未登录"), 401
    if identity.role != BUSINESS_USER:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        uploads = request.files.getlist("files")
        if not uploads:
            return jsonify(success=False, message="请选择文件夹"), 400
        target = request.form.get("folder", "") or RESEARCH_FOLDER_TYPES[0]
        completed = []
        failures = []
        for uploaded in uploads:
            relative = uploaded.filename or ""
            try:
                if relative.startswith("/") or "\\" in relative:
                    raise FileServiceError("INVALID_PROJECT_PATH", "非法项目文件路径", 400)
                subfolder, _, name = relative.rpartition("/")
                result = current_app.extensions["file_service"].upload_project_path(
                    uploaded.stream, original_name=name, folder=target + ("/" + subfolder if subfolder else ""),
                    project_id=project_id, actor_user_id=identity.user_id,
                    request_id=getattr(request, "request_id", "unknown"),
                )
                completed.append({"path": relative, "fileId": result["fileId"], "versionNo": result["versionNo"]})
            except FileServiceError as error:
                failures.append({"path": relative, "code": error.code,
                                 "message": error.message, "status": error.status_code})
            except (OSError, SQLAlchemyError):
                failures.append({"path": relative, "code": "FILE_OPERATION_FAILED",
                                 "message": "文件操作失败", "status": 500})
        message = f"上传成功 {len(completed)} 个文件"
        if failures:
            message += f"，失败 {len(failures)} 个：" + "；".join(
                f"{item['path']}（{item['message']}）" for item in failures
            )
        status = 200 if not failures else (207 if completed else failures[0]["status"])
        return jsonify(success=not failures, message=message, uploadedCount=len(completed),
                       failedCount=len(failures), files=completed, errors=failures), status
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def safe_project_path(upload_root, project_id, *relative_parts):
    """Return a path contained by one safe legacy project directory."""
    if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ValueError("非法项目编号")
    upload_root = os.path.realpath(upload_root)
    project_root = os.path.realpath(os.path.join(upload_root, project_id))
    if os.path.commonpath((upload_root, project_root)) != upload_root:
        raise ValueError("非法项目路径")
    candidate = os.path.realpath(os.path.join(project_root, *relative_parts))
    if os.path.commonpath((project_root, candidate)) != project_root:
        raise ValueError("非法路径")
    return candidate


def safe_project_documents_path(upload_root, project_id, *relative_parts):
    return safe_project_path(
        os.path.join(upload_root, "projects"), project_id, *relative_parts
    )


def legacy_page(service, *, category, page, page_size, status, keyword):
    result = service.list_legacy(
        category=category,
        page=page,
        page_size=page_size,
        status=status,
        keyword=keyword,
    )
    groups = {}
    for project in result["items"]:
        groups.setdefault(project.get("status") or "未知", []).append(project)
    return result, groups


def all_legacy_projects(service, category):
    items = []
    page = 1
    while True:
        result = service.list_legacy(
            category=category, page=page, page_size=100, status=None, keyword=None
        )
        items.extend(result["items"])
        if page * result["pageSize"] >= result["total"]:
            return items
        page += 1
