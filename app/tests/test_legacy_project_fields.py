from pathlib import Path
import re

import pytest
from flask import Flask
from jinja2 import Template

from app.tests.test_project_establishment import engine, service
from app.services.projects import ProjectServiceError


@pytest.mark.parametrize("category", ["GENERAL_RESEARCH", "SECURITY_CONFIDENTIALITY", "CRYPTO_APPLICATION"])
def test_legacy_planned_date_keeps_actual_date_and_state(service, category):
    project = service.create_standalone(category, {"projectId": "DATE-FIX", "name": "Fixture", "leader": "Test", "plannedEndDate": "2027-01-01"}, actor_user_id=7)
    service.update_legacy_field(category=category, business_id="DATE-FIX", field="planned_end_date", value="2027-02-01", actor_user_id=7)
    readback = service.get(project["id"])
    assert readback["plannedEndDate"] == "2027-02-01"
    assert service.get_legacy(category=category, business_id="DATE-FIX")["actual_end_date"] is None
    assert readback["status"] == "PENDING"
    with pytest.raises(ProjectServiceError):
        service.update_legacy_field(category=category, business_id="DATE-FIX", field="planned_end_date", value="2027-02-31", actor_user_id=7)
    assert service.get(project["id"])["plannedEndDate"] == "2027-02-01"
    service.update_legacy_field(category=category, business_id="DATE-FIX", field="planned_end_date", value="", actor_user_id=7)
    assert service.get(project["id"])["plannedEndDate"] is None
    assert service.get_legacy(category=category, business_id="DATE-FIX")["actual_end_date"] is None


@pytest.mark.parametrize("status", ["已结题", "已终止"])
def test_terminal_project_rejects_new_planned_date_path(service, status):
    project = service.create_standalone("CRYPTO_APPLICATION", {"projectId": "TERMINAL", "name": "Fixture", "leader": "Test", "plannedEndDate": "2027-01-01", "status": status}, actor_user_id=7)
    with pytest.raises(ProjectServiceError) as caught:
        service.update_legacy_field(category="CRYPTO_APPLICATION", business_id="TERMINAL", field="planned_end_date", value="2027-02-01", actor_user_id=7)
    assert caught.value.code == "STATE_CONFLICT"
    assert service.get(project["id"])["plannedEndDate"] == "2027-01-01"


@pytest.mark.parametrize("module", ["projects", "security_projects", "crypto_projects"])
def test_legacy_route_maps_planned_date_to_existing_service(module):
    from importlib import import_module
    app = Flask(__name__)
    app.secret_key = "fixture"
    app.register_blueprint(import_module(f"app.routes.{module}").bp)
    calls = []
    class Service:
        def update_legacy_field(self, **kwargs):
            calls.append(kwargs)
    app.extensions["project_service"] = Service()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "fixture"
        session["user_id"] = 7
        session["role"] = "BUSINESS_USER"
        session["account_version"] = 1
    response = client.post(f"/{module}/update_field/DATE-FIX", data={"field": "计划结束日期", "value": "2027-02-01"})
    assert response.json["success"] is True
    assert calls[0]["field"] == "planned_end_date"


@pytest.mark.parametrize("status", ["执行中", "已暂停", "已结题", "已终止"])
def test_legacy_select_displays_current_state_without_new_transition_option(status):
    source = (Path(__file__).parents[1] / "templates/projects/detail.html").read_text()
    select = re.search(r'<select[^>]+onchange="updateProjectField[^>]+状态[\s\S]*?</select>', source).group()
    html = Template(select).render(project={"project_id": "fixture", "status": status})
    assert f'<option value="{status}" selected disabled>{status}</option>' in html


@pytest.mark.parametrize("module,model", [("projects", "ProjectModel"), ("security_projects", "SecurityProjectModel"), ("crypto_projects", "CryptoProjectModel")])
def test_planned_date_without_service_never_uses_legacy_model(module, model, monkeypatch):
    from importlib import import_module
    route_module = import_module(f"app.routes.{module}")
    def unexpected_model():
        pytest.fail("planned date must not fall back to unvalidated legacy model")
    monkeypatch.setattr(route_module, model, unexpected_model)
    app = Flask(__name__)
    app.secret_key = "fixture"
    app.register_blueprint(route_module.bp)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user="fixture", user_id=7, role="BUSINESS_USER", account_version=1)
    response = client.post(f"/{module}/update_field/TERMINAL", data={"field": "计划结束日期", "value": "2027-02-31"})
    assert response.status_code == 503
    assert response.json["success"] is False
