"""Persist expert imports as owner-bound atomic batches.

Revision ID: 0005_expert_import_batches
Revises: 0004_project_lifecycle
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_expert_import_batches"
down_revision = "0004_project_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expert_import_batches",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True, server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "rows", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("valid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('PREVIEW','COMMITTED','CANCELLED')",
            name="ck_expert_import_batches_status",
        ),
        sa.CheckConstraint(
            "valid_count >= 0 AND error_count >= 0 AND duplicate_count >= 0",
            name="ck_expert_import_batches_counts",
        ),
        sa.CheckConstraint("version >= 1", name="ck_expert_import_batches_version"),
    )
    op.create_index(
        "ix_expert_import_batches_owner_status_created",
        "expert_import_batches",
        ["owner_user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_experts_created_id", "experts", ["created_at", "id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM expert_import_batches")):
        raise RuntimeError(
            "expert_import_batches contains data; export or remove it before downgrade"
        )
    op.drop_index("ix_experts_created_id", table_name="experts")
    op.drop_index(
        "ix_expert_import_batches_owner_status_created",
        table_name="expert_import_batches",
    )
    op.drop_table("expert_import_batches")
