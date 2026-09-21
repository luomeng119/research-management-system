from flask import Flask
from app.routes.utils import bp
import app.routes.utils as utils


def client(role='SYSTEM_MAINTAINER', provider='LOCAL'):
    app=Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='fixture', AI_PROVIDER=provider, ENABLE_LLM=False,
                      AI_FEATURES_VISIBLE=True,
                      LOCAL_MODEL_BASE_URL='http://127.0.0.1:18081',LOCAL_MODEL_NAME='Qwen3.5-9B')
    app.register_blueprint(bp)
    c=app.test_client()
    with c.session_transaction() as s:
        s.update(user_id=7,user='qa',name='QA',role=role,account_version=1)
    return c


def test_local_status_reads_real_model_contract_not_legacy_stats(monkeypatch):
    calls=[]
    def read(url):
        calls.append(url)
        return {'status':'ok'} if url.endswith('/health') else {'data':[{'id':'Qwen3.5-9B'}]}
    monkeypatch.setattr(utils,'_local_get_json',read,raising=False)
    r=client().get('/utils/api/monitor/status')
    assert r.status_code==200 and r.json['server_status']=='running'
    assert r.json['model_loaded']=='Qwen3.5-9B'
    assert r.json['total_requests'] is None and r.json['avg_latency_ms'] is None
    assert calls==['http://127.0.0.1:18081/health','http://127.0.0.1:18081/v1/models']


def test_local_status_outage_and_permissions(monkeypatch):
    def fail(url): raise OSError('/private/detail')
    monkeypatch.setattr(utils,'_local_get_json',fail,raising=False)
    r=client().get('/utils/api/monitor/status')
    assert r.status_code==200 and r.json['server_status']=='unavailable'
    assert '/private' not in r.text
    assert client('BUSINESS_USER').get('/utils/api/monitor/status').status_code==403
    assert client(provider='DISABLED').get('/utils/api/monitor/status').status_code==404


def test_local_restart_and_crud_never_invoke_legacy_process(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('legacy process must not run')))
    c=client()
    for path in ['/utils/api/monitor/restart','/utils/api/models','/utils/api/models/activate/1']:
        r=c.post(path,json={})
        assert r.status_code==409 and r.json['success'] is False
    assert client('BUSINESS_USER').post('/utils/api/monitor/restart').status_code==403


def test_status_transport_ignores_proxies_and_rejects_redirects(monkeypatch):
    import threading
    import pytest
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    paths=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path=='/v1/models':
                self.send_response(302);self.send_header('Location','/health');self.end_headers();return
            self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    for name in ['HTTP_PROXY','http_proxy','ALL_PROXY','all_proxy']:
        monkeypatch.setenv(name,'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY','');monkeypatch.setenv('no_proxy','')
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        assert utils._local_get_json(base+'/health')=={'status':'ok'}
        with pytest.raises(Exception): utils._local_get_json(base+'/v1/models')
        assert paths==['/health','/v1/models']
        with pytest.raises(ValueError): utils._local_get_json('http://example.com:80/health')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
