from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import sqlalchemy as sa


SCRIPT = Path(__file__).resolve().parents[1] / "verify_offline.py"


@pytest.mark.skipif(not os.environ.get("ACCEPTANCE_BACKUP_ROOT"), reason="requires a real acceptance backup")
def test_real_backup_rejects_corrupted_dump_without_modifying_original(tmp_path):
    module = _load_module()
    original = Path(os.environ["ACCEPTANCE_BACKUP_ROOT"])
    original_dump_hash = module.sha256_file(original / "database.dump")
    package = tmp_path / "damaged-package"
    shutil.copytree(original, package)
    with (package / "database.dump").open("r+b") as stream:
        stream.write(b"BAD")
    result = subprocess.run([
        sys.executable, str(SCRIPT), "verify-package", "--package-root", str(package),
        "--manifest", str(package / "manifest.json"),
    ], capture_output=True, text=True)
    assert result.returncode != 0
    assert "package manifest does not match" in result.stderr
    assert module.sha256_file(original / "database.dump") == original_dump_hash
    module.verify_package(original, json.loads((original / "manifest.json").read_text()))


@pytest.mark.skipif(not os.environ.get("T02_ISOLATED_POSTGRES_ROOT"), reason="requires owned PostgreSQL harness")
def test_real_dump_uses_snapshot_database_despite_conflicting_pg_environment(tmp_path, monkeypatch):
    """Catches pg_dump connecting via stale shell PG* instead of the snapshot engine."""
    module = _load_module()
    engine = sa.create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            database, port = connection.execute(sa.text(
                "SELECT current_database(), inet_server_port()"
            )).one()
        assert database == os.environ["T02_ISOLATED_POSTGRES_DATABASE"]
        assert port == int(os.environ["T02_ISOLATED_POSTGRES_PORT"])
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        package = tmp_path / "package"
        shutil.copytree(runtime, package / "payload")
        monkeypatch.setenv("PGHOST", "127.0.0.1")
        monkeypatch.setenv("PGPORT", "9")
        monkeypatch.setenv("PGDATABASE", "wrong_backup_database")
        monkeypatch.setenv("PGUSER", "wrong_backup_user")
        dump = package / "database.dump"
        manifest = module.create_consistent_postgres_backup(
            engine, runtime, package, package / "manifest.json", Path(shutil.which("pg_dump")), dump,
        )
        module.verify_package(package, manifest)
        listed = subprocess.run([shutil.which("pg_restore"), "--list", str(dump)], capture_output=True, text=True, check=True)
        assert "TABLE DATA public proposals" in listed.stdout
        assert manifest["database"]["relations"]["project_registry_to_category_table"] == 0
    finally:
        engine.dispose()


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_offline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_contract_database(path: Path) -> str:
    url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    statements = (
        "CREATE TABLE alembic_version (version_num TEXT NOT NULL)",
        "CREATE TABLE proposals (id TEXT PRIMARY KEY, business_id TEXT)",
        "CREATE TABLE project_registry (id TEXT PRIMARY KEY, category TEXT, business_id TEXT, proposal_id TEXT)",
        "CREATE TABLE projects (project_id TEXT, registry_id TEXT)",
        "CREATE TABLE security_projects (project_id TEXT, registry_id TEXT)",
        "CREATE TABLE crypto_projects (project_id TEXT, registry_id TEXT)",
        "CREATE TABLE equipment (equipment_id TEXT)",
        "CREATE TABLE equipment_groups (group_id TEXT, project_id TEXT)",
        "CREATE TABLE equipment_group_members (group_id TEXT, equipment_id TEXT)",
        "CREATE TABLE expert_groups (group_id TEXT)",
        "CREATE TABLE experts (expert_id TEXT)",
        "CREATE TABLE expert_group_members (group_id TEXT, expert_id TEXT)",
        "CREATE TABLE expense_reimbursement (id INTEGER PRIMARY KEY)",
        "CREATE TABLE expense_invoice (id INTEGER PRIMARY KEY, reimbursement_id INTEGER)",
        "CREATE TABLE expense_invoice_item (id INTEGER PRIMARY KEY, invoice_id INTEGER)",
        "CREATE TABLE expense_payment (id INTEGER PRIMARY KEY, reimbursement_id INTEGER)",
        "CREATE TABLE standards (doc_id TEXT)",
        "CREATE TABLE reference_template_items (template_id TEXT)",
        "CREATE TABLE generic_tables (table_id TEXT)",
        "CREATE TABLE project_documents (doc_id TEXT)",
        "CREATE TABLE stored_files (id TEXT PRIMARY KEY)",
        "CREATE TABLE stored_file_versions (file_id TEXT, storage_path TEXT, sha256 TEXT, size_bytes INTEGER)",
        "CREATE TABLE object_files (object_type TEXT, object_id TEXT, file_id TEXT)",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(sa.text(statement))
        connection.execute(sa.text("INSERT INTO alembic_version VALUES ('0009_equipment_import_batches')"))
        connection.execute(sa.text("INSERT INTO proposals VALUES ('proposal-1', 'PROP-1')"))
        connection.execute(sa.text("INSERT INTO project_registry VALUES ('registry-1', 'GENERAL_RESEARCH', 'PRJ-1', 'proposal-1')"))
        connection.execute(sa.text("INSERT INTO projects VALUES ('PRJ-1', 'registry-1')"))
        connection.execute(sa.text("INSERT INTO equipment VALUES ('EQ-1')"))
        connection.execute(sa.text("INSERT INTO equipment_groups VALUES ('GROUP-1', 'PRJ-1')"))
        connection.execute(sa.text("INSERT INTO equipment_group_members VALUES ('GROUP-1', 'EQ-1')"))
        connection.execute(sa.text("INSERT INTO expert_groups VALUES ('EXPERT-GROUP-1')"))
        connection.execute(sa.text("INSERT INTO experts VALUES ('EXPERT-1')"))
        connection.execute(sa.text("INSERT INTO expert_group_members VALUES ('EXPERT-GROUP-1', 'EXPERT-1')"))
        connection.execute(sa.text("INSERT INTO expense_reimbursement VALUES (1)"))
        connection.execute(sa.text("INSERT INTO expense_invoice VALUES (2, 1)"))
        connection.execute(sa.text("INSERT INTO expense_invoice_item VALUES (3, 2)"))
        connection.execute(sa.text("INSERT INTO expense_payment VALUES (4, 1)"))
        connection.execute(sa.text("INSERT INTO stored_files VALUES ('file-1')"))
        connection.execute(sa.text("INSERT INTO object_files VALUES ('EXPENSE', '1', 'file-1')"))
    engine.dispose()
    return url


def test_snapshot_and_verify_compare_database_relations_and_all_business_file_hashes(tmp_path):
    """Catches a verifier that ignores equipment/project relations or legacy business files."""
    module = _load_module()
    data_root = tmp_path / "runtime"
    controlled = data_root / "data" / "files" / "2026" / "09"
    controlled.mkdir(parents=True)
    stored = controlled / "report.pdf"
    stored.write_bytes(b"%PDF-research")
    upload = data_root / "uploads" / "PRJ-1" / "source.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("source", encoding="utf-8")
    document = data_root / "documents" / "result.docx"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"document")
    data_document = data_root / "data" / "documents" / "argumentation.docx"
    data_document.parent.mkdir(parents=True)
    data_document.write_bytes(b"argumentation")
    data_template = data_root / "data" / "templates" / "template.docx"
    data_template.parent.mkdir(parents=True)
    data_template.write_bytes(b"template")
    legacy_database = data_root / "data" / "research.db"
    legacy_database.write_bytes(b"legacy-sqlite")

    database_url = _create_contract_database(tmp_path / "contract.sqlite")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO stored_file_versions VALUES "
                "('file-1', '2026/09/report.pdf', :sha256, :size_bytes)"
            ),
            {
                "sha256": module.sha256_file(stored),
                "size_bytes": stored.stat().st_size,
            },
        )

    manifest = module.create_snapshot(engine, data_root)
    module.verify_snapshot(engine, data_root, manifest)

    assert manifest["database"]["tables"]["equipment_group_members"] == 1
    assert manifest["database"]["tables"]["expense_invoice"] == 1
    assert manifest["database"]["relations"]["equipment_group_to_project"] == 0
    assert {entry["root"] for entry in manifest["files"]} == {
        "data/files",
        "uploads",
        "documents",
        "data/documents",
        "data/templates",
        "APP_DATA_ROOT",
    }
    assert manifest["fileRoots"] == [
        "data/files", "uploads", "documents", "data/documents", "data/templates"
    ]
    assert "data/research.db" in manifest["singleFiles"]

    upload.write_text("changed", encoding="utf-8")
    with pytest.raises(module.VerificationError, match="file manifest"):
        module.verify_snapshot(engine, data_root, manifest)
    engine.dispose()


