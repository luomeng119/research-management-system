import pytest
from flask import request

from app.tests.test_auth_audit import app, engine, repositories, _insert_user, _login


@pytest.mark.parametrize('path,as_json', [('/qa-upload', False), ('/api/qa-upload', True), ('/expense/api/qa-upload', True)])
def test_oversized_authenticated_upload_is_localized_and_never_reaches_handler(app, engine, path, as_json):
    reached = []
    def upload():
        reached.append(True)
        return str(len(request.get_data()))
    app.add_url_rule(path, endpoint='qa_upload', view_func=upload, methods=['POST'])
    _insert_user(engine)
    client = app.test_client()
    assert _login(client).status_code == 302
    app.config['MAX_CONTENT_LENGTH'] = 1024
    response = client.post(path, data={'payload': 'x' * 2048})
    assert response.status_code == 413
    assert reached == []
    if as_json:
        assert response.is_json
        assert response.json['error']['code'] == 'PAYLOAD_TOO_LARGE'
        assert response.json['error']['requestId']
    else:
        body = response.get_data(as_text=True)
        assert '上传内容超过大小限制' in body
        assert '工作台' in body
        assert '1 KiB' in body
        assert 'Request Entity Too Large' not in body


def test_oversized_login_has_visible_error_without_authenticated_session(app):
    app.config['MAX_CONTENT_LENGTH'] = 1024
    response = app.test_client().post('/auth/login', data={'username': 'x' * 2048})
    assert response.status_code == 413
    assert '上传内容超过大小限制' in response.get_data(as_text=True)
    assert '<h1>' in response.get_data(as_text=True)


def test_oversized_legacy_project_fetch_uploads_return_the_json_contract(app, engine):
    reached = []
    for blueprint_name in ("projects", "security_projects", "crypto_projects"):
        app.add_url_rule(
            f"/{blueprint_name}/qa-oversized-upload",
            endpoint=f"{blueprint_name}.qa_oversized_upload",
            view_func=lambda: reached.append(True) or "unexpected",
            methods=["POST"],
        )
    _insert_user(engine)
    client = app.test_client()
    assert _login(client).status_code == 302
    app.config["MAX_CONTENT_LENGTH"] = 1024

    for blueprint_name in ("projects", "security_projects", "crypto_projects"):
        response = client.post(
            f"/{blueprint_name}/qa-oversized-upload",
            data={"payload": "x" * 2048},
        )
        assert response.status_code == 413
        assert response.is_json
        assert response.json == {
            "success": False,
            "message": "上传内容超过大小限制，请缩小文件或分批上传后重试。",
            "code": "PAYLOAD_TOO_LARGE",
        }
    assert reached == []
