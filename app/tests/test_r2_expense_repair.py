import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask import Flask


TEMPLATE = Path(__file__).parents[1] / 'templates/expense/records.html'


def _preview_controller(source):
    # Load the production controller plus its request/focus lifecycle dependencies.
    # The renderer remains stubbed below so URL and binary transport stay isolated.
    return source[source.index('var _previewRequestId ='):source.index('function renderPreviewContent(')]


@pytest.mark.parametrize('kind', ['invoice', 'payment'])
def test_empty_ocr_is_saved_as_manual_required(monkeypatch, kind):
    import app.routes.expense as routes

    class Files:
        @contextmanager
        def inspect_upload(self, *args):
            yield '/unused-ocr-input'

    class Service:
        def create_invoice_with_upload(self, *args, **kwargs):
            return 1, {'originalName': 'sample.png'}
        create_payment_with_upload = create_invoice_with_upload

    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.register_blueprint(routes.bp)
    app.extensions.update(file_service=Files(), expense_service=Service())
    monkeypatch.setattr(routes, 'recognize_file', lambda _: {'fields': {}, 'text': ''})
    monkeypatch.setattr(routes, 'recognize_payment', lambda _: {})
    monkeypatch.setattr(routes, 'match_invoices_and_payments', lambda: {'new_matches': []})
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user='test', user_id=1)
    response = client.post('/expense/api/upload', data={
        'type': kind, 'file': (io.BytesIO(b'fixture'), 'sample.png'),
    })
    assert response.status_code == 200
    assert response.json['success'] is True
    assert response.json['manual_required'] is True


@pytest.mark.parametrize('fail', [False, True])
def test_add_dialog_uses_attach_routes_and_keeps_failure_visible(fail):
    source = TEMPLATE.read_text()
    function = source.split('function confirmAddToReimb() {', 1)[1].split('\nfunction ', 1)[0]
    # Extract exactly the function, excluding the following section comment.
    function = 'function confirmAddToReimb() {' + function[:function.rfind('}') + 1]
    script = '''
const assert = require('node:assert/strict');
let calls = [], hidden = false, alerted = false;
let addSelectedInvoices = new Set([11]), addSelectedPayments = new Set([22]);
let addToReimbRid = 7, modalLoadedInvoices = [], expandedRid = null;
let document = {getElementById: () => ({}), querySelector: () => ({value:'采购报销'})};
let bootstrap = {Modal:{getInstance:()=>({hide:()=>hidden=true})}};
let alert = () => alerted=true, loadRecords=()=>{}, fetchDetail=()=>{};
let fetch = async (url, opts) => {
 if(opts.method === 'PUT') throw new Error('protected field request');
 calls.push([url, opts]);
 return {ok:!FAIL, json:async()=>({success:!FAIL,error:'关联被拒绝'})};
};
FUNCTION
confirmAddToReimb();
setImmediate(()=>{
 assert.equal(calls[0][0],'/expense/api/reimbursements/7/toggle_type');
 assert.equal(JSON.parse(calls[0][1].body).reimbursement_type,'采购报销');
 assert.equal(calls.some(([url,o])=>url.endsWith('/7/add_invoice/11') && o.method==='POST'),!FAIL);
 assert.equal(calls.some(([url,o])=>url.endsWith('/7/add_payment/22') && o.method==='POST'),!FAIL);
 assert.equal(hidden,!FAIL); assert.equal(alerted,FAIL);
});
'''.replace('FAIL', json.dumps(fail)).replace('FUNCTION', function)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_explicit_type_is_idempotent_and_legacy_toggle_preserved():
    from app.services.expenses import ExpenseService, ExpenseValidationError, ExpenseError
    class Repository:
        row = {'status': '草稿', 'reimbursement_type': '采购报销'}
        @property
        def engine(self): return self
        @contextmanager
        def begin(self): yield self
        def get_reimbursement(self, *args, **kwargs): return self.row.copy()
        def update_reimbursement(self, connection, rid, values): self.row.update(values)
    service = ExpenseService(Repository())
    assert service.toggle_type(1, reimbursement_type='出差报销') == '出差报销'
    assert service.toggle_type(1, reimbursement_type='出差报销') == '出差报销'
    assert service.toggle_type(1) == '采购报销'
    with pytest.raises(ExpenseValidationError):
        service.toggle_type(1, reimbursement_type='其他')
    assert service.repository.row['reimbursement_type'] == '采购报销'
    service.repository.row['status'] = '已确认'
    with pytest.raises(ExpenseError):
        service.toggle_type(1, reimbursement_type='出差报销')


