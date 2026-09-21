from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
from contextlib import contextmanager
import re
import stat
import shutil
import tempfile
import zipfile
import uuid

from flask import current_app, jsonify, redirect, request, send_file, url_for
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import BadRequest

from app.security.auth import FORMAL_ROLES, current_identity
from app.services.files import FileServiceError, validate_file_name
from app.services.projects import ProjectServiceError
from app.routes._shared import RESEARCH_FOLDER_TYPES


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")

def legacy_project_sources(project_id, folder, name):
    """Describe an intact legacy chain without modifying its source files."""
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise FileServiceError("INVALID_PROJECT_PATH", "非法项目文件路径", 400)
    root = Path(current_app.config["UPLOAD_DIR"])
    directory = root / project_id
    for part in folder.split("/") if folder else []:
        directory /= part
    current = directory / name
    # Do not resolve away symlinks before checking the configured tree.
    if root.is_symlink():
        raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件目录包含链接", 409)
    parent = root
    for part in directory.relative_to(root).parts:
        parent /= part
        if parent.is_symlink():
            raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件目录包含链接", 409)
    history = directory / ".history"
    if history.is_symlink() or current.is_symlink():
        raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件包含链接", 409)
    stem, extension = os.path.splitext(name)
    pattern = re.compile(re.escape(stem) + r"_v([0-9]+)" + re.escape(extension))
    versions = []
    try:
        entries = list(history.iterdir())
    except FileNotFoundError:
        entries = []
    for path in entries:
        match = pattern.fullmatch(path.name)
        if match:
            number = int(match[1])
            if match[1] != str(number) or not stat.S_ISREG(path.lstat().st_mode):
                raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件历史版本无效", 409)
            versions.append((number, path))
    versions.sort()
    if [number for number, _ in versions] != list(range(1, len(versions) + 1)):
        raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件历史版本不连续", 409)
    metadata = history / f"{stem}_versions.json"
    if metadata.is_symlink():
        raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件历史记录包含链接", 409)
    if versions or metadata.exists():
        try:
            if not stat.S_ISREG(metadata.stat().st_mode) or metadata.stat().st_size > 1024 * 1024:
                raise ValueError("invalid metadata")
            data = json.loads(metadata.read_text(encoding="utf-8"))
            if isinstance(data, dict) and name not in data and not versions and not current.exists():
                return []
            records = data[name]
            if (not isinstance(records, list)
                    or any(not isinstance(row, dict) or type(row.get("version")) is not int
                           or row.get("original_name") != name for row in records)
                    or [row["version"] for row in records] != [number for number, _ in versions]):
                raise ValueError("history mismatch")
        except (OSError, ValueError, KeyError, TypeError):
            raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件历史记录缺失或不一致", 409)
    try:
        current_stat = current.lstat()
    except FileNotFoundError:
        if versions or metadata.exists():
            raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件当前文件缺失", 409)
        return []
    if not stat.S_ISREG(current_stat.st_mode):
        raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件不是普通文件", 409)
    return [path for _, path in versions] + [current]

def is_deleted_project_path(filepath, deleted):
    if filepath in deleted:
        return True
    # Original uploader stores <stem>_vN<ext> and <stem>_versions.json.
    folder, separator, name = filepath.rpartition("/.history/")
    if filepath.startswith(".history/"):
        folder, separator, name = "", ".history/", filepath[len(".history/"):]
    if not separator:
        return False
    prefix = folder + "/" if folder else ""
    if name.endswith("_versions.json"):
        stem = name[:-len("_versions.json")]
        return any(os.path.splitext(path)[0] == prefix + stem for path in deleted)
    match = re.fullmatch(r"(.+)_v[0-9]+(\.[^./]+)?", name)
    return bool(match and prefix + match[1] + (match[2] or "") in deleted)


