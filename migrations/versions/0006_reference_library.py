"""Add retained standards and reference-template file-library metadata.

Revision ID: 0006_reference_library
Revises: 0005_expert_import_batches
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_reference_library"
down_revision = "0005_expert_import_batches"
branch_labels = None
depends_on = None


DEFAULT_TEMPLATE_FOLDERS = (
    "财务模板",
    "会务模板",
    "公文模板",
    "方案模板",
    "其他模板",
)

UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)


def _folder_id(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"research-v1-reference-template-folder:{name}")


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("updated_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("version >= 1"),
    ]


def upgrade() -> None:
    op.add_column(
        "standards",
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
    )
    op.create_check_constraint(
        "ck_standards_status", "standards", "status IN ('ACTIVE','ARCHIVED')"
    )
    op.create_index(
        "uq_standards_doc_id",
        "standards",
        ["doc_id"],
        unique=True,
        postgresql_where=sa.text("doc_id IS NOT NULL"),
    )

    op.create_table(
        "reference_template_folders",
        sa.Column(
            "id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "parent_id",
            UUID,
            sa.ForeignKey("reference_template_folders.id"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        *_audit_columns(),
        sa.CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED')",
            name="ck_reference_template_folders_status",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_reference_template_folders_root_name "
        "ON reference_template_folders (lower(name)) "
        "WHERE parent_id IS NULL AND status = 'ACTIVE'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_reference_template_folders_parent_name "
        "ON reference_template_folders (parent_id, lower(name)) "
        "WHERE parent_id IS NOT NULL AND status = 'ACTIVE'"
    )
    op.create_index(
        "ix_reference_template_folders_parent_status",
        "reference_template_folders",
        ["parent_id", "status"],
    )

    op.create_table(
        "reference_template_items",
        sa.Column(
            "id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("template_id", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "folder_id",
            UUID,
            sa.ForeignKey("reference_template_folders.id"),
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        *_audit_columns(),
        sa.CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED')",
            name="ck_reference_template_items_status",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_reference_template_items_root_display_name "
        "ON reference_template_items (lower(display_name)) "
        "WHERE folder_id IS NULL AND status = 'ACTIVE'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_reference_template_items_folder_display_name "
        "ON reference_template_items (folder_id, lower(display_name)) "
        "WHERE folder_id IS NOT NULL AND status = 'ACTIVE'"
    )
    op.create_index(
        "ix_reference_template_items_folder_status",
        "reference_template_items",
        ["folder_id", "status"],
    )

    folders = sa.table(
        "reference_template_folders",
        sa.column("id", UUID),
        sa.column("name", sa.Text()),
        sa.column("status", sa.Text()),
    )
    op.bulk_insert(
        folders,
        [
            {"id": _folder_id(name), "name": name, "status": "ACTIVE"}
            for name in DEFAULT_TEMPLATE_FOLDERS
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    item_count = bind.scalar(sa.text("SELECT count(*) FROM reference_template_items"))
    user_folder_count = bind.scalar(
        sa.text(
            "SELECT count(*) FROM reference_template_folders "
            "WHERE id <> ALL(:seed_ids)"
        ),
        {"seed_ids": list(map(_folder_id, DEFAULT_TEMPLATE_FOLDERS))},
    )
    if item_count or user_folder_count:
        raise RuntimeError(
            "reference library contains user records; export or remove them before downgrade"
        )

    op.drop_index(
        "ix_reference_template_items_folder_status",
        table_name="reference_template_items",
    )
    op.execute("DROP INDEX uq_reference_template_items_folder_display_name")
    op.execute("DROP INDEX uq_reference_template_items_root_display_name")
    op.drop_table("reference_template_items")
    op.drop_index(
        "ix_reference_template_folders_parent_status",
        table_name="reference_template_folders",
    )
    op.execute("DROP INDEX uq_reference_template_folders_parent_name")
    op.execute("DROP INDEX uq_reference_template_folders_root_name")
    op.drop_table("reference_template_folders")
    op.drop_index("uq_standards_doc_id", table_name="standards")
    op.drop_constraint("ck_standards_status", "standards", type_="check")
    op.drop_column("standards", "status")
