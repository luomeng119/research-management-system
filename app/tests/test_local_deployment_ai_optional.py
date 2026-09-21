import ast
from pathlib import Path

from flask import Flask


ROOT = Path(__file__).resolve().parents[2]


def test_local_deployment_does_not_autostart_a_local_model():
    tree = ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))

    top_level_calls = {
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }

    assert "ensure_inference_server" not in top_level_calls


def test_local_tools_page_exposes_readonly_model_status_only_to_maintainer():
    html = (ROOT / "app/templates/utils/index.html").read_text(encoding="utf-8")

    assert "/utils/model_config" in html
    assert "/utils/monitor" in html
    assert "SYSTEM_MAINTAINER" in html
    assert "当前交付的模型仅用于科研提案和报告工作流" in html
    assert "/utils/document_correction" in html
    assert "AI_PROVIDER" in html
    assert "敏感词库维护" in html
    assert "sensitive_terms.manage" in html
    assert "SENSITIVE_TERMS_AVAILABLE" in html


def test_local_correction_endpoint_is_an_honest_optional_failure():
    from app.routes.utils import bp

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret", ENABLE_LLM=False,
                      AI_FEATURES_VISIBLE=True,
                      AUXILIARY_AI_ENABLED=True)
    app.register_blueprint(bp)
    client = app.test_client()
    with client.session_transaction() as active_session:
        active_session["user"] = "zhang"

    response = client.post("/utils/api/correct", data={"text": "需要校对的文本"})

    assert response.status_code == 503
    assert response.get_json() == {
        "success": False,
        "error": "V1 未启用本地模型，请使用人工校对",
    }
    assert client.get("/utils/model_config").status_code == 404
    assert client.get("/utils/monitor").status_code == 404
