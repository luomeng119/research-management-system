from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.services.files import (
    FileServiceError,
    validate_file_candidate,
    validate_file_name,
)

from .core import (
    BatchConflict,
    SourceSafetyError,
    StructuralMigrationError,
    _canonical_json,
    _prepared_snapshot,
    _refresh_expected_table_report,
    _reflect_table,
    _safe_raw,
    _sha_bytes,
    _table_rows,
    discover_sources,
)


DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_BINARY_TABLES = (
    "standards", "doc_templates", "expense_invoice", "expense_payment"
)
_TEMPLATE_FOLDERS = {"财务模板", "会务模板", "公文模板", "方案模板", "其他模板"}


def _id_value(connection: sa.Connection, value: str) -> uuid.UUID | str:
    return uuid.UUID(value) if connection.dialect.name == "postgresql" else value


def _safe_values(table: sa.Table, **values: Any) -> dict[str, Any]:
    return {name: value for name, value in values.items() if name in table.c}


def _validate_storage_root(source_root: Path, storage_root: Path, *, create: bool) -> None:
    storage_root = storage_root.absolute()
    source = source_root.resolve(strict=True)
    existing_chain = []
    cursor = storage_root
    while True:
        if cursor.exists() or cursor.is_symlink():
            existing_chain.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if any(path.is_symlink() for path in existing_chain):
        raise SourceSafetyError("storage directory chain must not contain symlinks")
    if storage_root.exists():
        if storage_root.is_symlink() or not storage_root.is_dir():
            raise SourceSafetyError("storage root must be a real directory")
        storage = storage_root.resolve(strict=True)
    else:
        cursor = storage_root
        while not cursor.exists() and cursor != cursor.parent:
            if cursor.is_symlink():
                raise SourceSafetyError("storage directory chain must not contain symlinks")
            cursor = cursor.parent
        if cursor.is_symlink():
            raise SourceSafetyError("storage directory chain must not contain symlinks")
        storage = storage_root.resolve(strict=False)
    try:
        source.relative_to(storage)
        raise SourceSafetyError("source root and storage root must not contain each other")
    except ValueError:
        pass
    try:
        storage.relative_to(source)
        raise SourceSafetyError("source root and storage root must not contain each other")
    except ValueError:
        pass
    if create and not storage_root.exists():
        missing: list[Path] = []
        cursor = storage_root
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
    if storage_root.exists():
        current = storage_root
        if current.is_symlink() or not current.is_dir():
            raise SourceSafetyError("storage root must be a real directory")
        if os.name != "nt" and stat.S_IMODE(current.stat().st_mode) & 0o022:
            raise SourceSafetyError("storage root permissions are too broad")


def _open_controlled_chain(root: Path, relative: Path, *, create: bool) -> list[int]:
    absolute = root.absolute()
    if os.name == "nt":
        raise SourceSafetyError("legacy binary migration requires POSIX no-follow storage")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptors = [os.open("/", flags)]
    root_parts = absolute.parts[1:]
    try:
        for index, part in enumerate((*root_parts, *relative.parts), 1):
            parent = descriptors[-1]
            try:
                descriptor = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create or index <= len(root_parts):
                    raise
                os.mkdir(part, 0o700, dir_fd=parent)
                os.chmod(part, 0o700, dir_fd=parent, follow_symlinks=False)
                descriptor = os.open(part, flags, dir_fd=parent)
            details = os.fstat(descriptor)
            if index >= len(root_parts) and stat.S_IMODE(details.st_mode) & 0o022:
                os.close(descriptor)
                raise SourceSafetyError("controlled storage directory permissions are too broad")
            descriptors.append(descriptor)
        return descriptors
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _ensure_private_directory(root: Path, relative: Path) -> Path:
    descriptors = _open_controlled_chain(root, relative, create=True)
    for descriptor in reversed(descriptors):
        os.close(descriptor)
    return root / relative


