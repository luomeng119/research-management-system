from __future__ import annotations

import math

from flask import Blueprint, current_app, jsonify, request

from app.security.auth import business_required
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
