"""Report generation/adoption freeze vocabulary independently of later maintenance."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.tests.test_research_reports import runtime, create
from app.tests.test_research_report_drafts import drafts, generate
from app.repositories.research_reports import ResearchReportsRepository
from app.repositories.research_report_drafts import ResearchReportDraftsRepository
from app.services.research_reports import ResearchReportServiceError


class Vocabulary:
    def __init__(self):
        self.version = 1
        self.snapshots = {1: {'version': 1, 'rules': [dict(id='unit',source='保密单位',replacement='单位甲',enabled=True),dict(id='disabled',source='不替换',replacement='其他',enabled=False)]}}
        self.current_calls = 0
    def current(self):
        self.current_calls += 1
        return deepcopy(self.snapshots[self.version])
    def get_version(self, version):
        return deepcopy(self.snapshots[version])
    def advance(self):
        self.version = 2
        self.snapshots[2] = dict(version=2,rules=[dict(id='unit',source='保密单位',replacement='单位乙',enabled=True)])


@pytest.fixture
def redacted(drafts):
    service,reports,engine = drafts
    if 'redaction_snapshot' not in service.repository.table.c:
        path = Path(__file__).parents[2]/'migrations/versions/0013_report_redaction_snapshot.py'
        spec = importlib.util.spec_from_file_location('redaction_migration',path)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    reports.repository = ResearchReportsRepository(engine)
    service.repository = ResearchReportDraftsRepository(engine)
    vocab=Vocabulary()
    reports.sensitive_term_service=vocab
    service.sensitive_term_service=vocab
    return service,reports,engine,vocab


def test_generation_freezes_once_and_redacts_every_input_and_result(redacted):
    service,reports,engine,vocab=redacted
    reports.sensitive_term_service=None
    report=reports.create(dict(objectType='PROPOSAL',objectId='TP-TEST',title='保密单位报告',purpose='保密单位用途'),actor_user_id=1,request_id='r')
    reports.sensitive_term_service=vocab
    service.file_service.text='保密单位证据 不替换'
    original=service.file_service.open_version_stream
    def open_named(*args,**kwargs):
        result=original(*args,**kwargs);result['originalName']='保密单位资料.txt';return result
    service.file_service.open_version_stream=open_named
    seen={}
    def infer(**kwargs):
        seen.update(deepcopy(kwargs));vocab.advance()
        return dict(body='保密单位结果[S1]',model='local',promptVersion='p1',usage={})
    service.assistant.generate=infer
    payload_body='保密单位在编内容'
    calls=vocab.current_calls
    draft=generate(service,report,body=payload_body)
    assert vocab.current_calls == calls+1
    assert draft['body']=='单位甲结果[S1]'
    assert '保密单位' not in str(seen)
    assert seen['sources'][0]['filename']=='单位甲资料.txt'
    assert seen['sources'][0]['segments'][0]['text']=='单位甲证据 不替换'
    assert service.file_service.text=='保密单位证据 不替换'
    assert payload_body=='保密单位在编内容'
    assert draft['redactionSnapshot']['version']==1
    assert set(draft['redactionSnapshot'])=={'version','rulesHash','strategyVersion'}
    assert len(draft['redactionSnapshot']['rulesHash'])==64
    assert 'rules' not in draft['redactionSnapshot']
    with engine.connect() as conn:
        stored=service.repository.get(conn,report['id'],draft['id'])
        assert stored['redaction_snapshot']==draft['redactionSnapshot']


def test_accept_and_manual_edit_keep_old_mapping_after_vocabulary_changes(redacted):
    service,reports,_,vocab=redacted
    report=create(reports); draft=generate(service,report)
    vocab.advance()
    reports.save_version(report['id'],dict(baseVersion=1,body='保密单位采用',note='保密单位备注',draftId=draft['id']),actor_user_id=1,request_id='r')
    reports.save_version(report['id'],dict(baseVersion=2,body='保密单位继续'),actor_user_id=1,request_id='r')
    assert reports.get_version(report['id'],2)['body']=='单位甲采用'
    assert reports.get_version(report['id'],2)['note']=='单位甲备注'
    current=reports.current(report['id'])
    assert current['body']=='单位甲继续'
    assert current['redactionSnapshot']==draft['redactionSnapshot']


def test_missing_vocabulary_service_blocks_new_generation_without_model_call(drafts):
    service,reports,engine=drafts
    service.sensitive_term_service=None;reports.sensitive_term_service=None
    with pytest.raises(ResearchReportServiceError) as error:
        generate(service,create(reports))
    assert error.value.status_code==503
    assert not hasattr(service.assistant,'inputs')
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(service.repository.table))==0


def test_missing_frozen_version_never_advances_report(redacted):
    service,reports,engine,vocab=redacted
    report=create(reports);draft=generate(service,report)
    vocab.snapshots.clear()
    with pytest.raises(ResearchReportServiceError) as error:
        reports.save_version(report['id'],dict(baseVersion=1,body='保密单位',draftId=draft['id']),actor_user_id=1,request_id='r')
    assert error.value.status_code==503
    assert reports.get(report['id'])['currentVersion']==1


def test_legacy_null_is_not_claimed_redacted_and_edit_uses_current(redacted):
    service,reports,_,vocab=redacted
    reports.sensitive_term_service=None
    report=create(reports,body='保密单位原始历史')
    reports.sensitive_term_service=vocab
    old=reports.get_version(report['id'],1)
    assert old['redactionSnapshot'] is None and old['body']=='保密单位原始历史'
    reports.save_version(report['id'],dict(baseVersion=1,body='保密单位新版本'),actor_user_id=1,request_id='r')
    assert reports.current(report['id'])['body']=='单位甲新版本'
    assert reports.get_version(report['id'],1)==old


def test_rule_integrity_change_fails_closed(redacted):
    service,reports,_,vocab=redacted
    report=create(reports);draft=generate(service,report)
    vocab.snapshots[1]['rules'][0]['replacement']='篡改代称'
    with pytest.raises(ResearchReportServiceError) as error:
        reports.save_version(report['id'],dict(baseVersion=1,body='保密单位',draftId=draft['id']),actor_user_id=1,request_id='r')
    assert error.value.status_code==503
    assert reports.get(report['id'])['currentVersion']==1


def test_submitted_dictionary_version_keeps_selection_mapping_during_generation(redacted):
    service,reports,_,vocab=redacted
    report=create(reports)
    vocab.advance()
    draft=generate(service,report,body='保密单位选区已处理',dictionaryVersion=1)
    assert service.assistant.inputs['current_body']=='单位甲选区已处理'
    assert draft['redactionSnapshot']['version']==1


def test_fixed_report_rejects_different_submitted_dictionary_version(redacted):
    service,reports,_,vocab=redacted
    report=create(reports);vocab.advance()
    with pytest.raises(ResearchReportServiceError) as error:
        reports.save_version(report['id'],dict(baseVersion=1,body='保密单位',dictionaryVersion=2),actor_user_id=1,request_id='r')
    assert error.value.status_code==409
    assert reports.get(report['id'])['currentVersion']==1


def test_legacy_selection_can_save_explicit_old_dictionary(redacted):
    service,reports,_,vocab=redacted
    reports.sensitive_term_service=None
    report=create(reports)
    reports.sensitive_term_service=vocab;vocab.advance()
    reports.save_version(report['id'],dict(baseVersion=1,body='保密单位正文',dictionaryVersion=1),actor_user_id=1,request_id='r')
    assert reports.current(report['id'])['body']=='单位甲正文'
    assert reports.current(report['id'])['redactionSnapshot']['version']==1


def test_snapshot_column_is_append_only_and_downgrade_refuses_evidence(redacted):
    service,reports,engine,_=redacted
    report=create(reports);generate(service,report)
    for table in (reports.repository.versions,service.repository.table):
        with pytest.raises(sa.exc.DBAPIError),engine.begin() as conn:
            conn.execute(table.update().values(redaction_snapshot=None))
    path=Path(__file__).parents[2]/'migrations/versions/0013_report_redaction_snapshot.py'
    spec=importlib.util.spec_from_file_location('snapshot_down',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with pytest.raises(RuntimeError,match='contain data'),engine.begin() as conn,Operations.context(MigrationContext.configure(conn)):
        module.downgrade()


def test_replacement_expansion_fails_without_saving(redacted):
    service,reports,engine,vocab=redacted
    report=create(reports)
    vocab.snapshots[1]['rules'][0]['replacement']='扩'*500
    with pytest.raises(ResearchReportServiceError):
        generate(service,report,body='保密单位 '*30)
    assert not hasattr(service.assistant,'inputs')
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(service.repository.table))==0


def test_new_generation_upgrades_old_aliases_and_records_new_dictionary(redacted):
    service,reports,_,vocab=redacted
    report=create(reports,body='保密单位旧正文');vocab.advance()
    draft=generate(service,report,body='单位甲已有代称，保密单位新内容',dictionaryVersion=2,inputDictionaryVersion=1)
    assert draft['redactionSnapshot']['version']==2
    assert service.assistant.inputs['current_body']=='单位乙已有代称，单位乙新内容'
    reports.save_version(report['id'],dict(baseVersion=1,body=draft['body'],draftId=draft['id'],dictionaryVersion=2),actor_user_id=1,request_id='r')
    assert reports.current(report['id'])['redactionSnapshot']['version']==2
    assert reports.get_version(report['id'],1)['body']=='单位甲旧正文'
    assert reports.get_version(report['id'],1)['redactionSnapshot']['version']==1


def test_generation_without_target_form_version_uses_current_dictionary(redacted):
    service,reports,_,vocab=redacted
    report=create(reports);vocab.advance()
    draft=generate(service,report,body='单位甲待整理')
    assert draft['redactionSnapshot']['version']==2
    assert service.assistant.inputs['current_body']=='单位乙待整理'


def test_ambiguous_old_alias_upgrade_rejects_without_mutating_input(redacted):
    service,reports,engine,vocab=redacted
    vocab.snapshots[1]['rules'].append(dict(id='second',source='另一单位',replacement='单位甲',enabled=True))
    report=create(reports);vocab.advance()
    vocab.snapshots[2]['rules'].append(dict(id='second',source='另一单位',replacement='单位丙',enabled=True))
    original='单位甲旧正文'
    with pytest.raises(ResearchReportServiceError) as error:
        generate(service,report,body=original,dictionaryVersion=2)
    assert error.value.code=='REDACTION_UPGRADE_AMBIGUOUS'
    assert original=='单位甲旧正文'
    assert not hasattr(service.assistant,'inputs')
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(service.repository.table))==0
    # Clearing the ambiguous old body permits generation from original materials.
    draft=generate(service,report,body='',dictionaryVersion=2)
    assert draft['redactionSnapshot']['version']==2


def test_changed_rule_identity_cannot_reinterpret_old_alias(redacted):
    service,reports,_,vocab=redacted
    report=create(reports);vocab.advance()
    vocab.snapshots[2]['rules'][0]['source']='不同单位'
    with pytest.raises(ResearchReportServiceError) as error:
        generate(service,report,body='单位甲旧正文',dictionaryVersion=2)
    assert error.value.status_code==422


def test_upgraded_draft_title_purpose_export_and_next_generation_keep_mapping(redacted):
    service,reports,_,vocab=redacted
    report=reports.create(dict(objectType='PROPOSAL',objectId='TP-TEST',title='保密单位报告',purpose='保密单位用途'),actor_user_id=1,request_id='r')
    vocab.advance()
    draft=generate(service,report,body='单位甲正文',dictionaryVersion=2)
    assert draft['inputTitle']=='单位乙报告' and draft['inputPurpose']=='单位乙用途'
    reports.save_version(report['id'],dict(baseVersion=1,body=draft['body'],draftId=draft['id']),actor_user_id=1,request_id='r')
    assert reports.current(report['id'])['title']=='单位乙报告'
    assert reports.current(report['id'])['purpose']=='单位乙用途'
    assert reports.get_version(report['id'],1)['title']=='单位甲报告'
    vocab.snapshots[3]=dict(version=3,rules=[dict(id='unit',source='保密单位',replacement='单位丙',enabled=True)])
    vocab.version=3
    third=generate(service,report,baseVersion=2,body='单位乙正文',dictionaryVersion=3)
    assert third['inputTitle']=='单位丙报告'
    assert third['inputPurpose']=='单位丙用途'


def test_same_old_alias_self_overlap_is_rejected(redacted):
    service,reports,engine,vocab=redacted
    vocab.snapshots[1]['rules'][0]['replacement']='哈哈'
    report=create(reports)
    vocab.advance()
    with pytest.raises(ResearchReportServiceError) as error:
        generate(service,report,body='哈哈哈',dictionaryVersion=2)
    assert error.value.code=='REDACTION_UPGRADE_AMBIGUOUS'
    assert not hasattr(service.assistant,'inputs')
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(service.repository.table))==0
    assert generate(service,report,body='',dictionaryVersion=2)['redactionSnapshot']['version']==2


def test_author_display_uses_frozen_dictionary_without_changing_identity(redacted):
    _, reports, engine, vocabulary = redacted
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE users SET name='保密单位作者' WHERE id=1"))
    report = create(reports)
    vocabulary.advance()
    for version in (reports.current(report['id']), reports.get_version(report['id'],1), reports.history(report['id'])['items'][0]):
        assert version['createdByName'] == '单位甲作者'
        assert version['createdBy'] == 1
    with engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT name FROM users WHERE id=1')) == '保密单位作者'
    newer = create(reports)
    assert reports.current(newer['id'])['createdByName'] == '单位乙作者'
