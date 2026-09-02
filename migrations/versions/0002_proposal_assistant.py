"""Enable truthful proposal assistant provenance and stale-draft protection.

Revision ID: 0002_proposal_assistant
Revises: 0001_v1_core
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_proposal_assistant"
down_revision = "0001_v1_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proposal_ai_drafts",
        sa.Column("source_proposal_version", sa.Integer(), nullable=True),
    )
    # 0001 did not record the source proposal version. Preserve old rows for
    # traceability but mark the version unknown so they can never be applied.
    op.execute("UPDATE proposal_ai_drafts SET source_proposal_version = 0")
    op.alter_column("proposal_ai_drafts", "source_proposal_version", nullable=False)
    op.create_check_constraint(
        "ck_proposal_ai_drafts_source_version",
        "proposal_ai_drafts",
        "source_proposal_version >= 0",
    )
    op.drop_constraint(
        "ck_proposal_ai_drafts_provider_kind",
        "proposal_ai_drafts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_proposal_ai_drafts_provider_kind",
        "proposal_ai_drafts",
        "provider_kind IN ('LOCAL','DEEPSEEK')",
    )


def downgrade() -> None:
    # Revision 0001 cannot represent remote-provider provenance. Refuse a
    # lossy downgrade instead of silently deleting DeepSeek audit evidence.
    connection = op.get_bind()
    remote_draft_count = connection.execute(sa.text(
        "SELECT count(*) FROM proposal_ai_drafts WHERE provider_kind <> 'LOCAL'"
    )).scalar_one()
    if remote_draft_count:
        raise RuntimeError(
            "cannot downgrade 0002 while non-LOCAL proposal assistant drafts exist"
        )
    op.drop_constraint(
        "ck_proposal_ai_drafts_provider_kind",
        "proposal_ai_drafts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_proposal_ai_drafts_provider_kind",
        "proposal_ai_drafts",
        "provider_kind = 'LOCAL'",
    )
    op.drop_constraint(
        "ck_proposal_ai_drafts_source_version",
        "proposal_ai_drafts",
        type_="check",
    )
    op.drop_column("proposal_ai_drafts", "source_proposal_version")