def test_snapshot_rejects_broken_finance_and_equipment_relations(tmp_path):
    """Catches accepting a backup whose simple finance or equipment links are already broken."""
    module = _load_module()
    data_root = tmp_path / "runtime"
    for relative in ("data/files", "uploads", "documents"):
        (data_root / relative).mkdir(parents=True)
    database_url = _create_contract_database(tmp_path / "broken.sqlite")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO equipment_group_members VALUES ('GROUP-1', 'MISSING')"))
        connection.execute(sa.text("INSERT INTO expense_payment VALUES (5, 999)"))
        connection.execute(sa.text("INSERT INTO object_files VALUES ('PAYMENT', '999', 'file-1')"))

    with pytest.raises(module.VerificationError, match="relation integrity"):
        module.create_snapshot(engine, data_root)
    engine.dispose()


def test_snapshot_rejects_finance_attachment_linked_to_missing_business_object(tmp_path):
    """Catches checking an attachment's file while ignoring its deleted finance parent."""
    module = _load_module()
    data_root = tmp_path / "runtime"
    for relative in ("data/files", "uploads", "documents"):
        (data_root / relative).mkdir(parents=True)
    database_url = _create_contract_database(tmp_path / "orphan-attachment.sqlite")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO object_files VALUES ('PAYMENT', '999', 'file-1')")
        )

    with pytest.raises(module.VerificationError, match="business_object"):
        module.create_snapshot(engine, data_root)
    engine.dispose()


