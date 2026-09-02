from __future__ import annotations

import json

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from app.security.auth import business_required, current_identity
from app.services.assistant import AssistantServiceError


bp = Blueprint("assistant", __name__)
ASSISTANT_REQUEST_MAX_BYTES = 128 * 1024


def _service():
    service = current_app.extensions.get("assistant_service")
    if service is None:
        raise AssistantServiceError(
            "AI_UNAVAILABLE", "助手未配置，您仍可继续手工填写", 503
        )
    return service


def _request_id():
    return getattr(request, "request_id", "unknown")


def _payload():
    # Flask 3.1 enforces this while reading both Content-Length and streaming
    # (for example Transfer-Encoding: chunked) request bodies. The global
    # application limit remains larger for the product's file-upload routes.
    # Allow one sentinel byte so a lengthless stream can be distinguished from
    # a body that is exactly at the accepted boundary.
    request.max_content_length = ASSISTANT_REQUEST_MAX_BYTES + 1
    if (
        request.content_length is not None
        and request.content_length > ASSISTANT_REQUEST_MAX_BYTES
    ):
        raise AssistantServiceError("AI_INPUT_TOO_LARGE", "助手请求内容过大", 413)
    try:
        raw = request.get_data(cache=False)
    except RequestEntityTooLarge as error:
        raise AssistantServiceError(
            "AI_INPUT_TOO_LARGE", "助手请求内容过大", 413
        ) from error
    if len(raw) > ASSISTANT_REQUEST_MAX_BYTES:
        raise AssistantServiceError("AI_INPUT_TOO_LARGE", "助手请求内容过大", 413)
    if not request.is_json:
        raise AssistantServiceError("INVALID_JSON", "请提供有效 JSON 对象", 400)
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        value = None
    if not isinstance(value, dict):
        raise AssistantServiceError("INVALID_JSON", "请提供有效 JSON 对象", 400)
    return value


def _error(error):
    return jsonify({"error": {
        "code": error.code,
        "message": error.message,
        "requestId": getattr(request, "request_id", None),
    }}), error.status_code


@bp.post("/api/proposals/<business_id>/assistant-drafts")
@business_required
def generate(business_id):
    try:
        payload = _payload()
        identity = current_identity()
        result = _service().generate(
            business_id,
            source_text=payload.get("sourceText"),
            selected_file_ids=payload.get("selectedFileIds", []),
            proposal_version=payload.get("proposalVersion"),
            run_id=payload.get("runId"),
            actor_user_id=identity.user_id,
            request_id=_request_id(),
            remote_input_confirmed=payload.get("remoteInputConfirmed", False),
        )
        return jsonify(result), 201
    except AssistantServiceError as error:
        return _error(error)


@bp.errorhandler(Exception)
def unexpected_error(error):
    if isinstance(error, HTTPException):
        return jsonify({"error": {
            "code": f"HTTP_{error.code}", "message": "请求无法处理",
            "requestId": getattr(request, "request_id", None),
        }}), error.code
    current_app.logger.error(
        "assistant request failed",
        exc_info=error,
        extra={"request_id": getattr(request, "request_id", None)},
    )
    return jsonify({"error": {
        "code": "INTERNAL_ERROR", "message": "系统处理失败",
        "requestId": getattr(request, "request_id", None),
    }}), 500


@bp.post("/api/assistant-runs/<run_id>/cancel")
@business_required
def cancel(run_id):
    try:
        identity = current_identity()
        return jsonify(_service().cancel(
            run_id, actor_user_id=identity.user_id, request_id=_request_id()
        ))
    except AssistantServiceError as error:
        return _error(error)


@bp.post("/api/proposals/<business_id>/assistant-drafts/<draft_id>/apply")
@business_required
def apply(business_id, draft_id):
    try:
        payload = _payload()
        identity = current_identity()
        return jsonify(_service().apply(
            business_id,
            draft_id,
            fields=payload.get("fields"),
            proposal_version=payload.get("proposalVersion"),
            actor_user_id=identity.user_id,
            request_id=_request_id(),
        ))
    except AssistantServiceError as error:
        return _error(error)
