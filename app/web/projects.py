from __future__ import annotations

import math

from flask import Blueprint, current_app, jsonify, render_template, request

from app.security.auth import business_required, current_identity
from app.services.projects import ProjectServiceError


bp = Blueprint("project_registry", __name__)


def _service():
    service = current_app.extensions.get("project_service")
    if service is None:
        raise ProjectServiceError("SERVICE_UNAVAILABLE", "项目服务未配置", 503)
    return service


def _error(error: ProjectServiceError):
    payload = {
        "code": error.code,
        "message": error.message,
        "requestId": getattr(request, "request_id", None),
    }
    if error.fields:
        payload["details"] = {
            key: value if isinstance(value, list) else [value]
            for key, value in error.fields.items()
        }
    return jsonify({"error": payload}), error.status_code


@bp.get("/api/projects")
@business_required
def list_api():
    try:
        result = _service().list(
            page=request.args.get("page", 1),
            page_size=request.args.get("pageSize", 20),
            category=request.args.get("category") or None,
            status=request.args.get("status") or None,
        )
        total = result["total"]
        page_size = result["pageSize"]
        return jsonify({
            "data": result["items"],
            "pagination": {
                "page": result["page"],
                "pageSize": page_size,
                "totalItems": total,
                "totalPages": math.ceil(total / page_size) if total else 0,
            },
        })
    except ProjectServiceError as error:
        return _error(error)


@bp.get("/api/projects/<business_id>")
@business_required
def detail_api(business_id: str):
    try:
        return jsonify(_service().get(business_id))
    except ProjectServiceError as error:
        return _error(error)


def _actor_id() -> int:
    identity = current_identity()
    if identity is None:
        raise ProjectServiceError("AUTH_REQUIRED", "请先登录", 401)
    return identity.user_id


def _payload():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ProjectServiceError("VALIDATION_ERROR", "请求正文必须是 JSON 对象", 422)
    return value


def _progress_display_names(records):
    """Add a display name without replacing persisted actor IDs or dates."""
    users = current_app.extensions.get("users_repository")
    names = {str(account["id"]): account.get("name") or account.get("username")
             for account in users.list_accounts()} if users is not None else {}
    def display_name(user_id):
        return names.get(str(user_id)) or (
            f"未知用户（账号{user_id}）" if user_id is not None else "未记录"
        )

    return [{**item, "createdByName": display_name(item.get("createdBy")),
             "updatedByName": display_name(item.get("updatedBy"))}
            for item in records]


@bp.get("/projects/<project_id>/overview")
@business_required
def detail_page(project_id: str):
    try:
        detail = _service().detail(project_id)
        project = detail["project"]
        legacy = _service().get_legacy(category=project["category"], business_id=project["businessId"])
        source_proposal = None
        if project.get("sourceProposalId"):
            with _service().repository.engine.connect() as connection:
                source_proposal = _service().repository.get_proposal_by_id(connection, project["sourceProposalId"])
        detail = {**detail, "start_date": legacy.get("start_date"), "source_proposal": source_proposal}
        detail = {**detail, "progress": _progress_display_names(detail["progress"])}
        return render_template("projects/lifecycle_detail.html", **detail)
    except ProjectServiceError as error:
        return render_template(
            "projects/lifecycle_detail.html", page_error=error.message, project=None
        ), error.status_code


@bp.post("/api/projects/<project_id>/status-transitions")
@business_required
def transition_status_api(project_id: str):
    try:
        return jsonify(_service().transition_status(
            project_id, _payload(), actor_user_id=_actor_id(),
            request_id=request.request_id,
        ))
    except ProjectServiceError as error:
        return _error(error)


@bp.get("/api/projects/<project_id>/progress")
@business_required
def list_progress_api(project_id: str):
    try:
        result = _service().page_process_records(
            project_id, record_type="PROGRESS",
            page=request.args.get("page", 1), page_size=request.args.get("pageSize", 50),
        )
        return jsonify({
            "data": _progress_display_names(result["items"]),
            "pagination": {
                "page": result["page"], "pageSize": result["pageSize"],
                "totalItems": result["total"],
                "totalPages": math.ceil(result["total"] / result["pageSize"])
                if result["total"] else 0,
            },
        })
    except ProjectServiceError as error:
        return _error(error)


