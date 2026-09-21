from contextlib import contextmanager
import pytest
from app.services.local_model_runtime import RuntimeAssistant, RuntimeUnavailable, runtime_status

class Controller:
    def __init__(self): self.model='first'; self.held=False
    def model_status(self): return {'state':'ready','identityVerified':True,'model':self.model,'endpoint':'http://127.0.0.1:18081'}
    @contextmanager
    def inference_lease(self):
        self.held=True
        try: yield self.model_status()
        finally: self.held=False

class Assistant:
    def __init__(self,base_url,model): self.model=model
    def generate(self):
        assert ctl.held
        return self.model
    @property
    def last_metadata(self): return {'model':self.model}

ctl=Controller()

def test_dynamic_assistant_uses_current_model_and_releases_lease():
    provider=RuntimeAssistant(Assistant,{'LOCAL_MODEL_CONTROLLER':ctl})
    ctl.model='first'
    assert provider.generate()=='first'
    ctl.model='second'
    assert provider.generate()=='second'
    assert provider.last_metadata=={'model':'second'} and not ctl.held


def test_required_image_capability_is_checked_before_provider_call(monkeypatch):
    provider=RuntimeAssistant(Assistant,{'LOCAL_MODEL_CONTROLLER':ctl},required_capability='image')
    @contextmanager
    def text_only():
        yield {'state':'ready','identityVerified':True,'model':'text-only',
               'endpoint':'http://127.0.0.1:18081','capabilities':['text']}
    monkeypatch.setattr(ctl,'inference_lease',text_only)
    with pytest.raises(RuntimeUnavailable):
        provider.generate()


def test_missing_controller_is_unavailable():
    with pytest.raises(RuntimeUnavailable): runtime_status({})

from app.tests.test_local_model_status import client


def managed_client(monkeypatch, role='SYSTEM_MAINTAINER'):
    c=client(role)
    c.application.config['LOCAL_MODEL_CONTROLLER']=ctl
    c.application.add_url_rule('/auth/login', endpoint='auth.login', view_func=lambda: 'login')
    monkeypatch.setattr(ctl, 'model_status', lambda: {'state':'ready','profiles':[{'id':'test'}],'model':'test'})
    return c


def test_operations_reject_unknown_inputs_and_roles(monkeypatch):
    c=managed_client(monkeypatch)
    calls=[]
    monkeypatch.setattr(ctl,'model_operation',lambda *args: calls.append(args) or {'state':'unloaded'},raising=False)
    for data in ({'profileId':'test','command':'bad'}, {'profileId':'../../bad'}, {}, []):
        assert c.post('/utils/api/local-model/load',json=data).status_code==422
    assert not calls
    assert c.post('/utils/api/local-model/load',json={'profileId':'test'}).status_code==200
    assert calls==[('load','test')]
    assert c.post('/utils/api/local-model/unload',json={}).status_code==200
    assert managed_client(monkeypatch,'BUSINESS_USER').post('/utils/api/local-model/unload',json={}).status_code==403


def test_operations_require_csrf(monkeypatch):
    from app.security.csrf import protect_request
    c=managed_client(monkeypatch)
    c.application.config['CSRF_ENABLED']=True
    c.application.before_request(protect_request)
    assert c.post('/utils/api/local-model/unload',json={}).status_code==403


def test_busy_is_not_success_and_error_is_redacted(monkeypatch):
    c=managed_client(monkeypatch)
    def busy(*args): raise BlockingIOError('/private/secret')
    monkeypatch.setattr(ctl,'model_operation',busy,raising=False)
    response=c.post('/utils/api/local-model/unload',json={})
    assert response.status_code==409 and not response.json['success']
    assert '/private' not in response.text


def test_inference_failure_releases_lock_and_clears_metadata():
    class Failing(Assistant):
        def generate(self): raise RuntimeError('generation failed')
    provider=RuntimeAssistant(Failing,{'LOCAL_MODEL_CONTROLLER':ctl})
    with pytest.raises(RuntimeError): provider.generate()
    assert not ctl.held and provider.last_metadata=={}


def test_unloaded_model_cannot_make_request(monkeypatch):
    @contextmanager
    def unloaded(): yield {'state':'unloaded','model':None,'endpoint':None}
    monkeypatch.setattr(ctl, 'inference_lease', unloaded)
    provider=RuntimeAssistant(Assistant,{'LOCAL_MODEL_CONTROLLER':ctl})
    with pytest.raises(RuntimeUnavailable): provider.generate()


def test_corrector_resolves_loaded_model_under_lease():
    from app.llm.corrector import Corrector
    seen=[]
    def transport(**request):
        assert ctl.held
        seen.append(request['payload']['model'])
        return {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"corrected":"测试"}'}}]}
    ctl.model='new-model'
    corrector=Corrector(config={'AI_PROVIDER':'LOCAL','LOCAL_MODEL_CONTROLLER':ctl},transport=transport)
    assert corrector.correct('测试')['corrected']=='测试'
    assert seen==['new-model'] and not ctl.held


def test_loading_is_never_green(monkeypatch):
    c=managed_client(monkeypatch)
    monkeypatch.setattr(ctl, 'model_status', lambda: {'state':'loading','model':'test'})
    status=c.get('/utils/api/monitor/status').json
    assert status['server_status']=='unavailable' and status['model_loaded'] is None


