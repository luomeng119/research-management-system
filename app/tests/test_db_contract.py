from __future__ import annotations

import importlib
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError


HEAD_REVISION = "0004_project_lifecycle"

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


class _ProvisionCursor:
    def __init__(self, *, attributes, memberships=(), effective_permissions=None):
        self.attributes = attributes
        self.memberships = list(memberships)
        self.effective_permissions = effective_permissions
        self.result = None
        self.acl_statement_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters=None):
        if isinstance(statement, str) and "FROM pg_database" in statement:
            self.result = ("migration_owner", "rm_v1_t02", "migration_owner")
        elif isinstance(statement, str) and "FROM pg_namespace" in statement:
            self.result = ("migration_owner",)
        elif isinstance(statement, str) and "FROM pg_auth_members" in statement:
            self.result = self.memberships
        elif isinstance(statement, str) and "FROM pg_roles" in statement:
            self.result = self.attributes
        elif isinstance(statement, str) and "has_database_privilege" in statement:
            self.result = self.effective_permissions
        else:
            self.acl_statement_count += 1
            self.result = None

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


class _ProvisionConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def cursor(self):
        return self._cursor


def _run_provision_with_fake_postgres(
    *, attributes, memberships=(), effective_permissions=None, **kwargs
):
    provisioner = importlib.import_module("scripts.provision_postgres")
    cursor = _ProvisionCursor(
        attributes=attributes,
        memberships=memberships,
        effective_permissions=effective_permissions,
    )
    connection = _ProvisionConnection(cursor)
    with patch.object(provisioner.psycopg, "connect", return_value=connection):
        provisioner.provision(
            "postgresql+psycopg://migration_owner@localhost/rm_v1_t02",
            "runtime_role",
            **kwargs,
        )
    return connection, cursor


@pytest.mark.parametrize(
    "attributes",
    [
        (False, False, False, False, False, False),
        (True, True, False, False, False, False),
        (True, False, True, False, False, False),
        (True, False, False, True, False, False),
        (True, False, False, False, True, False),
        (True, False, False, False, False, True),
    ],
)
def test_provision_rejects_non_login_or_privileged_runtime_roles(attributes):
    with pytest.raises(RuntimeError):
        _run_provision_with_fake_postgres(attributes=attributes)


def test_provision_rejects_runtime_role_with_any_direct_membership():
    with pytest.raises(RuntimeError, match="independent leaf role"):
        _run_provision_with_fake_postgres(
            attributes=(True, False, False, False, False, False),
            memberships=[("pg_write_all_data",)],
        )


@pytest.mark.parametrize(
    "permission_index",
    range(8),
)
def test_provision_rolls_back_when_effective_permissions_are_not_exact(
    permission_index,
):
    expected = [False, False, False, True, True, False, False, False]
    expected[permission_index] = not expected[permission_index]
    provisioner = importlib.import_module("scripts.provision_postgres")
    cursor = _ProvisionCursor(
        attributes=(True, False, False, False, False, False),
        effective_permissions=tuple(expected),
    )
    connection = _ProvisionConnection(cursor)

    with patch.object(provisioner.psycopg, "connect", return_value=connection):
        with pytest.raises(RuntimeError, match="effective privileges"):
            provisioner.provision(
                "postgresql+psycopg://migration_owner@localhost/rm_v1_t02",
                "runtime_role",
            )

    assert cursor.acl_statement_count > 0
    assert connection.rolled_back is True
    assert connection.committed is False


def test_function_only_acl_failure_injection_rolls_back_after_acl_writes():
    provisioner = importlib.import_module("scripts.provision_postgres")
    cursor = _ProvisionCursor(
        attributes=(True, False, False, False, False, False),
        effective_permissions=(False, False, False, True, True, False, False, False),
    )
    connection = _ProvisionConnection(cursor)

    with patch.object(provisioner.psycopg, "connect", return_value=connection):
        with pytest.raises(RuntimeError, match="injected ACL failure"):
            provisioner.provision(
                "postgresql+psycopg://migration_owner@localhost/rm_v1_t02",
                "runtime_role",
                _fail_after_acl=True,
            )

    assert cursor.acl_statement_count > 0
    assert connection.rolled_back is True
    assert connection.committed is False


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


def test_schema_revision_rejects_missing_version_table(migration_engine, runtime_engine):
    db = _db_module()
    with migration_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE alembic_version RENAME TO alembic_version_hidden")
    try:
        with pytest.raises(RuntimeError, match="no Alembic schema"):
            db.check_schema_version(runtime_engine)
    finally:
        with migration_engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE alembic_version_hidden RENAME TO alembic_version")


def test_schema_revision_rejects_empty_version_table(migration_engine, runtime_engine):
    db = _db_module()
    with migration_engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM alembic_version"))
    try:
        with pytest.raises(RuntimeError, match="exactly one database revision"):
            db.check_schema_version(runtime_engine)
    finally:
        with migration_engine.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": HEAD_REVISION},
            )


