from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

import app.legacy_migration.core as legacy_core
from app.legacy_migration import (
    BatchConflict,
    ConversionIssue,
    SourceSafetyError,
    build_manifest,
    convert_bool,
    convert_date,
    convert_datetime,
    convert_decimal,
    convert_json,
    discover_sources,
    inventory_attachments,
    migrate_legacy,
    verify_completed_batch,
)
from app.legacy_migration.core import StructuralMigrationError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "legacy"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_sources(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "legacy"
    root.mkdir()
    for name in ("research", "generic_tables", "expense"):
        target = root / f"{name}.db"
        with sqlite3.connect(target) as db:
            db.executescript((FIXTURE_DIR / f"{name}.sql").read_text())
    uploads = root / "uploads"
    uploads.mkdir()
    (uploads / "invoice-a.txt").write_text("same invoice", encoding="utf-8")
    (uploads / "invoice-copy.txt").write_text("same invoice", encoding="utf-8")
    return root


def _target_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "legacy_migration_batches", metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("batch_key", sa.String, nullable=False, unique=True),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("summary", sa.JSON, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    sa.Table(
        "legacy_migration_issues", metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("batch_id", sa.String, nullable=False),
        sa.Column("source_table", sa.String, nullable=False),
        sa.Column("source_key", sa.String),
        sa.Column("field_name", sa.String),
        sa.Column("reason", sa.String, nullable=False),
        sa.Column("raw_value", sa.String),
    )
    sa.Table(
        "users", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String, unique=True, nullable=False),
        sa.Column("password", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("name", sa.String),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("directory_permissions", sa.JSON, nullable=False),
        sa.Column("must_change_password", sa.Boolean, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    sa.Table(
        "projects", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.String, unique=True, nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("start_date", sa.Date),
        sa.Column("status", sa.String),
        sa.Column("registry_id", sa.String),
    )
    for project_table in ("security_projects", "crypto_projects"):
        sa.Table(project_table, metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("project_id", sa.String, unique=True), sa.Column("name", sa.String, nullable=False), sa.Column("registry_id", sa.String))
    sa.Table("equipment", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("equipment_id", sa.String), sa.Column("name", sa.String, nullable=False), sa.Column("category", sa.String), sa.Column("price", sa.Numeric), sa.Column("related_files", sa.JSON), sa.Column("created_at", sa.DateTime(timezone=True)))
    sa.Table("standards", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("doc_id", sa.String), sa.Column("name", sa.String, nullable=False), sa.Column("category", sa.String), sa.Column("file_path", sa.String), sa.Column("upload_time", sa.DateTime(timezone=True)))
    sa.Table("experts", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("expert_id", sa.String, unique=True), sa.Column("name", sa.String, nullable=False), sa.Column("unit", sa.String), sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True)))
    sa.Table("doc_templates", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("template_id", sa.String, unique=True), sa.Column("name", sa.String, nullable=False), sa.Column("category", sa.String), sa.Column("file_path", sa.String), sa.Column("uploader", sa.String), sa.Column("created_at", sa.DateTime(timezone=True)))
    sa.Table("expert_groups", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("group_id", sa.String, unique=True), sa.Column("meeting_name", sa.String, nullable=False), sa.Column("creator", sa.String, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    sa.Table("expert_group_members", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("group_id", sa.String, nullable=False), sa.Column("expert_id", sa.String, nullable=False), sa.Column("selected_by", sa.String, nullable=False), sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False))
    sa.Table("llm_models", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String, nullable=False), sa.Column("model_type", sa.String, nullable=False), sa.Column("file_path", sa.String), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    sa.Table("project_documents", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("doc_id", sa.String, unique=True), sa.Column("project_id", sa.String), sa.Column("name", sa.String), sa.Column("file_path", sa.String))
    sa.Table("document_versions", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("version_id", sa.String, unique=True), sa.Column("doc_id", sa.String), sa.Column("version_number", sa.String), sa.Column("file_path", sa.String))
    sa.Table(
        "generic_tables", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("table_id", sa.String, unique=True, nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("creator", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_version_id", sa.String),
    )
    sa.Table("generic_table_versions", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("version_id", sa.String, unique=True), sa.Column("table_id", sa.String), sa.Column("version_number", sa.Integer), sa.Column("create_method", sa.String), sa.Column("row_count", sa.Integer), sa.Column("page_size", sa.Integer), sa.Column("creator", sa.String), sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("is_locked", sa.Boolean))
    sa.Table("generic_table_columns", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("version_id", sa.String), sa.Column("col_key", sa.String), sa.Column("col_name", sa.String), sa.Column("col_type", sa.String), sa.Column("col_index", sa.Integer), sa.Column("col_width", sa.Integer), sa.Column("col_align", sa.String), sa.Column("col_summary", sa.String), sa.Column("col_options", sa.JSON), sa.Column("created_at", sa.DateTime(timezone=True)))
    sa.Table("generic_table_data", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("version_id", sa.String), sa.Column("row_key", sa.String), sa.Column("row_index", sa.Integer), sa.Column("row_data", sa.JSON), sa.Column("row_color", sa.String), sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True)))
    sa.Table(
        "expense_reimbursement", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("reimbursement_no", sa.String, unique=True),
        sa.Column("title", sa.String),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("is_paid", sa.Boolean, nullable=False),
        sa.Column("documents", sa.JSON, nullable=False),
    )
    sa.Table("expense_invoice", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("reimbursement_id", sa.Integer), sa.Column("invoice_no", sa.String), sa.Column("date", sa.Date), sa.Column("amount", sa.Numeric), sa.Column("tax_amount", sa.Numeric), sa.Column("price_ex_tax", sa.Numeric), sa.Column("file_path", sa.String), sa.Column("matched_payment_ids", sa.JSON), sa.Column("created_at", sa.DateTime(timezone=True)))
    sa.Table("expense_payment", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("reimbursement_id", sa.Integer), sa.Column("payment_no", sa.String), sa.Column("amount", sa.Numeric), sa.Column("pay_date", sa.Date), sa.Column("file_path", sa.String), sa.Column("matched_invoice_ids", sa.JSON), sa.Column("created_at", sa.DateTime(timezone=True)))
    sa.Table("expense_invoice_item", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("invoice_id", sa.Integer), sa.Column("seq", sa.Integer), sa.Column("name", sa.String), sa.Column("quantity", sa.Numeric), sa.Column("unit_price", sa.Numeric), sa.Column("amount", sa.Numeric), sa.Column("tax_amount", sa.Numeric))
    sa.Table("project_registry", metadata, sa.Column("id", sa.String, primary_key=True), sa.Column("category", sa.String), sa.Column("business_id", sa.String), sa.Column("status", sa.String))
    metadata.create_all(engine)
    return engine


def test_discovers_exact_read_only_databases_and_rejects_unsafe_sources(tmp_path):
    root = _make_sources(tmp_path)
    sources = discover_sources(root)
    assert [source.name for source in sources] == [
        "research.db", "generic_tables.db", "expense.db"
    ]
    for source in sources:
        assert source.uri.startswith("file:") and "mode=ro" in source.uri
        with source.connect() as db:
            assert db.execute("PRAGMA query_only").fetchone()[0] == 1

    (root / "research.db-wal").write_bytes(b"active")
    with pytest.raises(SourceSafetyError, match="WAL"):
        discover_sources(root)
    (root / "research.db-wal").unlink()

    real = root / "research.db"
    real.rename(root / "research-real.db")
    real.symlink_to(root / "research-real.db")
    with pytest.raises(SourceSafetyError, match="symlink"):
        discover_sources(root)

    root2 = _make_sources(tmp_path / "root-link")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root2, target_is_directory=True)
    with pytest.raises(SourceSafetyError, match="real directory"):
        discover_sources(linked_root)


def test_rejects_missing_and_duplicate_candidates(tmp_path):
    root = _make_sources(tmp_path)
    (root / "expense.db").unlink()
    with pytest.raises(SourceSafetyError, match="missing"):
        discover_sources(root)
    source_bytes = (root / "research.db").read_bytes()
    (root / "expense-copy.db").write_bytes(source_bytes)
    (root / "expense.db").write_bytes(source_bytes)
    with pytest.raises(SourceSafetyError, match="duplicate"):
        discover_sources(root)


def test_manifest_and_attachment_inventory_are_deterministic_and_private(tmp_path):
    root = _make_sources(tmp_path)
    before = {p.name: _sha256(p) for p in root.glob("*.db")}
    first = build_manifest(root)
    second = build_manifest(root)
    assert first == second
    rendered = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert str(root) not in rendered
    assert "generated_at" not in rendered
    assert first["sha256"] == second["sha256"]

    inventory = inventory_attachments(root, [
        "uploads/invoice-a.txt", "uploads/missing.txt", "uploads/invoice-copy.txt"
    ])
    assert inventory["counts"] == {
        "referenced": 3, "present": 2, "missing": 1, "duplicate": 1, "unreferenced": 0
    }
    assert {item["path"] for item in inventory["files"]} == {
        "uploads/invoice-a.txt", "uploads/invoice-copy.txt", "uploads/missing.txt"
    }
    assert {p.name: _sha256(p) for p in root.glob("*.db")} == before

    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (root / "uploads" / "escape.txt").symlink_to(outside)
    with pytest.raises(SourceSafetyError):
        inventory_attachments(root, ["../secret.txt"])
    with pytest.raises(SourceSafetyError):
        inventory_attachments(root, ["uploads/escape.txt"])


def test_explicit_converters_reject_bad_values_without_defaults():
    assert convert_decimal("10.20") == Decimal("10.20")
    assert convert_date("2026-09-01").isoformat() == "2026-09-01"
    assert convert_datetime("2026-09-01T10:30:00+08:00").tzinfo is not None
    assert convert_datetime("2026-09-01 10:30:00").utcoffset().total_seconds() == 8 * 3600
    assert convert_bool("1") is True
    assert convert_json('{"ids":[1,2]}') == {"ids": [1, 2]}
    for converter, value in (
        (convert_decimal, "bad-money"), (convert_date, "2026-99-99"),
        (convert_datetime, "tomorrow"), (convert_bool, "perhaps"),
        (convert_json, "{bad-json"),
    ):
        with pytest.raises(ConversionIssue):
            converter(value)


def test_migration_is_transactional_idempotent_and_reports_issues(tmp_path):
    root = _make_sources(tmp_path)
    engine = _target_engine()
    source_before = {p.relative_to(root).as_posix(): _sha256(p) for p in root.rglob("*") if p.is_file()}

    first = migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)
    second = migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)
    assert first == second
    assert first["status"] == "completed_with_issues"
    assert first["counts"]["inserted"] == 19
    assert first["counts"]["rejected"] == 4
    assert first["counts"]["issues"] >= 2
    assert len(first["report_sha256"]) == 64
    assert first["recovery"] == "backup_restore_only"
    assert str(root) not in json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "plain-secret" not in json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "12.xx" not in json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "{bad-json" not in json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == hashlib.sha256(json.dumps(second, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    with engine.connect() as connection:
        assert connection.scalar(sa.text("select count(*) from users")) == 2
        assert connection.scalar(sa.text("select count(*) from projects")) == 1
        assert connection.scalar(sa.text("select count(*) from generic_tables")) == 1
        assert connection.scalar(sa.text("select count(*) from expense_reimbursement")) == 1
        assert connection.scalar(sa.text("select count(*) from expense_invoice")) == 1
        assert connection.scalar(sa.text("select count(*) from expense_payment")) == 1
        assert connection.scalar(sa.text("select count(*) from expense_invoice_item")) == 1
        assert connection.scalar(sa.text("select count(*) from generic_table_versions")) == 1
        assert connection.scalar(sa.text("select count(*) from generic_table_columns")) == 1
        assert connection.scalar(sa.text("select count(*) from generic_table_data")) == 1
        users = connection.execute(sa.text("select username,status,must_change_password,password from users order by username")).all()
        assert users[0][0:3] == ("hashed", "active", 1)
        assert users[1][0:3] == ("plain", "disabled", 1)
        assert users[1][3] != "plain-secret"
        assert connection.scalar(sa.text("select file_path from llm_models")) is None
        assert connection.scalar(sa.text("select count(*) from legacy_migration_batches where completed_at is not null")) == 1

    assert {p.relative_to(root).as_posix(): _sha256(p) for p in root.rglob("*") if p.is_file()} == source_before


def test_batch_manifest_change_and_mid_import_failure_roll_back(tmp_path):
    root = _make_sources(tmp_path)
    engine = _target_engine()
    migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)
    with sqlite3.connect(root / "research.db") as db:
        db.execute("update projects set name='changed' where project_id='P-001'")
    with pytest.raises(BatchConflict):
        migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)

    engine2 = _target_engine()
    with pytest.raises(RuntimeError, match="injected"):
        migrate_legacy(
            engine2, root, "fixture-fail", allow_test_sqlite=True,
            fail_after_table="projects",
        )
    with engine2.connect() as connection:
        for table in ("users", "projects", "generic_tables", "expense_reimbursement", "legacy_migration_batches"):
            assert connection.scalar(sa.text(f"select count(*) from {table}")) == 0


