"""Store immutable establishment request fingerprints.

Revision ID: 0003_project_establishment
Revises: 0002_proposal_assistant
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_project_establishment"
down_revision = "0002_proposal_assistant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    category_tables = {
        "GENERAL_RESEARCH": "projects",
        "SECURITY_CONFIDENTIALITY": "security_projects",
        "CRYPTO_APPLICATION": "crypto_projects",
    }
    invalid_links = []
    for category, table_name in category_tables.items():
        invalid_projects = connection.scalar(sa.text(
            f"SELECT count(*) FROM {table_name} p "
            "LEFT JOIN project_registry r ON r.id = p.registry_id "
            "AND r.category = :category AND r.business_id = p.project_id "
            "WHERE p.registry_id IS NULL OR r.id IS NULL"
        ), {"category": category})
        missing_projects = connection.scalar(sa.text(
            "SELECT count(*) FROM project_registry r "
            f"LEFT JOIN {table_name} p ON p.registry_id = r.id "
            "AND p.project_id = r.business_id "
            "WHERE r.category = :category AND p.id IS NULL"
        ), {"category": category})
        if invalid_projects or missing_projects:
            invalid_links.append(
                f"{table_name}(invalid={invalid_projects},missing={missing_projects})"
            )
    if invalid_links:
        raise RuntimeError(
            "project registry links must be complete before upgrading: "
            + ", ".join(invalid_links)
        )
    duplicates = connection.execute(sa.text(
        "SELECT business_id, count(*) AS duplicate_count "
        "FROM project_registry GROUP BY business_id HAVING count(*) > 1 "
        "ORDER BY business_id LIMIT 20"
    )).mappings().all()
    if duplicates:
        sample = ", ".join(
            f"{row['business_id']}({row['duplicate_count']})" for row in duplicates
        )
        raise RuntimeError(
            "project_registry contains duplicate business_id values; "
            f"resolve them before upgrading: {sample}"
        )
    op.create_unique_constraint(
        "uq_project_registry_business_id", "project_registry", ["business_id"]
    )
    # Existing DEFER/REJECT rows have no establishment request snapshot. New
    # ESTABLISH writes always populate this field; null legacy rows are never
    # accepted as a replay match.
    op.add_column(
        "proposal_decisions",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "proposal_decisions",
        sa.Column("result_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_proposal_decisions_request_fingerprint",
        "proposal_decisions",
        "request_fingerprint IS NULL OR length(request_fingerprint) = 64",
    )


def downgrade() -> None:
    connection = op.get_bind()
    evidence_count = connection.scalar(sa.text(
        "SELECT count(*) FROM proposal_decisions "
        "WHERE request_fingerprint IS NOT NULL OR result_snapshot IS NOT NULL"
    ))
    if evidence_count:
        raise RuntimeError(
            "refusing lossy downgrade: proposal establishment idempotency evidence exists"
        )
    op.drop_constraint(
        "ck_proposal_decisions_request_fingerprint",
        "proposal_decisions",
        type_="check",
    )
    op.drop_column("proposal_decisions", "result_snapshot")
    op.drop_column("proposal_decisions", "request_fingerprint")
    op.drop_constraint(
        "uq_project_registry_business_id", "project_registry", type_="unique"
    )
