from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SourceSafetyError(RuntimeError):
    pass


class BatchConflict(RuntimeError):
    pass


class StructuralMigrationError(RuntimeError):
    pass


class ConversionIssue(ValueError):
    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


SOURCE_NAMES = ("research.db", "generic_tables.db", "expense.db")
SOURCE_TABLE_CONTRACT = {
    "research.db": {
        "users", "projects", "security_projects", "crypto_projects", "equipment",
        "knowledge_subclasses", "standards", "experts", "doc_templates",
        "project_documents", "document_versions", "expert_groups",
        "expert_group_members", "equipment_groups", "equipment_group_members",
        "host_devices", "host_device_categories", "device_host_relations",
        "research_units", "llm_models", "inference_server_status",
    },
    "generic_tables.db": {
        "generic_tables", "generic_table_versions", "generic_table_columns",
        "generic_table_data",
    },
    "expense.db": {
        "expense_reimbursement", "expense_invoice", "expense_payment",
        "expense_invoice_item",
    },
}
EXPLICIT_SKIPPED_TABLES = {"research.db": {"inference_server_status"}}
FIELD_SKIP_CONTRACT = {"llm_models": {"file_path", "api_key"}}
REQUIRED_TABLES = {
    "research.db": {
        "users", "projects", "security_projects", "crypto_projects", "equipment",
        "standards", "experts", "doc_templates", "project_documents",
        "document_versions",
    },
    "generic_tables.db": {
        "generic_tables", "generic_table_versions", "generic_table_columns",
        "generic_table_data",
    },
    "expense.db": {
        "expense_reimbursement", "expense_invoice", "expense_payment",
        "expense_invoice_item",
    },
}
REQUIRED_FIELDS = {
    "users": {"id", "username", "password", "role", "status"},
    "projects": {"id", "project_id", "name"},
    "security_projects": {"id", "project_id", "name"},
    "crypto_projects": {"id", "project_id", "name"},
    "equipment": {"id", "name"}, "standards": {"id", "name"},
    "experts": {"id", "expert_id", "name"},
    "doc_templates": {"id", "template_id", "name"},
    "project_documents": {"id", "doc_id", "project_id"},
    "document_versions": {"id", "version_id", "doc_id"},
    "generic_tables": {"id", "table_id", "name"},
    "generic_table_versions": {"id", "version_id", "table_id", "version_number"},
    "generic_table_columns": {"id", "version_id", "col_key", "col_name"},
    "generic_table_data": {"id", "version_id", "row_key", "row_data"},
    "expense_reimbursement": {"id", "total_amount"},
    "expense_invoice": {"id", "reimbursement_id", "amount"},
    "expense_payment": {"id", "reimbursement_id", "amount"},
    "expense_invoice_item": {"id", "invoice_id", "amount"},
}
TABLE_ORDER = (
    "users", "knowledge_subclasses", "host_device_categories", "research_units",
    "projects", "security_projects", "crypto_projects", "equipment", "standards",
    "experts", "llm_models", "expert_groups", "equipment_groups",
    "host_devices", "expert_group_members", "equipment_group_members",
    "device_host_relations", "project_documents", "document_versions",
    "generic_tables", "generic_table_versions", "generic_table_columns",
    "generic_table_data", "expense_reimbursement", "expense_invoice",
    "expense_payment", "expense_invoice_item", "project_registry",
)
SKIPPED_TABLES = {"inference_server_status"}
SENSITIVE_FIELDS = re.compile(
    r"password|api[_-]?key|token|secret|bank[_-]?card|id[_-]?card|phone", re.I
)
PATH_FIELDS = {
    "file_path", "word_file_path", "equipment_image", "related_files", "documents"
}
DECIMAL_FIELDS = {
    "price", "total_amount", "amount", "tax_amount", "price_ex_tax", "quantity",
    "unit_price", "temperature", "model_load_time", "avg_latency_ms",
    "memory_usage_mb", "cpu_usage_percent",
}
DATE_FIELDS = {"start_date", "planned_end_date", "actual_end_date", "date", "pay_date", "departure_date", "decision_date"}
DATETIME_FIELDS = {"created_at", "updated_at", "changed_at", "selected_at", "upload_time", "confirmed_at", "departure_time", "recorded_at", "closed_at"}
BOOL_FIELDS = {"is_paid", "is_locked", "is_active", "must_change_password"}
JSON_FIELDS = {
    "directory_permissions", "chapter_tree", "row_data", "col_options", "documents",
    "related_files", "matched_payment_ids", "matched_invoice_ids",
}
NATURAL_KEYS = {
    "users": ("username",), "projects": ("project_id",),
    "security_projects": ("project_id",), "crypto_projects": ("project_id",),
    "experts": ("expert_id",), "doc_templates": ("template_id",),
    "generic_tables": ("table_id",), "expense_reimbursement": ("reimbursement_no",),
    "equipment": ("equipment_id",), "standards": ("doc_id",),
    "project_documents": ("doc_id",), "document_versions": ("version_id",),
    "expert_groups": ("group_id",), "equipment_groups": ("group_id",),
    "host_devices": ("host_id",), "research_units": ("unit_id",),
}
MIGRATION_KEY_FIELDS = {
    **NATURAL_KEYS,
    "equipment": ("id",), "standards": ("id",),
    "expense_reimbursement": ("id",), "expense_invoice": ("id",),
    "expense_payment": ("id",), "expense_invoice_item": ("id",),
    "expert_group_members": ("id",), "equipment_group_members": ("id",),
    "device_host_relations": ("id",), "generic_table_columns": ("id",),
    "generic_table_data": ("id",), "llm_models": ("id",),
    "project_registry": ("id",),
}
PARENT_KEYS = {
    "expert_group_members": ("expert_groups", "group_id", "group_id"),
    "equipment_group_members": ("equipment_groups", "group_id", "group_id"),
    "generic_table_versions": ("generic_tables", "table_id", "table_id"),
    "generic_table_columns": ("generic_table_versions", "version_id", "version_id"),
    "generic_table_data": ("generic_table_versions", "version_id", "version_id"),
    "expense_invoice": ("expense_reimbursement", "reimbursement_id", "id"),
    "expense_payment": ("expense_reimbursement", "reimbursement_id", "id"),
    "expense_invoice_item": ("expense_invoice", "invoice_id", "id"),
    "document_versions": ("project_documents", "doc_id", "doc_id"),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_from_handle(path: Path) -> SourceIdentity:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
            or current.st_dev != after.st_dev or current.st_ino != after.st_ino
            or current.st_size != after.st_size or current.st_mtime_ns != after.st_mtime_ns
        ):
            raise SourceSafetyError(f"file identity changed while reading: {path.name}")
        return SourceIdentity(
            device=after.st_dev, inode=after.st_ino, size=after.st_size,
            mtime_ns=after.st_mtime_ns, sha256=digest.hexdigest(),
        )
    finally:
        os.close(descriptor)


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _copy_bound_file(
    source: Path,
    destination: Path,
    expected: SourceIdentity | None = None,
    *,
    source_dir_fd: int | None = None,
    expected_stat: os.stat_result | None = None,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if source_dir_fd is None:
        source_fd = os.open(source, flags)
    else:
        source_fd = os.open(source.name, flags, dir_fd=source_dir_fd)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        before = os.fstat(source_fd)
        digest = hashlib.sha256()
        while chunk := os.read(source_fd, 1024 * 1024):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        current = (
            source.lstat()
            if source_dir_fd is None
            else os.stat(source.name, dir_fd=source_dir_fd, follow_symlinks=False)
        )
        observed = SourceIdentity(
            device=after.st_dev, inode=after.st_ino, size=after.st_size,
            mtime_ns=after.st_mtime_ns, sha256=digest.hexdigest(),
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or not _same_stat(before, after)
            or not _same_stat(current, after)
            or (expected_stat is not None and not _same_stat(expected_stat, before))
            or (expected is not None and observed != expected)
        ):
            raise SourceSafetyError(f"file identity changed while snapshotting: {source.name}")
    finally:
        os.close(source_fd)
        os.close(destination_fd)


@contextmanager
def _bound_snapshot(
    source_root: Path,
    sources: tuple[LegacySource, ...],
    *,
    expected_root_stat: os.stat_result | None = None,
) -> Iterable[Path]:
    temporary = Path(tempfile.mkdtemp(prefix="rm-v1-legacy-snapshot-")).resolve()
    os.chmod(temporary, 0o700)
    identities = {
        source.path.relative_to(source_root): source.identity for source in sources
    }
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    expected_root_stat = expected_root_stat or source_root.lstat()
    root_fd = os.open(source_root, directory_flags)
    if (
        not stat.S_ISDIR(expected_root_stat.st_mode)
        or not _same_stat(expected_root_stat, os.fstat(root_fd))
    ):
        os.close(root_fd)
        shutil.rmtree(temporary, ignore_errors=True)
        raise SourceSafetyError("legacy source root identity changed before snapshot")

    def copy_directory(directory_fd: int, relative: Path) -> None:
        for name in sorted(os.listdir(directory_fd)):
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            source = source_root / relative / name
            child_relative = relative / name
            if stat.S_ISLNK(entry.st_mode):
                raise SourceSafetyError("symlink is forbidden in legacy source snapshot")
            if stat.S_ISDIR(entry.st_mode):
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    if not _same_stat(entry, os.fstat(child_fd)):
                        raise SourceSafetyError(
                            f"directory identity changed while snapshotting: {name}"
                        )
                    copy_directory(child_fd, child_relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(entry.st_mode):
                raise SourceSafetyError(
                    f"non-regular file is forbidden in legacy source snapshot: {name}"
                )
            if any(name.endswith(suffix) for suffix in ("-wal", "-shm", "-journal")):
                raise SourceSafetyError("SQLite sidecar appeared during snapshot")
            _copy_bound_file(
                source,
                temporary / child_relative,
                identities.get(child_relative),
                source_dir_fd=directory_fd,
                expected_stat=entry,
            )

    try:
        copy_directory(root_fd, Path())
        yield temporary
    finally:
        os.close(root_fd)
        shutil.rmtree(temporary, ignore_errors=True)


@dataclass(frozen=True)
class SourceIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


SourceCustody = dict[str, tuple[int, int, int, int, int, str | None]]


def _capture_source_custody(root: str | Path) -> SourceCustody:
    root_path = Path(root).absolute()
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    custody: SourceCustody = {}

    def remember(
        relative: str, details: os.stat_result, digest: str | None = None
    ) -> None:
        custody[relative] = (
            details.st_dev, details.st_ino, stat.S_IFMT(details.st_mode),
            details.st_size, details.st_mtime_ns, digest,
        )

    descriptors = [os.open("/", directory_flags)]
    try:
        for part in root_path.parts[1:]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        root_fd = descriptors[-1]
        root_details = os.fstat(root_fd)
        if not stat.S_ISDIR(root_details.st_mode):
            raise SourceSafetyError("legacy source root must be a real directory")
        remember(".", root_details)

        def walk(directory_fd: int, relative: Path) -> None:
            for name in sorted(os.listdir(directory_fd)):
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                child_relative = relative / name
                logical = child_relative.as_posix()
                if stat.S_ISLNK(before.st_mode):
                    raise SourceSafetyError("symlink is forbidden in legacy source custody")
                if stat.S_ISDIR(before.st_mode):
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if not _same_stat(before, opened):
                            raise SourceSafetyError(
                                f"directory identity changed during custody check: {logical}"
                            )
                        remember(logical, opened)
                        walk(child_fd, child_relative)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(before.st_mode):
                    raise SourceSafetyError(
                        f"non-regular file is forbidden in legacy source custody: {logical}"
                    )
                file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(file_fd)
                    if not _same_stat(before, opened):
                        raise SourceSafetyError(
                            f"file identity changed during custody check: {logical}"
                        )
                    digest = hashlib.sha256()
                    while chunk := os.read(file_fd, 1024 * 1024):
                        digest.update(chunk)
                    after = os.fstat(file_fd)
                    if not _same_stat(opened, after):
                        raise SourceSafetyError(
                            f"file identity changed during custody check: {logical}"
                        )
                    remember(logical, after, digest.hexdigest())
                finally:
                    os.close(file_fd)

        walk(root_fd, Path())
    except OSError as exc:
        raise SourceSafetyError("legacy source custody path is unsafe") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return custody


def _assert_source_custody(
    root: str | Path, expected: SourceCustody
) -> None:
    observed = _capture_source_custody(root)
    for logical in observed:
        sidecars = (("-wal", "WAL"), ("-shm", "SHM"), ("-journal", "journal"))
        for suffix, label in sidecars:
            if logical.endswith(suffix):
                raise SourceSafetyError(
                    f"active {label} state appeared during migration: {logical}"
                )
    if observed != expected:
        raise SourceSafetyError("legacy source identity changed during migration")


@dataclass(frozen=True)
class LegacySource:
    name: str
    path: Path
    identity: SourceIdentity

    @property
    def uri(self) -> str:
        return f"file:{quote(str(self.path), safe='/')}?mode=ro&immutable=1"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise SourceSafetyError(f"query_only could not be enabled for {self.name}")
        quick = connection.execute("PRAGMA quick_check").fetchall()
        if [row[0] for row in quick] != ["ok"]:
            connection.close()
            raise SourceSafetyError(f"quick_check failed for {self.name}")
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            connection.close()
            raise SourceSafetyError(f"foreign_key_check failed for {self.name}")
        return connection


def discover_sources(root: str | Path) -> tuple[LegacySource, ...]:
    root_path = Path(root)
    if not root_path.is_dir() or root_path.is_symlink():
        raise SourceSafetyError("legacy source root must be a real directory")
    if any(parent.is_symlink() for parent in root_path.parents):
        raise SourceSafetyError("legacy source directory chain must not contain symlinks")
    candidates = [p for p in root_path.iterdir() if p.is_file() or p.is_symlink()]
    result: list[LegacySource] = []
    for expected in SOURCE_NAMES:
        stem = Path(expected).stem
        matches = [p for p in candidates if p.suffix == ".db" and p.stem.startswith(stem)]
        if not matches:
            raise SourceSafetyError(f"missing required legacy database: {expected}")
        canonical = root_path / expected
        if canonical.is_symlink():
            raise SourceSafetyError(f"symlink database is forbidden: {expected}")
        if len(matches) != 1:
            raise SourceSafetyError(f"duplicate candidates for {expected}")
        path = matches[0]
        if path.name != expected:
            raise SourceSafetyError(f"missing required legacy database: {expected}")
        if path.is_symlink():
            raise SourceSafetyError(f"symlink database is forbidden: {expected}")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(f"{path}{suffix}").exists():
                label = "WAL" if suffix == "-wal" else "SHM" if suffix == "-shm" else "journal"
                raise SourceSafetyError(f"active {label} state is forbidden: {expected}")
        source = LegacySource(expected, path, _identity_from_handle(path))
        with source.connect():
            pass
        result.append(source)
    sources = tuple(result)
    _validate_source_tables(sources)
    return sources


def _source_table_names(source: LegacySource) -> set[str]:
    with source.connect() as db:
        return {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def _validate_source_tables(sources: tuple[LegacySource, ...]) -> None:
    for source in sources:
        names = _source_table_names(source)
        missing = REQUIRED_TABLES[source.name] - names
        if missing:
            raise SourceSafetyError(
                f"missing required source table in {source.name}: {sorted(missing)!r}"
            )
        unknown = names - SOURCE_TABLE_CONTRACT[source.name]
        if unknown:
            raise SourceSafetyError(
                f"unknown source table in {source.name}: {sorted(unknown)!r}"
            )
        for table_name in sorted(names):
            required_fields = REQUIRED_FIELDS.get(table_name, set())
            if not required_fields:
                continue
            missing_fields = required_fields - _source_columns(source, table_name)
            if missing_fields:
                raise SourceSafetyError(
                    f"missing required source field in {table_name}: {sorted(missing_fields)!r}"
                )
        with source.connect() as db:
            required_row_count = sum(
                db.execute(
                    f'SELECT count(*) FROM "{table_name.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
                for table_name in REQUIRED_TABLES[source.name]
            )
        if required_row_count == 0:
            raise SourceSafetyError(f"required source database is empty: {source.name}")


def _assert_sources_unchanged(sources: tuple[LegacySource, ...]) -> None:
    for source in sources:
        if source.path.parent.is_symlink() or any(
            parent.is_symlink() for parent in source.path.parent.parents
        ):
            raise SourceSafetyError("legacy source directory chain changed to a symlink")
        uploads = source.path.parent / "uploads"
        if uploads.is_symlink():
            raise SourceSafetyError("uploads root changed to a symlink")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(f"{source.path}{suffix}").exists():
                label = "WAL" if suffix == "-wal" else "SHM" if suffix == "-shm" else "journal"
                raise SourceSafetyError(f"active {label} state appeared during migration: {source.name}")
        current = source.path.stat()
        identity = source.identity
        if (
            current.st_dev != identity.device or current.st_ino != identity.inode
            or current.st_size != identity.size or current.st_mtime_ns != identity.mtime_ns
            or _sha_file(source.path) != identity.sha256
        ):
            raise SourceSafetyError(f"legacy source identity changed during migration: {source.name}")


@contextmanager
def _prepared_snapshot(source_root: str | Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    original_root = Path(source_root)
    original_root_stat = original_root.lstat()
    original_sources = discover_sources(original_root)
    with _bound_snapshot(
        original_root,
        original_sources,
        expected_root_stat=original_root_stat,
    ) as snapshot_root:
        snapshot_sources = discover_sources(snapshot_root)
        manifest = build_manifest(snapshot_root, _sources=snapshot_sources)
        yield snapshot_root, manifest


def _fresh_bound_manifest(source_root: str | Path) -> dict[str, Any]:
    with _prepared_snapshot(source_root) as (_, manifest):
        return manifest


def _schema_and_rows(source: LegacySource) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    with source.connect() as db:
        schema_rows = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        schema = [dict(row) for row in schema_rows]
        tables = [row["name"] for row in schema_rows if row["type"] == "table"]
        row_hashes: list[dict[str, Any]] = []
        references: list[dict[str, str]] = []
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({quoted})")]
            order = " ORDER BY " + ",".join('"' + c.replace('"', '""') + '"' for c in columns) if columns else ""
            rows = [dict(r) for r in db.execute(f"SELECT * FROM {quoted}{order}")]
            row_hashes.append({"table": table, "count": len(rows), "sha256": _sha_bytes(_canonical_json(rows).encode())})
            for row in rows:
                for field in PATH_FIELDS & row.keys():
                    if table == "llm_models":
                        continue
                    if table == "expense_reimbursement" and field == "documents":
                        continue
                    references.extend(
                        {"path": value, "source": f"{source.name}:{table}.{field}"}
                        for value in _path_values(
                            row[field], json_only=field in {"documents", "related_files"}
                        )
                    )
    return _sha_bytes(_canonical_json(schema).encode()), row_hashes, references


def _path_values(value: Any, *, json_only: bool = False) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if json_only or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return []
            return [str(item) for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []
        return [stripped]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _safe_relative(root: Path, raw: str) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise SourceSafetyError("invalid attachment path")
    posix = PurePosixPath(raw.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts:
        raise SourceSafetyError("attachment path escapes source root")
    relative = posix.as_posix()
    candidate = root.joinpath(*posix.parts)
    cursor = root
    for part in posix.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SourceSafetyError("symlink attachment is forbidden")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise SourceSafetyError("attachment path escapes source root") from exc
    return relative, candidate


def inventory_attachments(
    root: str | Path,
    references: Iterable[str | dict[str, str]],
    *,
    tolerate_invalid: bool = False,
) -> dict[str, Any]:
    root_path = Path(root)
    uploads = root_path / "uploads"
    if uploads.is_symlink() or (uploads.exists() and not uploads.is_dir()):
        raise SourceSafetyError("uploads root must be a real directory")
    normalized: list[tuple[str, Path, str]] = []
    invalid_references: list[dict[str, Any]] = []
    for item in references:
        raw = item["path"] if isinstance(item, dict) else item
        source = item.get("source", "requested") if isinstance(item, dict) else "requested"
        try:
            relative, path = _safe_relative(root_path, raw)
        except SourceSafetyError:
            if not tolerate_invalid:
                raise
            invalid_references.append({
                "source": source, "value": _safe_raw("file_path", raw)
            })
            continue
        normalized.append((relative, path, source))
    by_path: dict[str, tuple[Path, set[str]]] = {}
    for relative, path, source in normalized:
        existing = by_path.setdefault(relative, (path, set()))
        existing[1].add(source)
    referenced = sorted(by_path.items())
    files: list[dict[str, Any]] = []
    hash_counts: dict[str, int] = {}
    present_paths: set[str] = set()
    for relative, (path, source_names) in referenced:
        entry: dict[str, Any] = {"path": relative, "sources": sorted(source_names), "exists": False, "size": None, "sha256": None}
        if path.is_file():
            digest = _sha_file(path)
            entry.update({"exists": True, "size": path.stat().st_size, "sha256": digest})
            hash_counts[digest] = hash_counts.get(digest, 0) + 1
            present_paths.add(relative)
        files.append(entry)
    all_files: set[str] = set()
    unreferenced_entries: list[dict[str, Any]] = []
    if uploads.exists():
        for path in uploads.rglob("*"):
            if path.is_symlink():
                raise SourceSafetyError("symlink attachment is forbidden")
            if path.is_file():
                all_files.add(path.relative_to(root_path).as_posix())
    referenced_paths = {item[0] for item in referenced}
    for relative in sorted(all_files - referenced_paths):
        path = root_path / relative
        unreferenced_entries.append({
            "path": relative, "size": path.stat().st_size, "sha256": _sha_file(path)
        })
    duplicate = sum(count - 1 for count in hash_counts.values() if count > 1)
    return {
        "counts": {
            "referenced": len(referenced), "present": len(present_paths),
            "missing": len(referenced) - len(present_paths), "duplicate": duplicate,
            "unreferenced": len(unreferenced_entries),
        },
        "files": files,
        "unreferenced_files": unreferenced_entries,
        "invalid_references": sorted(
            invalid_references, key=lambda item: _canonical_json(item)
        ),
    }


def build_manifest(
    root: str | Path,
    *,
    _sources: tuple[LegacySource, ...] | None = None,
) -> dict[str, Any]:
    databases = []
    references: list[dict[str, str]] = []
    for source in _sources or discover_sources(root):
        schema_sha, tables, source_refs = _schema_and_rows(source)
        references.extend(source_refs)
        databases.append({
            "name": source.name, "size": source.path.stat().st_size,
            "sha256": source.identity.sha256, "schema_sha256": schema_sha,
            "tables": tables,
        })
    attachments = inventory_attachments(root, references, tolerate_invalid=True)
    payload = {"databases": databases, "attachments": attachments}
    return {**payload, "sha256": _sha_bytes(_canonical_json(payload).encode())}


def convert_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConversionIssue("invalid decimal") from exc
    if not result.is_finite():
        raise ConversionIssue("non-finite decimal")
    return result


def convert_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConversionIssue("invalid ISO date") from exc


def convert_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConversionIssue("invalid ISO datetime") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return result


def convert_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if value in (True, 1, "1", "true", "TRUE", "yes", "YES"):
        return True
    if value in (False, 0, "0", "false", "FALSE", "no", "NO"):
        return False
    raise ConversionIssue("invalid boolean")


def convert_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConversionIssue("invalid JSON") from exc


def _safe_raw(field: str, value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).encode("utf-8", errors="replace")
    return _canonical_json({
        "type": type(value).__name__, "length": len(rendered),
        "sha256": _sha_bytes(rendered), "sensitive": bool(SENSITIVE_FIELDS.search(field)),
    })


def _normalized_hash(row: dict[str, Any]) -> str:
    return _sha_bytes(_canonical_json(row).encode())


def _table_rows(source: LegacySource, table: str) -> list[dict[str, Any]]:
    with source.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return []
        quoted = '"' + table.replace('"', '""') + '"'
        columns = [r[1] for r in db.execute(f"PRAGMA table_info({quoted})")]
        order = " ORDER BY " + ",".join('"' + c.replace('"', '""') + '"' for c in columns)
        return [dict(row) for row in db.execute(f"SELECT * FROM {quoted}{order}")]


def _source_columns(source: LegacySource, table: str) -> set[str]:
    with source.connect() as db:
        quoted = '"' + table.replace('"', '""') + '"'
        return {row[1] for row in db.execute(f"PRAGMA table_info({quoted})")}


def _validate_source_target_contracts(
    connection: sa.Connection, sources: tuple[LegacySource, ...]
) -> None:
    for source in sources:
        for table_name in sorted(_source_table_names(source)):
            if table_name in EXPLICIT_SKIPPED_TABLES.get(source.name, set()):
                continue
            target = _reflect_table(connection, table_name)
            if target is None:
                raise StructuralMigrationError(
                    f"target table is missing for source table: {table_name}"
                )
            unknown = (
                _source_columns(source, table_name)
                - set(target.c.keys())
                - FIELD_SKIP_CONTRACT.get(table_name, set())
            )
            if unknown:
                raise SourceSafetyError(
                    f"unknown source field in {table_name}: {sorted(unknown)!r}"
                )


def _quantize_for_target(value: Decimal | None, column: sa.Column[Any]) -> Decimal | None:
    if value is None or not isinstance(column.type, sa.Numeric):
        return value
    scale = column.type.scale
    precision = column.type.precision
    if scale is not None:
        quantum = Decimal(1).scaleb(-scale)
        quantized = value.quantize(quantum)
        if quantized != value:
            raise StructuralMigrationError(
                f"non-zero decimal precision loss for {column.table.name}.{column.name}"
            )
        value = quantized
    if precision is not None and scale is not None:
        integer_limit = Decimal(10) ** (precision - scale)
        if abs(value) >= integer_limit:
            raise StructuralMigrationError(
                f"decimal exceeds target precision for {column.table.name}.{column.name}"
            )
    return value


def _convert_row(table: str, row: dict[str, Any], target: sa.Table) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for field, value in row.items():
        if field not in target.c:
            continue
        if field == "file_path" and table in {
            "llm_models", "standards", "doc_templates",
        }:
            if table != "llm_models":
                converted[field] = None
            continue
        try:
            if field in DECIMAL_FIELDS:
                value = _quantize_for_target(convert_decimal(value), target.c[field])
            elif field in DATE_FIELDS:
                value = convert_date(value)
            elif field in DATETIME_FIELDS:
                value = convert_datetime(value)
            elif field in BOOL_FIELDS:
                value = convert_bool(value)
            elif field in JSON_FIELDS:
                value = convert_json(value)
        except ConversionIssue as exc:
            raise ConversionIssue(str(exc), field=field) from exc
        converted[field] = value
    if table == "users":
        stored = str(row.get("password") or "")
        legacy_hash = bool(re.fullmatch(r"[0-9a-fA-F]{96}", stored))
        converted.pop("id", None)
        converted["password"] = stored.lower() if legacy_hash else "!legacy-password-reset-required!"
        converted["must_change_password"] = True
        if not legacy_hash:
            converted["status"] = "disabled"
        role = str(row.get("role", "")).strip().lower()
        role_map = {
            "admin": "SYSTEM_MAINTAINER", "管理员": "SYSTEM_MAINTAINER",
            "system_maintainer": "SYSTEM_MAINTAINER", "user": "BUSINESS_USER",
            "用户": "BUSINESS_USER", "business_user": "BUSINESS_USER",
        }
        if role not in role_map:
            raise StructuralMigrationError(f"unknown legacy role: {role!r}")
        converted["role"] = role_map[role]
        converted["directory_permissions"] = convert_json(row.get("directory_permissions") or "{}")
    return converted


def _source_for_table(sources: tuple[LegacySource, ...], table: str) -> LegacySource | None:
    preferred = "generic_tables.db" if table.startswith("generic_") else "expense.db" if table.startswith("expense_") else "research.db"
    source = next(item for item in sources if item.name == preferred)
    with source.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return source if exists else None


def _record_issue(issues: list[dict[str, Any]], table: str, row: dict[str, Any], field: str | None, reason: str, value: Any = None) -> None:
    key = row.get("id")
    if key is None or key == "":
        key = next((
            row.get(name) for name in NATURAL_KEYS.get(table, ())
            if row.get(name) is not None and row.get(name) != ""
        ), None)
    issues.append({
        "source_table": table, "source_key": str(key) if key is not None else None,
        "field_name": field, "reason": reason,
        "raw_value": _safe_raw(field or "", value),
    })


def _reflect_table(connection: sa.Connection, name: str) -> sa.Table | None:
    if not sa.inspect(connection).has_table(name):
        return None
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _existing_parent(connection: sa.Connection, table: str, field: str, value: Any) -> bool:
    parent = _reflect_table(connection, table)
    if parent is None:
        return False
    return connection.scalar(sa.select(sa.literal(True)).where(parent.c[field] == value).limit(1)) is True


def _additional_parent_problem(
    connection: sa.Connection, table: str, row: dict[str, Any]
) -> tuple[str, Any] | None:
    checks: dict[str, tuple[tuple[str, str, str], ...]] = {
        "expert_group_members": (("experts", "expert_id", "expert_id"),),
        "equipment_group_members": (("equipment", "equipment_id", "equipment_id"),),
        "device_host_relations": (
            ("equipment", "device_id", "equipment_id"),
            ("host_devices", "host_id", "host_id"),
        ),
    }
    for parent_table, child_field, parent_field in checks.get(table, ()):
        if not _existing_parent(connection, parent_table, parent_field, row.get(child_field)):
            return child_field, row.get(child_field)
    if table == "project_documents" and row.get("project_id") is not None:
        if not any(
            _existing_parent(connection, parent, "project_id", row["project_id"])
            for parent in ("projects", "security_projects", "crypto_projects")
        ):
            return "project_id", row["project_id"]
    return None


def _uuid_value(connection: sa.Connection, value: uuid.UUID) -> uuid.UUID | str:
    return value if connection.dialect.name == "postgresql" else str(value)


def _create_project_registry(
    connection: sa.Connection,
    report: dict[str, Any],
    expected_rows: dict[str, list[dict[str, Any]]],
) -> None:
    registry = _reflect_table(connection, "project_registry")
    if registry is None:
        return
    if connection.scalar(sa.select(sa.func.count()).select_from(registry)):
        raise BatchConflict("target table must be empty for first migration: project_registry")
    categories = {
        "projects": "GENERAL_RESEARCH",
        "security_projects": "SECURITY_CONFIDENTIALITY",
        "crypto_projects": "CRYPTO_APPLICATION",
    }
    stats = {
        "source": 0, "converted": 0, "inserted": 0, "reused": 0,
        "rejected": 0, "row_hashes": [],
        "target_columns": {"id", "category", "business_id", "status"},
        "migration_key_fields": ["id"], "migration_key_values": [],
    }
    project_sources: list[tuple[str, str, dict[str, Any]]] = []
    business_id_sources: dict[str, list[str]] = {}
    for project_table_name, category in categories.items():
        project_table = _reflect_table(connection, project_table_name)
        if project_table is None:
            continue
        projects = connection.execute(sa.select(project_table.c.id, project_table.c.project_id)).mappings().all()
        for project_row in projects:
            project = dict(project_row)
            project_sources.append((project_table_name, category, project))
            business_id_sources.setdefault(str(project["project_id"]), []).append(project_table_name)
    duplicates = {
        business_id: sources
        for business_id, sources in business_id_sources.items()
        if len(sources) > 1
    }
    if duplicates:
        sample = ", ".join(
            f"{business_id}({','.join(sources)})"
            for business_id, sources in sorted(duplicates.items())[:20]
        )
        raise StructuralMigrationError(
            "legacy project ids must be globally unique across project categories: " + sample
        )
    for project_table_name, category, project in project_sources:
        project_table = _reflect_table(connection, project_table_name)
        if project_table is None:
            continue
        registry_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-project:{category}:{project['project_id']}")
        registry_id = _uuid_value(connection, registry_uuid)
        values = {
            "id": registry_id, "category": category,
            "business_id": project["project_id"], "status": "ACTIVE",
        }
        connection.execute(registry.insert().values(**{k: v for k, v in values.items() if k in registry.c}))
        expected_rows.setdefault("project_registry", []).append(values.copy())
        if "registry_id" in project_table.c:
            connection.execute(project_table.update().where(project_table.c.id == project["id"]).values(registry_id=registry_id))
            if project_table_name in report["tables"]:
                report["tables"][project_table_name]["target_columns"] = sorted({
                    *report["tables"][project_table_name]["target_columns"],
                    "registry_id",
                })
        stats["source"] += 1
        stats["converted"] += 1
        stats["inserted"] += 1
        stats["row_hashes"].append(_normalized_hash(values))
        stats["migration_key_values"].append([str(registry_id)])
    if not stats["source"]:
        return
    report["counts"]["source"] += stats["source"]
    report["counts"]["converted"] += stats["converted"]
    report["counts"]["inserted"] += stats["inserted"]
    stats["normalized_sha256"] = _sha_bytes(_canonical_json(sorted(stats.pop("row_hashes"))).encode())
    stats["target_columns"] = sorted(stats["target_columns"])
    report["tables"]["project_registry"] = stats


def _verify_json_internal_ids(
    connection: sa.Connection,
    report: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    result = {"checked": 0, "missing": 0}
    checks = (
        ("expense_invoice", "matched_payment_ids", "expense_payment"),
        ("expense_payment", "matched_invoice_ids", "expense_invoice"),
    )
    for owner_name, field, target_name in checks:
        owner = _reflect_table(connection, owner_name)
        target = _reflect_table(connection, target_name)
        if owner is None or target is None or field not in owner.c:
            continue
        target_ids = set(connection.scalars(sa.select(target.c.id)).all())
        for owner_row in connection.execute(sa.select(owner.c.id, owner.c[field])).mappings():
            raw = owner_row[field]
            values = raw if isinstance(raw, list) else convert_json(raw) if raw else []
            if not isinstance(values, list):
                raise StructuralMigrationError(
                    f"JSON internal IDs must be a list: {owner_name}.{field}"
                )
            for value in values:
                result["checked"] += 1
                if value not in target_ids:
                    result["missing"] += 1
                    raise StructuralMigrationError(
                        f"missing JSON internal ID: {owner_name}.{field}"
                    )
    report["json_internal_ids"] = result


def _sync_postgresql_sequences(connection: sa.Connection, tables: Iterable[str]) -> None:
    if connection.dialect.name != "postgresql":
        return
    for table_name in sorted(set(tables)):
        table = _reflect_table(connection, table_name)
        if table is None or "id" not in table.c or not isinstance(table.c.id.type, sa.BigInteger):
            continue
        sequence = connection.scalar(
            sa.text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
            {"table_name": table_name},
        )
        maximum = connection.scalar(sa.select(sa.func.max(table.c.id)))
        if sequence and maximum is not None:
            minimum = connection.scalar(
                sa.text(
                    "SELECT seqmin FROM pg_sequence "
                    "WHERE seqrelid = CAST(:sequence AS regclass)"
                ),
                {"sequence": sequence},
            )
            value = maximum if minimum is None or maximum >= minimum else minimum
            is_called = minimum is None or maximum >= minimum
            connection.execute(
                sa.text(
                    "SELECT setval(CAST(:sequence AS regclass), :value, :is_called)"
                ),
                {
                    "sequence": sequence, "value": value,
                    "is_called": is_called,
                },
            )


def _comparison_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo("UTC")).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _verify_expected_rows(
    connection: sa.Connection,
    expected_rows: dict[str, list[dict[str, Any]]],
    expected_amounts: dict[str, str],
) -> dict[str, str]:
    target_amounts: dict[str, Decimal] = {}
    for table_name, rows in sorted(expected_rows.items()):
        table = _reflect_table(connection, table_name)
        if table is None:
            raise StructuralMigrationError(f"target table disappeared: {table_name}")
        key_fields = MIGRATION_KEY_FIELDS.get(table_name, ("id",))
        for expected in rows:
            clauses = [table.c[field] == expected[field] for field in key_fields]
            matches = connection.execute(sa.select(table).where(*clauses)).mappings().all()
            if len(matches) != 1:
                raise StructuralMigrationError(
                    f"target migration key reconciliation failed: {table_name}"
                )
            actual = matches[0]
            for field, expected_value in expected.items():
                if _comparison_value(actual[field]) != _comparison_value(expected_value):
                    raise StructuralMigrationError(
                        f"target normalized row reconciliation failed: {table_name}.{field}"
                    )
                if field in DECIMAL_FIELDS and expected_value is not None:
                    key = f"{table_name}.{field}"
                    target_amounts[key] = target_amounts.get(key, Decimal("0")) + Decimal(actual[field])
    expected_decimal = {
        key: Decimal(value) for key, value in sorted(expected_amounts.items())
    }
    if target_amounts != expected_decimal:
        raise StructuralMigrationError("target decimal aggregate reconciliation failed")
    return dict(sorted(expected_amounts.items()))


def _take_migration_lock(connection: sa.Connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("SELECT pg_advisory_xact_lock(75200501)"))


def _refresh_expected_table_report(
    table_name: str,
    report: dict[str, Any],
    expected_rows: dict[str, list[dict[str, Any]]],
) -> None:
    stats = report["tables"].get(table_name)
    if stats is None:
        return
    rows = expected_rows.get(table_name, [])
    key_fields = stats["migration_key_fields"]
    stats["normalized_sha256"] = _sha_bytes(_canonical_json(sorted(
        _normalized_hash(row) for row in rows
    )).encode())
    stats["primary_keys_sha256"] = _sha_bytes(_canonical_json(sorted(
        (row["id"] for row in rows if row.get("id") is not None), key=str
    )).encode())
    natural = NATURAL_KEYS.get(table_name)
    natural_values = (
        [[row.get(field) for field in natural] for row in rows]
        if natural else []
    )
    stats["natural_keys_sha256"] = _sha_bytes(_canonical_json(sorted(
        natural_values, key=_canonical_json
    )).encode())
    stats["migration_key_values"] = sorted([
        [str(row.get(field)) if isinstance(row.get(field), uuid.UUID) else row.get(field)
         for field in key_fields]
        for row in rows
    ], key=_canonical_json)
    stats["target_columns"] = sorted({
        *stats["target_columns"], *(key for row in rows for key in row)
    })


def _repair_generic_table_state(
    connection: sa.Connection,
    deferred_current: dict[str, str],
    report: dict[str, Any],
    expected_rows: dict[str, list[dict[str, Any]]],
    manifest_sha256: str,
) -> None:
    if not deferred_current:
        return
    tables = {
        name: _reflect_table(connection, name)
        for name in (
            "generic_tables", "generic_table_versions",
            "generic_table_columns", "generic_table_data",
        )
    }
    if any(table is None for table in tables.values()):
        raise StructuralMigrationError("target generic table schema is incomplete")
    generic_tables = tables["generic_tables"]
    versions = tables["generic_table_versions"]
    columns = tables["generic_table_columns"]
    data = tables["generic_table_data"]
    generated_counts = {name: 0 for name in tables}

    def next_id(table: sa.Table) -> int:
        return int(connection.scalar(sa.select(sa.func.max(table.c.id))) or 0) + 1

    def insert_generated(table_name: str, values: dict[str, Any]) -> None:
        table = tables[table_name]
        safe = {key: value for key, value in values.items() if key in table.c}
        connection.execute(table.insert().values(**safe))
        expected_rows.setdefault(table_name, []).append(safe.copy())
        generated_counts[table_name] += 1

    for table_id, requested_version_id in sorted(deferred_current.items()):
        requested = connection.execute(
            sa.select(versions).where(versions.c.version_id == requested_version_id)
        ).mappings().first()
        if requested is None:
            raise StructuralMigrationError("orphan current_version_id in generic_tables")
        if str(requested["table_id"]) != table_id:
            raise StructuralMigrationError(
                "cross-table current_version_id in generic_tables"
            )
        table_versions = connection.execute(
            sa.select(versions).where(versions.c.table_id == table_id).order_by(
                versions.c.version_number.desc(), versions.c.version_id.desc()
            )
        ).mappings().all()
        if not table_versions:
            raise StructuralMigrationError("generic table has no version")
        unlocked = [row for row in table_versions if not bool(row["is_locked"])]
        if unlocked:
            current_version_id = str(unlocked[0]["version_id"])
        else:
            source = table_versions[0]
            version_number = int(source["version_number"]) + 1
            seed = (
                f"legacy-generic-working:{manifest_sha256}:{table_id}:"
                f"{source['version_id']}:{version_number}"
            )
            current_version_id = "GTV-MIG-" + uuid.uuid5(
                uuid.NAMESPACE_URL, seed
            ).hex.upper()
            version_values = dict(source)
            version_values.update({
                "id": next_id(versions),
                "version_id": current_version_id,
                "version_number": version_number,
                "version_label": f"v{version_number}_migration-working",
                "create_method": "import",
                "source_version_id": source["version_id"],
                "row_count": 0,
                "is_locked": False,
            })
            insert_generated("generic_table_versions", version_values)
            for source_column in connection.execute(
                sa.select(columns).where(
                    columns.c.version_id == source["version_id"]
                ).order_by(columns.c.col_index, columns.c.id)
            ).mappings():
                values = dict(source_column)
                values.update({"id": next_id(columns), "version_id": current_version_id})
                insert_generated("generic_table_columns", values)
            for source_row in connection.execute(
                sa.select(data).where(
                    data.c.version_id == source["version_id"]
                ).order_by(data.c.row_index, data.c.id)
            ).mappings():
                values = dict(source_row)
                values.update({"id": next_id(data), "version_id": current_version_id})
                insert_generated("generic_table_data", values)
        connection.execute(generic_tables.update().where(
            generic_tables.c.table_id == table_id
        ).values(current_version_id=current_version_id))
        for expected in expected_rows.get("generic_tables", []):
            if str(expected.get("table_id")) == table_id:
                expected["current_version_id"] = current_version_id

    counts = dict(connection.execute(
        sa.select(data.c.version_id, sa.func.count().label("count"))
        .group_by(data.c.version_id)
    ).all())
    for version in connection.execute(sa.select(versions)).mappings():
        actual = int(counts.get(version["version_id"], 0))
        connection.execute(versions.update().where(
            versions.c.id == version["id"]
        ).values(row_count=actual))
        for expected in expected_rows.get("generic_table_versions", []):
            if expected.get("id") == version["id"]:
                expected["row_count"] = actual

    for table_name, generated in generated_counts.items():
        if generated and table_name in report["tables"]:
            report["tables"][table_name]["inserted"] += generated
            report["counts"]["inserted"] += generated
        _refresh_expected_table_report(table_name, report, expected_rows)


@contextmanager
def _migration_transaction_with_file_cleanup(
    engine: Engine,
    prepared: list[dict[str, Any]],
    created_files: list[dict[str, Any]],
    final_source_check: Any = None,
) -> Iterable[sa.Connection]:
    connection = engine.connect()
    transaction = connection.begin()
    commit_check = None
    try:
        if final_source_check is not None:
            def commit_check(_connection: sa.Connection) -> None:
                final_source_check()

            sa.event.listen(connection, "commit", commit_check)
        yield connection
        if final_source_check is not None:
            final_source_check()
        transaction.commit()
    except Exception:
        if transaction.is_active:
            transaction.rollback()
        else:
            connection.rollback()
        from .binaries import cleanup_created_files, cleanup_prepared
        cleanup_prepared(prepared)
        cleanup_created_files(created_files)
        raise
    else:
        from .binaries import cleanup_created_files, cleanup_prepared
        cleanup_prepared(prepared)
        cleanup_created_files(created_files, remove=False)
    finally:
        if commit_check is not None and sa.event.contains(
            connection, "commit", commit_check
        ):
            sa.event.remove(connection, "commit", commit_check)
        connection.close()


def _decode_summary(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


def _report_sha256(report: dict[str, Any]) -> str:
    payload = {key: value for key, value in report.items() if key != "report_sha256"}
    return _sha_bytes(_canonical_json(payload).encode())


def _target_fingerprint(
    connection: sa.Connection,
    projection: dict[str, list[str]],
    migration_keys: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    for table_name in sorted(projection):
        table = _reflect_table(connection, table_name)
        if table is None:
            raise BatchConflict(f"target drift: missing table {table_name}")
        columns = projection[table_name]
        if any(column not in table.c for column in columns):
            raise BatchConflict(f"target drift: missing column in {table_name}")
        key_contract = migration_keys.get(table_name)
        if not key_contract:
            raise BatchConflict(f"target drift: missing migration keys for {table_name}")
        key_fields = key_contract["fields"]
        clauses = []
        for values in key_contract["values"]:
            comparisons = []
            for field, value in zip(key_fields, values, strict=True):
                column = table.c[field]
                if connection.dialect.name == "postgresql" and column.type.__class__.__name__ == "UUID" and isinstance(value, str):
                    value = uuid.UUID(value)
                comparisons.append(column == value)
            clauses.append(sa.and_(*comparisons))
        predicate = sa.or_(*clauses) if clauses else sa.false()
        rows = (
            [
                {column: row[column] for column in columns}
                for row in connection.execute(
                    sa.select(*(table.c[column] for column in columns)).where(predicate)
                ).mappings()
            ]
            if columns
            else [{}] * connection.scalar(
                sa.select(sa.func.count()).select_from(table).where(predicate)
            )
        )
        normalized = sorted((_canonical_json(row) for row in rows))
        tables.append({
            "table": table_name, "count": len(rows),
            "sha256": _sha_bytes(_canonical_json(normalized).encode()),
        })
    payload = {"tables": tables}
    return {**payload, "sha256": _sha_bytes(_canonical_json(payload).encode())}


def _validate_report_and_target(
    connection: sa.Connection, report: dict[str, Any], *, storage_root: str | Path | None = None
) -> None:
    expected_report_sha = report.get("report_sha256")
    if not expected_report_sha or expected_report_sha != _report_sha256(report):
        raise BatchConflict("report hash mismatch")
    target_projection = report.get("target_projection")
    migration_keys = report.get("migration_keys")
    expected_target = report.get("target_fingerprint")
    if (
        not isinstance(target_projection, dict)
        or not isinstance(migration_keys, dict)
        or not isinstance(expected_target, dict)
    ):
        raise BatchConflict("target drift evidence is missing")
    if _target_fingerprint(connection, target_projection, migration_keys) != expected_target:
        raise BatchConflict("target drift detected")
    from .binaries import verify_binary_report
    verify_binary_report(report, storage_root)


def _verify_completed_snapshot(
    engine: Engine,
    source_root: str | Path,
    batch_key: str,
    *,
    original_root: str | Path,
    baseline_manifest_sha256: str,
    storage_root: str | Path | None = None,
    original_custody: SourceCustody | None = None,
    allow_test_sqlite: bool = False,
) -> dict[str, Any]:
    if engine.dialect.name != "postgresql" and not allow_test_sqlite:
        raise SourceSafetyError("migration target must be PostgreSQL")
    sources = discover_sources(source_root)
    manifest = build_manifest(source_root, _sources=sources)
    with engine.begin() as connection:
        _take_migration_lock(connection)
        batches = _reflect_table(connection, "legacy_migration_batches")
        if batches is None:
            raise BatchConflict("migration batch table is missing")
        existing = connection.execute(
            sa.select(batches).where(batches.c.batch_key == batch_key)
        ).mappings().first()
        if not existing:
            raise BatchConflict("migration batch does not exist")
        if existing["source_manifest_sha256"] != manifest["sha256"]:
            raise BatchConflict("batch source manifest mismatch")
        if existing["status"] not in {"completed", "completed_with_issues"}:
            raise BatchConflict("migration batch is not completed")
        report = _decode_summary(existing["summary"])
        _validate_report_and_target(connection, report, storage_root=storage_root)
    _assert_sources_unchanged(sources)
    if original_custody is not None:
        _assert_source_custody(original_root, original_custody)
    elif _fresh_bound_manifest(original_root)["sha256"] != baseline_manifest_sha256:
        raise SourceSafetyError("legacy source or attachments changed during verification")
    return report


def verify_completed_batch(
    engine: Engine,
    source_root: str | Path,
    batch_key: str,
    *,
    storage_root: str | Path | None = None,
    allow_test_sqlite: bool = False,
) -> dict[str, Any]:
    original_custody = _capture_source_custody(source_root)
    with _prepared_snapshot(source_root) as (snapshot_root, manifest):
        return _verify_completed_snapshot(
            engine, snapshot_root, batch_key, original_root=source_root,
            baseline_manifest_sha256=manifest["sha256"],
            storage_root=storage_root,
            original_custody=original_custody,
            allow_test_sqlite=allow_test_sqlite,
        )


def _migrate_snapshot(
    engine: Engine,
    source_root: str | Path,
    batch_key: str,
    *,
    original_root: str | Path,
    baseline_manifest_sha256: str,
    storage_root: str | Path | None = None,
    max_file_bytes: int = 100 * 1024 * 1024,
    allow_test_sqlite: bool = False,
    fail_after_table: str | None = None,
    _before_final_source_check: Any = None,
    _fail_after_binary: int | None = None,
    original_custody: SourceCustody | None = None,
) -> dict[str, Any]:
    if engine.dialect.name != "postgresql" and not allow_test_sqlite:
        raise SourceSafetyError("migration target must be PostgreSQL")
    sources = discover_sources(source_root)
    manifest = build_manifest(source_root, _sources=sources)
    manifest_sha = manifest["sha256"]

    report: dict[str, Any] = {
        "status": "running",
        "source_manifest_sha256": manifest_sha,
        "counts": {"source": 0, "converted": 0, "inserted": 0, "reused": 0, "rejected": 0},
        "tables": {}, "issues": [], "relationships": {"checked": 0, "orphans": 0},
        "amounts": {}, "json_fields": {"checked": 0, "invalid": 0},
        "attachments": manifest["attachments"],
        "source_hashes": {item["name"]: item["sha256"] for item in manifest["databases"]},
        "recovery": "backup_restore_only",
        "same_manifest_reuse": "returns_existing_report_without_alias",
        "domain_redirects": {
            "research.db.doc_templates": "reference_template_items"
        },
        "failure_record_boundary": "failed_business_transaction_leaves_no_batch_record",
        "claim": "migration_program_and_fixture_verified_real_sanitized_databases_pending_acceptance",
        "acceptance_status_zh": "迁移程序和夹具验证通过，真实脱敏三库迁移待验收",
    }
    batch_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-batch:{batch_key}:{manifest_sha}")
    issue_rows: list[dict[str, Any]] = []
    expected_rows: dict[str, list[dict[str, Any]]] = {}
    prepared: list[dict[str, Any]] = []
    created_binary_files: list[dict[str, Any]] = []
    final_source_check = (
        (lambda: _assert_source_custody(original_root, original_custody))
        if original_custody is not None else None
    )
    with _migration_transaction_with_file_cleanup(
        engine, prepared, created_binary_files,
        final_source_check=final_source_check,
    ) as connection:
        _take_migration_lock(connection)
        batch_id = _uuid_value(connection, batch_uuid)
        batches = _reflect_table(connection, "legacy_migration_batches")
        issues_target = _reflect_table(connection, "legacy_migration_issues")
        if batches is None or issues_target is None:
            raise StructuralMigrationError("target schema is missing migration control tables")
        _validate_source_target_contracts(connection, sources)
        existing = connection.execute(
            sa.select(batches).where(batches.c.batch_key == batch_key)
        ).mappings().first()
        if existing:
            if existing["source_manifest_sha256"] != manifest_sha:
                raise BatchConflict("batch key already exists with a different source manifest")
            if existing["status"] not in {"completed", "completed_with_issues"}:
                raise BatchConflict("batch key exists but is not completed")
            existing_report = _decode_summary(existing["summary"])
            _validate_report_and_target(
                connection, existing_report, storage_root=storage_root
            )
            _assert_sources_unchanged(sources)
            if original_custody is None and (
                _fresh_bound_manifest(original_root)["sha256"]
                != baseline_manifest_sha256
            ):
                raise SourceSafetyError("legacy source or attachments changed during batch reuse")
            return existing_report
        reusable = connection.execute(
            sa.select(batches)
            .where(
                batches.c.source_manifest_sha256 == manifest_sha,
                batches.c.status.in_(("completed", "completed_with_issues")),
            )
            .order_by(batches.c.batch_key)
            .limit(1)
        ).mappings().first()
        if reusable:
            reusable_report = _decode_summary(reusable["summary"])
            _validate_report_and_target(
                connection, reusable_report, storage_root=storage_root
            )
            _assert_sources_unchanged(sources)
            if original_custody is None and (
                _fresh_bound_manifest(original_root)["sha256"]
                != baseline_manifest_sha256
            ):
                raise SourceSafetyError("legacy source or attachments changed during batch reuse")
            return reusable_report
        from .binaries import build_binary_plan, binary_issues
        binary_plan = build_binary_plan(
            connection, Path(source_root), manifest, max_bytes=max_file_bytes
        )
        rejected_standard_ids = {
            item["objectId"]
            for item in binary_plan["items"]
            if item["sourceTable"] == "standards"
            and item["issueCode"] == "MULTIPLE_OBJECT_CANDIDATES"
        }
        connection.execute(batches.insert().values(
            id=batch_id, batch_key=batch_key, source_manifest_sha256=manifest_sha,
            status="running", summary={},
        ))
        deferred_generic_current: dict[str, str] = {}
        for table_name in TABLE_ORDER:
            if table_name == "project_registry":
                continue
            source = _source_for_table(sources, table_name)
            target = _reflect_table(connection, table_name)
            if source is None or target is None:
                continue
            rows = _table_rows(source, table_name)
            if not rows:
                continue
            stats = {
                "source": len(rows), "converted": 0, "inserted": 0,
                "reused": 0, "rejected": 0, "row_hashes": [],
                "primary_keys": [], "natural_keys": [], "target_columns": set(),
                "migration_key_fields": list(MIGRATION_KEY_FIELDS.get(table_name, ("id",))),
                "migration_key_values": [],
            }
            report["counts"]["source"] += len(rows)
            if table_name != "users" and connection.scalar(sa.select(sa.func.count()).select_from(target)):
                raise BatchConflict(f"target table must be empty for first migration: {table_name}")
            for row in rows:
                if table_name in SKIPPED_TABLES:
                    continue
                if table_name == "standards" and (
                    not row.get("doc_id") or str(row.get("doc_id")) in rejected_standard_ids
                ):
                    stats["rejected"] += 1
                    continue
                if table_name in PARENT_KEYS:
                    parent_table, child_field, parent_field = PARENT_KEYS[table_name]
                    report["relationships"]["checked"] += 1
                    if not _existing_parent(connection, parent_table, parent_field, row.get(child_field)):
                        report["relationships"]["orphans"] += 1
                        raise StructuralMigrationError(
                            f"orphan parent in {table_name}.{child_field}"
                        )
                additional_problem = _additional_parent_problem(connection, table_name, row)
                if additional_problem:
                    field, value = additional_problem
                    report["relationships"]["checked"] += 1
                    report["relationships"]["orphans"] += 1
                    raise StructuralMigrationError(
                        f"orphan parent in {table_name}.{field}"
                    )
                try:
                    converted = _convert_row(table_name, row, target)
                    if table_name == "generic_tables":
                        current_version_id = converted.pop("current_version_id", None)
                        if not current_version_id:
                            raise StructuralMigrationError(
                                "generic table has no current_version_id"
                            )
                        deferred_generic_current[str(converted.get("table_id"))] = str(
                            current_version_id
                        )
                    for field in JSON_FIELDS & converted.keys():
                        report["json_fields"]["checked"] += 1
                        if converted[field] is not None and not isinstance(converted[field], (dict, list, int, float, bool, str)):
                            raise ConversionIssue("unsupported JSON type")
                except ConversionIssue as exc:
                    field = exc.field
                    if field in JSON_FIELDS:
                        report["json_fields"]["invalid"] += 1
                    _record_issue(issue_rows, table_name, row, field, str(exc), row.get(field) if field else None)
                    stats["rejected"] += 1
                    continue
                if table_name == "llm_models":
                    for field in row:
                        if SENSITIVE_FIELDS.search(field) or (field == "file_path" and row.get(field)):
                            _record_issue(issue_rows, table_name, row, field, "field_not_migrated", None)
                if table_name == "users" and not re.fullmatch(r"[0-9a-fA-F]{96}", str(row.get("password") or "")):
                    _record_issue(issue_rows, table_name, row, "password", "plaintext_password_not_migrated", None)
                stats["converted"] += 1
                report["counts"]["converted"] += 1
                stats["target_columns"].update(converted.keys())
                key_values = [converted.get(field) for field in stats["migration_key_fields"]]
                if any(value is None for value in key_values):
                    raise StructuralMigrationError(
                        f"missing deterministic migration key in {table_name}"
                    )
                stats["migration_key_values"].append([
                    str(value) if isinstance(value, uuid.UUID) else value
                    for value in key_values
                ])
                expected_rows.setdefault(table_name, []).append(converted.copy())
                row_hash = _normalized_hash(converted)
                stats["row_hashes"].append(row_hash)
                if row.get("id") is not None:
                    stats["primary_keys"].append(row["id"])
                natural = NATURAL_KEYS.get(table_name)
                if natural:
                    stats["natural_keys"].append([converted.get(key) for key in natural])
                existing_row = None
                if natural and all(converted.get(key) is not None for key in natural):
                    clauses = [target.c[key] == converted[key] for key in natural]
                    existing_row = connection.execute(sa.select(target).where(*clauses)).mappings().first()
                if existing_row:
                    comparable = {key: existing_row[key] for key in converted}
                    if _normalized_hash(comparable) != row_hash:
                        raise StructuralMigrationError(
                            f"natural-key conflict in {table_name}"
                        )
                    stats["reused"] += 1
                    report["counts"]["reused"] += 1
                    continue
                try:
                    with connection.begin_nested():
                        connection.execute(target.insert().values(**converted))
                except sa.exc.IntegrityError:
                    raise StructuralMigrationError(
                        f"target constraint violation in {table_name}"
                    )
                stats["inserted"] += 1
                report["counts"]["inserted"] += 1
                for field in DECIMAL_FIELDS & converted.keys():
                    if converted[field] is not None:
                        key = f"{table_name}.{field}"
                        report["amounts"][key] = str(Decimal(report["amounts"].get(key, "0")) + Decimal(converted[field]))
            stats["normalized_sha256"] = _sha_bytes(_canonical_json(sorted(stats.pop("row_hashes"))).encode())
            stats["primary_keys_sha256"] = _sha_bytes(_canonical_json(sorted(stats.pop("primary_keys"), key=str)).encode())
            stats["natural_keys_sha256"] = _sha_bytes(_canonical_json(sorted(stats.pop("natural_keys"), key=_canonical_json)).encode())
            stats["migration_key_values"] = sorted(
                stats["migration_key_values"], key=_canonical_json
            )
            identity_columns = {
                "id", "version", "status", "must_change_password",
                "registry_id", "current_version_id",
            }
            stats["target_columns"] = sorted(
                stats["target_columns"] | (identity_columns & set(target.c.keys()))
            )
            report["tables"][table_name] = stats
            report["counts"]["rejected"] += stats["rejected"]
            if fail_after_table == table_name:
                raise RuntimeError("injected migration failure")

        _repair_generic_table_state(
            connection, deferred_generic_current, report, expected_rows, manifest_sha
        )

        from .binaries import validate_binary_object_targets
        validate_binary_object_targets(connection, binary_plan)
        issue_rows.extend(binary_issues(binary_plan))
        if binary_plan["summary"]["planned"] and storage_root is None:
            raise SourceSafetyError(
                "controlled storage root is required for legacy binaries"
            )

        from .binaries import (
            apply_binary_metadata,
            cleanup_created_files,
            cleanup_prepared,
            finalize_binary_files,
            physical_fingerprint,
            prepare_binary_files,
        )
        try:
            _create_project_registry(connection, report, expected_rows)
            if binary_plan["summary"]["planned"]:
                prepared.extend(prepare_binary_files(
                    Path(source_root), Path(storage_root), binary_plan
                ))
                apply_binary_metadata(connection, prepared, report, expected_rows)
            _verify_json_internal_ids(connection, report, issue_rows)
            report["target_amounts"] = _verify_expected_rows(
                connection, expected_rows, report["amounts"]
            )
            _sync_postgresql_sequences(connection, report["tables"].keys())

            report["explicitly_skipped"] = {
                "research.db": sorted(EXPLICIT_SKIPPED_TABLES["research.db"])
            }

            issue_rows.sort(key=lambda item: _canonical_json(item))
            for index, issue in enumerate(issue_rows):
                issue_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-issue:{batch_uuid}:{index}:{_canonical_json(issue)}")
                issue_id = _uuid_value(connection, issue_uuid)
                connection.execute(issues_target.insert().values(id=issue_id, batch_id=batch_id, **issue))
            report["issues"] = issue_rows
            report["counts"]["issues"] = len(issue_rows)
            if _before_final_source_check is not None:
                _before_final_source_check()
            _assert_sources_unchanged(sources)
            after_manifest = build_manifest(source_root, _sources=sources)
            if after_manifest["sha256"] != manifest_sha:
                raise SourceSafetyError("legacy source changed during migration")
            if original_custody is None and (
                _fresh_bound_manifest(original_root)["sha256"]
                != baseline_manifest_sha256
            ):
                raise SourceSafetyError("legacy source or attachments changed during migration")
            report["custody_verified"] = True
            report["source_hashes_before"] = report.pop("source_hashes")
            report["source_hashes_after"] = {
                item["name"]: item["sha256"] for item in after_manifest["databases"]
            }
            report["attachment_manifest_sha256_before"] = _sha_bytes(_canonical_json(manifest["attachments"]).encode())
            report["attachment_manifest_sha256_after"] = _sha_bytes(_canonical_json(after_manifest["attachments"]).encode())
            planned_items = [
                {
                    key: value for key, value in item.items()
                    if key not in {
                        "stagePath", "stageName", "stageDirFd", "stageIdentity"
                    }
                }
                for item in prepared
            ]
            if prepared:
                created_binary_files.extend(finalize_binary_files(
                    Path(storage_root), prepared, fail_after=_fail_after_binary
                ))
                physical = physical_fingerprint(Path(storage_root), planned_items)
            else:
                physical = {"files": [], "sha256": _sha_bytes(b'{"files":[]}')}
            report["binaries"] = {
                "plan_sha256": binary_plan["sha256"],
                "summary": binary_plan["summary"],
                "items": planned_items,
                "issues": [
                    item for item in binary_plan["items"] if item["issueCode"] is not None
                ],
                "physical_files": physical,
                "physical_files_sha256": physical["sha256"],
            }
            report["status"] = "completed_with_issues" if issue_rows else "completed"
            report["target_projection"] = {
                table_name: table_report["target_columns"]
                for table_name, table_report in sorted(report["tables"].items())
            }
            report["target_projection"]["legacy_migration_issues"] = [
                "id", "batch_id", "source_table", "source_key", "field_name",
                "reason", "raw_value"
            ]
            report["migration_keys"] = {
                table_name: {
                    "fields": table_report["migration_key_fields"],
                    "values": table_report["migration_key_values"],
                }
                for table_name, table_report in sorted(report["tables"].items())
            }
            report["migration_keys"]["legacy_migration_issues"] = {
                "fields": ["batch_id"], "values": [[str(batch_id)]]
            }
            report["target_tables"] = sorted(report["target_projection"])
            report["target_fingerprint"] = _target_fingerprint(
                connection, report["target_projection"], report["migration_keys"]
            )
            report["report_sha256"] = _report_sha256(report)
            connection.execute(
                batches.update().where(batches.c.id == batch_id).values(
                    status=report["status"], summary=report, completed_at=sa.func.now()
                )
            )
        except Exception:
            cleanup_prepared(prepared)
            cleanup_created_files(created_binary_files)
            raise
    return report


def migrate_legacy(
    engine: Engine,
    source_root: str | Path,
    batch_key: str,
    *,
    storage_root: str | Path | None = None,
    max_file_bytes: int = 100 * 1024 * 1024,
    allow_test_sqlite: bool = False,
    fail_after_table: str | None = None,
    _before_final_source_check: Any = None,
    _fail_after_binary: int | None = None,
) -> dict[str, Any]:
    original_custody = _capture_source_custody(source_root)
    if storage_root is not None:
        from .binaries import _validate_storage_root
        _validate_storage_root(Path(source_root), Path(storage_root), create=False)
    with _prepared_snapshot(source_root) as (snapshot_root, manifest):
        return _migrate_snapshot(
            engine, snapshot_root, batch_key, original_root=source_root,
            baseline_manifest_sha256=manifest["sha256"],
            storage_root=storage_root,
            max_file_bytes=max_file_bytes,
            allow_test_sqlite=allow_test_sqlite,
            fail_after_table=fail_after_table,
            _before_final_source_check=_before_final_source_check,
            _fail_after_binary=_fail_after_binary,
            original_custody=original_custody,
        )