def test_production_migration_rejects_non_postgresql_target(tmp_path):
    root = _make_sources(tmp_path)
    with pytest.raises(SourceSafetyError, match="PostgreSQL"):
        migrate_legacy(_target_engine(), root, "fixture-v1")


def test_verify_detects_target_drift_and_report_hash_tamper(tmp_path):
    root = _make_sources(tmp_path)
    engine = _target_engine()
    report = migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)
    assert verify_completed_batch(engine, root, "fixture-v1", allow_test_sqlite=True) == report
    with engine.begin() as connection:
        connection.execute(sa.text("update projects set name='tampered' where project_id='P-001'"))
    with pytest.raises(BatchConflict, match="target drift"):
        verify_completed_batch(engine, root, "fixture-v1", allow_test_sqlite=True)
    with pytest.raises(BatchConflict, match="target drift"):
        migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)

    engine2 = _target_engine()
    migrate_legacy(engine2, root, "fixture-v1", allow_test_sqlite=True)
    with engine2.begin() as connection:
        summary = connection.scalar(sa.text("select summary from legacy_migration_batches"))
        summary = json.loads(summary) if isinstance(summary, str) else summary
        summary["counts"]["inserted"] += 1
        connection.execute(sa.text("update legacy_migration_batches set summary=:summary"), {"summary": json.dumps(summary)})
    with pytest.raises(BatchConflict, match="report hash"):
        verify_completed_batch(engine2, root, "fixture-v1", allow_test_sqlite=True)


