"""Single global vocabulary with immutable snapshots and a CAS pointer."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision='0012_sensitive_term_sets'
down_revision='0011_research_report_drafts'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('sensitive_term_sets',
        sa.Column('id',sa.Integer,primary_key=True),
        sa.Column('current_version',sa.Integer,nullable=False),
        sa.CheckConstraint('id = 1',name='ck_sensitive_term_sets_singleton'),
        sa.CheckConstraint('current_version >= 0',name='ck_sensitive_term_sets_version'),
    )
    op.execute('INSERT INTO sensitive_term_sets (id,current_version) VALUES (1,0)')
    op.create_table('sensitive_term_set_versions',
        sa.Column('version_no',sa.Integer,primary_key=True),
        sa.Column('rules',sa.JSON().with_variant(postgresql.JSONB(),'postgresql'),nullable=False),
        sa.Column('note',sa.Text,nullable=False),
        sa.Column('created_by',sa.BigInteger,sa.ForeignKey('users.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('version_no >= 1',name='ck_sensitive_term_set_versions_number'),
        sa.CheckConstraint('length(note) <= 2000',name='ck_sensitive_term_set_versions_note'),
    )
    op.create_index('ix_sensitive_term_set_versions_created_by','sensitive_term_set_versions',['created_by'])
    if op.get_bind().dialect.name=='postgresql':
        op.create_check_constraint('ck_sensitive_term_set_versions_rules','sensitive_term_set_versions',"jsonb_typeof(rules) = 'array' AND jsonb_array_length(rules) <= 1000")
        op.execute("""CREATE FUNCTION reject_sensitive_term_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Sensitive term versions are immutable'; END; $$""")
        op.execute('CREATE TRIGGER sensitive_term_versions_immutable BEFORE UPDATE OR DELETE ON sensitive_term_set_versions FOR EACH ROW EXECUTE FUNCTION reject_sensitive_term_version_mutation()')
    else:
        for operation in ['UPDATE','DELETE']:
            op.execute(f"CREATE TRIGGER sensitive_term_versions_no_{operation.lower()} BEFORE {operation} ON sensitive_term_set_versions BEGIN SELECT RAISE(ABORT, 'Sensitive term versions are immutable'); END")


def downgrade():
    if op.get_bind().dialect.name=='postgresql':
        op.execute('LOCK TABLE sensitive_term_sets,sensitive_term_set_versions IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM sensitive_term_set_versions')) or op.get_bind().scalar(sa.text('SELECT count(*) FROM sensitive_term_sets WHERE current_version <> 0')):
        raise RuntimeError('Sensitive term snapshots contain data; export and review before downgrade')
    op.drop_table('sensitive_term_set_versions')
    op.drop_table('sensitive_term_sets')
    if op.get_bind().dialect.name=='postgresql':
        op.execute('DROP FUNCTION reject_sensitive_term_version_mutation()')
