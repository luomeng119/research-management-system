"""Immutable generated report drafts with source snapshots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0011_research_report_drafts'
down_revision = '0010_research_reports'
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.String(36).with_variant(postgresql.UUID(as_uuid=True),'postgresql')
    json_type = sa.JSON().with_variant(postgresql.JSONB(),'postgresql')
    op.create_table('research_report_drafts',
        sa.Column('id',identifier,primary_key=True),
        sa.Column('report_id',identifier,sa.ForeignKey('research_reports.id'),nullable=False),
        sa.Column('base_version',sa.Integer,nullable=False),
        sa.Column('input_title',sa.Text,nullable=False),
        sa.Column('input_purpose',sa.Text,nullable=False),
        sa.Column('input_body',sa.Text,nullable=False),
        sa.Column('sources',json_type,nullable=False),
        sa.Column('model_name',sa.Text,nullable=False),
        sa.Column('prompt_version',sa.Text,nullable=False),
        sa.Column('usage',json_type,nullable=False),
        sa.Column('input_hash',sa.Text,nullable=True),
        sa.Column('generated_body',sa.Text,nullable=False),
        sa.Column('created_by',sa.BigInteger,sa.ForeignKey('users.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('report_id','id',name='uq_research_report_drafts_report_id'),
        sa.ForeignKeyConstraint(['report_id','base_version'],['research_report_versions.report_id','research_report_versions.version_no'],name='fk_research_report_drafts_base'),
        sa.CheckConstraint('base_version >= 1',name='ck_research_report_drafts_base'),
        sa.CheckConstraint('length(input_body) <= 200000 AND length(generated_body) <= 200000',name='ck_research_report_drafts_body'),
    )
    op.create_index('ix_research_report_drafts_report_created','research_report_drafts',['report_id','created_at'])
    op.create_index('ix_research_report_drafts_created_by','research_report_drafts',['created_by'])
    if op.get_bind().dialect.name == 'postgresql':
        op.add_column('research_report_versions',sa.Column('draft_id',identifier,nullable=True))
        op.create_foreign_key('fk_research_report_versions_draft','research_report_versions','research_report_drafts',['report_id','draft_id'],['report_id','id'])
        op.execute("""CREATE FUNCTION reject_research_report_draft_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Research report drafts are immutable'; END; $$""")
        op.execute('CREATE TRIGGER research_report_drafts_immutable BEFORE UPDATE OR DELETE ON research_report_drafts FOR EACH ROW EXECUTE FUNCTION reject_research_report_draft_mutation()')
    else:
        op.execute('ALTER TABLE research_report_versions ADD COLUMN draft_id VARCHAR(36) REFERENCES research_report_drafts(id)')
        op.execute("CREATE TRIGGER research_report_versions_draft_parent BEFORE INSERT ON research_report_versions WHEN NEW.draft_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM research_report_drafts WHERE id=NEW.draft_id AND report_id=NEW.report_id) BEGIN SELECT RAISE(ABORT, 'Report draft belongs to another report'); END")
        for operation in ['UPDATE','DELETE']:
            op.execute(f"CREATE TRIGGER research_report_drafts_no_{operation.lower()} BEFORE {operation} ON research_report_drafts BEGIN SELECT RAISE(ABORT, 'Research report drafts are immutable'); END")
    op.create_index('ix_research_report_versions_draft','research_report_versions',['draft_id'])


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('LOCK TABLE research_report_drafts, research_report_versions IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM research_report_drafts')) or op.get_bind().scalar(sa.text('SELECT count(*) FROM research_report_versions WHERE draft_id IS NOT NULL')):
        raise RuntimeError('Report drafts contain data; export and review before downgrade')
    op.drop_index('ix_research_report_versions_draft',table_name='research_report_versions')
    if op.get_bind().dialect.name == 'postgresql':
        op.drop_constraint('fk_research_report_versions_draft','research_report_versions',type_='foreignkey')
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('DROP TRIGGER research_report_versions_draft_parent')
    op.drop_column('research_report_versions','draft_id')
    op.drop_table('research_report_drafts')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION reject_research_report_draft_mutation()')
