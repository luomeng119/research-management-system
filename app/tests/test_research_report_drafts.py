"""Draft snapshots stay separate from accepted report revisions."""
import hashlib
import importlib.util
import io
from pathlib import Path
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.tests.test_research_reports import runtime, create
from app.repositories.research_reports import ResearchReportsRepository
from app.repositories.research_report_drafts import ResearchReportDraftsRepository
from app.services.research_report_drafts import ResearchReportDraftService
from app.services.research_reports import ResearchReportServiceError


class Files:
    def __init__(self):
        self.calls = []
        self.text = '来源原文（不可变）'
    def open_version_stream(self, file_id, version_no, **owner):
        self.calls.append((file_id,version_no,owner))
        data = self.text.encode()
        return dict(stream=io.BytesIO(data),originalName='资料.txt',sha256=hashlib.sha256(data).hexdigest(),sizeBytes=len(data),mediaType='text/plain')


def extract(opened):
    text = opened['stream'].read().decode()
    return dict(text=text,segments=[dict(locator='段落1',text=text)],extractionVersion='test-v1',textSha256=hashlib.sha256(text.encode()).hexdigest())


class Assistant:
    def generate(self, **inputs):
        self.inputs = inputs
        return dict(body='整合初稿[S1]',model='test-local',promptVersion='report-v1',usage={})


@pytest.fixture
def drafts(runtime):
    reports,engine,audit = runtime
    path = Path(__file__).parents[2]/'migrations/versions/0011_research_report_drafts.py'
    spec = importlib.util.spec_from_file_location('draft_migration',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        module.upgrade()
    snapshot_path = Path(__file__).parents[2]/'migrations/versions/0013_report_redaction_snapshot.py'
    snapshot_spec = importlib.util.spec_from_file_location('redaction_migration',snapshot_path)
    snapshot_module = importlib.util.module_from_spec(snapshot_spec)
    snapshot_spec.loader.exec_module(snapshot_module)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        snapshot_module.upgrade()
    class EmptyVocabulary:
        def current(self): return dict(version=0,rules=[])
        def get_version(self, version):
            if version != 0: raise ValueError('missing version')
            return self.current()
    reports.sensitive_term_service = EmptyVocabulary()
    reports.repository = ResearchReportsRepository(engine)
    service = ResearchReportDraftService(ResearchReportDraftsRepository(engine),reports,Files(),Assistant(),extract)
    return service,reports,engine


def generate(service,report,**overrides):
    payload = dict(baseVersion=1,body='人工在编内容',selections=[dict(fileId=str(uuid.uuid4()),versionNo=1)])
    payload.update(overrides)
    return service.generate(report['id'],payload,actor_user_id=1,request_id='draft-test')


def test_draft_generation_does_not_save_report_and_accept_preserves_snapshot(drafts):
    service,reports,_ = drafts
    report = create(reports)
    draft = generate(service,report)
    assert reports.get(report['id'])['currentVersion'] == 1
    assert draft['body'] == '整合初稿[S1]'
    assert service.file_service.calls[0][2] == dict(object_type='PROPOSAL',object_id='TP-TEST')
    service.file_service.text = '后来修改的来源'
    reports.save_version(report['id'],dict(baseVersion=1,body='人工修改正文',draftId=draft['id']),actor_user_id=1,request_id='accept')
    current = reports.current(report['id'])
    assert current['body'] == '人工修改正文'
    assert current['sources'][0]['text'] == '来源原文（不可变）'
    assert current['draftId'] == draft['id']
    assert reports.get_version(report['id'],1)['body'] == ''


def test_image_source_is_redacted_before_report_model_and_blocks_original_embedding(drafts):
    service,reports,_=drafts
    class Vocabulary:
        value=dict(version=1,rules=[
            dict(id='project',source='青岚-07',replacement='PRJ-B',enabled=True),
            dict(id='mark',source='内部试验资料',replacement='※',enabled=True),
        ])
        def current(self): return self.value
        def get_version(self,version):
            if version!=1: raise ValueError('missing')
            return self.value
    vocabulary=Vocabulary(); service.sensitive_term_service=vocabulary; reports.sensitive_term_service=vocabulary
    service.file_service.text='内部试验资料：青岚-07，样本24个'
    def image_extract(opened):
        text=opened['stream'].read().decode()
        return dict(text=text,segments=[dict(locator='图片摘要',text=text)],sourceKind='IMAGE',
                    extractionVersion='image-v1',textSha256=hashlib.sha256(text.encode()).hexdigest(),
                    image=dict(width=64,height=48,mediaType='image/png'))
    service.source_extractor=image_extract
    draft=generate(service,create(reports))
    source=draft['sources'][0]
    assert '青岚-07' not in source['text'] and '内部试验资料' not in source['text']
    assert 'PRJ-B' in source['text'] and '※' in source['text']
    assert source['originalImageBlockedByRedaction'] is True
    assert '青岚-07' not in service.assistant.inputs['sources'][0]['text']


def test_draft_wrong_report_and_stale_base_rejected(drafts):
    service,reports,_ = drafts
    first,second = create(reports),create(reports)
    draft = generate(service,first)
    with pytest.raises(ResearchReportServiceError):
        service.get(second['id'],draft['id'])
    with pytest.raises(ResearchReportServiceError):
        reports.save_version(second['id'],dict(baseVersion=1,body='x',draftId=draft['id']),actor_user_id=1,request_id='r')
    reports.save_version(first['id'],dict(baseVersion=1,body='manual'),actor_user_id=1,request_id='r')
    with pytest.raises(ResearchReportServiceError) as exc:
        reports.save_version(first['id'],dict(baseVersion=2,body='stale draft',draftId=draft['id']),actor_user_id=1,request_id='r')
    assert exc.value.status_code == 409


def test_model_failure_no_draft_or_report_write(drafts):
    service,reports,engine = drafts
    report = create(reports)
    def fail(**kwargs):
        raise RuntimeError('model failed')
    service.assistant.generate = fail
    with pytest.raises(ResearchReportServiceError) as exc:
        generate(service,report)
    assert exc.value.status_code == 503
    assert reports.get(report['id'])['currentVersion'] == 1
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(service.repository.table)) == 0