def test_schema_revision_rejects_wrong_single_revision(migration_engine, runtime_engine):
    db = _db_module()
    with migration_engine.begin() as connection:
        connection.execute(sa.text("UPDATE alembic_version SET version_num = 'wrong_head'"))
    try:
        with pytest.raises(RuntimeError, match="does not match code head"):
            db.check_schema_version(runtime_engine)
    finally:
        with migration_engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": HEAD_REVISION},
            )


def test_schema_revision_rejects_multiple_database_revisions(
    migration_engine, runtime_engine
):
    db = _db_module()
    with migration_engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES ('other_head')")
        )
    try:
        with pytest.raises(RuntimeError, match="exactly one database revision"):
            db.check_schema_version(runtime_engine)
    finally:
        with migration_engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM alembic_version WHERE version_num = 'other_head'")
            )


def test_schema_revision_rejects_multiple_code_heads(
    tmp_path: Path, runtime_engine
):
    db = _db_module()
    versions = tmp_path / "versions"
    versions.mkdir()
    (tmp_path / "env.py").write_text("", encoding="utf-8")
    for revision in ("head_a", "head_b"):
        (versions / f"{revision}.py").write_text(
            f"revision = {revision!r}\ndown_revision = None\n",
            encoding="utf-8",
        )
    config_path = tmp_path / "alembic.ini"
    config_path.write_text(
        "[alembic]\nscript_location = " + str(tmp_path) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="exactly one code head"):
        db.check_schema_version(runtime_engine, alembic_config_path=config_path)


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


def test_proposal_decision_ai_draft_and_project_category_columns_match_api_contract(
    migration_engine,
):
    inspector = sa.inspect(migration_engine)
    expected_columns = {
        "proposals": {
            "source_summary",
            "research_problem",
            "objectives",
            "research_content",
            "expected_outcomes",
        },
        "proposal_decisions": {
            "decision", "decision_date", "conclusion", "basis",
            "request_fingerprint", "result_snapshot",
        },
        "proposal_ai_drafts": {
            "status",
            "provider_kind",
            "model_version",
            "prompt_version",
            "content",
            "accepted_fields",
            "source_proposal_version",
        },
    }
    for table_name, required in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        assert required <= actual


def test_project_lifecycle_columns_and_enums_match_confirmed_prd(migration_engine):
    inspector = sa.inspect(migration_engine)
    expected_columns = {
        "project_progress": {"recorded_at", "status", "summary", "risk_level", "issues", "next_actions"},
        "project_changes": {"change_type", "before_summary", "after_summary", "basis", "decision", "decision_date"},
        "project_outputs": {"output_type", "title", "description", "formed_date", "contributors"},
        "project_closures": {"summary", "closed_at", "conclusion", "remaining_issues", "no_output_reason"},
    }
    for table_name, required in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        assert required <= actual

    progress_checks = " ".join(
        item["sqltext"] for item in inspector.get_check_constraints("project_progress")
    )
    closure_checks = " ".join(
        item["sqltext"] for item in inspector.get_check_constraints("project_closures")
    )
    assert all(status in progress_checks for status in ("NORMAL", "RISK", "BLOCKED"))
    assert all(value in closure_checks for value in ("PASS", "FAIL", "TERMINATED"))


