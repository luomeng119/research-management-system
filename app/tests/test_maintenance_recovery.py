from pathlib import Path
from datetime import datetime

from jinja2 import DictLoader, Environment
from app.tests.test_auth_audit import (
    app, engine, repositories, _csrf, _insert_user, _login, SYSTEM_MAINTAINER,
)


def test_disabled_account_has_enable_action():
    source = (Path(__file__).parents[1] / "templates/users/index.html").read_text()
    template = Environment(loader=DictLoader({
        "base.html": "{% block content %}{% endblock %}",
        "users/index.html": source,
    })).get_template("users/index.html")
    html = template.render(
        users=[dict(username="disabled_user", name="Test", status="disabled", role="BUSINESS_USER")],
        session={"user": "maintainer"},
        url_for=lambda *args, **kwargs: "/unused",
    )
    assert "approveUser('disabled_user', true)" in html
    assert ">启用</button>" in html


def test_account_labels_and_dates_are_readable():
    source = (Path(__file__).parents[1] / "templates/users/index.html").read_text()
    template = Environment(loader=DictLoader({
        "base.html": "{% block content %}{% endblock %}",
        "users/index.html": source,
    })).get_template("users/index.html")
    html = template.render(
        users=[
            dict(username="active_user", name="Test", status="active", role="BUSINESS_USER", created_at=datetime(2026, 9, 9, 8, 30, 22)),
            dict(username="maintainer", name="Test", status="disabled", role="SYSTEM_MAINTAINER", created_at=None),
            dict(username="pending_user", name="Test", status="pending", role="BUSINESS_USER", created_at=None),
        ],
        session={"user": "current"},
        url_for=lambda *args, **kwargs: "/unused",
    )
    for title, label in (("业务用户", "业务用户"), ("管理员", "管理员"), ("active", "已启用"), ("disabled", "已停用"), ("pending", "待审核")):
        assert f'title="{title}">{label}</span>' in html
    assert '系统维护员' not in html
    assert 'title="2026-09-09 08:30:22"' in html
    assert '>2026-09-09 08:30</time>' in html
    assert "未记录" in html


def test_permissions_notice_keeps_maintenance_boundary(app, engine):
    _insert_user(engine, username="maint", role=SYSTEM_MAINTAINER)
    _insert_user(engine, username="business")
    maint = app.test_client()
    _login(maint, "maint")
    response = maint.get("/users/permissions/business")
    assert response.status_code == 200
    assert "V1业务账号权限一致，无需单独设置" in response.get_data(as_text=True)
    assert "返回账号管理" in response.get_data(as_text=True)
    token = _csrf(maint, "/users/change-password")
    rejected = maint.post("/users/permissions/business", data={"_csrf_token": token})
    assert rejected.status_code == 404
    assert rejected.json["error"]["code"] == "NOT_SUPPORTED"
    business = app.test_client()
    _login(business, "business")
    assert business.get("/users/permissions/business").status_code == 403
