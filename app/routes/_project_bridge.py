from __future__ import annotations

import os
import re


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


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