def _source_path(root: Path, raw: Any) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise SourceSafetyError("invalid source path")
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        raise SourceSafetyError("absolute source path is forbidden")
    relative = PurePosixPath(raw.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SourceSafetyError("source path escapes source root")
    candidate = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SourceSafetyError("symlink source file is forbidden")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise SourceSafetyError("source path escapes source root") from exc
    return relative.as_posix(), candidate


def _folder_matches(connection: sa.Connection) -> dict[str, list[str]]:
    folders = _reflect_table(connection, "reference_template_folders")
    if folders is None:
        raise StructuralMigrationError("target table is missing: reference_template_folders")
    statement = sa.select(folders.c.id, folders.c.name).where(
        folders.c.status == "ACTIVE", folders.c.parent_id.is_(None)
    )
    matches: dict[str, list[str]] = {}
    for row in connection.execute(statement).mappings():
        name = str(row["name"])
        if name in _TEMPLATE_FOLDERS:
            matches.setdefault(name, []).append(str(row["id"]))
    return matches


def _empty_item(table: str, row: dict[str, Any], raw: Any) -> dict[str, Any]:
    key_name = {
        "standards": "doc_id", "doc_templates": "template_id",
        "expense_invoice": "id", "expense_payment": "id",
    }[table]
    object_type = {
        "standards": "STANDARD", "doc_templates": "TEMPLATE",
        "expense_invoice": "INVOICE", "expense_payment": "PAYMENT",
    }[table]
    object_id_value = row.get(key_name)
    source_key_value = object_id_value
    if source_key_value is None or source_key_value == "":
        source_key_value = row.get("id")
    return {
        "sourceTable": table,
        "sourceKey": (
            None if source_key_value is None or source_key_value == ""
            else str(source_key_value)
        ),
        "fieldName": "file_path",
        "sourceLogicalPath": _safe_raw("file_path", raw),
        "exists": False,
        "sizeBytes": None,
        "sha256": None,
        "objectType": object_type,
        "objectId": (
            None if object_id_value is None or object_id_value == ""
            else str(object_id_value)
        ),
        "fileId": None,
        "versionId": None,
        "linkId": None,
        "versionNo": 1,
        "storagePath": None,
        "issueCode": None,
    }


def _target_conflict(connection: sa.Connection, item: dict[str, Any]) -> bool:
    checks = (
        ("stored_files", "id", item["fileId"]),
        ("stored_file_versions", "id", item["versionId"]),
        ("object_files", "id", item["linkId"]),
    )
    for table_name, field, raw_value in checks:
        table = _reflect_table(connection, table_name)
        if table is None:
            raise StructuralMigrationError(f"target table is missing: {table_name}")
        value = _id_value(connection, raw_value)
        if connection.scalar(sa.select(sa.literal(True)).where(table.c[field] == value).limit(1)) is True:
            return True
    files = _reflect_table(connection, "stored_files")
    versions = _reflect_table(connection, "stored_file_versions")
    links = _reflect_table(connection, "object_files")
    file_id = _id_value(connection, item["fileId"])
    business_id = f"LEGACY-FILE-{item['fileId'].replace('-', '').upper()}"
    if connection.scalar(
        sa.select(sa.literal(True)).where(files.c.business_id == business_id).limit(1)
    ) is True:
        return True
    if connection.scalar(
        sa.select(sa.literal(True)).where(
            versions.c.file_id == file_id, versions.c.version_no == 1
        ).limit(1)
    ) is True:
        return True
    if connection.scalar(
        sa.select(sa.literal(True)).where(
            links.c.object_type == item["objectType"],
            links.c.object_id == item["objectId"],
            links.c.file_id == file_id,
        ).limit(1)
    ) is True:
        return True
    if item["objectType"] == "TEMPLATE":
        templates = _reflect_table(connection, "reference_template_items")
        if templates is None:
            raise StructuralMigrationError("target table is missing: reference_template_items")
        if connection.scalar(
            sa.select(sa.literal(True)).where(
                templates.c.template_id == item["objectId"]
            ).limit(1)
        ) is True:
            return True
        folder_id = _id_value(connection, item["folderId"])
        if connection.scalar(
            sa.select(sa.literal(True)).where(
                templates.c.folder_id == folder_id,
                sa.func.lower(templates.c.display_name)
                == item["displayName"].lower(),
                templates.c.status == "ACTIVE",
            ).limit(1)
        ) is True:
            return True
    return False


def build_binary_plan(
    connection: sa.Connection,
    source_root: Path,
    manifest: dict[str, Any],
    *,
    max_bytes: int,
) -> dict[str, Any]:
    sources = discover_sources(source_root)
    research = next(source for source in sources if source.name == "research.db")
    expense = next(source for source in sources if source.name == "expense.db")
    folders = _folder_matches(connection)
    items: list[dict[str, Any]] = []
    validated = 0
    for table_name in _BINARY_TABLES:
        source = expense if table_name.startswith("expense_") else research
        for row in _table_rows(source, table_name):
            raw = row.get("file_path")
            if raw in (None, ""):
                continue
            item = _empty_item(table_name, row, raw)
            items.append(item)
            if not item["objectId"]:
                item["issueCode"] = "MISSING_OBJECT_ID"
                continue
            if table_name == "doc_templates":
                category_matches = folders.get(str(row.get("category") or ""), [])
                if len(category_matches) != 1:
                    item["issueCode"] = "TEMPLATE_CATEGORY_UNRESOLVED"
                    continue
                item["folderId"] = category_matches[0]
                item["displayName"] = str(row.get("name") or "").strip()
                if not item["displayName"]:
                    item["issueCode"] = "MISSING_TEMPLATE_NAME"
                    continue
            try:
                logical, path = _source_path(source_root, raw)
                item["sourceLogicalPath"] = logical
                validate_file_name(Path(logical).name)
                item["exists"] = path.is_file()
                if not item["exists"]:
                    item["issueCode"] = "SOURCE_FILE_MISSING"
                    continue
                name, extension, media_type, size_bytes, digest = validate_file_candidate(
                    path, Path(logical).name, max_bytes=max_bytes
                )
            except FileServiceError as exc:
                item["issueCode"] = exc.code
                continue
            except SourceSafetyError:
                item["issueCode"] = "INVALID_SOURCE_PATH"
                continue
            validated += 1
            item.update({
                "sizeBytes": size_bytes,
                "sha256": digest,
                "originalName": name,
                "extension": extension,
                "mediaType": media_type,
            })
            seed = ":".join((
                manifest["sha256"], table_name, str(item["sourceKey"]),
                item["objectType"], item["objectId"], digest,
            ))
            file_id = uuid.uuid5(uuid.NAMESPACE_URL, "legacy-file:" + seed)
            item.update({
                "fileId": str(file_id),
                "versionId": str(uuid.uuid5(uuid.NAMESPACE_URL, "legacy-version:" + seed)),
                "linkId": str(uuid.uuid5(uuid.NAMESPACE_URL, "legacy-link:" + seed)),
                "storagePath": (
                    f"legacy/{manifest['sha256'][:12]}/{file_id}/v1{extension}"
                ),
            })
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        if item["issueCode"] is None:
            grouped.setdefault((item["objectType"], item["objectId"]), []).append(item)
    for candidates in grouped.values():
        if len(candidates) > 1:
            for item in candidates:
                item["issueCode"] = "MULTIPLE_OBJECT_CANDIDATES"
    for item in items:
        if item["issueCode"] is None and _target_conflict(connection, item):
            item["issueCode"] = "TARGET_CONFLICT"
    items.sort(key=lambda item: (item["sourceTable"], str(item["sourceKey"])))
    planned = [item for item in items if item["issueCode"] is None]
    payload = {
        "sourceManifestSha256": manifest["sha256"],
        "items": items,
        "summary": {
            "referenced": len(items),
            "eligible": validated,
            "planned": len(planned),
            "issues": len(items) - len(planned),
            "totalBytes": sum(item["sizeBytes"] for item in planned),
        },
    }
    return {**payload, "sha256": _sha_bytes(_canonical_json(payload).encode())}


def validate_binary_object_targets(
    connection: sa.Connection, plan: dict[str, Any]
) -> None:
    target_tables = {
        "INVOICE": "expense_invoice", "PAYMENT": "expense_payment"
    }
    for item in plan["items"]:
        table_name = target_tables.get(item["objectType"])
        if table_name is None or item["issueCode"] is not None:
            continue
        table = _reflect_table(connection, table_name)
        if table is None:
            raise StructuralMigrationError(f"target table is missing: {table_name}")
        try:
            object_id = int(item["objectId"])
        except (TypeError, ValueError):
            item["issueCode"] = "OBJECT_NOT_MIGRATED"
            continue
        exists = connection.scalar(
            sa.select(sa.literal(True)).where(table.c.id == object_id).limit(1)
        )
        if exists is not True:
            item["issueCode"] = "OBJECT_NOT_MIGRATED"
    payload = {
        "sourceManifestSha256": plan["sourceManifestSha256"],
        "items": plan["items"],
        "summary": {
            "referenced": len(plan["items"]),
            "eligible": sum(
                item.get("sha256") is not None for item in plan["items"]
            ),
            "planned": sum(
                item["issueCode"] is None for item in plan["items"]
            ),
            "issues": sum(
                item["issueCode"] is not None for item in plan["items"]
            ),
            "totalBytes": sum(
                item["sizeBytes"] for item in plan["items"]
                if item["issueCode"] is None
            ),
        },
    }
    plan.update(payload)
    plan["sha256"] = _sha_bytes(_canonical_json(payload).encode())


def plan_legacy_binaries(
    engine: Engine,
    source_root: str | Path,
    storage_root: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_test_sqlite: bool = False,
) -> dict[str, Any]:
    if engine.dialect.name != "postgresql" and not allow_test_sqlite:
        raise SourceSafetyError("migration target must be PostgreSQL")
    source = Path(source_root)
    storage = Path(storage_root)
    _validate_storage_root(source, storage, create=False)
    with _prepared_snapshot(source) as (snapshot_root, manifest):
        with engine.connect() as connection:
            return build_binary_plan(
                connection, snapshot_root, manifest, max_bytes=max_bytes
            )


def prepare_binary_files(
    source_root: Path,
    storage_root: Path,
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    planned = [dict(item) for item in plan["items"] if item["issueCode"] is None]
    if not planned:
        return []
    _validate_storage_root(source_root, storage_root, create=True)
    staging = _ensure_private_directory(storage_root, Path(".staging"))
    staging_descriptors = _open_controlled_chain(
        storage_root, Path(".staging"), create=False
    )
    staging_fd = staging_descriptors[-1]
    prepared: list[dict[str, Any]] = []
    try:
        for item in planned:
            _, source = _source_path(source_root, item["sourceLogicalPath"])
            stage = staging / f"{item['fileId']}-{uuid.uuid4().hex}.part"
            item["stagePath"] = stage
            item["stageName"] = stage.name
            item["stageDirFd"] = os.dup(staging_fd)
            item["stageIdentity"] = None
            prepared.append(item)
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            destination_fd = os.open(
                stage.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                dir_fd=staging_fd,
            )
            try:
                item["stageIdentity"] = _cleanup_identity(os.fstat(destination_fd))
                while chunk := os.read(source_fd, 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(destination_fd, view):]
                os.fsync(destination_fd)
            finally:
                os.close(source_fd)
                os.close(destination_fd)
            observed = validate_file_candidate(
                stage, item["originalName"], max_bytes=max(item["sizeBytes"], 1)
            )
            if observed[3] != item["sizeBytes"] or observed[4] != item["sha256"]:
                raise SourceSafetyError("binary source identity changed while staging")
        return prepared
    except Exception:
        cleanup_prepared(prepared)
        raise
    finally:
        for descriptor in reversed(staging_descriptors):
            os.close(descriptor)


def binary_issues(plan: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in plan["items"]:
        if item["issueCode"] is None:
            continue
        result.append({
            "source_table": item["sourceTable"],
            "source_key": item["sourceKey"],
            "field_name": item["fieldName"],
            "reason": item["issueCode"],
            "raw_value": (
                item["sourceLogicalPath"]
                if isinstance(item["sourceLogicalPath"], str)
                and item["sourceLogicalPath"].startswith("{")
                else None
            ),
        })
    return result


def apply_binary_metadata(
    connection: sa.Connection,
    prepared: list[dict[str, Any]],
    report: dict[str, Any],
    expected_rows: dict[str, list[dict[str, Any]]],
) -> None:
    tables = {
        name: _reflect_table(connection, name)
        for name in (
            "reference_template_items", "stored_files", "stored_file_versions",
            "object_files", "standards", "doc_templates",
            "expense_invoice", "expense_payment",
        )
    }
    if any(tables[name] is None for name in (
        "reference_template_items", "stored_files", "stored_file_versions", "object_files"
    )):
        raise StructuralMigrationError("target controlled-file schema is incomplete")
    inserted: dict[str, list[dict[str, Any]]] = {
        "reference_template_items": [], "stored_files": [],
        "stored_file_versions": [], "object_files": [],
    }
    updated_legacy_tables: set[str] = set()
    for item in prepared:
        if item["objectType"] == "TEMPLATE":
            template_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"legacy-reference-template:{report['source_manifest_sha256']}:{item['objectId']}",
            )
            values = _safe_values(
                tables["reference_template_items"],
                id=_id_value(connection, str(template_uuid)),
                template_id=item["objectId"], folder_id=_id_value(connection, item["folderId"]),
                display_name=item["displayName"], status="ACTIVE", version=1,
            )
            connection.execute(tables["reference_template_items"].insert().values(**values))
            inserted["reference_template_items"].append(values)
        file_id = _id_value(connection, item["fileId"])
        file_values = _safe_values(
            tables["stored_files"], id=file_id,
            business_id=f"LEGACY-FILE-{item['fileId'].replace('-', '').upper()}",
            original_name=item["originalName"], media_type=item["mediaType"],
            status="ACTIVE", version=1,
        )
        version_values = _safe_values(
            tables["stored_file_versions"], id=_id_value(connection, item["versionId"]),
            file_id=file_id, version_no=1, storage_path=item["storagePath"],
            sha256=item["sha256"], size_bytes=item["sizeBytes"],
            media_type=item["mediaType"], version=1,
        )
        link_values = _safe_values(
            tables["object_files"], id=_id_value(connection, item["linkId"]),
            object_type=item["objectType"], object_id=item["objectId"],
            file_id=file_id, purpose="LEGACY_IMPORT", version=1,
        )
        connection.execute(tables["stored_files"].insert().values(**file_values))
        connection.execute(tables["stored_file_versions"].insert().values(**version_values))
        connection.execute(tables["object_files"].insert().values(**link_values))
        inserted["stored_files"].append(file_values)
        inserted["stored_file_versions"].append(version_values)
        inserted["object_files"].append(link_values)
        legacy_table = tables[item["sourceTable"]]
        key_name = {
            "standards": "doc_id", "doc_templates": "template_id",
            "expense_invoice": "id", "expense_payment": "id",
        }[item["sourceTable"]]
        key_value: Any = item["objectId"]
        if item["sourceTable"].startswith("expense_"):
            key_value = int(key_value)
        if legacy_table is not None and "file_path" in legacy_table.c:
            connection.execute(
                legacy_table.update().where(
                    legacy_table.c[key_name] == key_value
                ).values(file_path=None)
            )
            for expected in expected_rows.get(item["sourceTable"], []):
                if expected.get(key_name) == key_value:
                    expected["file_path"] = None
            updated_legacy_tables.add(item["sourceTable"])
    for table_name in sorted(updated_legacy_tables):
        _refresh_expected_table_report(table_name, report, expected_rows)
    for table_name, rows in inserted.items():
        if not rows:
            continue
        table = tables[table_name]
        ids = [row["id"] for row in rows]
        captured_rows = [
            dict(row) for row in connection.execute(
                sa.select(table).where(table.c.id.in_(ids))
            ).mappings()
        ]
        captured_rows.sort(key=lambda row: str(row["id"]))
        expected_rows.setdefault(table_name, []).extend(captured_rows)
        target_columns = sorted(table.c.keys())
        key_values = [[str(row["id"])] for row in captured_rows]
        report["tables"][table_name] = {
            "source": len(rows), "converted": len(rows), "inserted": len(rows),
            "reused": 0, "rejected": 0,
            "normalized_sha256": _sha_bytes(_canonical_json(sorted(
                _sha_bytes(_canonical_json(row).encode()) for row in captured_rows
            )).encode()),
            "primary_keys_sha256": _sha_bytes(_canonical_json(key_values).encode()),
            "natural_keys_sha256": _sha_bytes(b"[]"),
            "target_columns": target_columns,
            "migration_key_fields": ["id"],
            "migration_key_values": key_values,
        }
        report["counts"]["source"] += len(rows)
        report["counts"]["converted"] += len(rows)
        report["counts"]["inserted"] += len(rows)


def finalize_binary_files(
    storage_root: Path,
    prepared: list[dict[str, Any]],
    *,
    fail_after: int | None = None,
) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(prepared, 1):
            relative = Path(item["storagePath"])
            parent_descriptors = _open_controlled_chain(
                storage_root, relative.parent, create=True
            )
            parent_fd = parent_descriptors[-1]
            staging_fd = item["stageDirFd"]
            destination = storage_root / relative
            try:
                existing = os.stat(
                    relative.name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                existing = None
            try:
                if existing is not None:
                    if not stat.S_ISREG(existing.st_mode):
                        raise BatchConflict("legacy binary target conflict")
                    size, digest = _read_physical_identity(storage_root, relative)
                    if size != item["sizeBytes"] or digest != item["sha256"]:
                        raise BatchConflict("legacy binary orphan content conflict")
                else:
                    cleanup_entry = {
                        "dirFd": os.dup(parent_fd),
                        "name": relative.name,
                        "path": destination,
                        "identity": item["stageIdentity"],
                    }
                    created.append(cleanup_entry)
                    os.link(
                        item["stageName"], relative.name,
                        src_dir_fd=staging_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    linked_identity = _cleanup_identity(os.stat(
                        relative.name, dir_fd=parent_fd, follow_symlinks=False
                    ))
                    if linked_identity != cleanup_entry["identity"]:
                        raise SourceSafetyError(
                            "binary target identity changed while finalizing"
                        )
                os.unlink(item["stageName"], dir_fd=staging_fd)
            finally:
                for descriptor in reversed(parent_descriptors):
                    os.close(descriptor)
            if fail_after is not None and index >= fail_after:
                raise RuntimeError("injected binary failure")
        return created
    except Exception:
        cleanup_created_files(created)
        cleanup_prepared(prepared)
        raise


def cleanup_prepared(prepared: list[dict[str, Any]]) -> None:
    for item in prepared:
        descriptor = item.get("stageDirFd")
        if isinstance(descriptor, int):
            try:
                _unlink_registered(
                    descriptor, item["stageName"], item.get("stageIdentity")
                )
            finally:
                os.close(descriptor)
                item["stageDirFd"] = None
            continue
        stage = item.get("stagePath")
        if isinstance(stage, Path):
            stage.unlink(missing_ok=True)


def cleanup_created_files(created: list[dict[str, Any]], *, remove: bool = True) -> None:
    for item in reversed(created):
        descriptor = item.get("dirFd")
        if not isinstance(descriptor, int):
            continue
        try:
            if remove:
                _unlink_registered(descriptor, item["name"], item.get("identity"))
        finally:
            os.close(descriptor)
            item["dirFd"] = None


def _cleanup_identity(details: os.stat_result) -> tuple[int, int, int]:
    return details.st_dev, details.st_ino, stat.S_IFMT(details.st_mode)


def _unlink_registered(
    directory_fd: int, name: str, expected: tuple[int, int, int] | None
) -> None:
    if expected is None:
        return
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if current is not None and _cleanup_identity(current) == expected:
        os.unlink(name, dir_fd=directory_fd)
        return
    for candidate in sorted(os.listdir(directory_fd)):
        try:
            details = os.stat(
                candidate, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            continue
        if _cleanup_identity(details) == expected:
            os.unlink(candidate, dir_fd=directory_fd)
            return


def _same_open_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _read_physical_identity(storage_root: Path, relative: Path) -> tuple[int, str]:
    import hashlib

    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptors = _open_controlled_chain(
        storage_root, relative.parent, create=False
    )
    try:
        file_fd = os.open(relative.name, file_flags, dir_fd=descriptors[-1])
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise BatchConflict("physical file path is unsafe")
        digest = hashlib.sha256()
        while chunk := os.read(file_fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(file_fd)
        if not _same_open_stat(before, after):
            raise BatchConflict("physical file identity changed during verification")
        return after.st_size, digest.hexdigest()
    except OSError as exc:
        raise BatchConflict("physical file is missing") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def physical_fingerprint(storage_root: Path, items: list[dict[str, Any]]) -> dict[str, Any]:
    files = []
    for item in items:
        relative = Path(item["storagePath"])
        if relative.is_absolute() or ".." in relative.parts:
            raise BatchConflict("physical file path is unsafe")
        current = storage_root
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink() or not current.is_dir():
                raise BatchConflict("physical file path is unsafe")
            if os.name != "nt" and stat.S_IMODE(current.stat().st_mode) & 0o022:
                raise BatchConflict("physical file storage permissions are too broad")
        size, digest = _read_physical_identity(storage_root, relative)
        if size != item["sizeBytes"] or digest != item["sha256"]:
            raise BatchConflict("physical file integrity mismatch")
        files.append({
            "storagePath": item["storagePath"], "sizeBytes": size, "sha256": digest
        })
    files.sort(key=lambda value: value["storagePath"])
    payload = {"files": files}
    return {**payload, "sha256": _sha_bytes(_canonical_json(payload).encode())}


def verify_binary_report(report: dict[str, Any], storage_root: str | Path | None) -> None:
    binaries = report.get("binaries")
    if not binaries or not binaries.get("items"):
        return
    if storage_root is None:
        raise BatchConflict("physical file storage root is required")
    root = Path(storage_root)
    if not root.is_dir() or root.is_symlink():
        raise BatchConflict("physical file storage root is invalid")
    if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) & 0o022:
        raise BatchConflict("physical file storage permissions are too broad")
    observed = physical_fingerprint(root, binaries["items"])
    if observed != binaries.get("physical_files"):
        raise BatchConflict("physical file fingerprint mismatch")
