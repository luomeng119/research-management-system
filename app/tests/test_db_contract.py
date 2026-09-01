from __future__ import annotations

import importlib
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError


HEAD_REVISION = "0001_v1_core"

CORE_TABLES = {
    "users",
    "proposals",
    "proposal_argumentations",
    "proposal_decisions",
    "proposal_ai_drafts",
    "project_registry",
    "projects",
    "security_projects",
    "crypto_projects",
    "project_progress",
    "project_changes",
    "project_outputs",
    "project_closures",
    "stored_files",
    "stored_file_versions",
    "object_files",
    "audit_events",
    "legacy_migration_batches",
    "legacy_migration_issues",
}

LEGACY_TABLES = {
    "equipment",
    "knowledge_subclasses",
    "standards",
    "experts",
    "doc_templates",
    "project_documents",
    "document_versions",
    "expert_groups",
    "expert_group_members",
    "equipment_groups",
    "equipment_group_members",
    "host_devices",
    "host_device_categories",
    "device_host_relations",
    "research_units",
    "generic_tables",
    "generic_table_versions",
    "generic_table_columns",
    "generic_table_data",
    "expense_reimbursement",
    "expense_invoice",
    "expense_invoice_item",
    "expense_payment",
    "llm_models",
    "inference_server_status",
}


def _db_module():
    try:
        return importlib.import_module("app.db")
    except ModuleNotFoundError:
        pytest.fail("app.db must provide the PostgreSQL runtime boundary", pytrace=False)


def _repository_module():
    try:
        return importlib.import_module("app.repositories.base")
    except ModuleNotFoundError:
        pytest.fail(
            "app.repositories.base must provide pagination and optimistic locking",
            pytrace=False,
        )


@pytest.fixture(scope="module")
def runtime_url():
    value = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not value:
        pytest.skip("real PostgreSQL contract requires TEST_DATABASE_URL")
    return value


@pytest.fixture(scope="module")
def migration_url():
    value = os.environ.get("MIGRATION_DATABASE_URL")
    if not value:
        pytest.skip("migration checks require MIGRATION_DATABASE_URL")
    return value


@pytest.fixture(scope="module")
def runtime_engine(runtime_url):
    engine = sa.create_engine(runtime_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def migration_engine(migration_url):
    engine = sa.create_engine(migration_url)
    yield engine
    engine.dispose()


def test_runtime_and_migration_urls_are_separate(monkeypatch):
    db = _db_module()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://runtime/db")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://migration/db")

    assert db.get_runtime_database_url() == "postgresql+psycopg://runtime/db"
    assert db.get_migration_database_url() == "postgresql+psycopg://migration/db"


def test_runtime_engine_has_bounded_overridable_pool():
    db = _db_module()
    engine = db.create_runtime_engine(
        "postgresql+psycopg://runtime:runtime@127.0.0.1:1/db",
        pool_size=3,
        max_overflow=2,
        pool_timeout=7,
    )
    try:
        assert engine.pool.size() == 3
        assert engine.pool._max_overflow == 2
        assert engine.pool._timeout == 7
        assert engine.pool._pre_ping is True
    finally:
        engine.dispose()


def test_paginate_applies_database_limit_and_offset():
    repositories = _repository_module()
    sample = sa.table("sample", sa.column("id"))

    statement = repositories.paginate(
        sa.select(sample).order_by(sample.c.id), page=3, page_size=20
    )

    compiled = statement.compile(compile_kwargs={"literal_binds": True})
    assert "LIMIT 20" in str(compiled)
    assert "OFFSET 40" in str(compiled)


@pytest.mark.parametrize(
    ("page", "page_size"),
    [(0, 20), (1, 0), (1, 101)],
)
def test_paginate_rejects_unbounded_or_invalid_requests(page, page_size):
    repositories = _repository_module()
    with pytest.raises(ValueError):
        repositories.paginate(sa.select(sa.literal(1)), page=page, page_size=page_size)


def test_schema_revision_matches_code_head(runtime_engine):
    db = _db_module()
    assert db.check_schema_version(runtime_engine) == HEAD_REVISION


def test_runtime_initialization_rejects_database_revision_drift(
    migration_engine, runtime_url
):
    db = _db_module()
    if not hasattr(db, "initialize_runtime_database"):
        pytest.fail(
            "app.db must expose checked production database initialization",
            pytrace=False,
        )
    with migration_engine.begin() as connection:
        connection.execute(sa.text("UPDATE alembic_version SET version_num = 'stale_revision'"))
    try:
        with pytest.raises(RuntimeError, match="does not match code head"):
            db.initialize_runtime_database(runtime_url)
    finally:
        with migration_engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": HEAD_REVISION},
            )