def test_same_manifest_different_batch_reuses_without_new_batch(tmp_path):
    root = _make_sources(tmp_path)
    engine = _target_engine()
    first = migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)
    second = migrate_legacy(engine, root, "fixture-alias", allow_test_sqlite=True)
    assert first == second
    with engine.connect() as connection:
        assert connection.scalar(sa.text("select count(*) from legacy_migration_batches")) == 1
        assert connection.scalar(sa.text("select count(*) from projects")) == 1


def test_structural_orphan_and_unknown_role_roll_back_everything(tmp_path):
    root = _make_sources(tmp_path)
    with sqlite3.connect(root / "research.db") as db:
        db.execute("update expert_group_members set group_id='missing'")
    engine = _target_engine()
    with pytest.raises(StructuralMigrationError, match="orphan"):
        migrate_legacy(engine, root, "orphan", allow_test_sqlite=True)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("select count(*) from legacy_migration_batches")) == 0
        assert connection.scalar(sa.text("select count(*) from users")) == 0

    root2 = _make_sources(tmp_path / "other")
    with sqlite3.connect(root2 / "research.db") as db:
        db.execute("update users set role='mystery' where username='plain'")
    with pytest.raises(StructuralMigrationError, match="role"):
        migrate_legacy(_target_engine(), root2, "role", allow_test_sqlite=True)

    root3 = _make_sources(tmp_path / "constraint")
    with sqlite3.connect(root3 / "research.db") as db:
        db.execute("update projects set name=null where project_id='P-001'")
    with pytest.raises(StructuralMigrationError, match="target constraint"):
        migrate_legacy(_target_engine(), root3, "constraint", allow_test_sqlite=True)

    root4 = _make_sources(tmp_path / "internal-id")
    with sqlite3.connect(root4 / "expense.db") as db:
        db.execute("update expense_invoice set matched_payment_ids='[999]' where id=40")
    with pytest.raises(StructuralMigrationError, match="missing JSON internal ID"):
        migrate_legacy(_target_engine(), root4, "internal-id", allow_test_sqlite=True)


