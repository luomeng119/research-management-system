"""Record immutable vocabulary identities without rewriting historical content."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0013_report_redaction_snapshot'
down_revision = '0012_sensitive_term_sets'
branch_labels = None
depends_on = None


def upgrade():
    for table in ('research_report_drafts', 'research_report_versions'):
        op.add_column(table, sa.Column('redaction_snapshot', sa.JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), 'postgresql'), nullable=True))


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == 'postgresql':
        op.execute('LOCK TABLE research_report_drafts, research_report_versions IN ACCESS EXCLUSIVE MODE')
    for table in ('research_report_drafts', 'research_report_versions'):
        if connection.scalar(sa.text(f'SELECT count(*) FROM {table} WHERE redaction_snapshot IS NOT NULL')):
            raise RuntimeError('Report redaction snapshots contain data; export and review before downgrade')
    for table in ('research_report_drafts', 'research_report_versions'):
        op.drop_column(table, 'redaction_snapshot')
