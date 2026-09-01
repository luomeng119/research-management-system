"""Compatibility adapter for the controlled V1 file preview contract.

Legacy filesystem paths are intentionally rejected. Callers must identify a
file already authorized through ``object_files``.
"""

from flask import Blueprint, jsonify, redirect, request, url_for

from app.web.files import business_user_required


bp = Blueprint("preview", __name__, url_prefix="/preview")


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