def test_concurrent_edit_during_generation_rejects_stale_draft(drafts):
    service,reports,_ = drafts
    report = create(reports)
    def generate_then_edit(**kwargs):
        reports.save_version(report['id'],dict(baseVersion=1,body='concurrent'),actor_user_id=1,request_id='other')
        return dict(body='draft',model='test',promptVersion='v1',usage={})
    service.assistant.generate = generate_then_edit
    with pytest.raises(ResearchReportServiceError) as exc:
        generate(service,report)
    assert exc.value.status_code == 409


def test_draft_database_immutable(drafts):
    service,reports,engine = drafts
    generate(service,create(reports))
    for statement in [service.repository.table.update().values(generated_body='覆盖'),service.repository.table.delete()]:
        with pytest.raises(sa.exc.DBAPIError), engine.begin() as conn:
            conn.execute(statement)


@pytest.mark.parametrize('selections',[[],[dict(fileId='x',versionNo=True)], [dict(fileId='x',versionNo=1)]*2])
def test_invalid_selections(drafts,selections):
    service,reports,_=drafts
    with pytest.raises(ResearchReportServiceError) as exc:
        generate(service,create(reports),selections=selections)
    assert exc.value.status_code == 422


def test_manual_edit_inherits_source_snapshot(drafts):
    service,reports,_=drafts
    report=create(reports)
    draft=generate(service,report)
    reports.save_version(report['id'],dict(baseVersion=1,body='采用',draftId=draft['id']),actor_user_id=1,request_id='r')
    reports.save_version(report['id'],dict(baseVersion=2,body='继续人工完善'),actor_user_id=1,request_id='r')
    assert reports.current(report['id'])['draftId']==draft['id']
    assert reports.current(report['id'])['sources']==draft['sources']


def test_draft_audit_failure_rolls_back(drafts):
    service,reports,engine=drafts
    report=create(reports)
    def fail(*args,**kwargs):
        raise RuntimeError('audit failed')
    reports.audit_service.record=fail
    with pytest.raises(ResearchReportServiceError) as exc:
        generate(service,report)
    assert exc.value.status_code==503
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(service.repository.table))==0


def test_file_error_is_safe_and_never_calls_model(drafts):
    from app.services.files import FileServiceError
    service,reports,_=drafts
    report=create(reports)
    def fail(*args,**kwargs):
        raise FileServiceError('FILE_INTEGRITY_FAILED','文件完整性校验失败',409)
    service.file_service.open_version_stream=fail
    with pytest.raises(ResearchReportServiceError) as exc:
        generate(service,report)
    assert exc.value.status_code==409
    assert not hasattr(service.assistant,'inputs')


