"""V1 PostgreSQL schema baseline.

Revision ID: 0001_v1_core
Revises: None
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_v1_core"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TZ = sa.DateTime(timezone=True)


def _legacy_id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True)


def _mainline_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("updated_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("version >= 1"),
    ]


def _create_project_table(name: str) -> None:
    op.create_table(
        name,
        _legacy_id(),
        sa.Column("project_id", sa.Text(), nullable=False, unique=True),
        sa.Column("registry_id", UUID, sa.ForeignKey("project_registry.id"), unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("leader", sa.Text()),
        sa.Column("start_date", sa.Date()),
        sa.Column("planned_end_date", sa.Date()),
        sa.Column("actual_end_date", sa.Date()),
        sa.Column("status", sa.Text()),
        sa.Column("created_at", TZ),
        sa.Column("task_number", sa.Text()),
    )
    op.create_index(f"ix_{name}_status_created", name, ["status", "created_at"])


def upgrade() -> None:
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    op.create_table(
        "users",
        _legacy_id(),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("created_at", TZ, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, server_default=sa.func.now()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("directory_permissions", JSONB, nullable=False, server_default="{}"),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "proposals",
        *_mainline_columns(),
        sa.Column("business_id", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text()),
        sa.Column("source_summary", sa.Text()),
        sa.Column("research_problem", sa.Text()),
        sa.Column("objectives", sa.Text()),
        sa.Column("research_content", sa.Text()),
        sa.Column("expected_outcomes", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="DRAFT"),
        sa.CheckConstraint(
            "source_type IS NULL OR source_type IN "
            "('IDEA','CREATIVE','MEETING_CONCLUSION','FINISHED_MATERIAL','OTHER')",
            name="ck_proposals_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','ARGUMENTATION','ESTABLISHED','DEFERRED','REJECTED')",
            name="ck_proposals_status",
        ),
    )
    op.create_index(
        "ix_proposals_status_updated", "proposals", ["status", sa.text("updated_at DESC"), "id"]
    )

    op.create_table(
        "proposal_argumentations",
        *_mainline_columns(),
        sa.Column("proposal_id", UUID, sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("facts", JSONB, nullable=False, server_default="{}"),
        sa.Column("conclusion", sa.Text()),
        sa.Column("basis", sa.Text()),
    )
    op.create_index(
        "ix_proposal_argumentations_proposal_created",
        "proposal_argumentations",
        ["proposal_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "proposal_decisions",
        *_mainline_columns(),
        sa.Column("proposal_id", UUID, sa.ForeignKey("proposals.id"), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint(
            "decision IN ('ESTABLISH','DEFER','REJECT')",
            name="ck_proposal_decisions_decision",
        ),
    )
    op.create_index(
        "ix_proposal_decisions_proposal_created",
        "proposal_decisions",
        ["proposal_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "proposal_ai_drafts",
        *_mainline_columns(),
        sa.Column("proposal_id", UUID, sa.ForeignKey("proposals.id", ondelete="CASCADE")),
        sa.Column("status", sa.Text(), nullable=False, server_default="READY"),
        sa.Column("provider_kind", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text()),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("accepted_fields", JSONB, nullable=False, server_default="[]"),
        sa.CheckConstraint(
            "status IN ('READY','APPLIED')", name="ck_proposal_ai_drafts_status"
        ),
        sa.CheckConstraint(
            "provider_kind = 'LOCAL'", name="ck_proposal_ai_drafts_provider_kind"
        ),
    )

    op.create_table(
        "project_registry",
        *_mainline_columns(),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("business_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", UUID, sa.ForeignKey("proposals.id"), unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.UniqueConstraint("category", "business_id", name="uq_project_registry_category_business"),
        sa.CheckConstraint(
            "category IN "
            "('GENERAL_RESEARCH','SECURITY_CONFIDENTIALITY','CRYPTO_APPLICATION')",
            name="ck_project_registry_category",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','ACTIVE','PAUSED','CLOSING','CLOSED','TERMINATED')",
            name="ck_project_registry_status",
        ),
    )
    op.create_index(
        "ix_project_registry_category_status_updated",
        "project_registry",
        ["category", "status", sa.text("updated_at DESC"), "id"],
    )

    for table_name in ("projects", "security_projects", "crypto_projects"):
        _create_project_table(table_name)

    for table_name, detail_columns in (
        (
            "project_progress",
            [
                sa.Column("recorded_at", TZ, nullable=False, server_default=sa.func.now()),
                sa.Column("status", sa.Text(), nullable=False),
                sa.Column("summary", sa.Text(), nullable=False),
                sa.Column("risk_level", sa.Text()),
            ],
        ),
        (
            "project_changes",
            [
                sa.Column("change_type", sa.Text(), nullable=False),
                sa.Column("before_summary", sa.Text()),
                sa.Column("after_summary", sa.Text(), nullable=False),
                sa.Column("basis", sa.Text()),
            ],
        ),
        (
            "project_outputs",
            [
                sa.Column("output_type", sa.Text(), nullable=False),
                sa.Column("title", sa.Text(), nullable=False),
                sa.Column("description", sa.Text()),
            ],
        ),
        (
            "project_closures",
            [
                sa.Column("summary", sa.Text(), nullable=False),
                sa.Column("closed_at", TZ, nullable=False),
                sa.UniqueConstraint("project_registry_id", name="uq_project_closures_project"),
            ],
        ),
    ):
        op.create_table(
            table_name,
            *_mainline_columns(),
            sa.Column(
                "project_registry_id",
                UUID,
                sa.ForeignKey("project_registry.id", ondelete="CASCADE"),
                nullable=False,
            ),
            *detail_columns,
        )
    op.create_index(
        "ix_project_progress_project_recorded",
        "project_progress",
        ["project_registry_id", sa.text("recorded_at DESC"), "id"],
    )
    op.create_index(
        "ix_project_changes_project_created",
        "project_changes",
        ["project_registry_id", sa.text("created_at DESC"), "id"],
    )
    op.create_index(
        "ix_project_outputs_project_created",
        "project_outputs",
        ["project_registry_id", sa.text("created_at DESC"), "id"],
    )

    op.create_table(
        "stored_files",
        *_mainline_columns(),
        sa.Column("business_id", sa.Text(), nullable=False, unique=True),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
    )
    op.create_table(
        "stored_file_versions",
        *_mainline_columns(),
        sa.Column("file_id", UUID, sa.ForeignKey("stored_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.UniqueConstraint("file_id", "version_no", name="uq_stored_file_versions_file_version"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_stored_file_versions_size"),
    )
    op.create_table(
        "object_files",
        *_mainline_columns(),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=False),
        sa.Column("file_id", UUID, sa.ForeignKey("stored_files.id"), nullable=False),
        sa.Column("purpose", sa.Text()),
        sa.UniqueConstraint("object_type", "object_id", "file_id", name="uq_object_files_link"),
        sa.CheckConstraint(
            "object_type IN ('PROPOSAL','PROJECT','EXPERT','EQUIPMENT','STANDARD',"
            "'TEMPLATE','GENERIC_TABLE','EXPENSE','INVOICE','PAYMENT','DOCUMENT')",
            name="ck_object_files_type",
        ),
    )
    op.create_index(
        "ix_object_files_object", "object_files", ["object_type", "object_id", "created_at"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("actor_user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text()),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text()),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_audit_events_created", "audit_events", [sa.text("created_at DESC"), "id"]
    )
    op.create_index(
        "ix_audit_events_actor_created",
        "audit_events",
        ["actor_user_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "legacy_migration_batches",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("batch_key", sa.Text(), nullable=False, unique=True),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TZ),
        sa.Column("summary", JSONB, nullable=False, server_default="{}"),
    )
    op.create_table(
        "legacy_migration_issues",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("batch_id", UUID, sa.ForeignKey("legacy_migration_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text()),
        sa.Column("field_name", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("raw_value", sa.Text()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_legacy_migration_issues_batch_table",
        "legacy_migration_issues",
        ["batch_id", "source_table"],
    )

    op.create_table(
        "equipment",
        _legacy_id(),
        sa.Column("equipment_id", sa.Text()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("category", sa.Text()),
        sa.Column("subclass", sa.Text()),
        sa.Column("form", sa.Text()),
        sa.Column("price", sa.Numeric(18, 2)),
        sa.Column("tech_index", sa.Text()),
        sa.Column("tech_status", sa.Text()),
        sa.Column("installation_requirements", sa.Text()),
        sa.Column("manufacturer", sa.Text()),
        sa.Column("equipment_image", sa.Text()),
        sa.Column("related_files", JSONB, server_default="[]"),
        sa.Column("main_purpose", sa.Text()),
        sa.Column("former_name", sa.Text()),
        sa.Column("resource_guarantee", sa.Text()),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
    )
    op.create_index("ix_equipment_category_subclass", "equipment", ["category", "subclass", "id"])
    op.create_table(
        "knowledge_subclasses",
        _legacy_id(),
        sa.Column("parent_category", sa.Text(), nullable=False),
        sa.Column("subclass_name", sa.Text(), nullable=False),
        sa.Column("created_at", TZ),
        sa.UniqueConstraint("parent_category", "subclass_name", name="uq_knowledge_subclasses_parent_name"),
    )
    op.create_table(
        "standards",
        _legacy_id(),
        sa.Column("doc_id", sa.Text()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text()),
        sa.Column("file_type", sa.Text()),
        sa.Column("uploader", sa.Text()),
        sa.Column("upload_time", TZ),
        sa.Column("file_path", sa.Text()),
    )
    op.create_index("ix_standards_category_name", "standards", ["category", "name", "id"])
    op.create_table(
        "experts",
        _legacy_id(),
        sa.Column("expert_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text()),
        sa.Column("position", sa.Text()),
        sa.Column("expertise", sa.Text()),
        sa.Column("bank_card", sa.Text()),
        sa.Column("bank_name", sa.Text()),
        sa.Column("uploader", sa.Text()),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
        sa.Column("phone", sa.Text()),
        sa.Column("id_card", sa.Text()),
    )
    op.create_index("ix_experts_unit_name", "experts", ["unit", "name", "id"])

    op.create_table(
        "doc_templates",
        _legacy_id(),
        sa.Column("template_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("uploader", sa.Text()),
        sa.Column("chapter_tree", JSONB),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
    )
    op.create_table(
        "project_documents",
        _legacy_id(),
        sa.Column("doc_id", sa.Text(), nullable=False, unique=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("category", sa.Text()),
        sa.Column("template_id", sa.Text()),
        sa.Column("content", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("word_file_path", sa.Text()),
        sa.Column("uploader", sa.Text()),
        sa.Column("status", sa.Text(), server_default="draft"),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
    )
    op.create_index("ix_project_documents_project_category", "project_documents", ["project_id", "category"])
    op.create_table(
        "document_versions",
        _legacy_id(),
        sa.Column("version_id", sa.Text(), nullable=False, unique=True),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("version_number", sa.Text()),
        sa.Column("version_num", sa.Integer()),
        sa.Column("content", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("word_file_path", sa.Text()),
        sa.Column("changer", sa.Text()),
        sa.Column("editor", sa.Text()),
        sa.Column("change_note", sa.Text()),
        sa.Column("changed_at", TZ),
        sa.Column("created_at", TZ),
    )
    op.create_index("ix_document_versions_doc_created", "document_versions", ["doc_id", "created_at"])

    op.create_table(
        "expert_groups",
        _legacy_id(),
        sa.Column("group_id", sa.Text(), nullable=False, unique=True),
        sa.Column("meeting_name", sa.Text(), nullable=False),
        sa.Column("creator", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
    )
    op.create_table(
        "expert_group_members",
        _legacy_id(),
        sa.Column("group_id", sa.Text(), sa.ForeignKey("expert_groups.group_id"), nullable=False),
        sa.Column("expert_id", sa.Text(), nullable=False),
        sa.Column("selected_by", sa.Text(), nullable=False),
        sa.Column("selected_at", TZ, nullable=False),
        sa.UniqueConstraint("group_id", "expert_id", name="uq_expert_group_members_group_expert"),
    )
    op.create_table(
        "equipment_groups",
        _legacy_id(),
        sa.Column("group_id", sa.Text(), nullable=False, unique=True),
        sa.Column("project_name", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text()),
        sa.Column("creator", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
    )
    op.create_table(
        "equipment_group_members",
        _legacy_id(),
        sa.Column("group_id", sa.Text(), sa.ForeignKey("equipment_groups.group_id"), nullable=False),
        sa.Column("equipment_id", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("selected_by", sa.Text(), nullable=False),
        sa.Column("selected_at", TZ, nullable=False),
        sa.UniqueConstraint("group_id", "equipment_id", name="uq_equipment_group_members_group_equipment"),
    )
    op.create_table(
        "host_devices",
        _legacy_id(),
        sa.Column("host_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("category", sa.Text()),
        sa.Column("form", sa.Text()),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
    )
    op.create_table(
        "host_device_categories",
        _legacy_id(),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", TZ),
    )
    op.create_table(
        "device_host_relations",
        _legacy_id(),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("host_id", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
        sa.UniqueConstraint("device_id", "host_id", name="uq_device_host_relations_device_host"),
    )
    op.create_table(
        "research_units",
        _legacy_id(),
        sa.Column("unit_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text()),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
    )

    op.create_table(
        "generic_tables",
        _legacy_id(),
        sa.Column("table_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("creator", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("current_version_id", sa.Text()),
    )
    op.create_table(
        "generic_table_versions",
        _legacy_id(),
        sa.Column("version_id", sa.Text(), nullable=False, unique=True),
        sa.Column("table_id", sa.Text(), sa.ForeignKey("generic_tables.table_id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("version_label", sa.Text()),
        sa.Column("create_method", sa.Text(), nullable=False),
        sa.Column("source_version_id", sa.Text()),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_size", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("creator", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("table_id", "version_number", name="uq_generic_table_versions_table_number"),
    )
    op.create_foreign_key(
        "fk_generic_tables_current_version",
        "generic_tables",
        "generic_table_versions",
        ["current_version_id"],
        ["version_id"],
        use_alter=True,
    )
    op.create_table(
        "generic_table_columns",
        _legacy_id(),
        sa.Column("version_id", sa.Text(), sa.ForeignKey("generic_table_versions.version_id"), nullable=False),
        sa.Column("col_key", sa.Text(), nullable=False),
        sa.Column("col_name", sa.Text(), nullable=False),
        sa.Column("col_type", sa.Text(), nullable=False),
        sa.Column("col_index", sa.Integer(), nullable=False),
        sa.Column("col_width", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("col_align", sa.Text(), nullable=False, server_default="left"),
        sa.Column("col_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("col_options", JSONB),
        sa.Column("created_at", TZ, nullable=False),
        sa.UniqueConstraint("version_id", "col_key", name="uq_generic_table_columns_version_key"),
    )
    op.create_table(
        "generic_table_data",
        _legacy_id(),
        sa.Column("version_id", sa.Text(), sa.ForeignKey("generic_table_versions.version_id"), nullable=False),
        sa.Column("row_key", sa.Text(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("row_data", JSONB, nullable=False),
        sa.Column("row_color", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.UniqueConstraint("version_id", "row_key", name="uq_generic_table_data_version_key"),
    )
    op.create_index("ix_generic_table_versions_table", "generic_table_versions", ["table_id", "version_number"])
    op.create_index("ix_generic_table_columns_version_order", "generic_table_columns", ["version_id", "col_index"])
    op.create_index("ix_generic_table_data_version_row", "generic_table_data", ["version_id", "row_index", "id"])

    op.create_table(
        "expense_reimbursement",
        _legacy_id(),
        sa.Column("reimbursement_no", sa.Text(), unique=True),
        sa.Column("title", sa.Text()),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="草稿"),
        sa.Column("remark", sa.Text()),
        sa.Column("approver", sa.Text()),
        sa.Column("created_at", TZ),
        sa.Column("updated_at", TZ),
        sa.Column("confirmed_at", TZ),
        sa.Column("reimbursement_type", sa.Text(), server_default="采购报销"),
        sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("documents", JSONB, nullable=False, server_default="[]"),
    )
    op.create_table(
        "expense_invoice",
        _legacy_id(),
        sa.Column("reimbursement_id", sa.BigInteger(), sa.ForeignKey("expense_reimbursement.id")),
        sa.Column("invoice_no", sa.Text()),
        sa.Column("date", sa.Date()),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("price_ex_tax", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("buyer", sa.Text()),
        sa.Column("seller", sa.Text()),
        sa.Column("content", sa.Text()),
        sa.Column("invoice_type", sa.Text()),
        sa.Column("tax_rate", sa.Text()),
        sa.Column("ocr_text", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("confidence", sa.Text(), server_default="高"),
        sa.Column("status", sa.Text(), server_default="未匹配"),
        sa.Column("matched_payment_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", TZ),
        sa.Column("spec", sa.Text(), server_default=""),
        sa.Column("train_no", sa.Text(), server_default=""),
        sa.Column("departure_station", sa.Text(), server_default=""),
        sa.Column("arrival_station", sa.Text(), server_default=""),
        sa.Column("departure_date", sa.Date()),
        sa.Column("seat_type", sa.Text(), server_default=""),
        sa.Column("passenger_name", sa.Text(), server_default=""),
        sa.Column("id_card_no", sa.Text(), server_default=""),
        sa.Column("flight_no", sa.Text(), server_default=""),
        sa.Column("departure_airport", sa.Text(), server_default=""),
        sa.Column("arrival_airport", sa.Text(), server_default=""),
        sa.Column("departure_time", TZ),
        sa.Column("departure_city", sa.Text(), server_default=""),
        sa.Column("arrival_city", sa.Text(), server_default=""),
    )
    op.create_table(
        "expense_invoice_item",
        _legacy_id(),
        sa.Column("invoice_id", sa.BigInteger(), sa.ForeignKey("expense_invoice.id")),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("name", sa.Text()),
        sa.Column("spec", sa.Text()),
        sa.Column("unit", sa.Text()),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax_rate", sa.Text()),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
    )
    op.create_table(
        "expense_payment",
        _legacy_id(),
        sa.Column("reimbursement_id", sa.BigInteger(), sa.ForeignKey("expense_reimbursement.id")),
        sa.Column("payment_no", sa.Text()),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("pay_date", sa.Date()),
        sa.Column("ocr_text", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("status", sa.Text(), server_default="未匹配"),
        sa.Column("matched_invoice_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", TZ),
        sa.Column("payer", sa.Text(), server_default=""),
    )
    op.create_index("ix_expense_reimbursement_status_created", "expense_reimbursement", ["status", sa.text("created_at DESC"), "id"])
    op.create_index("ix_expense_invoice_reimbursement_status", "expense_invoice", ["reimbursement_id", "status", "date"])
    op.create_index("ix_expense_payment_reimbursement_status", "expense_payment", ["reimbursement_id", "status", "pay_date"])

    op.create_table(
        "llm_models",
        _legacy_id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model_type", sa.Text(), nullable=False, server_default="corrector"),
        sa.Column("file_path", sa.Text()),
        sa.Column("n_ctx", sa.Integer(), server_default="512"),
        sa.Column("n_threads", sa.Integer(), server_default="4"),
        sa.Column("temperature", sa.Numeric(5, 4), server_default="0.3"),
        sa.Column("max_tokens", sa.Integer(), server_default="512"),
        sa.Column("prompt_template", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
    )
    op.create_table(
        "inference_server_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_status", sa.Text(), server_default="stopped"),
        sa.Column("model_loaded", sa.Text(), server_default=""),
        sa.Column("model_load_time", sa.Numeric(18, 3), server_default="0"),
        sa.Column("total_requests", sa.BigInteger(), server_default="0"),
        sa.Column("avg_latency_ms", sa.Numeric(18, 3), server_default="0"),
        sa.Column("last_request_time", TZ),
        sa.Column("memory_usage_mb", sa.Numeric(18, 3), server_default="0"),
        sa.Column("cpu_usage_percent", sa.Numeric(7, 3), server_default="0"),
        sa.Column("error_message", sa.Text(), server_default=""),
        sa.Column("updated_at", TZ, nullable=False),
        sa.CheckConstraint("id = 1", name="ck_inference_server_status_singleton"),
    )


def downgrade() -> None:
    op.drop_table("inference_server_status")
    op.drop_table("llm_models")
    op.drop_table("expense_payment")
    op.drop_table("expense_invoice_item")
    op.drop_table("expense_invoice")
    op.drop_table("expense_reimbursement")
    op.drop_table("generic_table_data")
    op.drop_table("generic_table_columns")
    op.drop_constraint("fk_generic_tables_current_version", "generic_tables", type_="foreignkey")
    op.drop_table("generic_table_versions")
    op.drop_table("generic_tables")
    op.drop_table("research_units")
    op.drop_table("device_host_relations")
    op.drop_table("host_device_categories")
    op.drop_table("host_devices")
    op.drop_table("equipment_group_members")
    op.drop_table("equipment_groups")
    op.drop_table("expert_group_members")
    op.drop_table("expert_groups")
    op.drop_table("document_versions")
    op.drop_table("project_documents")
    op.drop_table("doc_templates")
    op.drop_table("experts")
    op.drop_table("standards")
    op.drop_table("knowledge_subclasses")
    op.drop_table("equipment")
    op.drop_table("legacy_migration_issues")
    op.drop_table("legacy_migration_batches")
    op.drop_table("audit_events")
    op.drop_table("object_files")
    op.drop_table("stored_file_versions")
    op.drop_table("stored_files")
    op.drop_table("project_closures")
    op.drop_table("project_outputs")
    op.drop_table("project_changes")
    op.drop_table("project_progress")
    op.drop_table("crypto_projects")
    op.drop_table("security_projects")
    op.drop_table("projects")
    op.drop_table("project_registry")
    op.drop_table("proposal_ai_drafts")
    op.drop_table("proposal_decisions")
    op.drop_table("proposal_argumentations")
    op.drop_table("proposals")
    op.drop_table("users")