def test_api_contract_samples_insert_and_invalid_enums_are_rejected(runtime_engine):
    metadata = sa.MetaData()
    proposals = sa.Table("proposals", metadata, autoload_with=runtime_engine)
    decisions = sa.Table("proposal_decisions", metadata, autoload_with=runtime_engine)
    drafts = sa.Table("proposal_ai_drafts", metadata, autoload_with=runtime_engine)
    registry = sa.Table("project_registry", metadata, autoload_with=runtime_engine)
    marker = str(uuid.uuid4())

    with runtime_engine.begin() as connection:
        proposal_id = connection.scalar(
            proposals.insert()
            .values(
                business_id=f"TP-{marker}",
                title="便携式保障设备适配研究",
                source_type="IDEA",
                source_summary="主动提出的研究设想",
                research_problem="现有设备不适配",
                objectives="形成适配方案",
                research_content="开展现场验证",
                expected_outcomes="形成研究报告",
            )
            .returning(proposals.c.id)
        )
        for source_type in (
            "CREATIVE",
            "MEETING_CONCLUSION",
            "FINISHED_MATERIAL",
            "OTHER",
        ):
            connection.execute(
                proposals.insert().values(
                    business_id=f"TP-{source_type}-{marker}",
                    title=f"{source_type} 来源提案",
                    source_type=source_type,
                )
            )
        for decision in ("ESTABLISH", "DEFER", "REJECT"):
            connection.execute(
                decisions.insert().values(
                    proposal_id=proposal_id,
                    decision=decision,
                    decision_date=sa.text("DATE '2026-09-01'"),
                    conclusion=f"{decision} 结论",
                    basis="论证材料完整",
                    idempotency_key=f"decision-{decision}-{marker}",
                )
            )
        draft_ids = []
        for status in ("READY", "APPLIED"):
            draft_ids.append(
                connection.scalar(
                    drafts.insert()
                    .values(
                        proposal_id=proposal_id,
                        status=status,
                        provider_kind="LOCAL",
                        model_version="local-v1",
                        prompt_version="proposal-v1",
                        source_proposal_version=1,
                        content={
                            "title": "建议标题",
                            "researchProblem": "待解决问题",
                            "objectives": ["目标一"],
                            "researchContent": ["内容一"],
                            "expectedOutcomes": ["成果一"],
                            "missingInformation": [],
                        },
                        accepted_fields=(
                            ["title", "researchProblem"] if status == "APPLIED" else []
                        ),
                    )
                    .returning(drafts.c.id)
                )
            )
        draft_ids.append(
            connection.scalar(
                drafts.insert()
                .values(
                    proposal_id=proposal_id,
                    status="READY",
                    provider_kind="DEEPSEEK",
                    model_version="deepseek-test",
                    prompt_version="proposal-v1",
                    source_proposal_version=1,
                    content={
                        "title": "开发期建议标题",
                        "researchProblem": "待解决问题",
                        "objectives": ["目标一"],
                        "researchContent": ["内容一"],
                        "expectedOutcomes": ["成果一"],
                        "missingInformation": [],
                    },
                    accepted_fields=[],
                )
                .returning(drafts.c.id)
            )
        )
        for category in (
            "GENERAL_RESEARCH",
            "SECURITY_CONFIDENTIALITY",
            "CRYPTO_APPLICATION",
        ):
            connection.execute(
                registry.insert().values(
                    category=category,
                    business_id=f"{category}-{marker}",
                    proposal_id=proposal_id if category == "GENERAL_RESEARCH" else None,
                )
            )

    with runtime_engine.connect() as connection:
        stored = connection.execute(
            sa.select(drafts.c.content, drafts.c.accepted_fields, drafts.c.provider_kind).where(
                drafts.c.id == draft_ids[-1]
            )
        ).one()
    assert stored.content["researchProblem"] == "待解决问题"
    assert stored.accepted_fields == []
    assert stored.provider_kind == "DEEPSEEK"

    invalid_cases = (
        proposals.insert().values(
            business_id=f"INVALID-SOURCE-{marker}", title="invalid", source_type="MEETING"
        ),
        decisions.insert().values(
            proposal_id=proposal_id,
            decision="ESTABLISHED",
            decision_date=sa.text("DATE '2026-09-01'"),
            conclusion="invalid",
            basis="invalid",
            idempotency_key=f"invalid-decision-{marker}",
        ),
        registry.insert().values(category="GENERAL", business_id=f"INVALID-CATEGORY-{marker}"),
        drafts.insert().values(
            proposal_id=proposal_id,
            status="FAILED",
            provider_kind="LOCAL",
            model_version="invalid",
            prompt_version="invalid",
            source_proposal_version=1,
            content={},
        ),
        drafts.insert().values(
            proposal_id=proposal_id,
            status="READY",
            provider_kind="REMOTE",
            model_version="invalid",
            prompt_version="invalid",
            source_proposal_version=1,
            content={},
        ),
    )
    for statement in invalid_cases:
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(statement)


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
    assert ("business_id",) in registry_uniques

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
    assert privileges == (os.environ["T02_RUNTIME_ROLE"], False, False, False)
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


def test_default_privileges_cover_future_tables_and_sequences(
    migration_engine, runtime_engine
):
    table_name = f"future_permissions_{uuid.uuid4().hex}"
    sequence_name = f"future_sequence_{uuid.uuid4().hex}"
    with migration_engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE TABLE "{table_name}" (id integer PRIMARY KEY, value text)'
        )
        connection.exec_driver_sql(f'CREATE SEQUENCE "{sequence_name}"')
    try:
        with runtime_engine.begin() as connection:
            connection.exec_driver_sql(
                f'INSERT INTO "{table_name}" (id, value) VALUES (1, \'ok\')'
            )
            assert connection.exec_driver_sql(
                f'SELECT value FROM "{table_name}" WHERE id = 1'
            ).scalar_one() == "ok"
            assert connection.exec_driver_sql(
                f'SELECT nextval(\'"{sequence_name}"\')'
            ).scalar_one() == 1
    finally:
        with migration_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP TABLE "{table_name}"')
            connection.exec_driver_sql(f'DROP SEQUENCE "{sequence_name}"')