def test_package_verification_and_safe_extraction_reject_tampering_and_path_escape(tmp_path):
    """Catches trusting a modified dump or extracting an archive outside staging."""
    module = _load_module()
    package = tmp_path / "package"
    package.mkdir()
    dump = package / "database.dump"
    dump.write_bytes(b"postgres-custom-dump")
    payload = package / "payload" / "uploads" / "a.txt"
    payload.parent.mkdir(parents=True)
    payload.write_text("attachment", encoding="utf-8")
    manifest = {
        "schemaVersion": 1,
        "fileRoots": list(module.BUSINESS_FILE_ROOTS),
        "singleFiles": list(module.BUSINESS_SINGLE_FILES),
        "database": {
            "alembicRevisions": ["0009"],
            "tables": {name: 0 for name in module.REQUIRED_TABLES},
            "relations": {name: 0 for name in module.RELATION_KEYS},
        },
        "files": module.build_file_manifest(package / "payload"),
        "packageFiles": module.build_package_manifest(package),
    }
    module.verify_package(package, manifest)
    dump.write_bytes(b"tampered")
    with pytest.raises(module.VerificationError, match="package manifest"):
        module.verify_package(package, manifest)

    import zipfile

    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", "bad")
    with pytest.raises(module.VerificationError, match="unsafe archive path"):
        module.extract_package(archive, tmp_path / "extract")
    assert not (tmp_path / "escape.txt").exists()


def test_package_verification_compares_payload_to_live_file_snapshot(tmp_path):
    """Catches cross-store drift between the copied attachments and the final snapshot."""
    module = _load_module()
    package = tmp_path / "package"
    payload = package / "payload"
    for relative in ("data/files", "uploads", "documents"):
        (payload / relative).mkdir(parents=True)
    (package / "database.dump").write_bytes(b"dump")
    file_path = payload / "uploads" / "source.txt"
    file_path.write_text("copied first", encoding="utf-8")
    manifest = {
        "schemaVersion": 1,
        "fileRoots": list(module.BUSINESS_FILE_ROOTS),
        "singleFiles": list(module.BUSINESS_SINGLE_FILES),
        "database": {
            "alembicRevisions": ["0009"],
            "tables": {name: 0 for name in module.REQUIRED_TABLES},
            "relations": {name: 0 for name in module.RELATION_KEYS},
        },
        "files": [
            {
                "root": "uploads",
                "path": "source.txt",
                "sizeBytes": len(b"changed later"),
                "sha256": __import__("hashlib").sha256(b"changed later").hexdigest(),
            }
        ],
        "packageFiles": module.build_package_manifest(package),
    }
    with pytest.raises(module.VerificationError, match="payload files"):
        module.verify_package(package, manifest)


def test_package_verification_rejects_wrong_contract_before_restore(tmp_path):
    """Catches accepting a self-consistent archive with the wrong manifest contract."""
    module = _load_module()
    package = tmp_path / "package"
    package.mkdir()
    (package / "database.dump").write_bytes(b"dump")
    invalid = {
        "schemaVersion": 999,
        "fileRoots": ["uploads"],
        "database": {},
        "files": [],
        "packageFiles": module.build_package_manifest(package),
    }
    with pytest.raises(module.VerificationError, match="schema version"):
        module.verify_package(package, invalid)


