import pytest

from app import create_app


def _base_config(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "ai-visibility-test",
        "DATA_DIR": str(tmp_path / "data"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "FILE_STORAGE_ROOT": str(tmp_path / "files"),
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "LOG_FILE": None,
        "AI_PROVIDER": "LOCAL",
        "LOCAL_MODEL_BASE_URL": "http://127.0.0.1:18081",
        "LOCAL_MODEL_NAME": "test-local",
    }
    config.update(overrides)
    return config


def test_ai_features_are_hidden_by_default_and_services_are_not_attached(tmp_path):
    assistant_service = object()
    report_draft_service = object()

    app = create_app(_base_config(
        tmp_path,
        ASSISTANT_SERVICE=assistant_service,
        RESEARCH_REPORT_DRAFT_SERVICE=report_draft_service,
    ))

    assert app.config["AI_FEATURES_VISIBLE"] is False
    assert "assistant_service" not in app.extensions
    assert "research_report_draft_service" not in app.extensions
    assert app.config["AI_ASSISTANT_AVAILABLE"] is False
    assert app.config["RESEARCH_REPORT_GENERATION_AVAILABLE"] is False


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/utils/document_correction"),
    ("GET", "/utils/model_config"),
    ("GET", "/utils/monitor"),
    ("POST", "/utils/api/correct"),
    ("POST", "/utils/api/local-model/load"),
    ("GET", "/api/llm/status"),
    ("POST", "/api/correct"),
    ("POST", "/api/summarize"),
    ("POST", "/api/ai-search"),
    ("POST", "/api/proposals/TP-1/assistant-drafts"),
    ("POST", "/api/assistant-runs/run-1/cancel"),
    ("POST", "/api/proposals/TP-1/assistant-drafts/draft-1/apply"),
    ("POST", "/research-reports/report-1/generate"),
    ("GET", "/research-reports/report-1/drafts/draft-1"),
    ("GET", "/research-reports/report-1/versions/1/model-draft"),
    ("POST", "/research-reports/report-1/selection-suggestions"),
    ("POST", "/research-reports/report-1/selection-suggestions/cancel"),
])
def test_default_hidden_ai_only_routes_return_404(tmp_path, method, path):
    app = create_app(_base_config(tmp_path))
    client = app.test_client()

    assert client.open(path, method=method).status_code == 404


def test_explicit_visibility_override_attaches_services_and_opens_ai_routes(tmp_path):
    assistant_service = object()
    report_draft_service = object()
    app = create_app(_base_config(
        tmp_path,
        AI_FEATURES_VISIBLE=True,
        ASSISTANT_SERVICE=assistant_service,
        RESEARCH_REPORT_DRAFT_SERVICE=report_draft_service,
    ))
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(
            user_id=1,
            user="maintainer",
            name="维护员",
            role="SYSTEM_MAINTAINER",
            account_version=1,
        )

    assert app.extensions["assistant_service"] is assistant_service
    assert app.extensions["research_report_draft_service"] is report_draft_service
    assert client.get("/utils/model_config").status_code == 200


def test_default_hidden_public_and_tools_pages_do_not_claim_local_ai(tmp_path):
    app = create_app(_base_config(tmp_path))
    client = app.test_client()

    login_html = client.get("/auth/login").get_data(as_text=True)
    leadership_html = client.get("/system-intro").get_data(as_text=True)
    with client.session_transaction() as active_session:
        active_session.update(user="maintainer", role="SYSTEM_MAINTAINER")
    tools_html = client.get("/utils/").get_data(as_text=True)

    assert "AI 模型" not in login_html
    for hidden_claim in ("Qwen", "本地 AI", "本地AI", "本地多模态模型", "智能层"):
        assert hidden_claim not in leadership_html
    for hidden_control in ("本地模型维护", "模型配置", "当前交付的模型"):
        assert hidden_control not in tools_html
    assert "敏感词库维护" not in tools_html


def test_default_hidden_business_pages_keep_manual_work_without_ai_controls(tmp_path):
    class ProposalService:
        def get(self, business_id):
            return {
                "id": 1,
                "businessId": business_id,
                "title": "手工提案",
                "sourceType": "IDEA",
                "sourceSummary": "人工录入来源",
                "researchProblem": "人工填写问题",
                "objectives": "人工填写目标",
                "researchContent": "人工填写内容",
                "expectedOutcomes": "人工填写成果",
                "status": "DRAFT",
                "version": 1,
            }

    class ReportService:
        report = {
            "id": "report-1",
            "objectType": "PROPOSAL",
            "objectId": "TP-1",
            "title": "手工报告",
            "purpose": "人工编写",
            "currentVersion": 1,
        }
        version = {
            "versionNo": 1,
            "body": "人工报告正文",
            "note": "人工修订",
            "createdAt": "2026-09-21T10:00:00+08:00",
            "createdByName": "测试用户",
        }

        def get(self, report_id):
            return self.report

        def current(self, report_id):
            return self.version

        def history(self, report_id, **kwargs):
            return {"items": [self.version], "page": 1, "pageSize": 20, "total": 1}

    app = create_app(_base_config(
        tmp_path,
        PROPOSAL_SERVICE=ProposalService(),
        RESEARCH_REPORT_SERVICE=ReportService(),
    ))
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(
            user_id=1,
            user="researcher",
            name="测试用户",
            role="BUSINESS_USER",
            account_version=1,
        )

    proposal_html = client.get("/proposals/TP-1/edit").get_data(as_text=True)
    report_html = client.get("/research-reports/report-1").get_data(as_text=True)

    assert "手工提案" in proposal_html
    assert "人工报告正文" in report_html
    for hidden_control in ("提案助手", "AI 建议", "ai-action-badge"):
        assert hidden_control not in proposal_html
    for hidden_control in ("本地资料整合", "整合报告草稿", "选区措辞建议", "selection-suggestions", "ai-action-badge"):
        assert hidden_control not in report_html


def test_sensitive_term_maintenance_stays_available_when_ai_is_hidden(tmp_path):
    class SensitiveTerms:
        def current(self):
            return {"version": 0, "rules": []}

        def history(self, **kwargs):
            return {"items": [], "page": 1, "pageSize": 20, "total": 0}

    app = create_app(_base_config(tmp_path, SENSITIVE_TERM_SET_SERVICE=SensitiveTerms()))
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(
            user_id=1,
            user="maintainer",
            name="维护员",
            role="SYSTEM_MAINTAINER",
            account_version=1,
        )

    tools = client.get("/utils/")
    maintenance = client.get("/admin/sensitive-terms")

    assert tools.status_code == 200
    assert "敏感词库维护" in tools.get_data(as_text=True)
    assert maintenance.status_code == 200
    assert "添加规则" in maintenance.get_data(as_text=True)


def test_hidden_gate_removes_ai_tools_from_navigation_even_if_legacy_toggle_is_on(tmp_path):
    app = create_app(_base_config(tmp_path, AUXILIARY_AI_ENABLED=True))
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session.update(user="researcher", role="BUSINESS_USER")

    response = client.get("/api/tree")

    assert response.status_code == 200
    assert "/utils/document_correction" not in response.get_data(as_text=True)
