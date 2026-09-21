"""Isolated vocabulary persistence and maintenance permission behavior."""
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.repositories.sensitive_terms import SensitiveTermSetsRepository
from app.services.sensitive_term_sets import SensitiveTermSetService, SensitiveTermSetError


@pytest.fixture
def vocabulary(tmp_path):
    engine=sa.create_engine('sqlite:///'+str(tmp_path/'terms.db'))
    sa.event.listen(engine,'connect',lambda db,_:db.execute('PRAGMA foreign_keys=ON'))
    metadata=sa.MetaData()
    users=sa.Table('users',metadata,sa.Column('id',sa.Integer,primary_key=True),sa.Column('role',sa.Text),sa.Column('status',sa.Text),sa.Column('name',sa.Text),sa.Column('username',sa.Text))
    metadata.create_all(engine)
    spec=importlib.util.spec_from_file_location('term_migration',Path(__file__).parents[2]/'migrations/versions/0012_sensitive_term_sets.py')
    migration=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.execute(users.insert(),[
            dict(id=1,role='SYSTEM_MAINTAINER',status='active',name='维护员',username='maintainer'),
            dict(id=2,role='BUSINESS_USER',status='active',name='业务员',username='business'),
            dict(id=3,role='SYSTEM_MAINTAINER',status='disabled',name='停用',username='disabled'),
        ])
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
    class Audit:
        def __init__(self): self.events=[]
        def record(self,connection,**event): self.events.append(event)
    audit=Audit()
    service=SensitiveTermSetService(SensitiveTermSetsRepository(engine),audit)
    yield service,engine,audit
    engine.dispose()


def payload(expected=0):
    return dict(expectedVersion=expected,rules=[dict(id='project',source='青岚-07',replacement='PRJ-A',enabled=True)],note='首次保存')


def save(service,data=None,actor=1):
    return service.save_version(data if data is not None else payload(),actor_user_id=actor,request_id='test')


def test_zero_library_then_save_and_preserve_previous_version(vocabulary):
    service,_,audit=vocabulary
    assert service.current()['version']==0
    assert service.current()['rules']==[]
    first=save(service)
    assert first['version']==1
    assert first['createdByName']=='维护员'
    second=payload(1)
    second['rules'][0]['enabled']=False
    assert save(service,second)['rules'][0]['enabled'] is False
    assert service.get_version(1)['rules'][0]['enabled'] is True
    assert [row['version'] for row in service.history()['items']]==[2,1]
    assert '青岚' not in str(audit.events)


@pytest.mark.parametrize('actor,status',[(None,401),(True,401),(99,401),(2,403),(3,403)])
def test_only_active_maintenance_actor_can_save(vocabulary,actor,status):
    service,_,_=vocabulary
    with pytest.raises(SensitiveTermSetError) as exc:
        save(service,actor=actor)
    assert exc.value.status_code==status
    assert service.current()['version']==0


def test_conflict_and_concurrent_first_save(vocabulary):
    service,_,_=vocabulary
    barrier=Barrier(2)
    def operation(_):
        barrier.wait(timeout=5)
        try:return save(service)['version']
        except SensitiveTermSetError as error:return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(operation,[1,2]))==[1,409]
    assert service.history()['total']==1


def test_snapshot_update_and_delete_are_database_rejected(vocabulary):
    service,engine,_=vocabulary
    save(service)
    table=service.repository.versions
    for statement in [table.update().values(note='覆盖'),table.delete()]:
        with pytest.raises(sa.exc.DBAPIError),engine.begin() as connection:
            connection.execute(statement)
    assert service.get_version(1)['note']=='首次保存'


def test_audit_failure_rolls_back_pointer_and_rules(vocabulary):
    service,_,audit=vocabulary
    def fail(*args,**kwargs):raise RuntimeError('audit failed')
    audit.record=fail
    with pytest.raises(SensitiveTermSetError) as exc:
        save(service)
    assert exc.value.status_code==503
    assert service.current()['version']==0
    assert service.history()['total']==0


def test_disabled_rule_kept_but_collision_is_checked_on_enable(vocabulary):
    service,_,_=vocabulary
    data=payload()
    data['rules'].append(dict(id='other',source='PRJ-A',replacement='OTHER',enabled=False))
    save(service,data)
    data['expectedVersion']=1
    data['rules'][1]['enabled']=True
    with pytest.raises(SensitiveTermSetError) as exc:
        save(service,data)
    assert exc.value.status_code==422
    assert service.current()['version']==1


@pytest.mark.parametrize('data',[
    dict(expectedVersion=True,rules=[]),dict(expectedVersion=-1,rules=[]),dict(expectedVersion=0,rules=None),
    dict(expectedVersion=0,rules=[dict(id='a',source='甲',replacement='A',enabled='true')]),
    dict(expectedVersion=0,rules=[dict(id='a',source='甲',replacement='A',enabled=True),dict(id='a',source='乙',replacement='B',enabled=False)]),
])
def test_invalid_payload_is_422(vocabulary,data):
    service,_,_=vocabulary
    with pytest.raises(SensitiveTermSetError) as exc:save(service,data)
    assert exc.value.status_code==422


def test_empty_new_version_and_missing_version(vocabulary):
    service,_,_=vocabulary
    assert save(service,dict(expectedVersion=0,rules=[]))['version']==1
    with pytest.raises(SensitiveTermSetError) as exc:service.get_version(9)
    assert exc.value.status_code==404


@pytest.mark.parametrize('bad',['\ud800','\x01','\uffff'])
def test_note_rejects_non_utf8_or_xml_characters(vocabulary,bad):
    service,_,_=vocabulary
    data=payload()
    data['note']=bad
    with pytest.raises(SensitiveTermSetError) as exc:save(service,data)
    assert exc.value.status_code==422
    assert service.current()['version']==0


@pytest.mark.parametrize('field',['id','source','replacement'])
def test_bad_rule_unicode_fails_as_422(vocabulary,field):
    service,_,_=vocabulary
    data=payload()
    data['rules'][0][field]='\ud800'
    with pytest.raises(SensitiveTermSetError) as exc:save(service,data)
    assert exc.value.status_code==422
