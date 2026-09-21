# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, send_file, session, url_for

from app.services.reference_library import ReferenceLibraryError


bp = Blueprint("standards", __name__, url_prefix="/standards")


def _service():
    service = current_app.extensions.get("reference_library_service")
    if service is None:
        raise ReferenceLibraryError("REFERENCE_LIBRARY_UNAVAILABLE", "标准库服务未就绪", 503)
    return service


def _actor() -> int:
    try:
        return int(session.get("user_id", 0))
    except (TypeError, ValueError):
        return 0


def _error(error: ReferenceLibraryError):
    return jsonify({"success": False, "message": error.message, "code": error.code}), error.status_code


@bp.errorhandler(Exception)
def _unexpected_error(_error_value):
    return _error(ReferenceLibraryError("REFERENCE_LIBRARY_OPERATION_FAILED", "标准库操作失败", 500))


@bp.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("auth.login"))
    try:
        category_filter = request.args.get("category", "").strip()
        standards = _service().list_standards(category_filter)
        users = current_app.extensions.get("users_repository")
        uploader_names = {}
        accounts = users.list_accounts() if users is not None else []
        for account in accounts:
            name = account.get("name") or account["username"]
            uploader_names[account["username"]] = name
        # New uploads persist an ID; it takes precedence over legacy usernames.
        for account in accounts:
            uploader_names[str(account["id"])] = account.get("name") or account["username"]
        category_groups = {}
        for standard in standards:
            category_groups.setdefault(standard.get("category") or "未分类", []).append(standard)
        return render_template("standards/index.html", standards=standards, category_groups=category_groups, category_filter=category_filter, uploader_names=uploader_names)
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/upload", methods=["POST"])
def upload():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"success": False, "message": "请选择文件"}), 400
    try:
        _service().upload_standard(uploaded.stream, uploaded.filename, request.form.get("name", ""), request.form.get("category", ""), _actor(), getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "上传成功"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/download/<doc_id>")
def download(doc_id):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    try:
        opened = _service().open_standard_download(doc_id)
        response = send_file(opened["stream"], mimetype=opened["mediaType"], as_attachment=True, download_name=opened["originalName"], conditional=True, etag=opened["sha256"])
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/delete/<doc_id>", methods=["POST"])
def delete(doc_id):
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        _service().archive_standard(doc_id, actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "已归档"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/logs/<module_name>")
def logs(module_name):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    module_names = {"equipment": "设备知识库", "standards": "标准法规库", "templates": "科研模板"}
    try:
        logs = _service().list_logs(module_name, operator=request.args.get("operator"), file_name=request.args.get("file_name"), start_date=request.args.get("start_date"), end_date=request.args.get("end_date"), operation=request.args.get("operation_type"))
        return render_template("standards/logs.html", logs=logs, standards=_service().list_standards() if module_name == "standards" else [], category_groups={}, module_name=module_name, module_title=module_names.get(module_name, "日志"))
    except ReferenceLibraryError as error:
        return _error(error)