def test_schema_contains_confirmed_core_and_legacy_tables(migration_engine):
    inspector = sa.inspect(migration_engine)
    actual = set(inspector.get_table_names())
    assert CORE_TABLES | LEGACY_TABLES <= actual


def test_mainline_tables_use_uuid_timestamptz_audit_and_version(migration_engine):
    tables = [
        "proposals",
        "proposal_argumentations",
        "proposal_decisions",
        "proposal_ai_drafts",
        "project_registry",
        "project_progress",
        "project_changes",
        "project_outputs",
        "project_closures",
        "stored_files",
        "stored_file_versions",
        "object_files",
    ]
    inspector = sa.inspect(migration_engine)
    for table_name in tables:
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        assert isinstance(columns["id"]["type"], sa.dialects.postgresql.UUID)
        assert "gen_random_uuid" in str(columns["id"]["default"])
        assert columns["created_at"]["type"].timezone is True
        assert columns["updated_at"]["type"].timezone is True
        assert isinstance(columns["version"]["type"], sa.Integer)
        assert {"created_by", "updated_by"} <= columns.keys()


def test_three_legacy_project_tables_keep_business_id_and_registry_fk(migration_engine):
    inspector = sa.inspect(migration_engine)
    for table_name in ("projects", "security_projects", "crypto_projects"):
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        assert {"id", "project_id", "registry_id"} <= columns.keys()
        assert isinstance(columns["id"]["type"], sa.BigInteger)
        assert any(
            fk["referred_table"] == "project_registry"
            and fk["constrained_columns"] == ["registry_id"]
            for fk in inspector.get_foreign_keys(table_name)
        )


def test_schema_has_status_checks_uniques_and_list_indexes(migration_engine):
    inspector = sa.inspect(migration_engine)
    proposal_checks = " ".join(
        check["sqltext"] for check in inspector.get_check_constraints("proposals")
    )
    assert all(
        status in proposal_checks
        for status in ("DRAFT", "ARGUMENTATION", "ESTABLISHED", "DEFERRED", "REJECTED")
    )
    registry_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("project_registry")
    }
    assert ("category", "business_id") in registry_uniques

    expected_indexes = {
        "ix_proposals_status_updated",
        "ix_project_registry_category_status_updated",
        "ix_project_progress_project_recorded",
        "ix_audit_events_created",
        "ix_object_files_object",
        "ix_generic_table_data_version_row",
        "ix_expense_reimbursement_status_created",
    }
    actual_indexes = {
        index["name"]
        for table_name in CORE_TABLES | LEGACY_TABLES
        for index in inspector.get_indexes(table_name)
    }
    assert expected_indexes <= actual_indexes


def test_transaction_context_rolls_back_on_failure(runtime_engine):
    db = _db_module()
    marker = f"P-{uuid.uuid4()}"
    proposals = sa.Table("proposals", sa.MetaData(), autoload_with=runtime_engine)

    with pytest.raises(RuntimeError, match="force rollback"):
        with db.transaction(runtime_engine) as connection:
            connection.execute(
                proposals.insert().values(business_id=marker, title="rollback probe")
            )
            raise RuntimeError("force rollback")

    with runtime_engine.connect() as connection:
        count = connection.scalar(
            sa.select(sa.func.count()).select_from(proposals).where(
                proposals.c.business_id == marker
            )
        )
    assert count == 0


