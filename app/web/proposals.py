from __future__ import annotations

import math
import uuid

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from app.security.auth import business_required, current_identity
from app.services.files import FileServiceError
from app.services.proposals import ProposalServiceError, SOURCE_TYPES, STATUSES
from app.services.projects import ProjectServiceError
from app.web.files import business_user_required


bp = Blueprint("proposals", __name__)


def _service():
    return current_app.extensions["proposal_service"]


def _request_id() -> str:
    return getattr(request, "request_id", "unknown")


def _error(error: ProposalServiceError):
    payload = {
        "code": error.code,
        "message": error.message,
        "requestId": getattr(request, "request_id", None),
    }
    if error.fields:
        payload["details"] = {
            field: value if isinstance(value, list) else [value]
            for field, value in error.fields.items()
        }
    return jsonify({"error": payload}), error.status_code


def _json() -> dict:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ProposalServiceError("INVALID_JSON", "请提供有效 JSON 对象", 400)
    return value


def _list_response(result: dict):
    total = int(result["total"])
    page_size = int(result["pageSize"])
    return {
        "data": result["items"],
        "pagination": {
            "page": int(result["page"]),
            "pageSize": page_size,
            "totalItems": total,
            "totalPages": math.ceil(total / page_size) if total else 0,
        },
    }


def _files(business_id: str) -> list[dict]:
    service = current_app.extensions.get("file_service")
    if service is None:
        return []
    return service.list_for_object(object_type="PROPOSAL", object_id=business_id)


def _detail_context(business_id: str, **extra):
    proposal = _service().get(business_id)
    argumentation_page = extra.pop(
        "argumentation_page", request.args.get("argumentationPage", 1)
    )
    decision_page = extra.pop(
        "decision_page", request.args.get("decisionPage", 1)
    )
    argumentations = _service().list_argumentations(
        business_id, page=argumentation_page, page_size=10
    )
    decisions = _service().list_decisions(
        business_id, page=decision_page, page_size=10
    )
    linked_project = None
    project_service = current_app.extensions.get("project_service")
    if proposal["status"] == "ESTABLISHED" and project_service is not None:
        repository = project_service.repository
        with repository.engine.connect() as connection:
            registry, project = repository.get_project_ref_by_proposal(
                connection, proposal["id"]
            )
        if registry is not None and project is not None:
            linked_project = {
                "id": str(registry["id"]),
                "businessId": registry["business_id"],
                "name": project["name"],
            }
    return {
        "proposal": proposal,
        "linked_project": linked_project,
        "argumentations": argumentations["items"],
        "argumentation_pagination": argumentations,
        "decisions": decisions["items"],
        "decision_pagination": decisions,
        "files": _files(business_id),
        "idempotency_key": extra.pop("idempotency_key", uuid.uuid4().hex),
        "project_establishment_available": "project_service" in current_app.extensions,
        **extra,
    }


@bp.get("/proposals")
@business_required
def list_page():
    try:
        result = _service().list(
            page=request.args.get("page", 1),
            page_size=request.args.get("pageSize", 20),
            status=request.args.get("status") or None,
            source_type=request.args.get("sourceType") or None,
            keyword=request.args.get("keyword") or None,
            updated_after=request.args.get("updatedAfter") or None,
        )
        return render_template(
            "proposals/list.html", result=result, statuses=sorted(STATUSES)
        )
    except ProposalServiceError as error:
        return render_template(
            "proposals/list.html", result={"items": [], "total": 0},
            statuses=sorted(STATUSES), page_error=error.message,
        ), error.status_code


@bp.route("/proposals/new", methods=["GET", "POST"])
@business_required
def new_page():
    form = request.form.to_dict() if request.method == "POST" else {}
    errors = {}
    if request.method == "POST":
        identity = current_identity()
        try:
            created = _service().create(
                form, actor_user_id=identity.user_id, request_id=_request_id()
            )
            return redirect(url_for("proposals.detail_page", business_id=created["businessId"]))
        except ProposalServiceError as error:
            errors = error.fields
            return render_template(
                "proposals/form.html", form=form, errors=errors,
                source_types=sorted(SOURCE_TYPES), proposal=None,
            ), error.status_code
    return render_template(
        "proposals/form.html", form=form, errors=errors,
        source_types=sorted(SOURCE_TYPES), proposal=None,
    )


