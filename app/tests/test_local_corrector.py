import json
import io
import pytest
from flask import Flask
from app.llm.corrector import Corrector


def make_corrector(transport):
    return Corrector(config={"AI_PROVIDER": "LOCAL", "LOCAL_MODEL_BASE_URL": "http://127.0.0.1:18081", "LOCAL_MODEL_NAME": "Qwen3.5-9B"}, transport=transport)


def answer(text, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": json.dumps({"corrected": text}, ensure_ascii=False)}}]}


def test_local_correction_uses_same_loopback_no_proxy_and_grounded_diff():
    calls=[]
    corrector=make_corrector(lambda **kw: (calls.append(kw) or answer("研究计划。")))
    result=corrector.correct("研究计化。")
    assert result["corrected"] == "研究计划。"
    assert result["errors"] == [{"pos": 3, "old": "化", "new": "划", "reason": "校对建议，请人工确认"}]
    assert result["truncated"] is False
    assert len(calls)==1 and calls[0]["no_proxy"] is True
    assert calls[0]["url"]=="http://127.0.0.1:18081/v1/chat/completions"
    assert calls[0]["payload"]["messages"][-1]["content"]=="研究计化。"


@pytest.mark.parametrize("output", [None, {}, answer("结果", "length"), answer(""), answer("字"*4001)])
def test_failures_keep_original_and_never_expose_provider_details(output):
    result=make_corrector(lambda **kw:output).correct("原始文本")
    assert result["corrected"]=="原始文本" and result["errors"]==[]
    assert result["error"]=="自动校对未完成，请使用人工校对"


def test_disabled_never_calls_and_truncation_is_explicit():
    def forbidden(**kw):
        raise AssertionError("must not call")
    disabled=Corrector(config={"AI_PROVIDER":"DISABLED"},transport=forbidden)
    assert "error" in disabled.correct("原文")
    calls=[]
    result=make_corrector(lambda **kw:(calls.append(kw) or answer("字"*2000))).correct("字"*2001)
    assert result["truncated"] is True
    assert len(calls[0]["payload"]["messages"][-1]["content"])==2000


def client(provider="LOCAL"):
    from app.routes.utils import bp
    app=Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="fixture", AI_PROVIDER=provider,
                      AI_FEATURES_VISIBLE=True,
                      AUXILIARY_AI_ENABLED=True)
    app.register_blueprint(bp)
    c=app.test_client()
    with c.session_transaction() as session:
        session["user"]="qa"
    return c


def test_utils_failure_and_success_contract(monkeypatch):
    monkeypatch.setattr(Corrector,"correct",lambda *args: {"corrected":"原文", "errors":[], "truncated":False,"error":"/private/secret"})
    c=client()
    for path, data in [("/utils/api/correct", {"text":"原文"}), ("/utils/api/upload_and_correct", {"file":(io.BytesIO("原文".encode()),"中文.txt")})]:
        response=c.post(path,data=data)
        assert response.status_code==503 and response.json["success"] is False
        assert "/private" not in response.text
    monkeypatch.setattr(Corrector,"correct",lambda *args: {"corrected":"研究计划", "errors":[], "truncated":False})
    result=c.post("/utils/api/correct",data={"text":"研究计化"})
    assert result.status_code==200 and result.json["original"]=="研究计化"
    assert result.json["corrected"]=="研究计划" and result.json["errors"]==[]
    assert client("DISABLED").post("/utils/api/correct",data={"text":"原文"}).status_code==503


def test_summary_contract_is_local_bounded_and_does_not_claim_provider_failure_as_success(monkeypatch):
    from app.llm.corrector import LocalSummarizer
    calls=[]
    def transport(**kw):
        calls.append(kw)
        return {"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"summary":"记录设备测试。"}'}}]}
    summarizer=LocalSummarizer(config={"AI_PROVIDER":"LOCAL","LOCAL_MODEL_BASE_URL":"http://127.0.0.1:18081","LOCAL_MODEL_NAME":"Qwen3.5-9B"}, transport=transport)
    assert summarizer.summarize("字"*1501)=={"summary":"记录设备测试。","truncated":True}
    assert len(calls)==1 and len(calls[0]["payload"]["messages"][1]["content"])==1500
    assert "error" in summarizer.summarize("材料",max_length=1)
    from app.routes.api import bp
    app=Flask(__name__)
    app.config.update(TESTING=True,SECRET_KEY="test",AI_PROVIDER="LOCAL",
                      AI_FEATURES_VISIBLE=True,
                      AUXILIARY_AI_ENABLED=True)
    app.register_blueprint(bp)
    c=app.test_client()
    with c.session_transaction() as session:
        session["user"]="qa"
    monkeypatch.setattr(LocalSummarizer,"summarize",lambda *a,**kw:{"summary":"","truncated":False,"error":"/private/secret"})
    r=c.post("/api/summarize",json={"text":"材料"})
    assert r.status_code==503 and r.json["code"]!=0 and "/private" not in r.text
    assert c.post("/api/summarize",json={"text":12}).status_code==400
    assert c.post("/api/summarize",json={"text":"材料","max_length":True}).status_code==400


def test_client_endpoints_are_closed_when_auxiliary_ai_is_out_of_scope(monkeypatch):
    from app.llm.corrector import LocalSummarizer
    from app.routes.api import bp as api_bp
    from app.routes.utils import bp as utils_bp
    calls=[]
    monkeypatch.setattr(Corrector,"correct",lambda *args,**kwargs:calls.append("correct"))
    monkeypatch.setattr(LocalSummarizer,"summarize",lambda *args,**kwargs:calls.append("summary"))
    app=Flask(__name__)
    app.config.update(TESTING=True,SECRET_KEY="test",AI_PROVIDER="LOCAL",
                      AI_FEATURES_VISIBLE=True,
                      AUXILIARY_AI_ENABLED=False)
    app.register_blueprint(api_bp)
    app.register_blueprint(utils_bp)
    c=app.test_client()
    with c.session_transaction() as session:
        session["user"]="qa"
    assert c.get("/utils/document_correction").status_code==404
    assert c.post("/utils/api/correct",data={"text":"研究计化"}).status_code==404
    assert c.post("/api/correct",json={"text":"研究计化"}).status_code==404
    assert c.post("/api/summarize",json={"text":"材料"}).status_code==404
    assert c.post("/api/ai-search",json={"query":"设备","records":[]}).status_code==404
    assert calls==[]
