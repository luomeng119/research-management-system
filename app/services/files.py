from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import codecs
import mimetypes
import os
from pathlib import Path
import stat
import struct
import time
from urllib.parse import unquote
import uuid
import zipfile
from xml.parsers import expat

from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.selectable import Select
from app.repositories.files import PROJECT_PATH_PREFIX


ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yml", ".yaml", ".log",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
}
DANGEROUS_SUFFIXES = {
    ".exe", ".com", ".bat", ".cmd", ".sh", ".ps1", ".js", ".jar", ".msi", ".scr",
    ".php", ".py", ".pl", ".rb",
}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml", ".yml", ".yaml", ".log"}
TEXT_PREVIEW_MAX_BYTES = 200 * 1024
OFFICE_MAX_ENTRIES = 5_000
OFFICE_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
OFFICE_MAX_ENTRY_BYTES = 50 * 1024 * 1024
OFFICE_MAX_COMPRESSION_RATIO = 200
OFFICE_PREVIEW_MAX_ENTRIES = 1_000
OFFICE_PREVIEW_MAX_EXPANDED_BYTES = 25 * 1024 * 1024
OFFICE_PREVIEW_MAX_ENTRY_BYTES = 8 * 1024 * 1024
OFFICE_PREVIEW_MAX_XML_BYTES = 12 * 1024 * 1024
OFFICE_PREVIEW_MAX_XML_NODES = 50_000
OFFICE_PREVIEW_MAX_XML_CHARACTERS = 5 * 1024 * 1024


def office_archive_is_safe(path: Path) -> bool:
    try:
        position = path.tell() if hasattr(path, "tell") else None
        owns_source = isinstance(path, Path)
        source = path.open("rb") if owns_source else path
        try:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - 65_557))
            tail = source.read()
        finally:
            if owns_source:
                source.close()
        if position is not None:
            path.seek(position)
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0 or len(tail) - marker < 22:
            return False
        entry_count = struct.unpack_from("<H", tail, marker + 10)[0]
        central_size = struct.unpack_from("<I", tail, marker + 12)[0]
        central_offset = struct.unpack_from("<I", tail, marker + 16)[0]
        if (
            entry_count == 0xFFFF
            or entry_count > OFFICE_MAX_ENTRIES
            or central_size > 16 * 1024 * 1024
            or central_offset + central_size > size
        ):
            return False
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) != entry_count or len(infos) > OFFICE_MAX_ENTRIES:
                return False
            expanded = 0
            for info in infos:
                expanded += info.file_size
                if info.file_size > OFFICE_MAX_ENTRY_BYTES or expanded > OFFICE_MAX_EXPANDED_BYTES:
                    return False
                if info.file_size and info.compress_size == 0:
                    return False
                if info.compress_size and info.file_size / info.compress_size > OFFICE_MAX_COMPRESSION_RATIO:
                    return False
        return True
    except (OSError, zipfile.BadZipFile):
        return False


def office_archive_is_preview_safe(path: Path) -> bool:
    """Apply conservative budgets before an OOXML library builds object trees."""
    try:
        if not office_archive_is_safe(path):
            return False
        xml_bytes = 0
        xml_nodes = 0
        xml_characters = 0

        def reject_entity(*_args):
            raise ValueError("XML entities are not allowed in previews")

        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > OFFICE_PREVIEW_MAX_ENTRIES:
                return False
            if sum(info.file_size for info in infos) > OFFICE_PREVIEW_MAX_EXPANDED_BYTES:
                return False
            for info in infos:
                if info.file_size > OFFICE_PREVIEW_MAX_ENTRY_BYTES:
                    return False
                lowered = info.filename.lower()
                if not (lowered.endswith(".xml") or lowered.endswith(".rels")):
                    continue
                xml_bytes += info.file_size
                if xml_bytes > OFFICE_PREVIEW_MAX_XML_BYTES:
                    return False
                parser = expat.ParserCreate()
                parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
                parser.EntityDeclHandler = reject_entity
                parser.ExternalEntityRefHandler = lambda *_args: 0

                def count_node(_name, _attrs):
                    nonlocal xml_nodes
                    xml_nodes += 1
                    if xml_nodes > OFFICE_PREVIEW_MAX_XML_NODES:
                        raise ValueError("XML node budget exceeded")

                def count_characters(value):
                    nonlocal xml_characters
                    xml_characters += len(value)
                    if xml_characters > OFFICE_PREVIEW_MAX_XML_CHARACTERS:
                        raise ValueError("XML character budget exceeded")

                parser.StartElementHandler = count_node
                parser.CharacterDataHandler = count_characters
                with archive.open(info) as source:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        parser.Parse(chunk, False)
                    parser.Parse(b"", True)
        return True
    except (OSError, ValueError, expat.ExpatError, zipfile.BadZipFile):
        return False


class FileServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class _StagedFile:
    path: Path
    original_name: str
    extension: str
    media_type: str
    size_bytes: int
    sha256: str


def validate_file_name(original_name: str) -> tuple[str, str]:
    """Validate an untrusted leaf filename using the runtime upload policy."""
    if not isinstance(original_name, str) or not original_name.strip():
        raise FileServiceError("INVALID_FILENAME", "文件名无效")
    name = original_name.strip()
    if (
        any(marker in name for marker in ("/", "\\"))
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or Path(name).name != name
    ):
        raise FileServiceError("INVALID_FILENAME", "文件名无效")
    suffixes = [suffix.lower() for suffix in Path(name).suffixes]
    if not suffixes or suffixes[-1] not in ALLOWED_EXTENSIONS:
        raise FileServiceError("UNSUPPORTED_MEDIA_TYPE", "不支持该文件类型", 415)
    if any(
        suffix in DANGEROUS_SUFFIXES or suffix in ALLOWED_EXTENSIONS
        for suffix in suffixes[:-1]
    ):
        raise FileServiceError("INVALID_FILENAME", "不允许双重扩展名")
    return name, suffixes[-1]