@pytest.mark.parametrize("unsafe_name", ["payload/uploads/report.txt:stream", "payload/uploads/CON.txt", "payload/uploads/name. "])
def test_extraction_rejects_windows_unsafe_names(tmp_path, unsafe_name):
    """Catches archive names that alias or create ADS files on Windows."""
    module = _load_module()
    import zipfile

    archive = tmp_path / "unsafe-windows.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(unsafe_name, "bad")
    with pytest.raises(module.VerificationError, match="unsafe archive path"):
        module.extract_package(archive, tmp_path / "extract")


def test_windows_reparse_attribute_is_rejected_even_when_not_a_symlink():
    """Catches treating a Windows junction as an ordinary directory."""
    module = _load_module()
    fake = SimpleNamespace(st_file_attributes=0x400)
    assert module.is_reparse_stat(fake)


def test_postgres_snapshot_command_uses_the_exported_snapshot_and_custom_format(tmp_path):
    """Catches pg_dump drifting from the repeatable-read snapshot used by the manifest."""
    module = _load_module()
    command = module.postgres_dump_command(
        Path("C:/PostgreSQL/bin/pg_dump.exe"), tmp_path / "database.dump", "00000003-1"
    )
    assert "--format=custom" in command
    assert "--snapshot=00000003-1" in command
    assert "--no-password" in command
    assert all("postgresql://" not in value for value in command)


def test_postgres_backup_manifest_uses_the_connection_that_exported_the_dump_snapshot(
    tmp_path, monkeypatch
):
    """Catches generating database counts from a later live connection."""
    module = _load_module()

    class Transaction:
        def __init__(self):
            self.rolled_back = False

        def rollback(self):
            self.rolled_back = True

    class Connection:
        def __init__(self):
            self.transaction = Transaction()
            self.statements = []

        def begin(self):
            return self.transaction

        def exec_driver_sql(self, statement):
            self.statements.append(statement)

        def scalar(self, statement):
            self.statements.append(str(statement))
            return "00000003-1"

    class ConnectionContext:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self.connection

        def __exit__(self, *_args):
            return False

    connection = Connection()

    class Engine:
        dialect = SimpleNamespace(name="postgresql")
        url = sa.engine.make_url("postgresql://backup@127.0.0.1/owned_backup")

        def connect(self):
            return ConnectionContext(connection)

    observed = {}

    def snapshot(_engine, _data_root, _package_root, *, connection=None):
        observed["connection"] = connection
        return {"sameSnapshot": True}

    def run(command, check, env):
        observed["command"] = command
        observed["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module, "create_snapshot", snapshot)
    monkeypatch.setattr(module.subprocess, "run", run)
    output = tmp_path / "manifest.json"
    module.create_consistent_postgres_backup(
        Engine(), tmp_path / "runtime", tmp_path / "package", output,
        Path("pg_dump.exe"), tmp_path / "database.dump",
    )

    assert connection.statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert "pg_export_snapshot" in connection.statements[1]
    assert observed["connection"] is connection
    assert "--snapshot=00000003-1" in observed["command"]
    assert connection.transaction.rolled_back
    assert json.loads(output.read_text(encoding="utf-8")) == {"sameSnapshot": True}


def test_cli_verify_returns_nonzero_after_a_restored_file_changes(tmp_path):
    """Catches a CLI that reports success without comparing the restored live surface."""
    data_root = tmp_path / "runtime"
    for relative in ("data/files", "uploads", "documents"):
        (data_root / relative).mkdir(parents=True)
    business_file = data_root / "documents" / "result.txt"
    business_file.write_text("original", encoding="utf-8")
    database_url = _create_contract_database(tmp_path / "cli.sqlite")
    manifest = tmp_path / "manifest.json"

    snapshot = subprocess.run(
        [sys.executable, str(SCRIPT), "snapshot", "--database-url", database_url,
         "--data-root", str(data_root), "--output", str(manifest)],
        text=True, capture_output=True, check=False,
    )
    assert snapshot.returncode == 0, snapshot.stderr
    assert "databaseUrl" not in json.loads(manifest.read_text(encoding="utf-8"))

    business_file.write_text("changed", encoding="utf-8")
    verify = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--database-url", database_url,
         "--data-root", str(data_root), "--manifest", str(manifest)],
        text=True, capture_output=True, check=False,
    )
    assert verify.returncode == 1
    assert "verification failed" in verify.stderr.lower()