def test_paginated_query_executes_in_postgresql(runtime_engine):
    repositories = _repository_module()
    proposals = sa.Table("proposals", sa.MetaData(), autoload_with=runtime_engine)
    prefix = f"PAGE-{uuid.uuid4()}"
    with runtime_engine.begin() as connection:
        connection.execute(
            proposals.insert(),
            [
                {"business_id": f"{prefix}-{number}", "title": f"proposal {number}"}
                for number in range(3)
            ],
        )
    with runtime_engine.connect() as connection:
        rows = connection.execute(
            repositories.paginate(
                sa.select(proposals.c.business_id)
                .where(proposals.c.business_id.startswith(prefix))
                .order_by(proposals.c.business_id),
                page=2,
                page_size=2,
            )
        ).scalars().all()
    assert rows == [f"{prefix}-2"]


def test_optimistic_update_increments_version_and_detects_conflict(runtime_engine):
    repositories = _repository_module()
    proposals = sa.Table("proposals", sa.MetaData(), autoload_with=runtime_engine)
    with runtime_engine.begin() as connection:
        record_id = connection.scalar(
            proposals.insert()
            .values(business_id=f"LOCK-{uuid.uuid4()}", title="before")
            .returning(proposals.c.id)
        )

    with runtime_engine.begin() as connection:
        new_version = repositories.optimistic_update(
            connection,
            proposals,
            record_id=record_id,
            expected_version=1,
            values={"title": "after"},
        )
    assert new_version == 2

    with runtime_engine.begin() as connection:
        with pytest.raises(repositories.OptimisticLockConflict):
            repositories.optimistic_update(
                connection,
                proposals,
                record_id=record_id,
                expected_version=1,
                values={"title": "stale overwrite"},
            )


def test_runtime_role_cannot_create_schema_objects(runtime_engine):
    with runtime_engine.connect() as connection:
        privileges = connection.execute(
            sa.text(
                "SELECT current_user, "
                "has_schema_privilege(current_user, 'public', 'CREATE'), "
                "has_database_privilege(current_user, current_database(), 'CREATE'), "
                "has_database_privilege(current_user, current_database(), 'TEMPORARY')"
            )
        ).one()
    assert privileges == ("rm_v1_t02_runtime", False, False, False)
    with pytest.raises(DBAPIError):
        with runtime_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE forbidden_runtime_ddl (id integer)")


def test_runtime_role_can_append_and_read_audit_events(runtime_engine):
    audit_events = sa.Table("audit_events", sa.MetaData(), autoload_with=runtime_engine)
    request_id = f"AUDIT-{uuid.uuid4()}"
    with runtime_engine.begin() as connection:
        event_id = connection.scalar(
            audit_events.insert()
            .values(
                action="db_contract.append",
                object_type="SYSTEM",
                object_id="t02",
                result="SUCCESS",
                request_id=request_id,
            )
            .returning(audit_events.c.id)
        )
    with runtime_engine.connect() as connection:
        assert connection.scalar(
            sa.select(audit_events.c.request_id).where(audit_events.c.id == event_id)
        ) == request_id


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_runtime_role_cannot_rewrite_audit_history(runtime_engine, operation):
    audit_events = sa.Table("audit_events", sa.MetaData(), autoload_with=runtime_engine)
    with runtime_engine.begin() as connection:
        event_id = connection.scalar(
            audit_events.insert()
            .values(
                action="db_contract.immutable",
                object_type="SYSTEM",
                object_id="t02",
                result="SUCCESS",
                request_id=f"AUDIT-{uuid.uuid4()}",
            )
            .returning(audit_events.c.id)
        )

    statement = (
        audit_events.update().where(audit_events.c.id == event_id).values(result="ALTERED")
        if operation == "update"
        else audit_events.delete().where(audit_events.c.id == event_id)
    )
    with pytest.raises(DBAPIError):
        with runtime_engine.begin() as connection:
            connection.execute(statement)
