from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from app.ai.local_model import LocalProposalAssistant
from app.ai.settings import validate_assistant_settings


def response(**overrides):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3}, **overrides}


def test_local_request_is_schema_constrained_and_proxy_free():
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        return response()
    p = LocalProposalAssistant(base_url="http://127.0.0.1:18081", model="Qwen3.5-9B", transport=transport)
    assert p.generate("研究材料", deadline_seconds=4, cancel_check=lambda: False) == "{}"
    assert len(calls) == 1
    call = calls[0]
    assert call["no_proxy"] is True
    assert call["payload"]["response_format"]["type"] == "json_schema"
    assert call["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert call["payload"]["temperature"] == 0
    assert call["timeout"] == 4
    assert p.last_metadata["inputTokens"] == 5


@pytest.mark.parametrize("bad", [None, [], {}, response(choices=[]),
    response(choices=[{"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]),
    response(choices=[{"finish_reason": "stop", "message": {"role": "user", "content": "{}"}}]),
    response(choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": None}}]),
    response(error={"message": "bad"})])
def test_invalid_or_truncated_response_is_rejected(bad):
    p = LocalProposalAssistant(base_url="http://127.0.0.1:18081", model="m", transport=lambda **kw: bad)
    with pytest.raises(RuntimeError, match="结构无效"):
        p.generate("研究材料", deadline_seconds=4, cancel_check=lambda: False)
    assert p.last_metadata == {}


def test_metadata_is_thread_local_and_invalid_usage_is_unknown():
    barrier = Barrier(2)
    def transport(**kwargs):
        source = kwargs["payload"]["messages"][-1]["content"]
        return response(usage={"prompt_tokens": int(source), "completion_tokens": True})
    p = LocalProposalAssistant(base_url="http://127.0.0.1:18081", model="m", transport=transport)
    def run(n):
        p.generate(str(n), deadline_seconds=4, cancel_check=lambda: False)
        barrier.wait()
        return p.last_metadata
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, [4, 8]))
    assert [r["inputTokens"] for r in results] == [4, 8]
    assert all(r["outputTokens"] is None for r in results)
    assert p.last_metadata == {}


def test_current_configuration_allows_only_disabled_or_loopback_local():
    validate_assistant_settings({"AI_PROVIDER": "LOCAL", "LOCAL_MODEL_BASE_URL": "http://127.0.0.1:18081", "LOCAL_MODEL_NAME": "Qwen3.5-9B"})
    with pytest.raises(RuntimeError, match="AI_PROVIDER"):
        validate_assistant_settings({"AI_PROVIDER": "DEEPSEEK"})
    with pytest.raises(RuntimeError, match="回环"):
        validate_assistant_settings({"AI_PROVIDER": "LOCAL", "LOCAL_MODEL_BASE_URL": "http://example.com:80", "LOCAL_MODEL_NAME": "m"})


def test_real_transport_ignores_proxy_and_does_not_follow_redirect(monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    requests = []
    redirect = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            if redirect.is_set():
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                return
            body = json.dumps(response()).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("http_proxy", "HTTP_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        p = LocalProposalAssistant(base_url=f"http://127.0.0.1:{server.server_port}", model="m")
        assert p.generate("材料", deadline_seconds=2, cancel_check=lambda: False) == "{}"
        redirect.set()
        with pytest.raises(RuntimeError):
            p.generate("材料", deadline_seconds=2, cancel_check=lambda: False)
        assert requests == ["/v1/chat/completions", "/v1/chat/completions"]
        assert p.last_metadata == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_runtime_schema_avoids_grammar_expansion_but_parser_keeps_limits():
    import json
    from app.ai.contract import AssistantContractError, FIELD_LIMITS, parse_assistant_content, proposal_output_schema
    calls = []
    p = LocalProposalAssistant(base_url="http://127.0.0.1:18081", model="m",
        transport=lambda **kw: (calls.append(kw) or response()))
    p.generate("材料", deadline_seconds=2, cancel_check=lambda: False)
    schema = calls[0]["payload"]["response_format"]["json_schema"]["schema"]
    def assert_no_expansion_limits(value):
        if isinstance(value, dict):
            assert not ({"minLength", "maxLength", "minItems", "maxItems"} & value.keys())
            for child in value.values():
                assert_no_expansion_limits(child)
        elif isinstance(value, list):
            for child in value:
                assert_no_expansion_limits(child)
    assert_no_expansion_limits(schema)
    assert schema["required"] == proposal_output_schema()["required"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["objectives"] == {"type": "array", "items": {"type": "string"}}
    assert proposal_output_schema()["properties"]["title"]["maxLength"] == FIELD_LIMITS["title"]
    valid = {"title": "题目", "researchProblem": "问题", "objectives": [],
             "researchContent": [], "expectedOutcomes": [], "missingInformation": ["研究材料"]}
    for invalid in ({**valid, "title": "字" * (FIELD_LIMITS["title"] + 1)},
                    {**valid, "missingInformation": ["缺口"] * 7},
                    {**valid, "objectives": [""]}):
        with pytest.raises(AssistantContractError):
            parse_assistant_content(json.dumps(invalid, ensure_ascii=False))