def validate_file_content(path: Path, extension: str) -> str:
    """Validate a regular staged file's bytes using the runtime upload policy."""
    with path.open("rb") as source:
        prefix = source.read(8192)
    valid = False
    if extension == ".pdf":
        valid = prefix.startswith(b"%PDF-")
    elif extension in {".jpg", ".jpeg"}:
        valid = prefix.startswith(b"\xff\xd8\xff")
    elif extension == ".png":
        valid = prefix.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension == ".gif":
        valid = prefix.startswith((b"GIF87a", b"GIF89a"))
    elif extension == ".bmp":
        valid = prefix.startswith(b"BM")
    elif extension == ".webp":
        valid = prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    elif extension == ".rar":
        valid = prefix.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"))
    elif extension in {".doc", ".xls", ".ppt"}:
        import olefile

        try:
            with olefile.OleFileIO(str(path), raise_defects=olefile.DEFECT_INCORRECT) as document:
                required_groups = {
                    ".doc": (("WordDocument",), ("0Table", "1Table")),
                    ".xls": (("Workbook", "Book"),),
                    ".ppt": (("PowerPoint Document",),),
                }[extension]
                physical_size = path.stat().st_size
                valid = True
                for group in required_groups:
                    candidates = [name for name in group
                                  if document.get_type(name) == olefile.STGTY_STREAM]
                    if not candidates:
                        valid = False
                        break
                    name = candidates[0]
                    size = document.get_size(name)
                    if not 0 < size <= physical_size:
                        valid = False
                        break
                    with document.openstream(name) as stream:
                        if len(stream.read(size + 1)) != size:
                            valid = False
                            break
        except (OSError, ValueError, IndexError):
            valid = False
    elif extension in {".zip", ".docx", ".xlsx", ".pptx"}:
        try:
            valid = office_archive_is_safe(path)
            with zipfile.ZipFile(path) as archive:
                required = {
                    ".docx": "word/document.xml",
                    ".xlsx": "xl/workbook.xml",
                    ".pptx": "ppt/presentation.xml",
                }.get(extension)
                if required:
                    valid = valid and any(
                        info.filename == required for info in archive.infolist()
                    )
        except (OSError, zipfile.BadZipFile):
            valid = False
    elif extension in TEXT_EXTENSIONS:
        try:
            decoder = codecs.getincrementaldecoder("utf-8")()
            valid = True
            with path.open("rb") as source:
                while True:
                    raw = source.read(64 * 1024)
                    if not raw:
                        break
                    if b"\x00" in raw:
                        valid = False
                        break
                    decoder.decode(raw)
                decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise FileServiceError("UNSUPPORTED_MEDIA_TYPE", "文件内容与类型不匹配", 415)
    legacy_media_type = {
        ".doc": "application/msword",
        ".xls": "application/vnd.ms-excel",
        ".ppt": "application/vnd.ms-powerpoint",
    }.get(extension)
    if legacy_media_type:
        return legacy_media_type
    return mimetypes.guess_type("file" + extension)[0] or "application/octet-stream"


def validate_file_candidate(
    path: Path, original_name: str, *, max_bytes: int
) -> tuple[str, str, str, int, str]:
    """Pure shared policy entry for runtime staging and legacy migration."""
    name, extension = validate_file_name(original_name)
    try:
        details = path.stat()
    except OSError as exc:
        raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404) from exc
    if not stat.S_ISREG(details.st_mode):
        raise FileServiceError("INVALID_FILE", "文件无效")
    if details.st_size == 0:
        raise FileServiceError("UNSUPPORTED_MEDIA_TYPE", "空文件不允许上传", 415)
    if details.st_size > int(max_bytes):
        raise FileServiceError("FILE_TOO_LARGE", "文件超过大小限制", 413)
    media_type = validate_file_content(path, extension)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return name, extension, media_type, details.st_size, digest.hexdigest()


class _MetadataWriter:
    """Restricted view of the active connection for first-object metadata writes."""

    __slots__ = ("__active_connection",)

    def __init__(self, active_connection) -> None:
        self.__active_connection = active_connection

    def execute(self, statement, parameters=None) -> None:
        if not isinstance(statement, Insert):
            raise RuntimeError("metadata callback may only execute SQLAlchemy Insert statements")
        self.__active_connection.execute(statement, parameters)

    def one(self, statement):
        if not isinstance(statement, Select):
            raise RuntimeError("metadata callback may only read with SQLAlchemy Select statements")
        row = self.__active_connection.execute(statement).mappings().first()
        return dict(row) if row else None

    def update(self, statement) -> int:
        if not isinstance(statement, Update):
            raise RuntimeError("metadata callback may only update with SQLAlchemy Update statements")
        return int(self.__active_connection.execute(statement).rowcount)

    def in_transaction(self):
        return self.__active_connection.in_transaction()

    @staticmethod
    def _reject_transaction_control(*_args, **_kwargs):
        raise RuntimeError("metadata callback may not control its transaction")

    commit = _reject_transaction_control
    rollback = _reject_transaction_control
    close = _reject_transaction_control
    begin = _reject_transaction_control
    begin_nested = _reject_transaction_control
    invalidate = _reject_transaction_control
    detach = _reject_transaction_control
    __enter__ = _reject_transaction_control
    __exit__ = _reject_transaction_control

    def __getattr__(self, name):
        raise AttributeError(f"metadata callback writer does not expose {name!r}")