@bp.get("/proposals/<business_id>")
@business_required
def detail_page(business_id: str):
    try:
        return render_template("proposals/detail.html", **_detail_context(business_id))
    except ProposalServiceError as error:
        return render_template("proposals/not_found.html", message=error.message), error.status_code


@bp.route("/proposals/<business_id>/edit", methods=["GET", "POST"])
@business_required
def edit_page(business_id: str):
    try:
        proposal = _service().get(business_id)
        if request.method == "POST":
            form = request.form.to_dict()
            identity = current_identity()
            body = {key: value for key, value in form.items() if key in {
                "title", "sourceType", "sourceSummary", "researchProblem",
                "objectives", "researchContent", "expectedOutcomes",
            }}
            try:
                updated = _service().update(
                    business_id, body, expected_version=form.get("version"),
                    actor_user_id=identity.user_id, request_id=_request_id(),
                )
                return redirect(url_for("proposals.detail_page", business_id=updated["businessId"]))
            except ProposalServiceError as error:
                return render_template(
                    "proposals/form.html", form=form, errors=error.fields,
                    source_types=sorted(SOURCE_TYPES), proposal=proposal,
                    files=_files(business_id),
                ), error.status_code
        form = {key: proposal.get(key, "") for key in (
            "title", "sourceType", "sourceSummary", "researchProblem",
            "objectives", "researchContent", "expectedOutcomes",
        )}
        return render_template(
            "proposals/form.html", form=form, errors={},
            source_types=sorted(SOURCE_TYPES), proposal=proposal,
            files=_files(business_id),
        )
    except ProposalServiceError as error:
        return render_template("proposals/not_found.html", message=error.message), error.status_code


def _detail_error(business_id: str, error, **forms):
    if isinstance(error, ProposalServiceError):
        status = error.status_code
        message = error.message
    else:
        status = error.status_code
        message = error.message
    return render_template(
        "proposals/detail.html",
        **_detail_context(business_id, action_error=message, **forms),
    ), status


@bp.post("/proposals/<business_id>/argumentations")
@business_required
def add_argumentation_page(business_id: str):
    form = request.form.to_dict()
    try:
        identity = current_identity()
        _service().add_argumentation(
            business_id,
            {"summary": form.get("summary"), "argumentationDate": form.get("argumentationDate")},
            conclusion=form.get("conclusion", ""), basis=form.get("basis", ""),
            expected_version=form.get("version"), actor_user_id=identity.user_id,
            request_id=_request_id(),
        )
        return redirect(url_for("proposals.detail_page", business_id=business_id))
    except ProposalServiceError as error:
        return _detail_error(business_id, error, argumentation_form=form)


@bp.post("/proposals/<business_id>/decisions")
@business_required
def decide_page(business_id: str):
    form = request.form.to_dict()
    try:
        identity = current_identity()
        payload = {
            "decision": form.get("decision"),
            "decisionDate": form.get("decisionDate"),
            "conclusion": form.get("conclusion"),
            "basis": form.get("basis"),
        }
        if payload["decision"] == "ESTABLISH":
            payload["project"] = {
                "category": form.get("projectCategory"),
                "name": form.get("projectName"),
                "leader": form.get("projectLeader"),
                "plannedEndDate": form.get("plannedEndDate"),
            }
            project_service = current_app.extensions.get("project_service")
            if project_service is None:
                raise ProjectServiceError("SERVICE_UNAVAILABLE", "项目服务未配置", 503)
            project_service.establish_from_proposal(
                business_id, payload,
                idempotency_key=form.get("idempotencyKey", ""),
                expected_version=form.get("version"), actor_user_id=identity.user_id,
                request_id=_request_id(),
            )
        else:
            _service().decide(
                business_id, payload,
                idempotency_key=form.get("idempotencyKey", ""),
                expected_version=form.get("version"), actor_user_id=identity.user_id,
                request_id=_request_id(),
            )
        return redirect(url_for("proposals.detail_page", business_id=business_id))
    except (ProposalServiceError, ProjectServiceError) as error:
        return _detail_error(
            business_id, error, decision_form=form,
            idempotency_key=form.get("idempotencyKey") or uuid.uuid4().hex,
        )
    except Exception as error:
        if isinstance(error, HTTPException):
            raise
        current_app.logger.error(
            "proposal decision request failed", exc_info=error,
            extra={"request_id": _request_id()},
        )
        return _detail_error(
            business_id,
            ProjectServiceError(
                "PROCESSING_FAILED", "系统处理失败，已保留填写内容，请核实当前记录后再处理。", 500
            ),
            decision_form=form,
            idempotency_key=form.get("idempotencyKey") or uuid.uuid4().hex,
        )


