"""Persist equipment imports as owner-bound atomic batches.

Revision ID: 0009_equipment_import_batches
Revises: 0008_resource_dictionary_keys
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_equipment_import_batches"
down_revision = "0008_resource_dictionary_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "equipment_import_batches",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "statistics", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('PREVIEW','RUNNING','COMMITTED','CANCELLED','FAILED')",
            name="ck_equipment_import_batches_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rows) = 'array' AND jsonb_array_length(rows) <= 1000",
            name="ck_equipment_import_batches_rows",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(statistics) = 'object'",
            name="ck_equipment_import_batches_statistics",
        ),
        sa.CheckConstraint("version >= 1", name="ck_equipment_import_batches_version"),
    )
    op.create_index(
        "ix_equipment_import_batches_owner_status_created",
        "equipment_import_batches", ["owner_user_id", "status", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM equipment_import_batches")):
        raise RuntimeError(
            "equipment_import_batches contains data; export or remove it before downgrade"
        )
    op.drop_index(
        "ix_equipment_import_batches_owner_status_created",
        table_name="equipment_import_batches",
    )
    op.drop_table("equipment_import_batches")
