from __future__ import annotations

import os
import re

from flask import current_app, jsonify, redirect, request, url_for

from app.security.auth import BUSINESS_USER, current_identity
from app.services.files import FileServiceError
from app.services.projects import ProjectServiceError


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
