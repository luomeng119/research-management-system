"""Isolated storage behavior for plain-text research report revisions."""
import importlib.util
from pathlib import Path
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.repositories.research_reports import ResearchReportsRepository
from app.services.research_reports import ResearchReportService, ResearchReportServiceError


class Audit:
    def __init__(self):
        self.events = []

    def record(self, connection, **event):
        self.events.append(event)


@pytest.fixture
def runtime(tmp_path):
    engine = sa.create_engine('sqlite:///' + str(tmp_path / 'reports.db'))
    sa.event.listen(engine, 'connect', lambda db, _: db.execute('PRAGMA foreign_keys=ON'))
    meta = sa.MetaData()
    users = sa.Table('users', meta, sa.Column('id', sa.BigInteger, primary_key=True), sa.Column('name',sa.Text), sa.Column('username',sa.Text))
    for name in ['proposals', 'project_registry']:
        sa.Table(name, meta, sa.Column('id', sa.String(36), primary_key=True), sa.Column('business_id', sa.Text, nullable=False))
    meta.create_all(engine)
    migration_path = Path(__file__).parents[2] / 'migrations/versions/0010_research_reports.py'
    spec = importlib.util.spec_from_file_location('report_migration', migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as conn:
        conn.execute(users.insert().values(id=1,name='演练作者',username='test_author'))
        for name, bid in [('proposals','TP-TEST'), ('project_registry','KY-TEST')]:
            conn.execute(meta.tables[name].insert().values(id=str(uuid.uuid4()),business_id=bid))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    audit = Audit()
    yield ResearchReportService(ResearchReportsRepository(engine), audit), engine, audit
    engine.dispose()


def create(service, **overrides):
    return service.create(dict(objectType='PROPOSAL', objectId='TP-TEST', title='科研报告', purpose='待确认用途', **overrides),actor_user_id=1,request_id='request-test')


def test_create_empty_draft_and_owner_list(runtime):
    service, _, audit = runtime
    report = create(service)
    assert report['currentVersion'] == 1
    assert service.current(report['id'])['body'] == ''
    assert service.current(report['id'])['createdByName'] == '演练作者'
    assert service.list_for_object('PROPOSAL','TP-TEST')['total'] == 1
    assert service.list_for_object('PROJECT','KY-TEST')['total'] == 0
    assert audit.events[0]['user_id'] == 1
    assert 'body' not in audit.events[0]['properties']


def test_versions_are_exact_and_old_revision_is_preserved(runtime):
    service, _, _ = runtime
    text = '  （科研）Ａ²\r\n第一段\n\n尾行\t '
    report = create(service,body=text)
    updated = service.save_version(report['id'],{'baseVersion':1,'body':text+'补充','note':'加入第二份材料'},actor_user_id=1,request_id='r2')
    assert updated['currentVersion'] == 2
    assert service.get_version(report['id'],1)['body'] == text
    assert service.current(report['id'])['body'] == text+'补充'
    assert [x['versionNo'] for x in service.history(report['id'])['items']] == [2,1]


def test_stale_version_cannot_overwrite_or_append(runtime):
    service, _, _ = runtime
    report = create(service)
    args = dict(actor_user_id=1,request_id='r')
    service.save_version(report['id'],dict(baseVersion=1,body='first'),**args)
    with pytest.raises(ResearchReportServiceError) as exc:
        service.save_version(report['id'],dict(baseVersion=1,body='stale'),**args)
    assert exc.value.status_code == 409
    assert service.current(report['id'])['body'] == 'first'
    assert service.history(report['id'])['total'] == 2


@pytest.mark.parametrize('payload',[{'baseVersion':True,'body':'a'},{'baseVersion':0,'body':'a'},{'baseVersion':1,'body':None},{'baseVersion':1,'body':'\x00'}, {'baseVersion':1,'body':'a'*200001}])
def test_invalid_save_is_rejected_without_new_version(runtime,payload):
    service, _, _ = runtime
    report = create(service)
    with pytest.raises(ResearchReportServiceError) as exc:
        service.save_version(report['id'],payload,actor_user_id=1,request_id='r')
    assert exc.value.status_code == 422
    assert service.history(report['id'])['total'] == 1


def test_missing_parent_and_report_are_clear_errors(runtime):
    service, _, _ = runtime
    with pytest.raises(ResearchReportServiceError) as exc:
        service.create(dict(objectType='PROJECT',objectId='missing',title='x',purpose=''),actor_user_id=1,request_id='r')
    assert exc.value.status_code == 404
    for identifier in ['bad', str(uuid.uuid4())]:
        with pytest.raises(ResearchReportServiceError) as exc:
            service.get(identifier)
        assert exc.value.status_code == 404


def test_project_parent_and_invalid_actor(runtime):
    service, _, _ = runtime
    report = service.create(dict(objectType='PROJECT',objectId='KY-TEST',title='项目报告',purpose=''),actor_user_id=1,request_id='r')
    assert report['objectType'] == 'PROJECT'
    for actor in [None, True, 0]:
        with pytest.raises(ResearchReportServiceError) as exc:
            service.save_version(report['id'],dict(baseVersion=1,body='x'),actor_user_id=actor,request_id='r')
        assert exc.value.status_code == 401


def test_migration_constraints_reject_no_parent_and_duplicate_versions(runtime):
    service, engine, _ = runtime
    report = create(service)
    repo = service.repository
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(repo.reports.update().values(proposal_id=None).where(repo.reports.c.id==repo.identifier(conn,report['id'])))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        row = dict(repo.get_version(conn,report['id'],1))
        row.pop('created_by_name')
        row['id'] = repo.identifier(conn,uuid.uuid4())
        conn.execute(repo.versions.insert().values(**row))


def test_database_rejects_revision_mutation(runtime):
    service, engine, _ = runtime
    report = create(service, body='原版')
    versions = service.repository.versions
    for statement in [versions.update().values(body='覆盖'), versions.delete()]:
        with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
            connection.execute(statement)
    assert service.current(report['id'])['body'] == '原版'


def test_concurrent_save_has_one_winner(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    service, _, _ = runtime
    report = create(service)
    barrier = Barrier(2)
    def save(body):
        barrier.wait(timeout=5)
        try:
            return service.save_version(report['id'],{'baseVersion':1,'body':body},actor_user_id=1,request_id=body)['currentVersion']
        except ResearchReportServiceError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save,['a','b']))
    assert sorted(results) == [2,409]
    assert service.history(report['id'])['total'] == 2


def test_audit_failure_rolls_back_both_pointer_and_revision(runtime):
    service, _, _ = runtime
    report = create(service)
    def fail(*args, **kwargs):
        raise RuntimeError('audit unavailable')
    service.audit_service.record = fail
    with pytest.raises(ResearchReportServiceError) as exc:
        service.save_version(report['id'],{'baseVersion':1,'body':'not committed'},actor_user_id=1,request_id='r')
    assert exc.value.status_code == 503
    assert exc.value.code == 'AUDIT_UNAVAILABLE'
    assert service.get(report['id'])['currentVersion'] == 1
    assert service.history(report['id'])['total'] == 1


@pytest.mark.parametrize('body',['\x01','\ud800','\ufffe'])
def test_export_incompatible_characters_rejected(runtime,body):
    service, _, _ = runtime
    with pytest.raises(ResearchReportServiceError) as exc:
        create(service,body=body)
    assert exc.value.status_code == 422