class FileService:
    def __init__(self, repository, audit_service, *, storage_root, max_bytes: int, preview_max_bytes: int):
        self.repository = repository
        self.audit_service = audit_service
        self.storage_root = Path(storage_root)
        self.max_bytes = int(max_bytes)
        self.preview_max_bytes = int(preview_max_bytes)

    def _ensure_controlled_directory(self, directory: Path) -> None:
        if self.storage_root.is_symlink():
            raise FileServiceError("FILE_OPERATION_FAILED", "文件存储目录无效", 500)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        directory.mkdir(parents=True, exist_ok=True)
        root = self.storage_root.resolve()
        try:
            if os.path.commonpath((str(root), str(directory.resolve()))) != str(root):
                raise FileServiceError("FILE_OPERATION_FAILED", "文件存储目录无效", 500)
            current = self.storage_root
            if not current.is_dir() or current.is_symlink():
                raise FileServiceError("FILE_OPERATION_FAILED", "文件存储目录无效", 500)
            if os.name != "nt" and stat.S_IMODE(current.stat().st_mode) & 0o022:
                raise FileServiceError("FILE_OPERATION_FAILED", "文件存储目录权限过宽", 500)
            for part in directory.relative_to(self.storage_root).parts:
                current = current / part
                if not current.is_dir() or current.is_symlink():
                    raise FileServiceError("FILE_OPERATION_FAILED", "文件存储目录无效", 500)
                if os.name != "nt" and stat.S_IMODE(current.stat().st_mode) & 0o022:
                    raise FileServiceError("FILE_OPERATION_FAILED", "文件存储目录权限过宽", 500)
        except (OSError, ValueError):
            raise FileServiceError("FILE_OPERATION_FAILED", "文件存储目录无效", 500)

    def _validate_object(self, object_type: str, object_id: str) -> None:
        object_type = str(object_type or "").upper()
        with self.repository.engine.connect() as connection:
            exists = self.repository.object_exists(connection, object_type, str(object_id))
        if not exists:
            raise FileServiceError("OBJECT_NOT_FOUND", "关联业务对象不存在", 404)

    def _validate_object_write(self, object_type: str, object_id: str) -> None:
        with self.repository.engine.connect() as connection:
            writable = self.repository.object_allows_file_write(
                connection, object_type, str(object_id)
            )
        if writable is None:
            raise FileServiceError("OBJECT_NOT_FOUND", "关联业务对象不存在", 404)
        if not writable:
            raise FileServiceError("OBJECT_READ_ONLY", "终态业务对象不允许修改附件", 409)

    def _lock_object_write(self, connection, object_type: str, object_id: str) -> None:
        writable = self.repository.object_allows_file_write(
            connection, object_type, str(object_id), lock=True
        )
        if writable is None:
            raise FileServiceError("OBJECT_NOT_FOUND", "关联业务对象不存在", 404)
        if not writable:
            raise FileServiceError("OBJECT_READ_ONLY", "终态业务对象不允许修改附件", 409)

    @staticmethod
    def _validate_name(original_name: str) -> tuple[str, str]:
        return validate_file_name(original_name)

    @staticmethod
    def _validate_content(path: Path, extension: str) -> str:
        return validate_file_content(path, extension)

    def _stage(self, stream, original_name: str) -> _StagedFile:
        name, extension = self._validate_name(original_name)
        staging_dir = self.storage_root / ".staging"
        self._ensure_controlled_directory(staging_dir)
        stage_path = staging_dir / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with stage_path.open("xb") as destination:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise FileServiceError("INVALID_FILE", "文件流无效")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise FileServiceError("FILE_TOO_LARGE", "文件超过大小限制", 413)
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            name, extension, media_type, validated_size, validated_sha = validate_file_candidate(
                stage_path, name, max_bytes=self.max_bytes
            )
            return _StagedFile(
                stage_path, name, extension, media_type, validated_size, validated_sha
            )
        except Exception:
            stage_path.unlink(missing_ok=True)
            raise

    @contextmanager
    def inspect_upload(self, stream, original_name: str):
        """Validate and stage an upload for bounded pre-persistence inspection."""
        staged = self._stage(stream, original_name)
        try:
            yield staged.path
        finally:
            staged.path.unlink(missing_ok=True)

    def _destination(self, extension: str) -> tuple[str, Path]:
        now = datetime.now(timezone.utc)
        relative = Path(str(now.year), f"{now.month:02d}", f"{uuid.uuid4().hex}{extension}")
        final_path = self.storage_root / relative
        self._ensure_controlled_directory(final_path.parent)
        return relative.as_posix(), final_path

    @staticmethod
    def _size_bucket(size: int) -> str:
        if size < 1024 * 1024:
            return "LT_1_MB"
        if size < 10 * 1024 * 1024:
            return "1_TO_10_MB"
        return "GE_10_MB"

    def _audit(
        self, connection, *, operation: str, staged: _StagedFile | None,
        file_id: str, actor_user_id: int, request_id: str, started: float,
        result: str = "SUCCESS", error_code: str | None = None,
        file_type: str | None = None, size_bytes: int | None = None,
    ) -> None:
        self.audit_service.record(
            connection,
            event_name="file_operation_completed",
            user_id=actor_user_id,
            object_type="FILE",
            object_id=file_id,
            result=result,
            request_id=request_id,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code=error_code,
            properties={
                "operation": operation,
                "file_type": staged.extension.lstrip(".") if staged else (file_type or "metadata"),
                "size_bucket": self._size_bucket(staged.size_bytes) if staged else (
                    self._size_bucket(size_bytes) if size_bytes is not None else "N/A"
                ),
            },
        )

    def record_event(
        self, *, operation: str, actor_user_id: int, request_id: str,
        file_id: str | None, result: str, error_code: str | None = None,
        file_type: str | None = None, size_bytes: int | None = None,
    ) -> None:
        try:
            with self.repository.engine.begin() as connection:
                self._audit(
                    connection, operation=operation, staged=None,
                    file_id=file_id or "unknown", actor_user_id=actor_user_id,
                    request_id=request_id, started=time.monotonic(), result=result,
                    error_code=error_code, file_type=file_type, size_bytes=size_bytes,
                )
        except FileServiceError:
            raise
        except Exception as exc:
            raise FileServiceError("FILE_OPERATION_FAILED", "文件审计失败", 500) from exc

    @staticmethod
    def _result(file_id: str, staged: _StagedFile, relative_path: str, version_no: int) -> dict:
        return {
            "fileId": str(file_id),
            "versionNo": version_no,
            "originalName": staged.original_name,
            "mediaType": staged.media_type,
            "storagePath": relative_path,
            "sha256": staged.sha256,
            "sizeBytes": staged.size_bytes,
        }

    def _upload_staged(
        self, staged: _StagedFile, *, object_type: str, object_id: str,
        actor_user_id: int, request_id: str, create_metadata=None, project_path=None,
        legacy_sources=None,
    ) -> dict:
        relative_path, final_path = self._destination(staged.extension)
        file_id = uuid.uuid4()
        started = time.monotonic()
        moved = False
        legacy_staged = []
        legacy_moved = []
        version_no = 1
        try:
            with self.repository.engine.begin() as connection:
                if create_metadata is not None:
                    create_metadata(_MetadataWriter(connection))
                self._lock_object_write(connection, object_type, str(object_id))
                existing = self.repository.get_project_path_file(
                    connection, project_id=str(object_id), purpose=project_path,
                ) if project_path is not None else None
                if existing is not None:
                    if existing["status"] != "ACTIVE":
                        raise FileServiceError("FILE_ARCHIVED", "文件已归档", 409)
                    if staged.extension != Path(existing["original_name"]).suffix.lower() or staged.media_type != existing["media_type"]:
                        raise FileServiceError("FILE_TYPE_MISMATCH", "新版本必须与原文件类型一致", 415)
                    file_id = existing["id"]
                    version_no = int(existing["version"]) + 1
                    if not self.repository.bump_file(
                        connection, file_id=file_id, expected_version=version_no - 1,
                        actor_user_id=actor_user_id, original_name=staged.original_name,
                        media_type=staged.media_type,
                    ):
                        raise FileServiceError("VERSION_CONFLICT", "文件版本已变化", 409)
                else:
                    if (legacy_sources is not None and project_path is not None
                            and project_path[len(PROJECT_PATH_PREFIX):] not in
                            self.repository.list_deleted_project_paths(connection, project_id=str(object_id))):
                        for source in legacy_sources(staged.original_name):
                            before = source.lstat()
                            if not stat.S_ISREG(before.st_mode):
                                raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件不是普通文件", 409)
                            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                            with os.fdopen(descriptor, "rb") as stream:
                                opened = os.fstat(stream.fileno())
                                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                                    raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件已变化", 409)
                                old = self._stage(stream, staged.original_name)
                                legacy_staged.append(old)
                                after = os.fstat(stream.fileno())
                                if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                                    raise FileServiceError("LEGACY_FILE_CONFLICT", "旧附件已变化", 409)
                            if old.media_type != staged.media_type:
                                raise FileServiceError("FILE_TYPE_MISMATCH", "旧版本文件类型不一致", 415)
                    self.repository.create_file(
                        connection,
                        file_id=file_id,
                        business_id=f"FILE-{uuid.uuid4().hex.upper()}",
                        original_name=staged.original_name,
                        media_type=staged.media_type,
                        actor_user_id=actor_user_id,
                    )
                    for number, old in enumerate(legacy_staged, start=1):
                        old_relative, old_final = self._destination(old.extension)
                        self.repository.create_version(
                            connection, version_id=uuid.uuid4(), file_id=file_id, version_no=number,
                            storage_path=old_relative, sha256=old.sha256, size_bytes=old.size_bytes,
                            media_type=old.media_type, actor_user_id=actor_user_id,
                        )
                        if not self.repository.bump_file(connection, file_id=file_id,
                                                         expected_version=number, actor_user_id=actor_user_id):
                            raise FileServiceError("VERSION_CONFLICT", "文件版本已变化", 409)
                        os.replace(old.path, old_final)
                        legacy_moved.append(old_final)
                    version_no = len(legacy_staged) + 1
                self.repository.create_version(
                    connection,
                    version_id=uuid.uuid4(),
                    file_id=file_id,
                    version_no=version_no,
                    storage_path=relative_path,
                    sha256=staged.sha256,
                    size_bytes=staged.size_bytes,
                    media_type=staged.media_type,
                    actor_user_id=actor_user_id,
                )
                if existing is None:
                    self.repository.link_object(
                        connection,
                        link_id=uuid.uuid4(),
                        object_type=object_type,
                        object_id=str(object_id),
                        file_id=file_id,
                        actor_user_id=actor_user_id,
                        purpose=project_path,
                    )
                os.replace(staged.path, final_path)
                moved = True
                self._audit(
                    connection,
                    operation="ADOPT_LEGACY" if legacy_staged else ("ADD_VERSION" if existing is not None else "UPLOAD"),
                    staged=staged,
                    file_id=str(file_id),
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    started=started,
                )
            return self._result(str(file_id), staged, relative_path, version_no)
        except FileServiceError:
            for path in legacy_moved:
                path.unlink(missing_ok=True)
            if moved:
                final_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            for path in legacy_moved:
                path.unlink(missing_ok=True)
            if moved:
                final_path.unlink(missing_ok=True)
            raise FileServiceError("FILE_OPERATION_FAILED", "文件操作失败", 500) from exc
        finally:
            for old in legacy_staged:
                old.path.unlink(missing_ok=True)
            staged.path.unlink(missing_ok=True)

    def upload(self, stream, *, original_name: str, object_type: str, object_id: str, actor_user_id: int, request_id: str) -> dict:
        object_type = str(object_type or "").upper()
        self._validate_object_write(object_type, str(object_id))
        staged = self._stage(stream, original_name)
        return self._upload_staged(
            staged, object_type=object_type, object_id=str(object_id),
            actor_user_id=actor_user_id, request_id=request_id,
        )

    def list_deleted_project_paths(self, project_id: str) -> list[str]:
        self._validate_object("PROJECT", str(project_id))
        with self.repository.engine.connect() as connection:
            return self.repository.list_deleted_project_paths(connection, project_id=project_id)

    def delete_project_path(self, project_id: str, filepath: str, *, expected_file_id: str,
                            actor_user_id: int, request_id: str) -> dict:
        started = time.monotonic()
        try:
            with self.repository.engine.begin() as connection:
                self._lock_object_write(connection, "PROJECT", str(project_id))
                row = self.repository.get_project_path_file(
                    connection, project_id=project_id, purpose=PROJECT_PATH_PREFIX + filepath,
                )
                if row is None or str(row["id"]) != expected_file_id:
                    raise FileServiceError("FILE_IDENTITY_CONFLICT", "文件已变化，请刷新后重试", 409)
                row = self.repository.get_linked_file(connection, file_id=expected_file_id,
                                                      object_type="PROJECT", object_id=project_id, lock=True)
                if row is None or row["status"] != "ACTIVE":
                    raise FileServiceError("FILE_IDENTITY_CONFLICT", "文件已变化，请刷新后重试", 409)
                self.repository.delete_project_path(connection, project_id=project_id,
                                                    file_id=expected_file_id, filepath=filepath,
                                                    actor_user_id=actor_user_id)
                if not self.repository.has_live_links(connection, file_id=expected_file_id):
                    if not self.repository.archive_file(connection, file_id=expected_file_id,
                                                        expected_version=int(row["version"]), actor_user_id=actor_user_id):
                        raise FileServiceError("VERSION_CONFLICT", "文件版本已变化", 409)
                self._audit(connection, operation="DELETE", staged=None, file_id=expected_file_id,
                            actor_user_id=actor_user_id, request_id=request_id, started=started)
            return {"fileId": expected_file_id, "path": filepath}
        except FileServiceError:
            raise
        except Exception as exc:
            raise FileServiceError("FILE_OPERATION_FAILED", "文件删除失败", 500) from exc

    def delete_legacy_project_file(self, project_id: str, filepath: str, *, physical_file,
                                   actor_user_id: int, request_id: str) -> dict:
        started = time.monotonic()
        try:
            with ExitStack() as recovery:
                with self.repository.engine.begin() as connection:
                    self._lock_object_write(connection, "PROJECT", str(project_id))
                    if (self.repository.get_project_path_file(
                            connection, project_id=project_id, purpose=PROJECT_PATH_PREFIX + filepath) is not None
                            or filepath in self.repository.list_deleted_project_paths(connection, project_id=project_id)):
                        raise FileServiceError("FILE_IDENTITY_CONFLICT", "文件已变化，请刷新后重试", 409)
                    recovery_id = recovery.enter_context(physical_file())
                    self.audit_service.record(
                        connection, event_name="file_operation_completed", user_id=actor_user_id,
                        object_type="PROJECT", object_id=project_id, result="SUCCESS", request_id=request_id,
                        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                        properties={"operation": "DELETE_LEGACY_FILE", "file_type": "legacy", "size_bucket": "N/A"},
                    )
            return {"recoveryId": recovery_id}
        except FileServiceError:
            raise
        except Exception as exc:
            raise FileServiceError("FILE_OPERATION_FAILED", "文件删除失败，请检查后重试", 500) from exc

    def delete_project_folder(self, project_id: str, folder: str, *, physical_directory,
                              actor_user_id: int, request_id: str) -> dict:
        """Detach a directory's links together; keep bytes for recovery."""
        started = time.monotonic()
        prefix = PROJECT_PATH_PREFIX + folder + "/"
        try:
            # The filesystem context outlives commit so commit errors restore
            # the quarantined legacy directory as well as rolling back rows.
            with ExitStack() as recovery:
                with self.repository.engine.begin() as connection:
                    self._lock_object_write(connection, "PROJECT", str(project_id))
                    rows = [row for row in self.repository.list_project_paths(connection, project_id=project_id)
                            if row["purpose"].startswith(prefix)]
                    moved = recovery.enter_context(physical_directory())
                    if not moved and not rows:
                        raise FileServiceError("FOLDER_NOT_FOUND", "文件夹不存在", 404)
                    for item in sorted(rows, key=lambda row: str(row["id"])):
                        file_id = str(item["id"])
                        row = self.repository.get_linked_file(connection, file_id=file_id,
                                                              object_type="PROJECT", object_id=project_id, lock=True)
                        if row is None or row["status"] != "ACTIVE":
                            raise FileServiceError("FILE_IDENTITY_CONFLICT", "文件已变化，请刷新后重试", 409)
                        self.repository.delete_project_path(
                            connection, project_id=project_id, file_id=file_id,
                            filepath=item["purpose"][len(PROJECT_PATH_PREFIX):], actor_user_id=actor_user_id,
                        )
                        if not self.repository.has_live_links(connection, file_id=file_id):
                            if not self.repository.archive_file(connection, file_id=file_id,
                                                                expected_version=int(row["version"]), actor_user_id=actor_user_id):
                                raise FileServiceError("VERSION_CONFLICT", "文件版本已变化", 409)
                        self._audit(connection, operation="DELETE", staged=None, file_id=file_id,
                                    actor_user_id=actor_user_id, request_id=request_id, started=started)
                    self.audit_service.record(
                        connection, event_name="file_operation_completed", user_id=actor_user_id,
                        object_type="PROJECT", object_id=project_id, result="SUCCESS", request_id=request_id,
                        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                        properties={"operation": "DELETE_FOLDER", "file_type": "directory", "size_bucket": "N/A"},
                    )
            return {"deletedFileCount": len(rows)}
        except FileServiceError:
            raise
        except Exception as exc:
            raise FileServiceError("FILE_OPERATION_FAILED", "目录删除失败，请检查后重试", 500) from exc

    def rename_project_path(self, project_id: str, filepath: str, new_name: str,
                            *, expected_file_id: str, actor_user_id: int, request_id: str) -> dict | None:
        name, extension = self._validate_name(new_name)
        purpose = "PROJECT_TREE:" + filepath
        folder, _, _old_name = filepath.rpartition("/")
        new_path = (folder + "/" if folder else "") + name
        started = time.monotonic()
        try:
            with self.repository.engine.begin() as connection:
                self._lock_object_write(connection, "PROJECT", str(project_id))
                row = self.repository.get_project_path_file(connection, project_id=project_id, purpose=purpose)
                if row is None:
                    return None
                if str(row["id"]) != expected_file_id:
                    raise FileServiceError("FILE_IDENTITY_CONFLICT", "文件已变化，请刷新后重试", 409)
                row = self.repository.get_linked_file(connection, file_id=str(row["id"]),
                                                      object_type="PROJECT", object_id=project_id, lock=True)
                if row["status"] != "ACTIVE":
                    raise FileServiceError("FILE_ARCHIVED", "文件已归档", 409)
                if extension != Path(row["original_name"]).suffix.lower():
                    raise FileServiceError("FILE_TYPE_MISMATCH", "不能通过改名改变文件类型", 415)
                if new_path != filepath:
                    if new_path in self.repository.list_deleted_project_paths(connection, project_id=project_id):
                        raise FileServiceError("FILE_NAME_CONFLICT", "文件路径存在历史记录", 409)
                    if self.repository.has_multiple_links(connection, file_id=str(row["id"])):
                        raise FileServiceError("FILE_SHARED", "文件被多处引用，不能在单个项目内改名", 409)
                    if self.repository.get_project_path_file(
                        connection, project_id=project_id, purpose="PROJECT_TREE:" + new_path,
                    ) is not None:
                        raise FileServiceError("FILE_NAME_CONFLICT", "文件名已存在", 409)
                    self.repository.rename_project_path(
                        connection, project_id=project_id, file_id=str(row["id"]),
                        purpose=purpose, new_purpose="PROJECT_TREE:" + new_path,
                        original_name=name, actor_user_id=actor_user_id,
                    )
                self._audit(connection, operation="RENAME", staged=None, file_id=str(row["id"]),
                            actor_user_id=actor_user_id, request_id=request_id, started=started)
                return {"fileId": str(row["id"]), "path": new_path, "versionNo": int(row["version"])}
        except FileServiceError:
            raise
        except Exception as exc:
            raise FileServiceError("FILE_OPERATION_FAILED", "文件改名失败", 500) from exc

    def list_project_paths(self, project_id: str) -> list[dict]:
        self._validate_object("PROJECT", str(project_id))
        with self.repository.engine.connect() as connection:
            rows = self.repository.list_project_paths(connection, project_id=str(project_id))
        return [{
            "fileId": str(row["id"]), "versionNo": int(row["version"]),
            "path": row["purpose"][len("PROJECT_TREE:"):],
            "name": row["original_name"], "size": row["size_bytes"],
            "modified": "", "has_history": "true" if row["version"] > 1 else "false",
            "current_version": f"v{row['version']}",
        } for row in rows]

    def upload_project_path(
        self, stream, *, original_name: str, folder: str, project_id: str,
        actor_user_id: int, request_id: str, legacy_sources=None,
    ) -> dict:
        """Append to a logical project file while retaining every controlled version."""
        parts = folder.split("/") if isinstance(folder, str) and folder else []
        if (not isinstance(folder, str) or not isinstance(original_name, str)
                or not original_name or "/" in original_name or "\\" in original_name
                or "\\" in folder
                or any(part in {"", ".", ".."} or part.casefold() == ".history" for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in folder + original_name)):
            raise FileServiceError("INVALID_PROJECT_PATH", "非法项目文件路径", 400)
        self._validate_object_write("PROJECT", str(project_id))
        staged = self._stage(stream, original_name)
        return self._upload_staged(
            staged, object_type="PROJECT", object_id=str(project_id),
            actor_user_id=actor_user_id, request_id=request_id,
            project_path="PROJECT_TREE:" + "/".join([*parts, staged.original_name]),
            legacy_sources=legacy_sources,
        )

    def upload_new_object(
        self, stream, *, original_name: str, object_type: str, object_id: str,
        create_metadata, actor_user_id: int, request_id: str,
    ) -> dict:
        """Create a business object and its first controlled file in one transaction.

        ``create_metadata`` receives a restricted active-transaction writer. It
        supports SQLAlchemy metadata statements but cannot control the transaction.
        """
        object_type = str(object_type or "").upper()
        staged = self._stage(stream, original_name)
        return self._upload_staged(
            staged, object_type=object_type, object_id=str(object_id),
            actor_user_id=actor_user_id, request_id=request_id,
            create_metadata=create_metadata,
        )

    def upload_expense_documents_atomic(
        self, items, *, object_id: str, actor_user_id: int, request_id: str,
        finalize_metadata,
    ) -> list[dict]:
        """Persist a generated document set and its expense state in one transaction."""
        staged_items = []
        moved_paths = []
        try:
            for stream, original_name in items:
                staged = self._stage(stream, original_name)
                relative_path, final_path = self._destination(staged.extension)
                staged_items.append((staged, relative_path, final_path))
            results = []
            with self.repository.engine.begin() as connection:
                status = self.repository.lock_expense_for_document_generation(connection, str(object_id))
                if status is None:
                    raise FileServiceError("OBJECT_NOT_FOUND", "关联业务对象不存在", 404)
                if status not in {"已确认", "已生成文档"}:
                    raise FileServiceError("OBJECT_READ_ONLY", "请先确认报销项", 409)
                for staged, relative_path, final_path in staged_items:
                    file_id = uuid.uuid4()
                    self.repository.create_file(
                        connection, file_id=file_id, business_id=f"FILE-{uuid.uuid4().hex.upper()}",
                        original_name=staged.original_name, media_type=staged.media_type,
                        actor_user_id=actor_user_id,
                    )
                    self.repository.create_version(
                        connection, version_id=uuid.uuid4(), file_id=file_id, version_no=1,
                        storage_path=relative_path, sha256=staged.sha256,
                        size_bytes=staged.size_bytes, media_type=staged.media_type,
                        actor_user_id=actor_user_id,
                    )
                    self.repository.link_object(
                        connection, link_id=uuid.uuid4(), object_type="EXPENSE",
                        object_id=str(object_id), file_id=file_id, actor_user_id=actor_user_id,
                    )
                    os.replace(staged.path, final_path)
                    moved_paths.append(final_path)
                    self._audit(
                        connection, operation="UPLOAD", staged=staged, file_id=str(file_id),
                        actor_user_id=actor_user_id, request_id=request_id, started=time.monotonic(),
                    )
                    results.append(self._result(str(file_id), staged, relative_path, 1))
                finalize_metadata(connection)
            return results
        except Exception:
            for path in moved_paths:
                path.unlink(missing_ok=True)
            raise
        finally:
            for staged, _relative, _final in staged_items:
                staged.path.unlink(missing_ok=True)

    def add_version(self, file_id: str, stream, *, original_name: str, object_type: str, object_id: str, expected_version: int, actor_user_id: int, request_id: str) -> dict:
        object_type = str(object_type or "").upper()
        self._validate_object_write(object_type, str(object_id))
        staged = self._stage(stream, original_name)
        relative_path, final_path = self._destination(staged.extension)
        started = time.monotonic()
        moved = False
        try:
            with self.repository.engine.begin() as connection:
                self._lock_object_write(connection, object_type, str(object_id))
                file_row = self.repository.get_linked_file(
                    connection, file_id=file_id, object_type=object_type, object_id=str(object_id), lock=True
                )
                if not file_row:
                    raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
                if file_row["status"] != "ACTIVE":
                    raise FileServiceError("FILE_ARCHIVED", "文件已归档", 409)
                if int(file_row["version"]) != int(expected_version):
                    raise FileServiceError("VERSION_CONFLICT", "文件版本已变化", 409)
                if staged.extension != Path(file_row["original_name"]).suffix.lower() or staged.media_type != file_row["media_type"]:
                    raise FileServiceError("FILE_TYPE_MISMATCH", "新版本必须与原文件类型一致", 415)
                if not self.repository.bump_file(
                    connection,
                    file_id=file_id,
                    expected_version=int(expected_version),
                    actor_user_id=actor_user_id,
                    original_name=staged.original_name,
                    media_type=staged.media_type,
                ):
                    raise FileServiceError("VERSION_CONFLICT", "文件版本已变化", 409)
                version_no = int(expected_version) + 1
                self.repository.create_version(
                    connection,
                    version_id=uuid.uuid4(),
                    file_id=file_id,
                    version_no=version_no,
                    storage_path=relative_path,
                    sha256=staged.sha256,
                    size_bytes=staged.size_bytes,
                    media_type=staged.media_type,
                    actor_user_id=actor_user_id,
                )
                os.replace(staged.path, final_path)
                moved = True
                self._audit(
                    connection,
                    operation="ADD_VERSION",
                    staged=staged,
                    file_id=file_id,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    started=started,
                )
            return self._result(file_id, staged, relative_path, version_no)
        except FileServiceError:
            if moved:
                final_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if moved:
                final_path.unlink(missing_ok=True)
            raise FileServiceError("FILE_OPERATION_FAILED", "文件操作失败", 500) from exc
        finally:
            staged.path.unlink(missing_ok=True)

    def archive(self, file_id: str, *, object_type: str, object_id: str, actor_user_id: int, request_id: str) -> dict:
        object_type = str(object_type or "").upper()
        self._validate_object_write(object_type, str(object_id))
        started = time.monotonic()
        try:
            with self.repository.engine.begin() as connection:
                self._lock_object_write(connection, object_type, str(object_id))
                row = self.repository.get_linked_file(
                    connection,
                    file_id=file_id,
                    object_type=object_type,
                    object_id=str(object_id),
                    lock=True,
                )
                if not row:
                    raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
                if row["status"] == "ARCHIVED":
                    self._audit(
                        connection, operation="ARCHIVE_NOOP", staged=None,
                        file_id=file_id, actor_user_id=actor_user_id,
                        request_id=request_id, started=started,
                    )
                    return {"fileId": file_id, "status": "ARCHIVED"}
                if self.repository.has_multiple_links(connection, file_id=file_id):
                    raise FileServiceError("FILE_SHARED", "文件被多处引用，不能单独归档", 409)
                if not self.repository.archive_file(
                    connection,
                    file_id=file_id,
                    expected_version=int(row["version"]),
                    actor_user_id=actor_user_id,
                ):
                    raise FileServiceError("VERSION_CONFLICT", "文件版本已变化", 409)
                self._audit(
                    connection,
                    operation="ARCHIVE",
                    staged=None,
                    file_id=file_id,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    started=started,
                )
            return {"fileId": file_id, "status": "ARCHIVED"}
        except FileServiceError:
            raise
        except Exception as exc:
            raise FileServiceError("FILE_OPERATION_FAILED", "文件操作失败", 500) from exc

    def _safe_stored_path(self, storage_path: str) -> Path:
        decoded = str(storage_path or "")
        for _ in range(3):
            candidate = unquote(decoded)
            if candidate == decoded:
                break
            decoded = candidate
        if not decoded or "\\" in decoded:
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
        relative = Path(decoded)
        if relative.is_absolute() or ".." in relative.parts:
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
        root = self.storage_root.resolve()
        candidate = self.storage_root / relative
        try:
            if os.path.commonpath((str(root), str(candidate.resolve(strict=False)))) != str(root):
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            current = self.storage_root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            details = candidate.stat()
            if not stat.S_ISREG(details.st_mode):
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
        except (OSError, ValueError):
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
        return candidate.resolve()

    def open_version(self, file_id: str, version_no: int, *, object_type: str, object_id: str) -> dict:
        try:
            version_no = int(version_no)
        except (TypeError, ValueError):
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
        object_type = str(object_type or "").upper()
        with self.repository.engine.connect() as connection:
            if not self.repository.object_exists(connection, object_type, str(object_id)):
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            file_row = self.repository.get_linked_file(
                connection,
                file_id=file_id,
                object_type=object_type,
                object_id=str(object_id),
            )
            if not file_row:
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            if file_row["status"] != "ACTIVE":
                raise FileServiceError("FILE_ARCHIVED", "文件已归档", 409)
            version = self.repository.get_version(connection, file_id=file_id, version_no=version_no)
        if not version:
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
        path = self._safe_stored_path(version["storage_path"])
        return {
            "fileId": file_id,
            "versionNo": version_no,
            "originalName": file_row["original_name"],
            "mediaType": version["media_type"],
            "sizeBytes": int(version["size_bytes"]),
            "sha256": version["sha256"],
            "path": path,
        }

    def count_versions(self, file_id: str, *, object_type: str, object_id: str) -> int:
        object_type = str(object_type or "").upper()
        with self.repository.engine.connect() as connection:
            if not self.repository.object_exists(connection, object_type, str(object_id)):
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            file_row = self.repository.get_linked_file(
                connection,
                file_id=file_id,
                object_type=object_type,
                object_id=str(object_id),
            )
            if not file_row:
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            return self.repository.count_versions(connection, file_id=file_id)

    def open_version_stream(self, file_id: str, version_no: int, *, object_type: str, object_id: str) -> dict:
        opened = self.open_version(
            file_id, version_no, object_type=object_type, object_id=object_id
        )
        if os.name == "nt":
            return self._open_version_stream_windows(opened)
        relative = opened["path"].relative_to(self.storage_root.resolve())
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptors = []
        try:
            current = os.open(self.storage_root.resolve(), directory_flags)
            descriptors.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                descriptors.append(current)
            file_descriptor = os.open(relative.parts[-1], file_flags, dir_fd=current)
            details = os.fstat(file_descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size != opened["sizeBytes"]:
                os.close(file_descriptor)
                raise FileServiceError("FILE_INTEGRITY_FAILED", "文件完整性校验失败", 409)
            stream = os.fdopen(file_descriptor, "rb")
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != opened["sha256"]:
                stream.close()
                raise FileServiceError("FILE_INTEGRITY_FAILED", "文件完整性校验失败", 409)
            stream.seek(0)
            opened["stream"] = stream
            return opened
        except FileServiceError:
            raise
        except (OSError, ValueError) as exc:
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404) from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _open_version_stream_windows(self, opened: dict) -> dict:
        """Open once, then verify the Windows handle's final resolved path."""
        try:
            import ctypes
            import msvcrt

            stream = opened["path"].open("rb")
            handle = msvcrt.get_osfhandle(stream.fileno())
            buffer = ctypes.create_unicode_buffer(32_768)
            length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                stream.close()
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            final_path = buffer.value
            if final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            root = os.path.normcase(str(self.storage_root.resolve()))
            resolved = os.path.normcase(str(Path(final_path).resolve()))
            if os.path.commonpath((root, resolved)) != root:
                stream.close()
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size != opened["sizeBytes"]:
                stream.close()
                raise FileServiceError("FILE_INTEGRITY_FAILED", "文件完整性校验失败", 409)
            digest = hashlib.sha256()
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != opened["sha256"]:
                stream.close()
                raise FileServiceError("FILE_INTEGRITY_FAILED", "文件完整性校验失败", 409)
            stream.seek(0)
            opened["stream"] = stream
            return opened
        except FileServiceError:
            raise
        except (OSError, ValueError, NotImplementedError) as exc:
            raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404) from exc

    def list_versions(self, file_id: str, *, object_type: str, object_id: str) -> dict:
        object_type = str(object_type or "").upper()
        with self.repository.engine.connect() as connection:
            if not self.repository.object_exists(connection, object_type, str(object_id)):
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            file_row = self.repository.get_linked_file(
                connection, file_id=file_id, object_type=object_type, object_id=str(object_id),
            )
            if not file_row:
                raise FileServiceError("FILE_NOT_FOUND", "文件不存在", 404)
            if file_row["status"] != "ACTIVE":
                raise FileServiceError("FILE_ARCHIVED", "文件已归档", 409)
            versions = self.repository.list_versions(connection, file_id=file_id)
        return {
            "fileId": str(file_row["id"]), "originalName": file_row["original_name"],
            "versionNo": int(file_row["version"]),
            "versions": [{"versionNo": int(row["version_no"]),
                          "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
                          "sizeBytes": int(row["size_bytes"]), "mediaType": row["media_type"]}
                         for row in versions],
        }

    def list_for_object(self, *, object_type: str, object_id: str) -> list[dict]:
        object_type = str(object_type or "").upper()
        self._validate_object(object_type, str(object_id))
        with self.repository.engine.connect() as connection:
            rows = self.repository.list_for_object(connection, object_type=object_type, object_id=str(object_id))
        return [
            {
                "fileId": str(row["id"]),
                "businessId": row["business_id"],
                "originalName": row["original_name"],
                "mediaType": row["media_type"],
                "versionNo": int(row["version"]),
                "status": row["status"],
                "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
            }
            for row in rows
        ]