def test_source_table_field_and_target_contracts_are_closed(tmp_path):
    root = _make_sources(tmp_path)
    with sqlite3.connect(root / "research.db") as db:
        db.execute("create table mystery_table (id integer)")
    with pytest.raises(SourceSafetyError, match="unknown source table"):
        migrate_legacy(_target_engine(), root, "unknown-table", allow_test_sqlite=True)

    root2 = _make_sources(tmp_path / "field")
    with sqlite3.connect(root2 / "research.db") as db:
        db.execute("alter table projects add column mystery_field text")
    with pytest.raises(SourceSafetyError, match="unknown source field"):
        migrate_legacy(_target_engine(), root2, "unknown-field", allow_test_sqlite=True)

    root3 = _make_sources(tmp_path / "target")
    engine = _target_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("drop table equipment")
    with pytest.raises(StructuralMigrationError, match="target table"):
        migrate_legacy(engine, root3, "missing-target", allow_test_sqlite=True)

    root4 = _make_sources(tmp_path / "required-table")
    with sqlite3.connect(root4 / "expense.db") as db:
        db.execute("drop table expense_reimbursement")
    with pytest.raises(SourceSafetyError, match="missing required source table"):
        migrate_legacy(_target_engine(), root4, "missing-required-table", allow_test_sqlite=True)

    root5 = _make_sources(tmp_path / "required-field")
    with sqlite3.connect(root5 / "expense.db") as db:
        db.execute("alter table expense_reimbursement drop column total_amount")
    with pytest.raises(SourceSafetyError, match="missing required source field"):
        migrate_legacy(_target_engine(), root5, "missing-required-field", allow_test_sqlite=True)


