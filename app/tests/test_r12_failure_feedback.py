from flask import Flask
from pathlib import Path
from jinja2 import FileSystemLoader
import pytest

from app.tests.test_file_service import engine, service, web_app, _business_client, _pdf


def correction_client(enabled=False):
    from app.routes.api import bp
    app=Flask(__name__)
    app.config.update(TESTING=True,SECRET_KEY='test',ENABLE_LLM=False,
                      AI_FEATURES_VISIBLE=True,
                      AI_PROVIDER='LOCAL' if enabled else 'DISABLED',
                      AUXILIARY_AI_ENABLED=True)
    app.register_blueprint(bp)
    client=app.test_client()
    with client.session_transaction() as session:
        session['user']='qa'
    return client


def test_preview_correction_disabled_never_loads_model(monkeypatch):
    import app.llm
    calls=[]
    monkeypatch.setattr(app.llm,'get_pool',lambda: calls.append(True))
    response=correction_client().post('/api/correct',json={'text':'演练研究计化'})
    assert response.status_code==503
    assert response.json['code']!=0
    assert response.json['msg']=='V1 未启用本地模型，请使用人工校对'
    assert calls==[] and 'data' not in response.json


def test_corrector_error_is_not_success_or_internal_leak(monkeypatch):
    import app.llm
    from app.llm.corrector import Corrector
    monkeypatch.setattr(app.llm,'get_pool',lambda: object())
    monkeypatch.setattr(Corrector,'correct',lambda *_:{'corrected':'原文','error':'/private/model-secret failed'})
    response=correction_client(True).post('/api/correct',json={'text':'原文'})
    assert response.status_code==503 and response.json['code']!=0
    assert '人工校对' in response.json['msg']
    assert '/private/' not in response.get_data(as_text=True)


def test_corrector_real_success_contract_is_preserved(monkeypatch):
    import app.llm
    from app.llm.corrector import Corrector
    monkeypatch.setattr(app.llm,'get_pool',lambda: object())
    outcome={'corrected':'演练研究计划','errors':[],'truncated':False}
    monkeypatch.setattr(Corrector,'correct',lambda *_:outcome)
    response=correction_client(True).post('/api/correct',json={'text':'演练研究计化'})
    assert response.status_code==200 and response.json=={'code':0,'data':outcome}


def test_corrector_exception_has_fixed_failure_message(monkeypatch):
    import app.llm
    def fail():
        raise RuntimeError('/private/model-secret')
    from app.llm.corrector import Corrector
    monkeypatch.setattr(Corrector,'correct',lambda *_: fail())
    response=correction_client(True).post('/api/correct',json={'text':'演练'})
    assert response.status_code==503 and response.json=={'code':2,'msg':'自动校对未完成，请使用人工校对'}


def test_controlled_path_rejection_negotiates_html_but_keeps_json_and_file_csp(web_app):
    web_app.jinja_loader=FileSystemLoader(Path(__file__).resolve().parents[1]/'templates')
    client=_business_client(web_app)
    path='/preview/file?path=/private/secret.txt&type=other'
    api=client.get(path,headers={'Accept':'application/json'})
    assert api.status_code==400 and api.json['error']['code']=='CONTROLLED_FILE_REFERENCE_REQUIRED'
    html=client.get(path,headers={'Accept':'text/html'})
    assert html.status_code==400 and html.mimetype=='text/html'
    assert '请从所属项目或资源页面重新打开附件' in html.get_data(as_text=True)
    assert '/private/secret.txt' not in html.get_data(as_text=True)
    assert client.get(path,headers={'Accept':'*/*'}).is_json
    uploaded=client.post('/api/files',data={'objectType':'EXPENSE','objectId':'7','file':(_pdf(b'original'),'original.pdf')}).json
    url=f"/api/files/{uploaded['fileId']}/versions/1/download?objectType=EXPENSE&objectId=7"
    download=client.get(url)
    assert download.data==b'%PDF-1.7\noriginal'
    assert download.headers['Content-Security-Policy']=="default-src 'none'; sandbox"