def merge_project_files(tree, project_id):
    """Project controlled logical paths onto the retained physical folder tree."""
    service = current_app.extensions.get("file_service")
    if service is None:
        return tree
    deleted = set(service.list_deleted_project_paths(project_id))
    def hide_deleted(nodes):
        for node in nodes:
            node["files"] = [entry for entry in node["files"] if entry["path"] not in deleted]
            node["fileCount"] = len(node["files"])
            hide_deleted(node["children"])
    hide_deleted(tree)
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


def controlled_project_download(project_id, category, filepath, *, preview=False):
    service = current_app.extensions.get("file_service")
    if service is None:
        return None
    identity = current_identity()
    if identity is None or identity.role not in FORMAL_ROLES:
        return jsonify(success=False, message="无权访问业务附件"), 403
    if (filepath != filepath.strip() or "\\" in filepath
            or any(part in {"", ".", ".."} for part in filepath.split("/"))):
        return jsonify(success=False, message="非法路径"), 400
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        item = next((item for item in service.list_project_paths(project_id) if item["path"] == filepath), None)
        if item is not None:
            if preview:
                opened = service.open_version_stream(item["fileId"], item["versionNo"],
                                                     object_type="PROJECT", object_id=project_id)
                try:
                    response = send_file(opened["stream"], mimetype=opened["mediaType"],
                                         download_name=opened["originalName"], as_attachment=False,
                                         conditional=True, etag=opened["sha256"])
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
                    service.record_event(operation="PREVIEW", result="SUCCESS", file_id=item["fileId"],
                                         actor_user_id=identity.user_id,
                                         request_id=getattr(request, "request_id", "unknown"),
                                         size_bytes=opened["sizeBytes"])
                    return response
                except Exception:
                    opened["stream"].close()
                    raise
            return redirect(url_for("files.download_file", file_id=item["fileId"],
                                    version_no=item["versionNo"], objectType="PROJECT", objectId=project_id))
        if is_deleted_project_path(filepath, service.list_deleted_project_paths(project_id)):
            return jsonify(success=False, message="文件不存在"), 404
        return None
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def controlled_project_archive(project_id, category):
    service = current_app.extensions.get("file_service")
    if service is None:
        return None
    identity = current_identity()
    if identity is None or identity.role not in FORMAL_ROLES:
        return jsonify(success=False, message="无权访问业务附件"), 403
    archive_stream = None
    handed_off = False
    try:
        project = current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        selected = request.form.getlist("folders")
        if not selected:
            payload = request.get_json() if request.is_json else None
            if payload is not None and not isinstance(payload, dict):
                raise ValueError("invalid selection")
            selected = (payload or {}).get("paths", [])
        if not isinstance(selected, list) or any(
            not isinstance(path, str) or path != path.strip() or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/")) for path in selected
        ):
            raise ValueError("invalid selection")
        def includes(path):
            return not selected or any(path == folder or path.startswith(folder + "/") for folder in selected)
        live = service.list_project_paths(project_id)
        deleted = set(service.list_deleted_project_paths(project_id))
        hidden = deleted | {row["path"] for row in live}
        if os.path.islink(os.path.join(current_app.config["UPLOAD_DIR"], project_id)):
            raise ValueError("linked project root")
        project_dir = safe_project_path(current_app.config["UPLOAD_DIR"], project_id)
        archive_stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        def scan_failed(error):
            raise error
        try:
            os.stat(project_dir)
        except FileNotFoundError:
            legacy_entries = ()
        else:
            legacy_entries = os.walk(project_dir, onerror=scan_failed)
        with zipfile.ZipFile(archive_stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for root, dirs, files in legacy_entries:
                if any(os.path.islink(os.path.join(root, name)) for name in dirs):
                    raise ValueError("linked directory")
                for name in files:
                    path = os.path.join(root, name)
                    relative = os.path.relpath(path, project_dir).replace(os.sep, "/")
                    if relative in hidden or is_deleted_project_path(relative, deleted) or not includes(relative):
                        continue
                    if os.path.islink(path) or not os.path.isfile(path):
                        raise ValueError("invalid file")
                    archive.write(safe_project_path(current_app.config["UPLOAD_DIR"], project_id, relative), relative)
            for row in live:
                if not includes(row["path"]):
                    continue
                opened = service.open_version_stream(row["fileId"], row["versionNo"],
                                                     object_type="PROJECT", object_id=project_id)
                with opened["stream"] as source, archive.open(row["path"], "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        archive_stream.seek(0)
        name = re.sub(r"[^\w\s.-]", "_", project["name"]).strip(". ") or "项目"
        response = send_file(archive_stream, mimetype="application/zip", as_attachment=True,
                             download_name=f"{name}_{project_id}.zip")
        response.call_on_close(archive_stream.close)
        handed_off = True
        return response
    except (FileServiceError, ProjectServiceError) as error:
        if archive_stream is not None:
            archive_stream.close()
        return jsonify(success=False, message=error.message, code=error.code), error.status_code
    except (ValueError, BadRequest, zipfile.BadZipFile):
        if archive_stream is not None:
            archive_stream.close()
        return jsonify(success=False, message="打包失败，请检查所选路径及文件"), 400
    except (OSError, SQLAlchemyError):
        if archive_stream is not None:
            archive_stream.close()
        return jsonify(success=False, message="打包读取失败，请重试"), 500
    finally:
        if archive_stream is not None and not handed_off:
            archive_stream.close()


@contextmanager
def quarantine_project_folder(project_id, folder):
    """Move legacy bytes aside, retaining a local recovery manifest, never purge."""
    root = Path(current_app.config["UPLOAD_DIR"])
    source = root / project_id / folder
    current = root
    for part in ("", project_id, *folder.split("/")):
        if part:
            current /= part
        if current.is_symlink():
            raise FileServiceError("INVALID_PROJECT_PATH", "目录包含链接", 400)
    try:
        mode = source.stat().st_mode
    except FileNotFoundError:
        yield False
        return
    if not stat.S_ISDIR(mode):
        raise FileServiceError("INVALID_PROJECT_PATH", "目标不是文件夹", 400)
    def scan_failed(error):
        raise error
    for directory, dirs, files in os.walk(source, onerror=scan_failed):
        for name in dirs + files:
            mode = (Path(directory) / name).lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise FileServiceError("INVALID_PROJECT_PATH", "目录包含链接或特殊文件", 400)
    private_root = Path(current_app.config["DATA_DIR"])
    archive_root = private_root / "legacy-folder-archive"
    if private_root.is_symlink() or archive_root.is_symlink():
        raise FileServiceError("FILE_OPERATION_FAILED", "恢复目录无效", 500)
    archive_root.mkdir(mode=0o700, exist_ok=True)
    if archive_root.stat().st_dev != source.stat().st_dev:
        raise FileServiceError("FILE_OPERATION_FAILED", "恢复目录必须与原目录位于同一文件系统", 500)
    destination = archive_root / uuid.uuid4().hex
    destination.mkdir(mode=0o700)
    manifest = destination / "recovery.json"
    payload = destination / "payload"
    moved = False
    try:
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump({"projectId": project_id, "folder": folder}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(source, payload)
        moved = True
        yield True
    except Exception:
        if moved:
            # Never overwrite a newly created directory during recovery.
            try:
                if os.path.lexists(source):
                    raise OSError("original directory was recreated")
                os.rename(payload, source)
            except OSError as error:
                current_app.logger.critical("Legacy folder recovery required; recovery ID=%s", destination.name)
                raise FileServiceError("FILE_RECOVERY_REQUIRED", "目录恢复失败，原资料已保留，请联系维护人员", 500) from error
        manifest.unlink(missing_ok=True)
        destination.rmdir()
        raise


def delete_project_folder(project_id, category):
    identity = current_identity()
    if identity is None or identity.role not in FORMAL_ROLES:
        return jsonify(success=False, message="无权访问业务附件"), 403
    folder = request.form.get("folder_path", "")
    if (not PROJECT_ID_PATTERN.fullmatch(project_id) or not folder or folder != folder.strip()
            or "\\" in folder or any(part in {"", ".", ".."} or part.startswith(".") for part in folder.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in folder)):
        return jsonify(success=False, message="非法目录路径"), 400
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        result = current_app.extensions["file_service"].delete_project_folder(
            project_id, folder, physical_directory=lambda: quarantine_project_folder(project_id, folder),
            actor_user_id=identity.user_id, request_id=getattr(request, "request_id", "unknown"),
        )
        return jsonify(success=True, message="删除成功", **result)
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


@contextmanager
def quarantine_legacy_project_file(project_id, category, filepath):
    """Retain the exact legacy chain; rollback also covers the database commit."""
    folder, _, name = filepath.rpartition("/")
    sources = legacy_project_sources(project_id, folder, name)
    if not sources:
        raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
    project_root = Path(current_app.config["UPLOAD_DIR"]) / project_id
    metadata = project_root / folder / ".history" / (Path(name).stem + "_versions.json")
    replacement = None
    metadata_snapshot = None
    if metadata.exists():
        metadata_snapshot = metadata.read_bytes()
        records = json.loads(metadata_snapshot)
        remaining = {key: value for key, value in records.items() if key != name}
        if remaining:
            replacement = json.dumps(remaining, ensure_ascii=False).encode("utf-8")
        sources.append(metadata)
    archive_root = Path(current_app.config["DATA_DIR"]) / "legacy-folder-archive"
    if archive_root.parent.is_symlink() or archive_root.is_symlink():
        raise FileServiceError("FILE_OPERATION_FAILED", "恢复目录无效", 500)
    archive_root.mkdir(mode=0o700, exist_ok=True)
    entries = []
    identities = []
    for source in sources:
        before = source.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_dev != archive_root.stat().st_dev:
            raise FileServiceError("FILE_OPERATION_FAILED", "旧文件必须为同文件系统的普通文件", 500)
        digest = hashlib.sha256()
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件已变化，请刷新后重试", 409)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件已变化，请刷新后重试", 409)
        identities.append((before.st_dev, before.st_ino))
        if source == metadata and digest.hexdigest() != hashlib.sha256(metadata_snapshot).hexdigest():
            raise FileServiceError("LEGACY_FILE_CONFLICT", "旧历史记录已变化，请刷新后重试", 409)
        relative = source.relative_to(project_root).as_posix()
        entries.append({"sourceRelativePath": relative, "payloadRelativePath": "payload/" + relative,
                        "sizeBytes": before.st_size, "sha256": digest.hexdigest()})
    destination = archive_root / uuid.uuid4().hex
    destination.mkdir(mode=0o700)
    manifest = destination / "recovery.json"
    moved = []
    replacement_written = False
    try:
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump({"kind": "LEGACY_PROJECT_FILE_DELETE", "category": category,
                       "projectId": project_id, "filepath": filepath, "files": entries}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        for source, entry, identity in zip(sources, entries, identities):
            target = destination / entry["payloadRelativePath"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
            moved.append((source, target))
            actual = target.lstat()
            if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != identity:
                raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件已变化，请刷新后重试", 409)
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if actual.st_size != entry["sizeBytes"] or digest.hexdigest() != entry["sha256"]:
                raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件已变化，请刷新后重试", 409)
        if replacement is not None:
            pending = destination / "metadata-next.json"
            with pending.open("xb") as handle:
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
            # Publish the complete replacement without overwriting a recreated file.
            os.link(pending, metadata)
            replacement_written = True
            pending.unlink()
        yield destination.name
    except Exception:
        try:
            if replacement_written:
                if metadata.is_symlink() or metadata.read_bytes() != replacement:
                    raise OSError("shared metadata changed during recovery")
                metadata.unlink()
            for source, target in reversed(moved):
                if os.path.lexists(source):
                    raise OSError("original file was recreated")
                os.rename(target, source)
        except OSError as error:
            current_app.logger.critical("Legacy file recovery required; recovery ID=%s", destination.name)
            raise FileServiceError("FILE_RECOVERY_REQUIRED", "文件恢复失败，原资料已保留，请联系维护人员", 500) from error
        shutil.rmtree(destination)
        raise


def delete_controlled_project_file(project_id, category):
    service = current_app.extensions.get("file_service")
    if service is None:
        return None
    identity = current_identity()
    if identity is None or identity.role not in FORMAL_ROLES:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        filepath = request.form.get("file_path", "")
        if (filepath != filepath.strip() or "\\" in filepath
                or any(part in {"", ".", ".."} for part in filepath.split("/"))):
            return jsonify(success=False, message="非法路径"), 400
        expected_id = request.form.get("expectedFileId", "").strip()
        if not any(row["path"] == filepath for row in service.list_project_paths(project_id)):
            if expected_id or filepath in service.list_deleted_project_paths(project_id):
                return jsonify(success=False, message="文件已变化，请刷新后重试"), 409
            result = service.delete_legacy_project_file(
                project_id, filepath,
                physical_file=lambda: quarantine_legacy_project_file(project_id, category, filepath),
                actor_user_id=identity.user_id, request_id=getattr(request, "request_id", "unknown"),
            )
            return jsonify(success=True, message="删除成功", **result)
        result = service.delete_project_path(project_id, filepath, expected_file_id=expected_id,
                                             actor_user_id=identity.user_id,
                                             request_id=getattr(request, "request_id", "unknown"))
        return jsonify(success=True, message="删除成功", **result)
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def rename_controlled_project_file(project_id, category):
    service = current_app.extensions.get("file_service")
    if service is None:
        return None
    identity = current_identity()
    if identity is None or identity.role not in FORMAL_ROLES:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        filepath = request.form.get("file_path", "")
        requested_name = request.form.get("new_name", "").strip()
        # Compare canonical logical paths before falling back to physical files.
        # Renaming is a leaf-name operation, never a move between directories.
        if (filepath != filepath.strip() or "\\" in filepath
                or any(part in {"", ".", ".."} for part in filepath.split("/"))
                or requested_name in {"", ".", ".."}
                or "/" in requested_name or "\\" in requested_name):
            raise ValueError("noncanonical rename path")
        paths = service.list_project_paths(project_id)
        folder, _, _old_name = filepath.rpartition("/")
        requested_target = (folder + "/" if folder else "") + requested_name
        if requested_target != filepath and any(item["path"] == requested_target for item in paths):
            return jsonify(success=False, message="文件名已存在"), 409
        # Unknown legacy paths remain with the existing adapter until adopted.
        if not any(item["path"] == filepath for item in paths):
            if request.form.get("expectedFileId") or is_deleted_project_path(filepath, service.list_deleted_project_paths(project_id)):
                return jsonify(success=False, message="文件已变化，请刷新后重试"), 409
            return None
        expected_file_id = request.form.get("expectedFileId", "").strip()
        if not expected_file_id:
            return jsonify(success=False, message="请刷新页面后重试"), 409
        name, _extension = validate_file_name(request.form.get("new_name", ""))
        folder, _, _old_name = filepath.rpartition("/")
        target = (folder + "/" if folder else "") + name
        if target != filepath and os.path.lexists(safe_project_path(current_app.config["UPLOAD_DIR"], project_id, target)):
            return jsonify(success=False, message="文件名已存在"), 409
        result = service.rename_project_path(project_id, filepath, name, actor_user_id=identity.user_id,
                                             expected_file_id=expected_file_id,
                                             request_id=getattr(request, "request_id", "unknown"))
        if result is None:
            return jsonify(success=False, message="文件路径已变化"), 409
        return jsonify(success=True, message="重命名成功", **result)
    except ValueError:
        return jsonify(success=False, message="非法路径"), 400
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def upload_project_file(project_id, category):
    """Keep the legacy upload URL while using controlled file/version storage."""
    identity = current_identity()
    if identity is None:
        return jsonify(success=False, message="未登录"), 401
    if identity.role not in FORMAL_ROLES:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(
            category=category, business_id=project_id,
        )
        uploads = request.files.getlist("file")
        if len(uploads) > 1:
            return jsonify(success=False, message="此接口每次只接收一个文件，请通过多选上传逐份提交", code="MULTIPLE_FILES_NOT_ALLOWED"), 400
        uploaded = uploads[0] if uploads else None
        if uploaded is None or not uploaded.filename:
            return jsonify(success=False, message="请选择文件"), 400
        result = current_app.extensions["file_service"].upload_project_path(
            uploaded.stream, original_name=uploaded.filename,
            folder=request.form.get("folder", ""), project_id=project_id,
            legacy_sources=lambda normalized_name: legacy_project_sources(project_id, request.form.get("folder", ""), normalized_name),
            actor_user_id=identity.user_id, request_id=getattr(request, "request_id", "unknown"),
        )
        return jsonify(success=True, message="上传成功", fileId=result["fileId"], versionNo=result["versionNo"])
    except (FileServiceError, ProjectServiceError) as error:
        return jsonify(success=False, message=error.message, code=error.code), error.status_code


def upload_project_folder(project_id, category):
    """Keep folder-upload names and use the same controlled writer as single files."""
    identity = current_identity()
    if identity is None:
        return jsonify(success=False, message="未登录"), 401
    if identity.role not in FORMAL_ROLES:
        return jsonify(success=False, message="无权访问业务附件"), 403
    try:
        current_app.extensions["project_service"].get_legacy(category=category, business_id=project_id)
        uploads = request.files.getlist("files")
        if not uploads:
            return jsonify(success=False, message="请选择文件夹"), 400
        target = request.form.get("folder", "") or RESEARCH_FOLDER_TYPES[0]
        completed = []
        failures = []
        for uploaded in uploads:
            relative = uploaded.filename or ""
            try:
                if relative.startswith("/") or "\\" in relative:
                    raise FileServiceError("INVALID_PROJECT_PATH", "非法项目文件路径", 400)
                subfolder, _, name = relative.rpartition("/")
                result = current_app.extensions["file_service"].upload_project_path(
                    uploaded.stream, original_name=name, folder=target + ("/" + subfolder if subfolder else ""),
                    project_id=project_id, actor_user_id=identity.user_id,
                    legacy_sources=lambda normalized_name: legacy_project_sources(project_id, target + ("/" + subfolder if subfolder else ""), normalized_name),
                    request_id=getattr(request, "request_id", "unknown"),
                )
                completed.append({"path": relative, "fileId": result["fileId"], "versionNo": result["versionNo"]})
            except FileServiceError as error:
                failures.append({"path": relative, "code": error.code,
                                 "message": error.message, "status": error.status_code})
            except (OSError, SQLAlchemyError):
                failures.append({"path": relative, "code": "FILE_OPERATION_FAILED",
                                 "message": "文件操作失败", "status": 500})
        message = f"上传成功 {len(completed)} 个文件"
        if failures:
            message += f"，失败 {len(failures)} 个：" + "；".join(
                f"{item['path']}（{item['message']}）" for item in failures
            )
        status = 200 if not failures else (207 if completed else failures[0]["status"])
        return jsonify(success=not failures, message=message, uploadedCount=len(completed),
                       failedCount=len(failures), files=completed, errors=failures), status
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