@bp.post("/proposals/<business_id>/reopen")
@business_required
def reopen_page(business_id: str):
    try:
        identity = current_identity()
        _service().reopen(
            business_id, expected_version=request.form.get("version"),
            actor_user_id=identity.user_id, request_id=_request_id(),
        )
        return redirect(url_for("proposals.detail_page", business_id=business_id))
    except ProposalServiceError as error:
        return _detail_error(business_id, error)


@bp.post("/proposals/<business_id>/return-to-draft")
@business_required
def return_to_draft_page(business_id: str):
    try:
        identity = current_identity()
        _service().return_to_draft(
            business_id, expected_version=request.form.get("version"),
            actor_user_id=identity.user_id, request_id=_request_id(),
        )
        return redirect(url_for("proposals.detail_page", business_id=business_id))
    except ProposalServiceError as error:
        return _detail_error(business_id, error)


@bp.post("/proposals/<business_id>/attachments")
@business_required
def upload_attachment_page(business_id: str):
    try:
        proposal = _service().get(business_id)
        if proposal["status"] in {"ESTABLISHED", "REJECTED"}:
            raise ProposalServiceError(
                "STATE_CONFLICT", "终态提案不允许新增附件", 409
            )
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise FileServiceError("FILE_REQUIRED", "请选择文件")
        identity = current_identity()
        current_app.extensions["file_service"].upload(
            uploaded.stream, original_name=uploaded.filename,
            object_type="PROPOSAL", object_id=business_id,
            actor_user_id=identity.user_id, request_id=_request_id(),
        )
        return redirect(url_for("proposals.detail_page", business_id=business_id))
    except (FileServiceError, ProposalServiceError) as error:
        return _detail_error(business_id, error)


@bp.post("/proposals/<business_id>/attachments/<file_id>/versions")
@business_user_required
def upload_attachment_version_page(business_id: str, file_id: str):
    try:
        _service().get(business_id)
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise FileServiceError("FILE_REQUIRED", "请选择文件")
        try:
            expected_version = int(request.form.get("expectedVersion", ""))
        except ValueError:
            raise FileServiceError("EXPECTED_VERSION_REQUIRED", "必须提供当前版本号")
        current_app.extensions["file_service"].add_version(
            file_id, uploaded.stream, original_name=uploaded.filename,
            object_type="PROPOSAL", object_id=business_id, expected_version=expected_version,
            actor_user_id=current_identity().user_id, request_id=_request_id(),
        )
        return redirect(url_for("proposals.detail_page", business_id=business_id))
    except (FileServiceError, ProposalServiceError) as error:
        if error.status_code == 404:
            return render_template("proposals/not_found.html", message="提案或附件不存在"), 404
        return _detail_error(business_id, error)


@bp.get("/api/proposals")
@business_required
def list_api():
    try:
        return jsonify(_list_response(_service().list(
            page=request.args.get("page", 1),
            page_size=request.args.get("pageSize", 20),
            status=request.args.get("status") or None,
            source_type=request.args.get("sourceType") or None,
            keyword=request.args.get("keyword") or None,
            updated_after=request.args.get("updatedAfter") or None,
        )))
    except ProposalServiceError as error:
        return _error(error)


@bp.post("/api/proposals")
@business_required
def create_api():
    try:
        identity = current_identity()
        created = _service().create(
            _json(), actor_user_id=identity.user_id, request_id=_request_id()
        )
        return jsonify(created), 201
    except ProposalServiceError as error:
        return _error(error)


@bp.get("/api/proposals/<business_id>")
@business_required
def detail_api(business_id: str):
    try:
        proposal = _service().get(business_id)
        return jsonify({**proposal, "files": _files(business_id)})
    except ProposalServiceError as error:
        return _error(error)


