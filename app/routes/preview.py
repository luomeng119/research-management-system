"""Compatibility adapter for the controlled V1 file preview contract.

Legacy filesystem paths are intentionally rejected. Callers must identify a
file already authorized through ``object_files``.
"""

import hashlib
import io
import mimetypes
import os
from pathlib import Path
import stat

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from app.web.files import business_user_required, render_file_preview
from app.routes._project_bridge import PROJECT_ID_PATTERN, is_deleted_project_path
from app.services.files import FileServiceError
from app.services.projects import ProjectServiceError


bp = Blueprint("preview", __name__, url_prefix="/preview")


def _legacy_snapshot(project_id, filepath):
    """Read a bounded snapshot via directory handles, without adopting old files."""
    root = Path(current_app.config["UPLOAD_DIR"]).absolute()
    parts = [project_id, *filepath.split("/")]
    descriptors = []
    try:
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise FileServiceError("PREVIEW_UNAVAILABLE", "当前环境请下载查看旧附件", 409)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, directory_flags)
        descriptors.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
        maximum = current_app.extensions["file_service"].preview_max_bytes
        if before.st_size > maximum:
            raise FileServiceError("PREVIEW_UNAVAILABLE", "文件过大，请下载查看", 409)
        with os.fdopen(os.dup(descriptor), "rb") as source:
            data = source.read(maximum + 1)
        after = os.fstat(descriptor)
        if (len(data) > maximum or len(data) != before.st_size
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (before.st_size, before.st_mtime_ns, before.st_ctime_ns)):
            raise FileServiceError("FILE_INTEGRITY_FAILED", "文件已变化，请刷新后重试", 409)
        return {"stream": io.BytesIO(data), "originalName": parts[-1],
                "sizeBytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "mediaType": mimetypes.guess_type(parts[-1])[0] or "application/octet-stream",
                "legacyProjectId": project_id}
    except OSError as exc:
        raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@bp.get("/project")
@business_user_required
def preview_project():
    project_id = request.args.get("projectId", "")
    category = request.args.get("category", "")
    filepath = request.args.get("filepath", "")
    prefixes = {"GENERAL_RESEARCH": "projects", "SECURITY_CONFIDENTIALITY": "security_projects",
                "CRYPTO_APPLICATION": "crypto_projects"}
    if (category not in prefixes or not PROJECT_ID_PATTERN.fullmatch(project_id)
            or not filepath or filepath != filepath.strip() or "\\" in filepath
            or any(ord(char) < 32 or ord(char) == 127 for char in filepath)
            or any(not part or part.startswith(".") for part in filepath.split("/"))):
        return jsonify(success=False, message="非法项目文件引用"), 400
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        service = current_app.extensions["file_service"]
        item = next((item for item in service.list_project_paths(project_id) if item["path"] == filepath), None)
        if item:
            return redirect(url_for("files.preview_file", file_id=item["fileId"], version_no=item["versionNo"],
                                    objectType="PROJECT", objectId=project_id))
        if is_deleted_project_path(filepath, service.list_deleted_project_paths(project_id)):
            return jsonify(success=False, message="文件不存在"), 404
        download_url = url_for(prefixes[category] + ".download_file", project_id=project_id, filepath=filepath)
        return render_file_preview(lambda: _legacy_snapshot(project_id, filepath),
                                   download_url=download_url, legacy_project_id=project_id)
    except (ProjectServiceError, FileServiceError) as error:
        return jsonify(success=False, message=error.message), error.status_code


def _controlled_reference():
    if "path" in request.args:
        return None
    file_id = request.args.get("fileId", "").strip()
    version_no = request.args.get("versionNo", "").strip()
    object_type = request.args.get("objectType", "").strip().upper()
    object_id = request.args.get("objectId", "").strip()
    if not all((file_id, version_no, object_type, object_id)):
        return None
    try:
        version_no = int(version_no)
    except ValueError:
        return None
    return file_id, version_no, object_type, object_id


def _invalid_reference():
    if request.accept_mimetypes['text/html'] > request.accept_mimetypes['application/json']:
        return render_template('errors/preview_reference.html'), 400
    return jsonify({"error": {
        "code": "CONTROLLED_FILE_REFERENCE_REQUIRED",
        "message": "必须使用受控文件编号进行预览",
        "requestId": getattr(request, "request_id", None),
    }}), 400


@bp.get("/file")
@business_user_required
def preview_file():
    reference = _controlled_reference()
    if reference is None:
        return _invalid_reference()
    file_id, version_no, object_type, object_id = reference
    return redirect(url_for(
        "files.preview_file", file_id=file_id, version_no=version_no,
        objectType=object_type, objectId=object_id,
    ))


@bp.get("/img")
@business_user_required
def preview_img():
    return preview_file.__wrapped__()
