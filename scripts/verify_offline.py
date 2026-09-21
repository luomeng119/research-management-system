#!/usr/bin/env python3
"""Create and verify deterministic offline backup evidence.

This tool coordinates PostgreSQL's exported snapshot with ``pg_dump`` and owns
the portable evidence contract: table counts, critical relations, and the
SHA-256 inventory for every business file root.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import zipfile

import sqlalchemy as sa
from sqlalchemy.engine import Engine


SCHEMA_VERSION = 1
MAX_ARCHIVE_ENTRIES = 200_000
MAX_ARCHIVE_COMPRESSION_RATIO = 1_000
MIN_FREE_AFTER_EXTRACT = 256 * 1024 * 1024
BUSINESS_FILE_ROOTS = (
    "data/files",
    "uploads",
    "documents",
    "data/documents",
    "data/templates",
    "data/legacy-folder-archive",
)
BUSINESS_SINGLE_FILES = (
    "data/research.db",
    "data/research.db-wal",
    "data/research.db-shm",
    "data/research.db-journal",
)
REQUIRED_TABLES = {
    "alembic_version",
    "proposals",
    "project_registry",
    "projects",
    "security_projects",
    "crypto_projects",
    "equipment",
    "equipment_groups",
    "equipment_group_members",
    "experts",
    "expert_groups",
    "expert_group_members",
    "expense_reimbursement",
    "expense_invoice",
    "expense_invoice_item",
    "expense_payment",
    "standards",
    "reference_template_items",
    "generic_tables",
    "project_documents",
    "stored_files",
    "stored_file_versions",
    "object_files",
}
OBJECT_TABLES = {
    "PROPOSAL": ("proposals", "business_id"),
    "PROJECT": ("project_registry", "business_id"),
    "EXPERT": ("experts", "expert_id"),
    "EQUIPMENT": ("equipment", "equipment_id"),
    "STANDARD": ("standards", "doc_id"),
    "TEMPLATE": ("reference_template_items", "template_id"),
    "GENERIC_TABLE": ("generic_tables", "table_id"),
    "EXPENSE": ("expense_reimbursement", "id"),
    "INVOICE": ("expense_invoice", "id"),
    "PAYMENT": ("expense_payment", "id"),
    "DOCUMENT": ("project_documents", "doc_id"),
}
RELATION_KEYS = {
    "proposal_to_project",
    "project_registry_to_category_table",
    "equipment_group_to_project",
    "equipment_member_to_group",
    "equipment_member_to_equipment",
    "expert_member_to_group",
    "expert_member_to_expert",
    "finance_invoice_to_registration",
    "finance_payment_to_registration",
    "finance_item_to_invoice",
    "file_version_to_file",
    "object_file_to_file",
    "object_file_to_business_object",
    "object_file_unknown_type",
}


class VerificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_reparse_stat(details) -> bool:
    """Return true for Windows junctions/reparse points, not just symlinks."""
    attributes = int(getattr(details, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _is_linklike(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or is_reparse_stat(details)


def _assert_no_reparse_ancestors(path: Path) -> None:
    current = Path(path).absolute()
    chain = [current, *current.parents]
    for candidate in reversed(chain):
        if _is_linklike(candidate):
            raise VerificationError(f"reparse points are not allowed in backup paths: {candidate}")


def _regular_files(root: Path):
    if not root.exists():
        return
    _assert_no_reparse_ancestors(root)
    if _is_linklike(root) or not root.is_dir():
        raise VerificationError(f"business file root is not a real directory: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if _is_linklike(path):
            raise VerificationError(f"symbolic links are not allowed in backups: {path}")
        if path.is_dir():
            continue
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise VerificationError(f"cannot read backup file metadata: {path}") from exc
        if not stat.S_ISREG(mode):
            raise VerificationError(f"non-regular files are not allowed in backups: {path}")
        yield path


def build_file_manifest(data_root: Path) -> list[dict[str, object]]:
    data_root = Path(data_root).absolute()
    _assert_no_reparse_ancestors(data_root)
    entries: list[dict[str, object]] = []
    for root_name in BUSINESS_FILE_ROOTS:
        root = data_root.joinpath(*PurePosixPath(root_name).parts)
        for path in _regular_files(root):
            entries.append(
                {
                    "root": root_name,
                    "path": path.relative_to(root).as_posix(),
                    "sizeBytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    for relative_name in BUSINESS_SINGLE_FILES:
        path = data_root.joinpath(*PurePosixPath(relative_name).parts)
        if not path.exists():
            continue
        _assert_no_reparse_ancestors(path)
        details = path.lstat()
        if _is_linklike(path) or not stat.S_ISREG(details.st_mode):
            raise VerificationError(f"legacy business file is not a regular file: {path}")
        entries.append(
            {
                "root": "APP_DATA_ROOT",
                "path": relative_name,
                "sizeBytes": details.st_size,
                "sha256": sha256_file(path),
            }
        )
    entries.sort(key=lambda entry: (str(entry["root"]), str(entry["path"])))
    return entries


def build_package_manifest(package_root: Path) -> list[dict[str, object]]:
    package_root = Path(package_root).resolve()
    entries: list[dict[str, object]] = []
    for path in _regular_files(package_root):
        relative = path.relative_to(package_root).as_posix()
        if relative == "manifest.json":
            continue
        entries.append(
            {
                "path": relative,
                "sizeBytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return entries


def _count(connection, sql: str) -> int:
    return int(connection.scalar(sa.text(sql)) or 0)


def collect_database_snapshot(
    engine: Engine, connection=None
) -> dict[str, object]:
    manager = nullcontext(connection) if connection is not None else engine.connect()
    with manager as connection:
        inspector = sa.inspect(connection)
        tables = sorted(inspector.get_table_names())
        missing = sorted(REQUIRED_TABLES.difference(tables))
        if missing:
            raise VerificationError(
                f"required database tables are missing: {', '.join(missing)}"
            )
        quote = connection.dialect.identifier_preparer.quote
        counts = {
            table: _count(connection, f"SELECT count(*) FROM {quote(table)}")
            for table in tables
        }
        revisions = sorted(
            str(value)
            for value in connection.scalars(
                sa.text("SELECT version_num FROM alembic_version")
            ).all()
        )
        relations = {
            "proposal_to_project": _count(
                connection,
                "SELECT count(*) FROM project_registry r LEFT JOIN proposals p "
                "ON p.id = r.proposal_id WHERE r.proposal_id IS NOT NULL AND p.id IS NULL",
            ),
            "project_registry_to_category_table": sum(
                _count(
                    connection,
                    f"SELECT count(*) FROM project_registry r LEFT JOIN {table} p "
                    "ON p.project_id = r.business_id AND p.registry_id = r.id "
                    f"WHERE r.category = '{category}' AND p.project_id IS NULL",
                )
                for category, table in (
                    ("GENERAL_RESEARCH", "projects"),
                    ("SECURITY_CONFIDENTIALITY", "security_projects"),
                    ("CRYPTO_APPLICATION", "crypto_projects"),
                )
            ),
            "equipment_group_to_project": _count(
                connection,
                "SELECT count(*) FROM equipment_groups g LEFT JOIN ("
                "SELECT project_id FROM projects UNION ALL "
                "SELECT project_id FROM security_projects UNION ALL "
                "SELECT project_id FROM crypto_projects) p ON p.project_id = g.project_id "
                "WHERE g.project_id IS NOT NULL AND p.project_id IS NULL",
            ),
            "equipment_member_to_group": _count(
                connection,
                "SELECT count(*) FROM equipment_group_members m LEFT JOIN equipment_groups g "
                "ON g.group_id = m.group_id WHERE g.group_id IS NULL",
            ),
            "equipment_member_to_equipment": _count(
                connection,
                "SELECT count(*) FROM equipment_group_members m LEFT JOIN equipment e "
                "ON e.equipment_id = m.equipment_id WHERE e.equipment_id IS NULL",
            ),
            "expert_member_to_group": _count(
                connection,
                "SELECT count(*) FROM expert_group_members m LEFT JOIN expert_groups g "
                "ON g.group_id = m.group_id WHERE g.group_id IS NULL",
            ),
            "expert_member_to_expert": _count(
                connection,
                "SELECT count(*) FROM expert_group_members m LEFT JOIN experts e "
                "ON e.expert_id = m.expert_id WHERE e.expert_id IS NULL",
            ),
            "finance_invoice_to_registration": _count(
                connection,
                "SELECT count(*) FROM expense_invoice i LEFT JOIN expense_reimbursement r "
                "ON r.id = i.reimbursement_id "
                "WHERE i.reimbursement_id IS NOT NULL AND r.id IS NULL",
            ),
            "finance_payment_to_registration": _count(
                connection,
                "SELECT count(*) FROM expense_payment p LEFT JOIN expense_reimbursement r "
                "ON r.id = p.reimbursement_id "
                "WHERE p.reimbursement_id IS NOT NULL AND r.id IS NULL",
            ),
            "finance_item_to_invoice": _count(
                connection,
                "SELECT count(*) FROM expense_invoice_item item LEFT JOIN expense_invoice i "
                "ON i.id = item.invoice_id WHERE item.invoice_id IS NOT NULL AND i.id IS NULL",
            ),
            "file_version_to_file": _count(
                connection,
                "SELECT count(*) FROM stored_file_versions v LEFT JOIN stored_files f "
                "ON f.id = v.file_id WHERE f.id IS NULL",
            ),
            "object_file_to_file": _count(
                connection,
                "SELECT count(*) FROM object_files o LEFT JOIN stored_files f "
                "ON f.id = o.file_id WHERE f.id IS NULL",
            ),
        }
        relations["object_file_to_business_object"] = sum(
            int(
                connection.scalar(
                    sa.text(
                        f"SELECT count(*) FROM object_files o LEFT JOIN {quote(table)} target "
                        f"ON CAST(target.{quote(key)} AS TEXT) = o.object_id "
                        "WHERE o.object_type = :object_type AND target."
                        f"{quote(key)} IS NULL"
                    ),
                    {"object_type": object_type},
                )
                or 0
            )
            for object_type, (table, key) in OBJECT_TABLES.items()
        )
        allowed_types = ", ".join(f"'{value}'" for value in OBJECT_TABLES)
        relations["object_file_unknown_type"] = _count(
            connection,
            f"SELECT count(*) FROM object_files WHERE object_type NOT IN ({allowed_types})",
        )
    return {"alembicRevisions": revisions, "tables": counts, "relations": relations}


def _verify_database_files(engine: Engine, data_root: Path, connection=None) -> None:
    storage_root = (Path(data_root).absolute() / "data" / "files").absolute()
    manager = nullcontext(connection) if connection is not None else engine.connect()
    with manager as connection:
        rows = connection.execute(
            sa.text(
                "SELECT storage_path, sha256, size_bytes FROM stored_file_versions "
                "ORDER BY storage_path"
            )
        ).mappings()
        for row in rows:
            raw = str(row["storage_path"] or "")
            relative = PurePosixPath(raw)
            if not raw or relative.is_absolute() or ".." in relative.parts or "\\" in raw:
                raise VerificationError(f"database file integrity failed for path: {raw!r}")
            path = storage_root.joinpath(*relative.parts)
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(storage_root)
            except (OSError, ValueError) as exc:
                raise VerificationError(f"database file integrity failed for path: {raw!r}") from exc
            if not resolved.is_file() or resolved.is_symlink():
                raise VerificationError(f"database file integrity failed for path: {raw!r}")
            if resolved.stat().st_size != int(row["size_bytes"]):
                raise VerificationError(f"database file integrity failed for path: {raw!r}")
            if sha256_file(resolved) != str(row["sha256"]):
                raise VerificationError(f"database file integrity failed for path: {raw!r}")


def _assert_relations(database: dict[str, object]) -> None:
    violations = {
        name: value
        for name, value in database["relations"].items()
        if int(value) != 0
    }
    if violations:
        summary = ", ".join(f"{name}={value}" for name, value in sorted(violations.items()))
        raise VerificationError(f"relation integrity failed: {summary}")


def _validate_manifest(expected: dict[str, object], *, require_package: bool) -> None:
    if expected.get("schemaVersion") != SCHEMA_VERSION:
        raise VerificationError("unsupported manifest schema version")
    if expected.get("fileRoots") != list(BUSINESS_FILE_ROOTS):
        raise VerificationError("manifest file roots do not match the V1 data layout")
    if expected.get("singleFiles") != list(BUSINESS_SINGLE_FILES):
        raise VerificationError("manifest legacy file paths do not match the V1 data layout")
    database = expected.get("database")
    if not isinstance(database, dict):
        raise VerificationError("manifest database snapshot is missing")
    revisions = database.get("alembicRevisions")
    tables = database.get("tables")
    relations = database.get("relations")
    if not isinstance(revisions, list) or len(revisions) != 1 or not isinstance(revisions[0], str):
        raise VerificationError("manifest Alembic revision is invalid")
    if not isinstance(tables, dict) or not REQUIRED_TABLES.issubset(tables):
        raise VerificationError("manifest database table counts are incomplete")
    if not isinstance(relations, dict) or not RELATION_KEYS.issubset(relations):
        raise VerificationError("manifest relation checks are incomplete")
    files = expected.get("files")
    if not isinstance(files, list):
        raise VerificationError("manifest business file list is missing")
    for entry in files:
        if (
            not isinstance(entry, dict)
            or entry.get("root") not in (*BUSINESS_FILE_ROOTS, "APP_DATA_ROOT")
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("sizeBytes"), int)
            or not isinstance(entry.get("sha256"), str)
            or len(entry["sha256"]) != 64
        ):
            raise VerificationError("manifest business file entry is invalid")
        if entry["root"] == "APP_DATA_ROOT" and entry["path"] not in BUSINESS_SINGLE_FILES:
            raise VerificationError("manifest legacy business file entry is invalid")
    if require_package:
        package_files = expected.get("packageFiles")
        if not isinstance(package_files, list) or not any(
            isinstance(entry, dict) and entry.get("path") == "database.dump"
            for entry in package_files
        ):
            raise VerificationError("package manifest is missing database.dump")


def create_snapshot(
    engine: Engine,
    data_root: Path,
    package_root: Path | None = None,
    *,
    connection=None,
) -> dict[str, object]:
    database = collect_database_snapshot(engine, connection)
    _assert_relations(database)
    _verify_database_files(engine, data_root, connection)
    manifest: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "fileRoots": list(BUSINESS_FILE_ROOTS),
        "singleFiles": list(BUSINESS_SINGLE_FILES),
        "database": database,
        "files": build_file_manifest(Path(data_root)),
    }
    if package_root is not None:
        manifest["packageFiles"] = build_package_manifest(Path(package_root))
    return manifest


def verify_snapshot(
    engine: Engine, data_root: Path, expected: dict[str, object]
) -> None:
    _validate_manifest(expected, require_package=False)
    actual_database = collect_database_snapshot(engine)
    _assert_relations(actual_database)
    _verify_database_files(engine, data_root)
    if actual_database != expected.get("database"):
        raise VerificationError("database snapshot does not match the backup manifest")
    actual_files = build_file_manifest(Path(data_root))
    if actual_files != expected.get("files"):
        raise VerificationError("file manifest does not match the backup manifest")


def verify_package(package_root: Path, expected: dict[str, object]) -> None:
    _validate_manifest(expected, require_package=True)
    package_files = expected.get("packageFiles")
    if not isinstance(package_files, list):
        raise VerificationError("package manifest is missing")
    if build_package_manifest(Path(package_root)) != package_files:
        raise VerificationError("package manifest does not match the extracted archive")
    payload_files = build_file_manifest(Path(package_root) / "payload")
    if payload_files != expected.get("files"):
        raise VerificationError("package payload files do not match the live file snapshot")


def postgres_dump_command(
    pg_dump_exe: Path, dump_output: Path, snapshot_id: str
) -> list[str]:
    return [
        str(pg_dump_exe),
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        f"--snapshot={snapshot_id}",
        f"--file={dump_output}",
    ]


def create_consistent_postgres_backup(
    engine: Engine,
    data_root: Path,
    package_root: Path,
    output: Path,
    pg_dump_exe: Path,
    dump_output: Path,
) -> dict[str, object]:
    if engine.dialect.name != "postgresql":
        raise VerificationError("exported backup snapshots require PostgreSQL")
    url = engine.url
    if not url.host or not url.username or not url.database or url.query:
        raise VerificationError("backup requires an explicit PostgreSQL host, user and database without query parameters")
    # Do not let shell PGHOST/PGSERVICE/PGHOSTADDR redirect pg_dump away from the snapshot.
    dump_environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    dump_environment.update({
        "PGHOST": url.host, "PGPORT": str(url.port or 5432),
        "PGUSER": url.username, "PGDATABASE": url.database,
        "PGPASSWORD": str(url.password or ""),
    })
    dump_output = Path(dump_output).absolute()
    dump_output.parent.mkdir(parents=True, exist_ok=True)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            snapshot_id = str(connection.scalar(sa.text("SELECT pg_export_snapshot()")))
            if not snapshot_id:
                raise VerificationError("PostgreSQL did not export a backup snapshot")
            completed = subprocess.run(
                postgres_dump_command(pg_dump_exe, dump_output, snapshot_id),
                check=False,
                env=dump_environment,
            )
            if completed.returncode != 0:
                raise VerificationError(
                    f"pg_dump failed with exit code {completed.returncode}"
                )
            manifest = create_snapshot(
                engine,
                data_root,
                package_root,
                connection=connection,
            )
            Path(output).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            return manifest
        finally:
            transaction.rollback()


def _windows_safe_parts(relative: PurePosixPath) -> bool:
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{number}" for number in range(1, 10))
    reserved.update(f"LPT{number}" for number in range(1, 10))
    for part in relative.parts:
        if (
            not part
            or part.endswith((".", " "))
            or any(character in part for character in '<>:"|?*')
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].upper() in reserved
        ):
            return False
    return True


def extract_package(archive: Path, destination: Path) -> None:
    archive = Path(archive).absolute()
    destination = Path(destination).absolute()
    _assert_no_reparse_ancestors(archive)
    _assert_no_reparse_ancestors(destination)
    if destination.exists() and any(destination.iterdir()):
        raise VerificationError("extraction destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_ancestors(destination)
    seen: set[str] = set()
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise VerificationError("archive contains too many entries")
        expanded_bytes = sum(member.file_size for member in members if not member.is_dir())
        free_bytes = shutil.disk_usage(destination.parent).free
        if expanded_bytes > max(0, free_bytes - MIN_FREE_AFTER_EXTRACT):
            raise VerificationError("archive is too large for the staging disk")
        for member in members:
            normalized = member.filename.replace("\\", "/")
            relative = PurePosixPath(normalized)
            mode = member.external_attr >> 16
            if (
                not normalized
                or normalized.startswith("/")
                or relative.is_absolute()
                or ".." in relative.parts
                or not _windows_safe_parts(relative)
                or stat.S_ISLNK(mode)
            ):
                raise VerificationError(f"unsafe archive path: {member.filename}")
            if (
                member.file_size > 0
                and member.compress_size == 0
                and not member.is_dir()
            ):
                raise VerificationError(f"unsafe archive compression: {member.filename}")
            if (
                member.compress_size > 0
                and member.file_size / member.compress_size
                > MAX_ARCHIVE_COMPRESSION_RATIO
            ):
                raise VerificationError(f"unsafe archive compression: {member.filename}")
            key = normalized.rstrip("/").casefold()
            if key in seen:
                raise VerificationError(f"duplicate archive path: {member.filename}")
            seen.add(key)
            target = destination.joinpath(*relative.parts)
            try:
                target.resolve(strict=False).relative_to(destination)
            except ValueError as exc:
                raise VerificationError(f"unsafe archive path: {member.filename}") from exc
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                _assert_no_reparse_ancestors(target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _assert_no_reparse_ancestors(target.parent)
            with source.open(member) as input_file, target.open("xb") as output_file:
                written = 0
                for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                    output_file.write(chunk)
                    written += len(chunk)
                    if written > member.file_size:
                        raise VerificationError(
                            f"archive entry exceeds declared size: {member.filename}"
                        )
                if written != member.file_size:
                    raise VerificationError(
                        f"archive entry size mismatch: {member.filename}"
                    )


def begin_restore(data_root: Path) -> None:
    """Persist an exclusive marker before any restore writes; never reuse it."""
    _assert_no_reparse_ancestors(data_root)
    if not data_root.is_dir():
        raise VerificationError("restore target must be an existing directory")
    marker = data_root / "restore.failed.json"
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"status": "IN_PROGRESS"}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        descriptor = os.open(data_root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _load_manifest(path: Path) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VerificationError("manifest must be a JSON object")
    return value


def _database_url(value: str | None) -> str:
    result = value or os.environ.get("DATABASE_URL")
    if not result:
        raise VerificationError("DATABASE_URL is required")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("begin-restore")
    begin.add_argument("--data-root", required=True, type=Path)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--database-url")
    snapshot.add_argument("--data-root", required=True, type=Path)
    snapshot.add_argument("--package-root", type=Path)
    snapshot.add_argument("--output", required=True, type=Path)
    snapshot.add_argument("--pg-dump-exe", type=Path)
    snapshot.add_argument("--dump-output", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--database-url")
    verify.add_argument("--data-root", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--complete-restore", action="store_true")
    package = commands.add_parser("verify-package")
    package.add_argument("--package-root", required=True, type=Path)
    package.add_argument("--manifest", required=True, type=Path)
    extract = commands.add_parser("extract")
    extract.add_argument("--archive", required=True, type=Path)
    extract.add_argument("--destination", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "begin-restore":
            begin_restore(args.data_root)
        elif args.command == "extract":
            extract_package(args.archive, args.destination)
        elif args.command == "verify-package":
            verify_package(args.package_root, _load_manifest(args.manifest))
        else:
            engine = sa.create_engine(_database_url(args.database_url))
            try:
                if args.command == "snapshot":
                    if (args.pg_dump_exe is None) != (args.dump_output is None):
                        raise VerificationError(
                            "--pg-dump-exe and --dump-output must be supplied together"
                        )
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    if args.pg_dump_exe is not None:
                        if args.package_root is None:
                            raise VerificationError(
                                "--package-root is required for a PostgreSQL backup snapshot"
                            )
                        create_consistent_postgres_backup(
                            engine,
                            args.data_root,
                            args.package_root,
                            args.output,
                            args.pg_dump_exe,
                            args.dump_output,
                        )
                    else:
                        manifest = create_snapshot(
                            engine, args.data_root, args.package_root
                        )
                        args.output.write_text(
                            json.dumps(
                                manifest,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                else:
                    marker = args.data_root / "restore.failed.json"
                    if args.complete_restore:
                        _assert_no_reparse_ancestors(marker)
                        if _load_manifest(marker).get("status") != "IN_PROGRESS":
                            raise VerificationError("restore target has no active restore marker")
                    verify_snapshot(engine, args.data_root, _load_manifest(args.manifest))
                    if args.complete_restore:
                        marker.unlink()
            finally:
                engine.dispose()
        print("Offline backup verification passed.")
        return 0
    except (VerificationError, OSError, ValueError, json.JSONDecodeError, sa.exc.SQLAlchemyError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