def test_model_token_budget_error_is_422(drafts):
    service,reports,_=drafts
    def fail(**kwargs):
        raise ValueError('输入超过本地模型容量，未截断')
    service.assistant.generate=fail
    with pytest.raises(ResearchReportServiceError) as exc:
        generate(service,create(reports))
    assert exc.value.status_code==422


def test_database_rejects_cross_report_draft_link(drafts):
    service,reports,engine=drafts
    first,second=create(reports),create(reports)
    draft=generate(service,first)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        row=dict(reports.repository.get_version(connection,second['id'],1))
        row.pop('created_by_name')
        row.update(id=str(uuid.uuid4()),version_no=2,draft_id=draft['id'])
        connection.execute(reports.repository.versions.insert().values(**row))


def test_snapshot_filename_rejects_xml_noncharacters(drafts):
    service,reports,_=drafts
    report=create(reports)
    real_open=service.file_service.open_version_stream
    opened_list=[]
    def bad_name(*args,**kwargs):
        opened=real_open(*args,**kwargs)
        opened['originalName']='资料\ufffe.txt'
        opened_list.append(opened)
        return opened
    service.file_service.open_version_stream=bad_name
    with pytest.raises(ResearchReportServiceError) as exc:
        generate(service,report)
    assert exc.value.status_code==422
    assert opened_list[0]['stream'].closed
    assert not hasattr(service.assistant,'inputs')


@pytest.mark.parametrize('old_body,order,expected', [
    ('方案[来源 S1]', [3,1,2,0], '方案[来源S4]'),
    ('方案（S1）', [0,1,2,3], '方案（S1）'),
    ('方案(S1,S2)', [1,0,2,3], '方案[来源S2][来源S1]'),
    ('方案（S1-S3）', [2,0,1,3], '方案[来源S2][来源S3][来源S1]'),
])
def test_revision_remaps_frozen_file_version_citations(drafts,old_body,order,expected):
    service,reports,_=drafts
    report=create(reports)
    selections=[dict(fileId=str(uuid.uuid4()),versionNo=1) for _ in range(4)]
    first=generate(service,report,selections=selections)
    reports.save_version(report['id'],dict(baseVersion=1,body=old_body,draftId=first['id']),actor_user_id=1,request_id='accept')
    captured={}
    def infer(**kwargs):
        captured.update(kwargs)
        return dict(body='新版[来源S1]',model='test',promptVersion='test')
    service.assistant.generate=infer
    second=generate(service,report,baseVersion=2,body=old_body,selections=[selections[i] for i in order])
    assert captured['current_body']==expected
    assert second['inputBody']==expected
    assert reports.get_version(report['id'],2)['body']==old_body
    assert service.get(report['id'],first['id'])['sources'][0]['fileId']==selections[0]['fileId']


def test_revision_omitted_or_changed_file_version_is_not_new_source_one(drafts):
    service,reports,_=drafts
    report=create(reports)
    selected=dict(fileId=str(uuid.uuid4()),versionNo=1)
    first=generate(service,report,selections=[selected])
    body='旧事实[来源S1]'
    reports.save_version(report['id'],dict(baseVersion=1,body=body,draftId=first['id']),actor_user_id=1,request_id='accept')
    captured={}
    def infer(**kwargs):
        captured.update(kwargs)
        return dict(body='新版[来源S1]',model='test',promptVersion='test')
    service.assistant.generate=infer
    second=generate(service,report,baseVersion=2,body=body,selections=[dict(selected,versionNo=2)])
    assert '[来源S1]' not in captured['current_body']
    assert '旧来源未纳入' in captured['current_body'] and '不可作为本次证据' in captured['current_body']
    assert second['inputBody']==captured['current_body']
    assert reports.current(report['id'])['body']==body


@pytest.mark.parametrize('body', ['（S1[来源S2]）','（S2-S1）','[来源S9]','（S1-S3）'])
def test_source_remap_rejects_untraceable_or_malformed_references(body):
    from app.services.research_report_drafts import _remap_source_citations
    old=[dict(sourceId='S1',fileId='a',versionNo=1),dict(sourceId='S2',fileId='b',versionNo=1)]
    with pytest.raises(ResearchReportServiceError): _remap_source_citations(body,old,old)


def test_source_remap_without_prior_snapshot_preserves_existing_behavior():
    from app.services.research_report_drafts import _remap_source_citations
    assert _remap_source_citations('手工稿[来源S1]',None,[])=='手工稿[来源S1]'