@bp.post("/api/projects/<project_id>/progress")
@business_required
def add_progress_api(project_id: str):
    try:
        result = _service().add_progress(
            project_id, _payload(), actor_user_id=_actor_id(),
            request_id=request.request_id,
        )
        return jsonify(result), 201
    except ProjectServiceError as error:
        return _error(error)


@bp.patch("/api/projects/<project_id>/progress/<progress_id>")
@business_required
def update_progress_api(project_id: str, progress_id: str):
    try:
        result = _service().update_progress(
            project_id, progress_id, _payload(), actor_user_id=_actor_id(),
            request_id=request.request_id,
        )
        return jsonify(result)
    except ProjectServiceError as error:
        return _error(error)


@bp.get("/api/projects/<project_id>/changes")
@business_required
def list_changes_api(project_id: str):
    try:
        result = _service().page_process_records(
            project_id, record_type="CHANGE",
            page=request.args.get("page", 1), page_size=request.args.get("pageSize", 50),
        )
        return jsonify({
            "data": result["items"],
            "pagination": {
                "page": result["page"], "pageSize": result["pageSize"],
                "totalItems": result["total"],
                "totalPages": math.ceil(result["total"] / result["pageSize"])
                if result["total"] else 0,
            },
        })
    except ProjectServiceError as error:
        return _error(error)


@bp.post("/api/projects/<project_id>/changes")
@business_required
def add_change_api(project_id: str):
    try:
        result = _service().add_change(
            project_id, _payload(), actor_user_id=_actor_id(),
            request_id=request.request_id,
        )
        return jsonify(result), 201
    except ProjectServiceError as error:
        return _error(error)


@bp.get("/api/projects/<project_id>/outputs")
@business_required
def list_outputs_api(project_id: str):
    try:
        result = _service().page_process_records(
            project_id, record_type="OUTPUT",
            page=request.args.get("page", 1), page_size=request.args.get("pageSize", 50),
        )
        return jsonify({
            "data": result["items"],
            "pagination": {
                "page": result["page"], "pageSize": result["pageSize"],
                "totalItems": result["total"],
                "totalPages": math.ceil(result["total"] / result["pageSize"])
                if result["total"] else 0,
            },
        })
    except ProjectServiceError as error:
        return _error(error)


@bp.post("/api/projects/<project_id>/outputs")
@business_required
def add_output_api(project_id: str):
    try:
        result = _service().add_output(
            project_id, _payload(), actor_user_id=_actor_id(),
            request_id=request.request_id,
        )
        return jsonify(result), 201
    except ProjectServiceError as error:
        return _error(error)


@bp.get("/api/projects/<project_id>/closure")
@business_required
def get_closure_api(project_id: str):
    try:
        return jsonify({"data": _service().get_closure(project_id)})
    except ProjectServiceError as error:
        return _error(error)


@bp.post("/api/projects/<project_id>/closure")
@business_required
def close_project_api(project_id: str):
    try:
        result = _service().close_project(
            project_id, _payload(), actor_user_id=_actor_id(),
            request_id=request.request_id,
        )
        return jsonify(result), 201
    except ProjectServiceError as error:
        return _error(error)


@bp.get("/api/projects/<project_id>/research-path")
@business_required
def research_path_api(project_id: str):
    try:
        result = _service().research_path(project_id)
        nodes = result["tree"]["children"][1]["children"]
        names = _progress_display_names([{"createdBy": node["data"].get("ownerId")} for node in nodes])
        for node, name in zip(nodes, names):
            node["data"]["ownerDisplay"] = name["createdByName"]
        return jsonify(result)
    except ProjectServiceError as error:
        return _error(error)
