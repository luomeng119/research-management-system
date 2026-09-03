# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
import re

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, send_file, session, url_for

from app.services.reference_library import ReferenceLibraryError


bp = Blueprint("templates", __name__, url_prefix="/templates")


def _service():
    service = current_app.extensions.get("reference_library_service")
    if service is None:
        raise ReferenceLibraryError("REFERENCE_LIBRARY_UNAVAILABLE", "模板库服务未就绪", 503)
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
    return _error(ReferenceLibraryError("REFERENCE_LIBRARY_OPERATION_FAILED", "模板库操作失败", 500))


def _raw_path_is_safe() -> bool:
    raw_uri = request.environ.get("RAW_URI", request.full_path)
    return re.search(r"%(?:25)*(?:2f|5c)", raw_uri, flags=re.IGNORECASE) is None


def _dated_name(original_name: str) -> str:
    if "." in original_name:
        name, extension = original_name.rsplit(".", 1)
        return f"{name}{datetime.now().strftime('（%Y年%m月%d日版）')}.{extension}"
    return f"{original_name}{datetime.now().strftime('（%Y年%m月%d日版）')}"


@bp.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("auth.login"))
    try:
        current_category = request.args.get("cat", "").strip()
        return render_template("templates/index.html", folder_tree=_service().build_template_tree(current_category or None), current_cat=current_category)
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/upload", methods=["POST"])
def upload():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    files = [file for file in request.files.getlist("file") if file.filename]
    if not files:
        return jsonify({"success": False, "message": "请选择文件"}), 400
    try:
        folder = request.form.get("folder", "") or "其他模板"
        for uploaded in files:
            _service().upload_template(uploaded.stream, uploaded.filename, folder, _dated_name(uploaded.filename), actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "上传成功"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/upload_folder", methods=["POST"])
def upload_folder():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    files = [file for file in request.files.getlist("files") if file.filename]
    if not files:
        return jsonify({"success": False, "message": "请选择文件夹"}), 400
    try:
        base_folder = request.form.get("folder", "") or "其他模板"
        count = 0
        for uploaded in files:
            logical = _service().normalize_logical_path(uploaded.filename)
            segments = logical.split("/")
            target_folder = base_folder
            for segment in segments[:-1]:
                parent = target_folder
                target_folder = f"{target_folder}/{segment}"
                try:
                    _service().create_folder(segment, parent, actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
                except ReferenceLibraryError as error:
                    if error.code != "DUPLICATE_TEMPLATE_NAME":
                        raise
            _service().upload_template(uploaded.stream, segments[-1], target_folder, _dated_name(segments[-1]), actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
            count += 1
        return jsonify({"success": True, "message": f"上传成功 {count} 个文件"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/create_folder", methods=["POST"])
def create_folder():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        _service().create_folder(request.form.get("folder_name", ""), request.form.get("parent_folder", "") or None, actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "创建成功"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/download/<path:filepath>")
def download(filepath):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    try:
        if not _raw_path_is_safe():
            raise ReferenceLibraryError("INVALID_TEMPLATE_PATH", "模板路径无效")
        opened = _service().open_template_download(filepath)
        response = send_file(opened["stream"], mimetype=opened["mediaType"], as_attachment=True, download_name=opened["originalName"], conditional=True, etag=opened["sha256"])
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except ReferenceLibraryError as error:
        return _error(error)
    except Exception:
        return _error(ReferenceLibraryError("REFERENCE_LIBRARY_OPERATION_FAILED", "模板文件操作失败", 500))


@bp.route("/delete_file", methods=["POST"])
def delete_file():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        _service().archive_template(request.form.get("filepath", ""), actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "已归档"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/delete_folder", methods=["POST"])
def delete_folder():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        _service().archive_folder(request.form.get("folder", ""), actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "已归档"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/rename_folder", methods=["POST"])
def rename_folder():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        _service().rename_folder(request.form.get("folder", ""), request.form.get("new_name", ""), actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "重命名成功"})
    except ReferenceLibraryError as error:
        return _error(error)
    except Exception:
        return _error(ReferenceLibraryError("REFERENCE_LIBRARY_OPERATION_FAILED", "模板文件操作失败", 500))


@bp.route("/rename_file", methods=["POST"])
def rename_file():
    if "user" not in session:
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        _service().rename_template(request.form.get("filepath", ""), request.form.get("new_name", ""), actor_user_id=_actor(), request_id=getattr(request, "request_id", "unknown"))
        return jsonify({"success": True, "message": "重命名成功"})
    except ReferenceLibraryError as error:
        return _error(error)


@bp.route("/logs/<module_name>")
def logs(module_name):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    module_names = {"equipment": "设备知识库", "standards": "标准法规库", "templates": "科研模板"}
    try:
        logs = _service().list_logs(module_name, operator=request.args.get("operator"), file_name=request.args.get("file_name"), start_date=request.args.get("start_date"), end_date=request.args.get("end_date"), operation=request.args.get("operation_type"))
        return render_template("templates/logs.html", logs=logs, module_name=module_name, module_title=module_names.get(module_name, "日志"), show_templates_link=True)
    except ReferenceLibraryError as error:
        return _error(error)