def test_public_status_hides_internal_details_and_requires_identity(monkeypatch):
    c=managed_client(monkeypatch)
    monkeypatch.setattr(ctl,'model_status',lambda: {
        'state':'ready','model':'test','endpoint':'http://127.0.0.1:18081',
        'profileId':'test','identityVerified':False,'argv':['/private/secret'],
        'error_message':'/private/secret', 'errorCode':'/private/code',
        'profiles':[{'id':'test','name':'Test','available':True,'path':'/private/model'}],
    })
    response=c.get('/utils/api/monitor/status')
    assert response.json['state']=='failed'
    assert response.json['model_loaded'] is None
    assert '/private' not in response.text


def test_anonymous_cannot_read_or_operate_models(monkeypatch):
    c=managed_client(monkeypatch)
    with c.session_transaction() as session: session.clear()
    for path in ('/utils/api/monitor/status','/utils/api/local-model/unload'):
        response=c.get(path) if path.endswith('status') else c.post(path,json={})
        assert response.status_code in (302,401)


def test_valid_ready_contract_and_memory_sources(monkeypatch):
    c=managed_client(monkeypatch)
    monkeypatch.setattr(ctl,'model_status',lambda: {
        'state':'ready','model':'Qwen3.5-9B','profileId':'qwen3.5-9b-q8',
        'endpoint':'http://127.0.0.1:18081','identityVerified':True,
        'profiles':[{'id':'qwen3.5-9b-q8','name':'Qwen3.5-9B Q8','expectedModel':'Qwen3.5-9B','available':True}],
        'processMemoryMiB':1234,'processMemorySource':'ps_rss',
        'systemMemory':{'totalMiB':49152,'availableMiB':30000,'usedMiB':19152,'source':'psutil_virtual_memory'},
    })
    result=c.get('/utils/api/monitor/status').json
    assert result['success'] and result['server_status']=='running'
    assert result['loadedProfileId']=='qwen3.5-9b-q8'
    assert result['processMemoryMiB']==1234
    assert result['profiles'][0]['available'] is True
    assert result['systemMemory']['source']=='psutil_virtual_memory'


def test_csrf_valid_token_reaches_control(monkeypatch):
    from app.security.csrf import protect_request
    c=managed_client(monkeypatch)
    c.application.config['CSRF_ENABLED']=True
    c.application.before_request(protect_request)
    with c.session_transaction() as session: session['csrf_token']='valid-token'
    monkeypatch.setattr(ctl,'model_operation',lambda *args: {'state':'unloaded'},raising=False)
    assert c.post('/utils/api/local-model/unload',json={},headers={'X-CSRF-Token':'valid-token'}).status_code==200


def test_managed_summary_keeps_prompt_and_output_limit():
    from app.llm.corrector import LocalSummarizer
    seen=[]
    def transport(**request):
        seen.append(request['payload']['messages'][0]['content'])
        return {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"summary":"过长的摘要"}'}}]}
    summarizer=LocalSummarizer(config={'AI_PROVIDER':'LOCAL','LOCAL_MODEL_CONTROLLER':ctl},transport=transport)
    assert 'error' in summarizer.summarize('原始资料',max_length=2)
    assert len(seen)==1 and '摘要' in seen[0] and '2' in seen[0]


def test_corrector_identity_failure_never_calls_transport(monkeypatch):
    from app.llm.corrector import Corrector
    @contextmanager
    def invalid(): yield {'state':'ready','identityVerified':False,'model':'bad','endpoint':'http://127.0.0.1:18081'}
    monkeypatch.setattr(ctl,'inference_lease',invalid)
    seen=[]
    result=Corrector(config={'AI_PROVIDER':'LOCAL','LOCAL_MODEL_CONTROLLER':ctl},transport=lambda **kw:seen.append(kw)).correct('原文')
    assert 'error' in result and not seen


@pytest.mark.parametrize('available,expected', [(False,'Qwen3.5-9B'),(True,'other-model')])
def test_public_ready_rejects_unavailable_or_mismatched_profile(available,expected):
    from app.services.local_model_runtime import public_status
    result=public_status({'state':'ready','identityVerified':True,'model':'Qwen3.5-9B',
                         'endpoint':'http://127.0.0.1:18081','profileId':'qwen3.5-9b-q8',
                         'profiles':[{'id':'qwen3.5-9b-q8','name':'Qwen3.5-9B Q8',
                                      'available':available,'expectedModel':expected}]})
    assert result['state']=='failed' and result['loadedProfileId'] is None


def test_status_exception_returns_stable_redacted_contract(monkeypatch):
    c=managed_client(monkeypatch)
    def fail(): raise RuntimeError('/private/secret --command')
    monkeypatch.setattr(ctl,'model_status',fail)
    response=c.get('/utils/api/monitor/status')
    assert response.status_code==200 and response.json['success'] is False
    assert response.json['profiles']==[] and response.json['processMemoryMiB'] is None
    assert response.json['systemMemory']['totalMiB'] is None
    assert '/private' not in response.text and '--command' not in response.text
