"""Complete the confirmed V1 project lifecycle record contract.

Revision ID: 0004_project_lifecycle
Revises: 0003_project_establishment
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_project_lifecycle"
down_revision = "0003_project_establishment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_progress", sa.Column("issues", sa.Text()))
    op.add_column("project_progress", sa.Column("next_actions", sa.Text()))
    op.create_check_constraint(
        "ck_project_progress_status",
        "project_progress",
        "status IN ('NORMAL','RISK','BLOCKED')",
    )
    op.create_check_constraint(
        "ck_project_progress_risk_level",
        "project_progress",
        "risk_level IS NULL OR risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL')",
    )

    op.add_column("project_changes", sa.Column("decision", sa.Text()))
    op.add_column("project_changes", sa.Column("decision_date", sa.Date()))
    op.create_check_constraint(
        "ck_project_changes_decision",
        "project_changes",
        "decision IS NULL OR decision IN ('AGREED','REJECTED','FILED')",
    )
    op.create_check_constraint(
        "ck_project_changes_type",
        "project_changes",
        "change_type IN ('GOAL','PERIOD','LEADER','CONTENT','OTHER','STATUS_TRANSITION')",
    )

    op.add_column("project_outputs", sa.Column("formed_date", sa.Date()))
    op.add_column("project_outputs", sa.Column("contributors", sa.Text()))
    op.create_check_constraint(
        "ck_project_outputs_type",
        "project_outputs",
        "output_type IN ('REPORT','PAPER','PATENT','SOFTWARE','STANDARD','PROTOTYPE','DATA','OTHER')",
    )

    op.add_column("project_closures", sa.Column("conclusion", sa.Text()))
    op.add_column("project_closures", sa.Column("remaining_issues", sa.Text()))
    op.add_column("project_closures", sa.Column("no_output_reason", sa.Text()))
    op.create_check_constraint(
        "ck_project_closures_conclusion",
        "project_closures",
        "conclusion IS NULL OR conclusion IN ('PASS','FAIL','TERMINATED')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    probes = (
        ("project_progress", "issues IS NOT NULL OR next_actions IS NOT NULL"),
        ("project_changes", "decision IS NOT NULL OR decision_date IS NOT NULL"),
        ("project_outputs", "formed_date IS NOT NULL OR contributors IS NOT NULL"),
        (
            "project_closures",
            "conclusion IS NOT NULL OR remaining_issues IS NOT NULL OR no_output_reason IS NOT NULL",
        ),
    )
    for table_name, predicate in probes:
        count = connection.scalar(
            sa.text(f"SELECT count(*) FROM {table_name} WHERE {predicate}")
        )
        if count:
            raise RuntimeError(
                "0004 downgrade would discard populated project lifecycle fields"
            )

    op.drop_constraint(
        "ck_project_closures_conclusion", "project_closures", type_="check"
    )
    op.drop_column("project_closures", "no_output_reason")
    op.drop_column("project_closures", "remaining_issues")
    op.drop_column("project_closures", "conclusion")

    op.drop_constraint("ck_project_outputs_type", "project_outputs", type_="check")
    op.drop_column("project_outputs", "contributors")
    op.drop_column("project_outputs", "formed_date")

    op.drop_constraint("ck_project_changes_type", "project_changes", type_="check")
    op.drop_constraint(
        "ck_project_changes_decision", "project_changes", type_="check"
    )
    op.drop_column("project_changes", "decision_date")
    op.drop_column("project_changes", "decision")

    op.drop_constraint(
        "ck_project_progress_risk_level", "project_progress", type_="check"
    )
    op.drop_constraint(
        "ck_project_progress_status", "project_progress", type_="check"
    )
    op.drop_column("project_progress", "next_actions")
    op.drop_column("project_progress", "issues")
