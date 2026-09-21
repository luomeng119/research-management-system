"""Opt-in vocabulary check confined to a disposable local test database."""
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

from app.repositories.sensitive_terms import SensitiveTermSetsRepository
from app.services.sensitive_term_sets import SensitiveTermSetService,SensitiveTermSetError


def test_postgres_vocabulary_cas_immutable_history_and_permissions():
    url=os.environ.get('RESEARCH_REPORT_TEST_DATABASE_URL')
    if not url:pytest.skip('Explicit disposable RESEARCH_REPORT_TEST_DATABASE_URL required')
    parsed=sa.engine.make_url(url)
    if parsed.host not in {'127.0.0.1','localhost'} or parsed.port==58562 or not (parsed.database or '').startswith('report_test'):
        pytest.fail('Only local report_test database outside QA port 58562 is allowed')
    schema='term_test_'+uuid.uuid4().hex
    admin=sa.create_engine(url)
    engine=None
    try:
        with admin.begin() as connection:connection.execute(sa.text(f'CREATE SCHEMA {schema}'))
        engine=sa.create_engine(url,connect_args={'options':f'-c search_path={schema}'})
        metadata=sa.MetaData()
        users=sa.Table('users',metadata,sa.Column('id',sa.BigInteger,primary_key=True),sa.Column('role',sa.Text),sa.Column('status',sa.Text),sa.Column('name',sa.Text),sa.Column('username',sa.Text))
        metadata.create_all(engine)
        spec=importlib.util.spec_from_file_location('pg_term_migration',Path(__file__).parents[2]/'migrations/versions/0012_sensitive_term_sets.py')
        migration=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            connection.execute(users.insert(),[dict(id=1,role='SYSTEM_MAINTAINER',status='active',name='维护员',username='maintainer'),dict(id=2,role='BUSINESS_USER',status='active',name='业务员',username='business')])
            with Operations.context(MigrationContext.configure(connection)):migration.upgrade()
        class Audit:
            def record(self,connection,**event):pass
        service=SensitiveTermSetService(SensitiveTermSetsRepository(engine),Audit())
        payload=dict(expectedVersion=0,rules=[dict(id='one',source='青岚',replacement='QL',enabled=True)])
        with pytest.raises(SensitiveTermSetError) as exc:service.save_version(payload,actor_user_id=2,request_id='pg')
        assert exc.value.status_code==403
        barrier=Barrier(2)
        def save(_):
            barrier.wait(timeout=10)
            try:return service.save_version(payload,actor_user_id=1,request_id='pg')['version']
            except SensitiveTermSetError as error:return error.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:assert sorted(executor.map(save,[1,2]))==[1,409]
        assert service.current()['rules']==payload['rules']
        for sql in ["UPDATE sensitive_term_set_versions SET note='覆盖'",'DELETE FROM sensitive_term_set_versions']:
            with pytest.raises(sa.exc.DBAPIError),engine.begin() as connection:connection.execute(sa.text(sql))
        with pytest.raises(RuntimeError,match='contain data'),engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):migration.downgrade()
        assert service.history()['total']==1
    finally:
        if engine is not None:engine.dispose()
        with admin.begin() as connection:connection.execute(sa.text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        admin.dispose()