def test_decimal_precision_loss_is_structural_and_rolls_back(tmp_path):
    root = _make_sources(tmp_path)
    with sqlite3.connect(root / "expense.db") as db:
        db.execute("update expense_reimbursement set total_amount='1.235' where id=30")
    engine = _target_engine()
    with pytest.raises(StructuralMigrationError, match="precision loss"):
        migrate_legacy(engine, root, "precision-loss", allow_test_sqlite=True)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("select count(*) from legacy_migration_batches")) == 0
        assert connection.scalar(sa.text("select count(*) from expense_reimbursement")) == 0


def test_target_fingerprint_is_batch_scoped_and_tracks_identity(tmp_path):
    root = _make_sources(tmp_path)
    engine = _target_engine()
    migrate_legacy(engine, root, "fixture-v1", allow_test_sqlite=True)
    with engine.begin() as connection:
        connection.execute(sa.text(
            "insert into projects(id,project_id,name,status) values (999,'P-LATER','Later','ACTIVE')"
        ))
        connection.execute(sa.text(
            "insert into legacy_migration_issues(id,batch_id,source_table,reason) "
            "values ('unrelated','other-batch','other','unrelated')"
        ))
    verify_completed_batch(engine, root, "fixture-v1", allow_test_sqlite=True)

    with engine.begin() as connection:
        connection.execute(sa.text("update users set version=2 where username='hashed'"))
    with pytest.raises(BatchConflict, match="target drift"):
        verify_completed_batch(engine, root, "fixture-v1", allow_test_sqlite=True)


def test_final_source_identity_sidecars_and_unreferenced_content_are_guarded(tmp_path):
    root = _make_sources(tmp_path)
    unreferenced = root / "uploads" / "unreferenced.bin"
    unreferenced.write_bytes(b"unreferenced-content")
    manifest = build_manifest(root)
    assert manifest["attachments"]["unreferenced_files"] == [{
        "path": "uploads/unreferenced.bin",
        "size": len(b"unreferenced-content"),
        "sha256": hashlib.sha256(b"unreferenced-content").hexdigest(),
    }]
    (root / "research.db-journal").write_bytes(b"journal")
    with pytest.raises(SourceSafetyError, match="journal"):
        discover_sources(root)
    (root / "research.db-journal").unlink()

    def add_wal():
        (root / "research.db-wal").write_bytes(b"appeared-mid-migration")

    with pytest.raises(SourceSafetyError, match="WAL"):
        migrate_legacy(
            _target_engine(), root, "wal-race", allow_test_sqlite=True,
            _before_final_source_check=add_wal,
        )

    root2 = _make_sources(tmp_path / "uploads-link")
    uploads = root2 / "uploads"
    real_uploads = root2 / "real-uploads"
    uploads.rename(real_uploads)
    uploads.symlink_to(real_uploads, target_is_directory=True)
    with pytest.raises(SourceSafetyError, match="uploads root"):
        build_manifest(root2)


def test_bound_snapshot_rejects_source_replacement_and_attachment_changes(tmp_path):
    root = _make_sources(tmp_path)
    replacement = tmp_path / "expense-replacement.db"
    replacement.write_bytes((root / "expense.db").read_bytes())
    with sqlite3.connect(replacement) as db:
        db.execute("update expense_reimbursement set title='changed' where id=30")

    def replace_database():
        os.replace(replacement, root / "expense.db")

    with pytest.raises(SourceSafetyError, match="changed during migration"):
        migrate_legacy(
            _target_engine(), root, "replace-race", allow_test_sqlite=True,
            _before_final_source_check=replace_database,
        )

    root2 = _make_sources(tmp_path / "attachment-change")
    engine = _target_engine()
    migrate_legacy(engine, root2, "fixture-v1", allow_test_sqlite=True)
    (root2 / "uploads" / "invoice-a.txt").write_text("changed attachment")
    with pytest.raises(BatchConflict, match="different source manifest"):
        migrate_legacy(engine, root2, "fixture-v1", allow_test_sqlite=True)
    with pytest.raises(BatchConflict, match="source manifest"):
        verify_completed_batch(engine, root2, "fixture-v1", allow_test_sqlite=True)