def test_type_route_validates_payload_and_preserves_empty_legacy_request():
    from app.routes.expense import bp
    class Service:
        calls = []
        def toggle_type(self, rid, **fields):
            self.calls.append((rid, fields))
            return fields.get('reimbursement_type', '出差报销')
    service = Service()
    app = Flask(__name__)
    app.secret_key = 'r2-route-only'
    app.register_blueprint(bp)
    app.extensions['expense_service'] = service
    client = app.test_client()
    url = '/expense/api/reimbursements/7/toggle_type'
    assert client.post(url).status_code == 401
    with client.session_transaction() as session:
        session['user'] = 'test'
    for payload in ({}, [], {'reimbursement_type': None}, {'reimbursement_type': []},
                    {'reimbursement_type': '其他'}, {'reimbursement_type': '采购报销', 'status': '已确认'}):
        assert client.post(url, json=payload).status_code == 400
    assert client.post(url, data='{', content_type='application/json').status_code == 400
    assert client.post(url, data={'status': '已确认'}).status_code == 400
    assert client.post(url, data={'reimbursement_type': '其他'}, content_type='multipart/form-data').status_code == 400
    assert service.calls == []
    assert client.post(url, json={'reimbursement_type': '采购报销'}).json['success'] is True
    assert service.calls[-1] == (7, {'reimbursement_type': '采购报销'})
    assert client.post(url).json['success'] is True
    assert service.calls[-1] == (7, {})


def test_type_route_rejects_forms_already_read_by_csrf():
    from app.routes.expense import bp
    class Service:
        def toggle_type(self, *args, **kwargs):
            raise AssertionError('invalid body must not toggle')
    app = Flask(__name__)
    app.secret_key = 'r2-csrf-form-test'
    app.register_blueprint(bp)
    app.extensions['expense_service'] = Service()
    @app.before_request
    def consume_form():
        from flask import request
        request.form.get('csrf_token')
    client = app.test_client()
    with client.session_transaction() as session:
        session['user'] = 'test'
    url = '/expense/api/reimbursements/7/toggle_type'
    assert client.post(url, data={'status': '已确认'}).status_code == 400
    assert client.post(url, data={'status': '已确认'}, content_type='multipart/form-data').status_code == 400
    assert client.post(url, data={'file': (io.BytesIO(b'fixture'), 'fixture.txt')}).status_code == 400


def test_manual_upload_feedback_is_warning():
    source = TEMPLATE.read_text()
    assert 'manual_required: data.manual_required' in source
    assert 'manual_required: r.manual_required' in source
    assert '文件已保存，待手工补录' in source
    assert "results.some(r => r.manual_required)" in source


@pytest.mark.parametrize('target, allowed', [
    ('/preview/file?fileId=abc&versionNo=1&objectType=INVOICE&objectId=11', True),
    ('https://evil.example/preview/file?fileId=abc&versionNo=1&objectType=INVOICE&objectId=11', False),
    ('/etc/passwd', False),
    ('/preview/file?path=/etc/passwd', False),
])
def test_preview_uses_controlled_url_only(target, allowed):
    source = (TEMPLATE.parents[1] / 'components/preview_panel.html').read_text()
    function = _preview_controller(source)
    script = '''
const assert = require('node:assert/strict');
let calls=[], nodes={};
let window={location:{origin:'http://localhost'}};
let document={addEventListener:()=>{},getElementById:id=>nodes[id] ||= {style:{},setAttribute:()=>{},focus:()=>{},classList:{add:()=>{},remove:()=>{}}}};
let fetch=async url=>{calls.push(url); return {headers:{get:()=> 'application/json'},json:async()=>({success:false,error:{message:'受控附件不可用'}})}};
FUNCTION
openPreview(TARGET,'sample.png','image','');
setImmediate(()=>{
 assert.equal(calls.length,ALLOWED?1:0);
 if(ALLOWED) { assert.equal(calls[0],TARGET); assert.equal(nodes.previewPanelBody.textContent,'受控附件不可用'); }
});
'''.replace('FUNCTION', function).replace('TARGET', json.dumps(target)).replace('ALLOWED', json.dumps(allowed))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('media, kind', [('image/png', 'image'), ('application/pdf', 'pdf')])
def test_controlled_binary_preview(media, kind):
    source = (TEMPLATE.parents[1] / 'components/preview_panel.html').read_text()
    function = _preview_controller(source)
    script = '''
const assert=require('node:assert/strict');
let document={addEventListener:()=>{},getElementById:()=>({style:{},setAttribute:()=>{},focus:()=>{},classList:{add:()=>{},remove:()=>{}}})};
let window={location:{origin:'http://localhost'}}, rendered;
let renderPreviewContent=data=>rendered=data;
let fetch=async()=>({ok:true,url:'/api/files/fixture/preview',headers:{get:()=>MEDIA}});
FUNCTION
openPreview('/preview/file?fileId=abc&versionNo=1&objectType=INVOICE&objectId=11','sample','other','');
setImmediate(()=>assert.deepEqual(rendered,{success:true,type:KIND,url:'/api/files/fixture/preview'}));
'''.replace('FUNCTION', function).replace('MEDIA', json.dumps(media)).replace('KIND', json.dumps(kind))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
