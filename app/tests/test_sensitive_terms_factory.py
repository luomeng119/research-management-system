from app import create_app


def config(tmp_path):
    return dict(TESTING=True,SECRET_KEY='term-factory-test',DATABASE_ENGINE=None,AI_PROVIDER='DISABLED',SECURITY_AUTH_ENABLED=False,SESSION_FILE_DIR=str(tmp_path/'sessions'),FILE_STORAGE_ROOT=str(tmp_path/'files'))


def test_factory_injected_term_service(tmp_path):
    settings=config(tmp_path)
    service=object()
    settings['SENSITIVE_TERM_SET_SERVICE']=service
    app=create_app(settings)
    assert app.extensions['sensitive_term_set_service'] is service
    assert app.config['SENSITIVE_TERMS_AVAILABLE'] is True
    assert 'sensitive_terms.manage' in app.view_functions


def test_factory_without_tables_keeps_service_unavailable(tmp_path):
    app=create_app(config(tmp_path))
    assert app.config['SENSITIVE_TERMS_AVAILABLE'] is False
    assert 'sensitive_term_set_service' not in app.extensions


from app.tests.test_research_reports import runtime
import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migrate(engine,name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[2]/'migrations/versions'/name)
    migration=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection,Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()


def _local_config(tmp_path,runtime):
    settings=config(tmp_path)
    settings.update(DATABASE_ENGINE=runtime[1],AUDIT_SERVICE=runtime[2],FILE_SERVICE=object(),AI_PROVIDER='LOCAL',AI_FEATURES_VISIBLE=True,LOCAL_MODEL_BASE_URL='http://127.0.0.1:18081',LOCAL_MODEL_NAME='test-local',SENSITIVE_TERM_SET_SERVICE=object())
    return settings


def test_factory_injects_vocabulary_into_report_service(tmp_path,runtime):
    settings=_local_config(tmp_path,runtime)
    app=create_app(settings)
    assert app.extensions['research_report_service'].sensitive_term_service is settings['SENSITIVE_TERM_SET_SERVICE']


def test_factory_blocks_generation_without_snapshot_columns(tmp_path,runtime):
    _migrate(runtime[1],'0011_research_report_drafts.py')
    app=create_app(_local_config(tmp_path,runtime))
    assert app.config['RESEARCH_REPORTS_AVAILABLE'] is True
    assert app.config['RESEARCH_REPORT_GENERATION_AVAILABLE'] is False


def test_factory_enables_generation_only_with_snapshot_columns_and_vocabulary(tmp_path,runtime):
    _migrate(runtime[1],'0011_research_report_drafts.py')
    _migrate(runtime[1],'0013_report_redaction_snapshot.py')
    settings=_local_config(tmp_path,runtime)
    app=create_app(settings)
    assert app.config['RESEARCH_REPORT_GENERATION_AVAILABLE'] is True
    assert app.extensions['research_report_draft_service'].sensitive_term_service is settings['SENSITIVE_TERM_SET_SERVICE']
    settings.pop('SENSITIVE_TERM_SET_SERVICE')
    missing=create_app(settings)
    assert missing.config['RESEARCH_REPORT_GENERATION_AVAILABLE'] is False