@bp.patch("/api/proposals/<business_id>")
@business_required
def update_api(business_id: str):
    try:
        payload = _json()
        identity = current_identity()
        body = {key: value for key, value in payload.items() if key != "version"}
        result = _service().update(
            business_id, body, expected_version=payload.get("version"),
            actor_user_id=identity.user_id, request_id=_request_id(),
        )
        return jsonify(result)
    except ProposalServiceError as error:
        return _error(error)


@bp.get("/api/proposals/<business_id>/argumentations")
@business_required
def argumentations_api(business_id: str):
    try:
        return jsonify(_list_response(_service().list_argumentations(
            business_id,
            page=request.args.get("page", 1),
            page_size=request.args.get("pageSize", 20),
        )))
    except ProposalServiceError as error:
        return _error(error)


@bp.post("/api/proposals/<business_id>/argumentations")
@business_required
def add_argumentation_api(business_id: str):
    try:
        payload = _json()
        identity = current_identity()
        facts = payload.get("facts") or {
            "summary": payload.get("summary"),
            "argumentationDate": payload.get("argumentationDate"),
        }
        result = _service().add_argumentation(
            business_id, facts, conclusion=payload.get("conclusion", ""),
            basis=payload.get("basis", ""), expected_version=payload.get("version"),
            actor_user_id=identity.user_id, request_id=_request_id(),
        )
        return jsonify(result), 201
    except ProposalServiceError as error:
        return _error(error)


@bp.post("/api/proposals/<business_id>/decisions")
@business_required
def decide_api(business_id: str):
    try:
        payload = _json()
        identity = current_identity()
        if payload.get("decision") == "ESTABLISH":
            service = current_app.extensions.get("project_service")
            if service is None:
                raise ProjectServiceError("SERVICE_UNAVAILABLE", "项目服务未配置", 503)
            result = service.establish_from_proposal(
                business_id, payload,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                expected_version=payload.get("version"), actor_user_id=identity.user_id,
                request_id=_request_id(),
            )
        else:
            result = _service().decide(
                business_id, payload,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                expected_version=payload.get("version"), actor_user_id=identity.user_id,
                request_id=_request_id(),
            )
        result.pop("_reused", None)
        return jsonify(result), 201
    except (ProposalServiceError, ProjectServiceError) as error:
        return _error(error)


@bp.get("/api/proposals/<business_id>/decisions")
@business_required
def decisions_api(business_id: str):
    try:
        return jsonify(_list_response(_service().list_decisions(
            business_id,
            page=request.args.get("page", 1),
            page_size=request.args.get("pageSize", 20),
        )))
    except ProposalServiceError as error:
        return _error(error)


@bp.post("/api/proposals/<business_id>/reopen")
@business_required
def reopen_api(business_id: str):
    try:
        payload = _json()
        identity = current_identity()
        return jsonify(_service().reopen(
            business_id, expected_version=payload.get("version"),
            actor_user_id=identity.user_id, request_id=_request_id(),
        ))
    except ProposalServiceError as error:
        return _error(error)


@bp.post("/api/proposals/<business_id>/return-to-draft")
@business_required
def return_to_draft_api(business_id: str):
    try:
        payload = _json()
        identity = current_identity()
        return jsonify(_service().return_to_draft(
            business_id, expected_version=payload.get("version"),
            actor_user_id=identity.user_id, request_id=_request_id(),
        ))
    except ProposalServiceError as error:
        return _error(error)


@bp.errorhandler(Exception)
def unexpected_error(error):
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            code = (
                "PAYLOAD_TOO_LARGE"
                if isinstance(error, RequestEntityTooLarge)
                else f"HTTP_{error.code}"
            )
            message = (
                "请求内容过大"
                if isinstance(error, RequestEntityTooLarge)
                else "请求无法处理"
            )
            return jsonify({"error": {
                "code": code, "message": message,
                "requestId": getattr(request, "request_id", None),
            }}), error.code
        return error
    current_app.logger.error(
        "proposal request failed",
        exc_info=error,
        extra={"request_id": getattr(request, "request_id", None)},
    )
    if request.path.startswith("/api/"):
        return jsonify({"error": {
            "code": "INTERNAL_ERROR", "message": "系统处理失败",
            "requestId": getattr(request, "request_id", None),
        }}), 500
    return render_template("proposals/not_found.html", message="系统处理失败"), 500