def test_bound_snapshot_does_not_follow_replaced_attachment_parent(
    tmp_path, monkeypatch
):
    root = _make_sources(tmp_path / "source")
    uploads = root / "uploads"
    original_uploads = root / "uploads-original"
    outside_uploads = tmp_path / "outside-uploads"
    outside_uploads.mkdir()
    (outside_uploads / "invoice-a.txt").write_text("external", encoding="utf-8")
    original_copy = legacy_core._copy_bound_file
    replaced = False

    def replace_parent_before_open(source, destination, expected=None, **kwargs):
        nonlocal replaced
        if source == uploads / "invoice-a.txt" and not replaced:
            replaced = True
            uploads.rename(original_uploads)
            uploads.symlink_to(outside_uploads, target_is_directory=True)
            try:
                return original_copy(source, destination, expected, **kwargs)
            finally:
                uploads.unlink()
                original_uploads.rename(uploads)
        return original_copy(source, destination, expected, **kwargs)

    monkeypatch.setattr(legacy_core, "_copy_bound_file", replace_parent_before_open)
    sources = discover_sources(root)
    with legacy_core._bound_snapshot(root, sources) as snapshot:
        copied = (snapshot / "uploads" / "invoice-a.txt").read_text(encoding="utf-8")

    assert replaced is True
    assert copied == "same invoice"


def test_bound_snapshot_rejects_replaced_source_root_ancestor(tmp_path, monkeypatch):
    source_parent = tmp_path / "source-parent"
    root = _make_sources(source_parent)
    original_parent = tmp_path / "source-parent-original"
    alternate_parent = tmp_path / "alternate-parent"
    alternate_root = alternate_parent / "legacy"
    alternate_root.mkdir(parents=True)
    for name in ("research.db", "generic_tables.db", "expense.db"):
        os.link(root / name, alternate_root / name)
    alternate_uploads = alternate_root / "uploads"
    alternate_uploads.mkdir()
    (alternate_uploads / "invoice-a.txt").write_text("external", encoding="utf-8")
    (alternate_uploads / "invoice-copy.txt").write_text("external", encoding="utf-8")
    original_snapshot = legacy_core._bound_snapshot

    @contextmanager
    def replace_ancestor_before_root_open(*args, **kwargs):
        source_parent.rename(original_parent)
        source_parent.symlink_to(alternate_parent, target_is_directory=True)
        try:
            with original_snapshot(*args, **kwargs) as snapshot:
                yield snapshot
        finally:
            source_parent.unlink()
            original_parent.rename(source_parent)

    monkeypatch.setattr(
        legacy_core, "_bound_snapshot", replace_ancestor_before_root_open
    )
    with pytest.raises(SourceSafetyError, match="root identity changed"):
        with legacy_core._prepared_snapshot(root):
            pass


@pytest.mark.skipif(
    not os.environ.get("T05_TEST_DATABASE_URL"),
    reason="requires isolated migrated PostgreSQL",
)
def test_postgresql_concurrent_same_batch_and_identity_sequence(tmp_path):
    root = _make_sources(tmp_path)
    url = os.environ["T05_TEST_DATABASE_URL"]
    first_engine = sa.create_engine(url, pool_pre_ping=True)
    second_engine = sa.create_engine(url, pool_pre_ping=True)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(migrate_legacy, engine, root, "concurrent-v1")
                for engine in (first_engine, second_engine)
            ]
            reports = [future.result(timeout=30) for future in futures]
        assert reports[0] == reports[1]
        with first_engine.begin() as connection:
            assert connection.scalar(sa.text("select count(*) from legacy_migration_batches")) == 1
            assert connection.scalar(sa.text("select count(*) from projects")) == 1
            inserted_id = connection.scalar(sa.text(
                "insert into projects(project_id,name) values ('P-NEXT','Next') returning id"
            ))
            assert inserted_id == 12
    finally:
        first_engine.dispose()
        second_engine.dispose()
