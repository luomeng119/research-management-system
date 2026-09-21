"""Opt-in PostgreSQL test, restricted to a disposable report_test database."""
import importlib.util
import os
from pathlib import Path
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.repositories.research_reports import ResearchReportsRepository
from app.services.research_reports import ResearchReportService, ResearchReportServiceError


def test_postgres_migration_concurrent_versions_and_immutability():
    url = os.environ.get('RESEARCH_REPORT_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Explicit disposable RESEARCH_REPORT_TEST_DATABASE_URL required')
    parsed = sa.engine.make_url(url)
    if parsed.host not in {'localhost','127.0.0.1'} or parsed.port == 58562 or not (parsed.database or '').startswith('report_test'):
        pytest.fail('Only a local report_test database outside QA port 58562 is allowed')
    schema = 'report_test_' + uuid.uuid4().hex
    admin = sa.create_engine(url)
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA {schema}'))
        engine = sa.create_engine(url, connect_args={'options':f'-c search_path={schema}'})
        metadata = sa.MetaData()
        users = sa.Table('users',metadata,sa.Column('id',sa.BigInteger,primary_key=True),sa.Column('name',sa.Text),sa.Column('username',sa.Text))
        for name in ['proposals','project_registry']:
            sa.Table(name,metadata,sa.Column('id',sa.Uuid,primary_key=True),sa.Column('business_id',sa.Text,nullable=False))
        metadata.create_all(engine)
        path = Path(__file__).parents[2] / 'migrations/versions/0010_research_reports.py'
        spec = importlib.util.spec_from_file_location('pg_report_migration',path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            connection.execute(users.insert().values(id=1,name='PG演练作者',username='pg_test'))
            connection.execute(metadata.tables['proposals'].insert().values(id=uuid.uuid4(),business_id='TP-PG-TEST'))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                draft_spec = importlib.util.spec_from_file_location('pg_draft_migration',path.with_name('0011_research_report_drafts.py'))
                draft_migration = importlib.util.module_from_spec(draft_spec)
                draft_spec.loader.exec_module(draft_migration)
                draft_migration.upgrade()
                snapshot_spec = importlib.util.spec_from_file_location('pg_snapshot_migration',path.with_name('0013_report_redaction_snapshot.py'))
                snapshot_migration = importlib.util.module_from_spec(snapshot_spec)
                snapshot_spec.loader.exec_module(snapshot_migration)
                snapshot_migration.upgrade()
        class Audit:
            def record(self, connection, **event):
                pass
        class EmptyVocabulary:
            def current(self): return dict(version=0,rules=[])
            def get_version(self, version):
                if version != 0: raise ValueError('missing version')
                return self.current()
        service = ResearchReportService(ResearchReportsRepository(engine),Audit(),EmptyVocabulary())
        report = service.create(dict(objectType='PROPOSAL',objectId='TP-PG-TEST',title='并发测试报告',purpose='',body='（原版）²\r\n'),actor_user_id=1,request_id='pg')
        from app.repositories.research_report_drafts import ResearchReportDraftsRepository
        from app.services.research_report_drafts import ResearchReportDraftService
        from app.tests.test_research_report_drafts import Files, Assistant, extract
        drafts = ResearchReportDraftService(ResearchReportDraftsRepository(engine),service,Files(),Assistant(),extract)
        draft = drafts.generate(report['id'],dict(baseVersion=1,body='',selections=[dict(fileId=str(uuid.uuid4()),versionNo=1)]),actor_user_id=1,request_id='pg-draft')
        barrier = Barrier(2)
        def save(body):
            barrier.wait(timeout=10)
            try:
                return service.save_version(report['id'],dict(baseVersion=1,body=body,draftId=draft['id']),actor_user_id=1,request_id='pg')['currentVersion']
            except ResearchReportServiceError as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(save,['版本甲','版本乙'])) == [2,409]
        assert service.get_version(report['id'],1)['body'] == '（原版）²\r\n'
        assert service.current(report['id'])['createdByName'] == 'PG演练作者'
        assert service.current(report['id'])['sources'][0]['text'] == '来源原文（不可变）'
        assert service.current(report['id'])['redactionSnapshot'] == draft['redactionSnapshot']
        assert draft['redactionSnapshot']['version'] == 0
        for sql in ['UPDATE research_report_versions SET body=\'覆盖\'','DELETE FROM research_report_versions']:
            with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
                connection.execute(sa.text(sql))
        for sql in ["UPDATE research_report_drafts SET generated_body='覆盖'",'DELETE FROM research_report_drafts']:
            with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
                connection.execute(sa.text(sql))
        for table in ('research_report_drafts','research_report_versions'):
            with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
                connection.execute(sa.text(f'UPDATE {table} SET redaction_snapshot=NULL'))
        with pytest.raises(RuntimeError,match='contain data'), engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                snapshot_migration.downgrade()
        with pytest.raises(RuntimeError,match='contain data'), engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                draft_migration.downgrade()
        with pytest.raises(RuntimeError,match='contain data'), engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
        assert service.history(report['id'])['total'] == 2
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        admin.dispose()
