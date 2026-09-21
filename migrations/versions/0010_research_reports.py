"""Independent research reports and append-only plain-text revisions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0010_research_reports'
down_revision = '0009_equipment_import_batches'
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.String(36).with_variant(postgresql.UUID(as_uuid=True), 'postgresql')
    op.create_table(
        'research_reports',
        sa.Column('id', identifier, primary_key=True),
        sa.Column('proposal_id', identifier, sa.ForeignKey('proposals.id'), nullable=True),
        sa.Column('project_registry_id', identifier, sa.ForeignKey('project_registry.id'), nullable=True),
        sa.Column('title', sa.Text, nullable=False),
        sa.Column('purpose', sa.Text, nullable=False),
        sa.Column('current_version', sa.Integer, nullable=False),
        sa.Column('created_by', sa.BigInteger, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_by', sa.BigInteger, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('(proposal_id IS NOT NULL AND project_registry_id IS NULL) OR (proposal_id IS NULL AND project_registry_id IS NOT NULL)', name='ck_research_reports_one_parent'),
        sa.CheckConstraint('current_version >= 1', name='ck_research_reports_current_version'),
        sa.CheckConstraint('length(title) BETWEEN 1 AND 200', name='ck_research_reports_title'),
        sa.CheckConstraint('length(purpose) <= 2000', name='ck_research_reports_purpose'),
    )
    op.create_index('ix_research_reports_proposal_updated', 'research_reports', ['proposal_id', 'updated_at'])
    op.create_index('ix_research_reports_project_updated', 'research_reports', ['project_registry_id', 'updated_at'])
    op.create_table(
        'research_report_versions',
        sa.Column('id', identifier, primary_key=True),
        sa.Column('report_id', identifier, sa.ForeignKey('research_reports.id'), nullable=False),
        sa.Column('version_no', sa.Integer, nullable=False),
        sa.Column('body', sa.Text, nullable=False),
        sa.Column('note', sa.Text, nullable=False),
        sa.Column('created_by', sa.BigInteger, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('report_id', 'version_no', name='uq_research_report_versions_number'),
        sa.CheckConstraint('version_no >= 1', name='ck_research_report_versions_number'),
        sa.CheckConstraint('length(body) <= 200000', name='ck_research_report_versions_body'),
        sa.CheckConstraint('length(note) <= 2000', name='ck_research_report_versions_note'),
    )

    for table, column in [('research_reports','created_by'), ('research_reports','updated_by'), ('research_report_versions','created_by')]:
        op.create_index(f'ix_{table}_{column}', table, [column])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""CREATE FUNCTION reject_research_report_version_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Research report versions are immutable';
            END; $$""")
        op.execute("""CREATE TRIGGER research_report_versions_immutable
            BEFORE UPDATE OR DELETE ON research_report_versions
            FOR EACH ROW EXECUTE FUNCTION reject_research_report_version_mutation()""")
    else:
        for operation in ['UPDATE','DELETE']:
            op.execute(f"""CREATE TRIGGER research_report_versions_no_{operation.lower()}
                BEFORE {operation} ON research_report_versions BEGIN
                SELECT RAISE(ABORT, 'Research report versions are immutable'); END""")


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('LOCK TABLE research_reports, research_report_versions IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().scalar(sa.text('SELECT (SELECT count(*) FROM research_reports) + (SELECT count(*) FROM research_report_versions)')):
        raise RuntimeError('Research reports contain data; export and review before downgrade')
    op.drop_table('research_report_versions')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION reject_research_report_version_mutation()')
    op.drop_index('ix_research_reports_project_updated', table_name='research_reports')
    op.drop_index('ix_research_reports_proposal_updated', table_name='research_reports')
    op.drop_table('research_reports')
