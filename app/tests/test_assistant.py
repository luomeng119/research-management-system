from __future__ import annotations

import json
import threading
import io
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app.ai.contract import (
    AssistantContractError,
    parse_assistant_content,
)

from app.repositories.assistant import AssistantRepository
from app.repositories.proposals import ProposalsRepository
from app.repositories.audit import AuditRepository
from app.services.audit import AuditService
from app.services.assistant import AssistantService, AssistantServiceError, RunRegistry
from app.services.proposals import ProposalService
from app.tests.test_proposals import Audit, _schema


@pytest.fixture()
def engine():
    value = sa.create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _schema(value)
    return value


@pytest.fixture()
def service(engine):
    return ProposalService(ProposalsRepository(engine), Audit())


VALID = {
    "title": "便携式保障设备适配研究",
    "researchProblem": "现有设备在低温环境下稳定性不足",
    "objectives": ["明确低温影响因素", "形成可验证的适配方案"],
    "researchContent": ["开展低温环境试验", "比较结构优化方案"],
    "expectedOutcomes": ["研究报告", "适配方案"],
    "missingInformation": ["目标温度范围尚未明确"],
}


def test_eval_script_direct_entrypoint_loads_project_package():
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/run_assistant_eval.py", "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Run the frozen proposal assistant evaluation" in result.stdout


def test_strict_contract_accepts_only_exact_plain_json_object():
    parsed = parse_assistant_content(json.dumps(VALID, ensure_ascii=False))
    assert parsed == VALID

    invalid_values = [
        "",
        "```json\n" + json.dumps(VALID, ensure_ascii=False) + "\n```",
        json.dumps({**VALID, "decision": "ESTABLISH"}, ensure_ascii=False),
        json.dumps({**VALID, "objectives": "形成方案"}, ensure_ascii=False),
        json.dumps({**VALID, "title": None}, ensure_ascii=False),
        "[]",
        '{"title":"一","title":"二","researchProblem":"问题","objectives":["目标"],"researchContent":["内容"],"expectedOutcomes":["成果"],"missingInformation":[]}',
    ]
    for value in invalid_values:
        with pytest.raises(AssistantContractError) as captured:
            parse_assistant_content(value)
        assert captured.value.code == "AI_OUTPUT_INVALID"


def test_contract_rejects_oversized_or_non_string_items():
    with pytest.raises(AssistantContractError):
        parse_assistant_content(json.dumps({**VALID, "title": "x" * 201}))
    with pytest.raises(AssistantContractError):
        parse_assistant_content(json.dumps({**VALID, "objectives": ["目标", 2]}))
    with pytest.raises(AssistantContractError):
        parse_assistant_content(
            json.dumps({**VALID, "researchContent": ["内容"] * 21})
        )


def test_contract_accepts_sparse_truthful_draft_only_with_missing_information():
    sparse = {
        "title": "", "researchProblem": "", "objectives": [],
        "researchContent": [], "expectedOutcomes": [],
        "missingInformation": ["研究对象", "目标指标"],
    }
    assert parse_assistant_content(json.dumps(sparse, ensure_ascii=False)) == sparse
    with pytest.raises(AssistantContractError, match="缺失信息"):
        parse_assistant_content(json.dumps({**sparse, "missingInformation": []}))


def test_fixed_eval_dataset_is_versioned_balanced_and_hash_stable():
    from app.tests.ai_eval.dataset import DATASET_SHA256, DATASET_VERSION, load_samples

    samples = load_samples()
    assert DATASET_VERSION == "proposal-assistant-eval-v1"
    assert len(samples) == 20
    categories = {}
    for sample in samples:
        categories[sample["category"]] = categories.get(sample["category"], 0) + 1
        assert set(sample) == {
            "id", "category", "input", "allowedFacts", "expectedFieldPoints",
            "requiredMissingInformation", "forbiddenGeneration",
        }
    assert categories == {
        "IDEA": 5,
        "MEETING_CONCLUSION": 5,
        "FINISHED_MATERIAL": 5,
        "INSUFFICIENT": 5,
    }
    assert len(DATASET_SHA256) == 64
    assert DATASET_SHA256 == __import__("hashlib").sha256(
        json.dumps(samples, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_eval_refuses_fake_quality_and_requires_two_human_reviewers():
    from app.tests.ai_eval.dataset import load_samples
    from app.tests.ai_eval.scorer import evaluate

    assert evaluate([{"providerKind": "FAKE"}])["status"] == "NOT_RUN"
    records = []
    raw = json.dumps(VALID, ensure_ascii=False)
    for sample in load_samples():
        records.append({
            "sampleId": sample["id"], "providerKind": "DEEPSEEK",
            "modelVersion": "test", "rawOutput": raw,
            "outputSha256": __import__("hashlib").sha256(raw.encode()).hexdigest(),
            "latencySeconds": 1, "inputTokens": 100, "outputTokens": 100,
        })
    pending = evaluate(records)
    assert pending["status"] == "PENDING_HUMAN_REVIEW"
    assert pending["automatic"]["schemaValid"] == 20


def test_production_rejects_deepseek_and_non_loopback_local_configuration(tmp_path):
    from app import create_app

    common = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATA_DIR": str(tmp_path / "data"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "LOG_FILE": None,
    }
    with pytest.raises(RuntimeError, match="DeepSeek"):
        create_app({**common, "DEPLOYMENT_MODE": "PRODUCTION", "AI_PROVIDER": "DEEPSEEK"})
    with pytest.raises(RuntimeError, match="DeepSeek"):
        create_app({
            **common,
            "DEPLOYMENT_MODE": "PRODUCTION",
            "AI_PROVIDER": "DISABLED",
            "DEEPSEEK_API_KEY": "must-not-exist",
        })
    with pytest.raises(RuntimeError, match="回环"):
        create_app({
            **common,
            "DEPLOYMENT_MODE": "PRODUCTION",
            "AI_PROVIDER": "LOCAL",
            "LOCAL_MODEL_BASE_URL": "http://192.168.1.10:8000",
        })
    with pytest.raises(RuntimeError, match="DEPLOYMENT_MODE"):
        create_app({**common, "DEPLOYMENT_MODE": "PRODCUTION", "AI_PROVIDER": "DEEPSEEK"})


def test_deepseek_adapter_uses_one_fixed_json_request_without_retry():
    from app.ai.deepseek import DeepSeekProposalAssistant

    calls = []

    def transport(*, url, headers, payload, timeout, cancel_check):
        calls.append((url, headers, payload, timeout, cancel_check))
        return {
            "choices": [{"message": {"content": json.dumps(VALID, ensure_ascii=False)}}],
            "model": "deepseek-test",
        }

    provider = DeepSeekProposalAssistant(
        api_key="secret", model="deepseek-test", transport=transport
    )
    raw = provider.generate("脱敏测试材料", deadline_seconds=60, cancel_check=lambda: False)
    assert json.loads(raw) == VALID
    assert len(calls) == 1
    url, headers, payload, timeout, _ = calls[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    assert payload["model"] == "deepseek-test"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 2000
    assert timeout == 60
    assert "必须只返回" in payload["messages"][0]["content"]

    failures = []

    def failing(**kwargs):
        failures.append(kwargs)
        raise TimeoutError("timeout")

    provider = DeepSeekProposalAssistant(api_key="secret", model="m", transport=failing)
    with pytest.raises(TimeoutError):
        provider.generate("脱敏测试材料", deadline_seconds=3, cancel_check=lambda: False)
    assert len(failures) == 1


def test_local_adapter_rejects_redirects_and_uses_loopback_without_proxy():
    from app.ai.local_model import LocalProposalAssistant

    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": json.dumps(VALID)}}], "model": "local"}

    provider = LocalProposalAssistant(
        base_url="http://127.0.0.1:8000", model="local-test", transport=transport
    )
    provider.generate("本地材料", deadline_seconds=10, cancel_check=lambda: False)
    assert calls[0]["url"] == "http://127.0.0.1:8000/v1/chat/completions"

    for url in (
        "http://localhost:8000",
        "http://192.168.1.8:8000",
        "http://127.0.0.1:8000/path",
        "http://user@127.0.0.1:8000",
    ):
        with pytest.raises(ValueError, match="回环"):
            LocalProposalAssistant(base_url=url, model="local-test", transport=transport)


def test_http_transport_enforces_deadline_cancel_limit_and_ignores_curlrc(
    tmp_path, monkeypatch,
):
    from app.ai.http_transport import post_json

    normal_body = json.dumps({"ok": True}).encode()
    oversized_body = json.dumps({"ok": "x" * (512 * 1024)}).encode()

    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            received = self.rfile.read(content_length)
            if self.path == "/slow-success":
                time.sleep(1.2)
                body = json.dumps({"received": json.loads(received)}).encode()
            elif self.path == "/oversized":
                body = oversized_body
            else:
                body = normal_body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if self.path == "/slow-timeout":
                    time.sleep(0.4)
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        trace_path = tmp_path / "curl-trace.txt"
        (tmp_path / ".curlrc").write_text(
            f'trace-ascii = "{trace_path}"\n', encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        started = time.monotonic()
        assert post_json(
            url=f"{root}/slow-success", headers={"Authorization": "Bearer secret-marker"},
            payload={"text": "慢首包"}, timeout=3,
            cancel_check=lambda: False, no_proxy=True,
        ) == {"received": {"text": "慢首包"}}
        assert 1.1 < time.monotonic() - started < 2.5
        assert not trace_path.exists()

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            post_json(
                url=f"{root}/slow-timeout", headers={}, payload={}, timeout=0.15,
                cancel_check=lambda: False, no_proxy=True,
            )
        assert time.monotonic() - started < 0.8

        cancelled = threading.Event()
        threading.Timer(0.1, cancelled.set).start()
        started = time.monotonic()
        with pytest.raises(InterruptedError):
            post_json(
                url=f"{root}/slow-timeout", headers={}, payload={}, timeout=2,
                cancel_check=cancelled.is_set, no_proxy=True,
            )
        assert time.monotonic() - started < 0.8

        with pytest.raises(RuntimeError, match="response too large"):
            post_json(
                url=f"{root}/oversized", headers={}, payload={}, timeout=2,
                cancel_check=lambda: False, no_proxy=True,
            )
    finally:
        server.shutdown()
        server.server_close()


class FakeProvider:
    provider_kind = "LOCAL"
    model_version = "fake-local"

    def __init__(self, raw=None, error=None):
        self.raw = raw if raw is not None else json.dumps(VALID, ensure_ascii=False)
        self.error = error
        self.calls = 0

    def generate(self, source_text, *, deadline_seconds, cancel_check):
        self.calls += 1
        self.last_source_text = source_text
        if self.error:
            raise self.error
        return self.raw


def _assistant_service(engine, audit, provider=None, registry=None, file_service=None):
    return AssistantService(
        AssistantRepository(engine),
        audit,
        provider or FakeProvider(),
        run_registry=registry or RunRegistry(max_entries=16),
        file_service=file_service,
    )


def test_generate_persists_only_valid_draft_and_never_changes_proposal(engine, service):
    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        },
        actor_user_id=7,
        request_id="req-create",
    )
    before = service.get(created["businessId"])
    assistant = _assistant_service(engine, service.audit_service)
    draft = assistant.generate(
        created["businessId"],
        source_text="用户明确选择的脱敏文字",
        selected_file_ids=[],
        proposal_version=1,
        run_id="run-valid",
        actor_user_id=7,
        request_id="req-ai",
    )
    assert draft["status"] == "READY"
    assert draft["providerKind"] == "LOCAL"
    assert draft["content"] == VALID
    assert service.get(created["businessId"]) == before

    invalid = _assistant_service(
        engine, service.audit_service, FakeProvider(raw=json.dumps({**VALID, "decision": "YES"}))
    )
    with pytest.raises(AssistantServiceError) as captured:
        invalid.generate(
            created["businessId"], source_text="测试", selected_file_ids=[],
            proposal_version=1, run_id="run-invalid", actor_user_id=7,
            request_id="req-invalid",
        )
    assert captured.value.code == "AI_OUTPUT_INVALID"
    assert len(invalid.repository.list_drafts_for_test()) == 1


def test_apply_is_atomic_selected_only_and_old_draft_cannot_overwrite(engine, service):
    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        },
        actor_user_id=7,
        request_id="req-create",
    )
    assistant = _assistant_service(engine, service.audit_service)
    draft = assistant.generate(
        created["businessId"], source_text="测试", selected_file_ids=[], proposal_version=1,
        run_id="run-apply", actor_user_id=7, request_id="req-generate",
    )
    applied = assistant.apply(
        created["businessId"], draft["id"], fields=["title"], proposal_version=1,
        actor_user_id=7, request_id="req-apply",
    )
    assert applied["proposal"]["title"] == VALID["title"]
    assert applied["proposal"]["researchProblem"] == "原问题"
    assert applied["proposal"]["objectives"] == "原目标"
    assert applied["draft"]["status"] == "APPLIED"
    assert applied["draft"]["acceptedFields"] == ["title"]

    old = assistant.generate(
        created["businessId"], source_text="测试", selected_file_ids=[], proposal_version=2,
        run_id="run-stale", actor_user_id=7, request_id="req-stale-generate",
    )
    service.update(
        created["businessId"], {"researchProblem": "人工新问题"}, expected_version=2,
        actor_user_id=7, request_id="req-human-edit",
    )
    with pytest.raises(AssistantServiceError) as stale:
        assistant.apply(
            created["businessId"], old["id"], fields=["researchProblem"],
            proposal_version=3, actor_user_id=7, request_id="req-stale-apply",
        )
    assert stale.value.code == "VERSION_CONFLICT"
    assert service.get(created["businessId"])["researchProblem"] == "人工新问题"

    drafts = sa.Table("proposal_ai_drafts", sa.MetaData(), autoload_with=engine)
    proposals = sa.Table("proposals", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        proposal_id = connection.scalar(
            sa.select(proposals.c.id).where(
                proposals.c.business_id == created["businessId"]
            )
        )
        legacy_id = "44444444-4444-4444-8444-444444444444"
        connection.execute(drafts.insert().values(
            id=legacy_id, proposal_id=proposal_id, status="READY",
            provider_kind="LOCAL", model_version="legacy",
            prompt_version="proposal-v1", source_proposal_version=0,
            content=VALID, accepted_fields=[], created_by=7, updated_by=7, version=1,
        ))
    with pytest.raises(AssistantServiceError) as legacy:
        assistant.apply(
            created["businessId"], legacy_id, fields=["title"],
            proposal_version=3, actor_user_id=7, request_id="req-legacy-apply",
        )
    assert legacy.value.code == "VERSION_CONFLICT"


def test_sparse_draft_empty_field_cannot_be_applied(engine, service):
    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        }, actor_user_id=7, request_id="req-create",
    )
    sparse = {
        "title": "", "researchProblem": "", "objectives": [],
        "researchContent": [], "expectedOutcomes": [],
        "missingInformation": ["研究对象", "目标指标"],
    }
    assistant = _assistant_service(
        engine, service.audit_service,
        FakeProvider(raw=json.dumps(sparse, ensure_ascii=False)),
    )
    draft = assistant.generate(
        created["businessId"], source_text="研究一下新材料",
        selected_file_ids=[], proposal_version=1, run_id="run-sparse",
        actor_user_id=7, request_id="req-sparse",
    )
    with pytest.raises(AssistantServiceError) as captured:
        assistant.apply(
            created["businessId"], draft["id"], fields=["title"],
            proposal_version=1, actor_user_id=7, request_id="req-empty-apply",
        )
    assert captured.value.code == "AI_FIELD_EMPTY"
    assert service.get(created["businessId"])["title"] == "原题目"


def test_invisible_only_content_is_not_meaningful(engine, service):
    invisible = {
        "title": "\u200b", "researchProblem": "\ufeff", "objectives": [],
        "researchContent": [], "expectedOutcomes": [], "missingInformation": [],
    }
    with pytest.raises(AssistantContractError) as missing:
        parse_assistant_content(json.dumps(invisible, ensure_ascii=False))
    assert "缺失信息" in str(missing.value)

    invalid_item = {
        **invisible,
        "objectives": ["\u200b"],
        "missingInformation": ["研究目标"],
    }
    with pytest.raises(AssistantContractError) as item:
        parse_assistant_content(json.dumps(invalid_item, ensure_ascii=False))
    assert "objectives 包含无效条目" in str(item.value)

    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        }, actor_user_id=7, request_id="req-invisible-create",
    )
    assistant = _assistant_service(
        engine, service.audit_service,
        FakeProvider(raw=json.dumps({
            **invisible,
            "missingInformation": ["研究对象", "研究问题", "研究目标", "研究内容", "预期成果"],
        }, ensure_ascii=False)),
    )
    draft = assistant.generate(
        created["businessId"], source_text="研究新材料",
        selected_file_ids=[], proposal_version=1, run_id="run-invisible",
        actor_user_id=7, request_id="req-invisible-generate",
    )
    with pytest.raises(AssistantServiceError) as apply_error:
        assistant.apply(
            created["businessId"], draft["id"], fields=["title"],
            proposal_version=1, actor_user_id=7, request_id="req-invisible-apply",
        )
    assert apply_error.value.code == "AI_FIELD_EMPTY"


@pytest.mark.parametrize("invisible", ["\ufe0f", "\u034f"])
def test_mark_only_content_is_not_meaningful(invisible):
    content = {
        "title": invisible,
        "researchProblem": invisible,
        "objectives": [invisible],
        "researchContent": [],
        "expectedOutcomes": [],
        "missingInformation": [],
    }
    with pytest.raises(AssistantContractError):
        parse_assistant_content(json.dumps(content, ensure_ascii=False))


def test_missing_information_has_six_item_limit():
    content = {
        "title": "", "researchProblem": "", "objectives": [],
        "researchContent": [], "expectedOutcomes": [],
        "missingInformation": [f"缺失项{i}" for i in range(6)],
    }
    assert len(parse_assistant_content(json.dumps(content, ensure_ascii=False))["missingInformation"]) == 6
    content["missingInformation"].append("缺失项6")
    with pytest.raises(AssistantContractError) as captured:
        parse_assistant_content(json.dumps(content, ensure_ascii=False))
    assert "受限数组" in str(captured.value)


def test_deepseek_requires_server_side_sanitization_confirmation(engine, service):
    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        }, actor_user_id=7, request_id="req-create",
    )

    class DeepSeekFake(FakeProvider):
        provider_kind = "DEEPSEEK"
        model_version = "fake-deepseek"

    provider = DeepSeekFake()
    assistant = _assistant_service(engine, service.audit_service, provider=provider)
    with pytest.raises(AssistantServiceError) as missing:
        assistant.generate(
            created["businessId"], source_text="脱敏测试材料", selected_file_ids=[],
            proposal_version=1, run_id="run-no-confirm", actor_user_id=7,
            request_id="req-no-confirm",
        )
    assert missing.value.code == "AI_REMOTE_CONFIRMATION_REQUIRED"
    assert provider.calls == 0
    draft = assistant.generate(
        created["businessId"], source_text="脱敏测试材料", selected_file_ids=[],
        proposal_version=1, run_id="run-confirmed", actor_user_id=7,
        request_id="req-confirmed", remote_input_confirmed=True,
    )
    assert draft["providerKind"] == "DEEPSEEK"
    assert service.audit_service.events[-1]["properties"]["remote_input_confirmed"] is True


def test_selected_text_attachment_must_belong_to_proposal_and_be_safe(engine, service):
    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        }, actor_user_id=7, request_id="req-create",
    )

    file_id = "11111111-1111-4111-8111-111111111111"

    class Files:
        def list_for_object(self, *, object_type, object_id):
            assert (object_type, object_id) == ("PROPOSAL", created["businessId"])
            return [{
                "fileId": file_id, "originalName": "依据.txt", "versionNo": 1,
                "status": "ACTIVE",
            }]

        def open_version_stream(self, file_id, version_no, *, object_type, object_id):
            assert (file_id, version_no, object_type, object_id) == (
                file_id, 1, "PROPOSAL", created["businessId"],
            )
            return {"sizeBytes": 12, "stream": io.BytesIO("附件事实".encode())}

    provider = FakeProvider()
    assistant = _assistant_service(
        engine, service.audit_service, provider=provider, file_service=Files()
    )
    assistant.generate(
        created["businessId"], source_text="用户选择文字",
        selected_file_ids=[file_id], proposal_version=1,
        run_id="run-file-valid", actor_user_id=7, request_id="req-file-valid",
    )
    assert "用户选择文字" in provider.last_source_text
    assert "附件事实" in provider.last_source_text
    assert "依据.txt" not in provider.last_source_text

    with pytest.raises(AssistantServiceError) as foreign:
        assistant.generate(
            created["businessId"], source_text="用户选择文字",
            selected_file_ids=["22222222-2222-4222-8222-222222222222"], proposal_version=1,
            run_id="run-file-foreign", actor_user_id=7, request_id="req-file-foreign",
        )
    assert foreign.value.code == "AI_FILE_NOT_FOUND"

    class UnsafeFiles(Files):
        def __init__(self, raw, size):
            self.raw = raw
            self.size = size

        def open_version_stream(self, *args, **kwargs):
            return {"sizeBytes": self.size, "stream": io.BytesIO(self.raw)}

    binary = _assistant_service(
        engine, service.audit_service, provider=FakeProvider(),
        file_service=UnsafeFiles(b"text\x00binary", 11),
    )
    with pytest.raises(AssistantServiceError) as binary_error:
        binary.generate(
            created["businessId"], source_text="用户选择文字",
            selected_file_ids=[file_id], proposal_version=1,
            run_id="run-file-binary", actor_user_id=7, request_id="req-file-binary",
        )
    assert binary_error.value.code == "AI_FILE_TYPE_UNSUPPORTED"

    oversized = _assistant_service(
        engine, service.audit_service, provider=FakeProvider(),
        file_service=UnsafeFiles(b"x", 64 * 1024 + 1),
    )
    with pytest.raises(AssistantServiceError) as size_error:
        oversized.generate(
            created["businessId"], source_text="用户选择文字",
            selected_file_ids=[file_id], proposal_version=1,
            run_id="run-file-large", actor_user_id=7, request_id="req-file-large",
        )
    assert size_error.value.code == "AI_FILE_TOO_LARGE"


def test_cancel_acknowledges_running_generation_and_prevents_ready_draft(engine, service):
    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        },
        actor_user_id=7,
        request_id="req-create",
    )
    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def generate(self, source_text, *, deadline_seconds, cancel_check):
            entered.set()
            assert release.wait(2)
            return self.raw

    registry = RunRegistry(max_entries=16)
    assistant = _assistant_service(
        engine, service.audit_service, BlockingProvider(), registry=registry
    )
    result = {}

    def generate():
        try:
            assistant.generate(
                created["businessId"], source_text="测试", selected_file_ids=[],
                proposal_version=1, run_id="run-cancel", actor_user_id=7,
                request_id="req-cancel-generate",
            )
        except AssistantServiceError as error:
            result["code"] = error.code

    thread = threading.Thread(target=generate)
    thread.start()
    assert entered.wait(1)
    with pytest.raises(AssistantServiceError) as other_user:
        assistant.cancel("run-cancel", actor_user_id=8, request_id="req-other-user")
    assert other_user.value.code == "RUN_NOT_FOUND"
    assert assistant.cancel("run-cancel", actor_user_id=7, request_id="req-cancel") == {
        "runId": "run-cancel", "status": "CANCELLED"
    }
    release.set()
    thread.join(2)
    assert result["code"] == "AI_CANCELLED"
    assert assistant.repository.list_drafts_for_test() == []

    assert assistant.cancel(
        "run-cancel-before-start", actor_user_id=7, request_id="req-pre-cancel"
    )["status"] == "CANCELLED"
    with pytest.raises(AssistantServiceError) as pre_cancelled:
        assistant.generate(
            created["businessId"], source_text="测试", selected_file_ids=[],
            proposal_version=1, run_id="run-cancel-before-start", actor_user_id=7,
            request_id="req-pre-cancel-generate",
        )
    assert pre_cancelled.value.code == "AI_CANCELLED"


def test_apply_rejects_invalid_field_types_and_malformed_draft_id(engine, service):
    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        }, actor_user_id=7, request_id="req-create",
    )
    assistant = _assistant_service(engine, service.audit_service)
    with pytest.raises(AssistantServiceError) as invalid_fields:
        assistant.apply(
            created["businessId"], "missing", fields=[{"title": True}],
            proposal_version=1, actor_user_id=7, request_id="req-invalid-fields",
        )
    assert invalid_fields.value.code == "AI_FIELDS_INVALID"
    with pytest.raises(AssistantServiceError) as missing:
        assistant.apply(
            created["businessId"], "not-a-uuid", fields=["title"],
            proposal_version=1, actor_user_id=7, request_id="req-invalid-id",
        )
    assert missing.value.code == "AI_DRAFT_NOT_FOUND"


def test_assistant_http_contract_and_edit_panel_keep_manual_path(tmp_path, engine, service):
    from app import create_app

    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        },
        actor_user_id=7,
        request_id="req-create",
    )
    assistant = _assistant_service(engine, service.audit_service)
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATA_DIR": str(tmp_path / "data"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "DATABASE_ENGINE": engine,
        "PROPOSAL_SERVICE": service,
        "ASSISTANT_SERVICE": assistant,
        "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False,
        "LOG_FILE": None,
        "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=7, user="alice", name="Alice", role="BUSINESS_USER",
            account_version=1,
        )

    edit = client.get(f"/proposals/{created['businessId']}/edit")
    assert edit.status_code == 200
    html = edit.get_data(as_text=True)
    assert "科研提案助手" in html
    assert "生成建议" in html
    assert "保存草稿" in html

    generated = client.post(
        f"/api/proposals/{created['businessId']}/assistant-drafts",
        json={
            "sourceText": "测试材料", "selectedFileIds": [],
            "proposalVersion": 1, "runId": "run-http",
        },
    )
    assert generated.status_code == 201
    draft = generated.get_json()
    assert draft["content"] == VALID

    applied = client.post(
        f"/api/proposals/{created['businessId']}/assistant-drafts/{draft['id']}/apply",
        json={"fields": ["objectives"], "proposalVersion": 1},
    )
    assert applied.status_code == 200
    assert applied.get_json()["proposal"]["objectives"] == "\n".join(VALID["objectives"])
    assert applied.get_json()["proposal"]["title"] == "原题目"

    unsupported_file = client.post(
        f"/api/proposals/{created['businessId']}/assistant-drafts",
        json={
            "sourceText": "测试", "selectedFileIds": ["33333333-3333-4333-8333-333333333333"],
            "proposalVersion": 2, "runId": "run-file",
        },
    )
    assert unsupported_file.status_code == 422
    assert unsupported_file.get_json()["error"]["code"] == "AI_FILE_INPUT_UNSUPPORTED"

    oversized = client.post(
        f"/api/proposals/{created['businessId']}/assistant-drafts",
        data=b"x" * (128 * 1024 + 1), content_type="application/json",
    )
    assert oversized.status_code == 413
    assert oversized.is_json
    assert oversized.get_json()["error"]["code"] == "AI_INPUT_TOO_LARGE"

    chunked_body = json.dumps({"sourceText": "x" * (128 * 1024)}).encode()
    chunked = client.open(
        f"/api/proposals/{created['businessId']}/assistant-drafts",
        method="POST",
        input_stream=io.BytesIO(chunked_body),
        content_type="application/json",
        environ_overrides={
            "CONTENT_LENGTH": "",
            "HTTP_TRANSFER_ENCODING": "chunked",
            "wsgi.input_terminated": True,
        },
    )
    assert chunked.status_code == 413
    assert chunked.is_json
    assert chunked.get_json()["error"]["code"] == "AI_INPUT_TOO_LARGE"


def test_generation_audit_failure_rolls_back_and_returns_fixed_json(tmp_path, engine, service):
    from app import create_app

    created = service.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        }, actor_user_id=7, request_id="req-create",
    )
    service.audit_service.fail = True
    assistant = _assistant_service(engine, service.audit_service)
    app = create_app({
        "TESTING": True, "SECRET_KEY": "test-secret",
        "DATA_DIR": str(tmp_path / "data"), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "DATABASE_ENGINE": engine, "PROPOSAL_SERVICE": service,
        "ASSISTANT_SERVICE": assistant, "SECURITY_AUTH_ENABLED": False,
        "CSRF_ENABLED": False, "LOG_FILE": None, "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_id=7, user="alice", name="Alice", role="BUSINESS_USER",
            account_version=1,
        )
    response = client.post(
        f"/api/proposals/{created['businessId']}/assistant-drafts",
        json={
            "sourceText": "测试", "selectedFileIds": [],
            "proposalVersion": 1, "runId": "run-audit-fail",
        },
    )
    assert response.status_code == 503
    assert response.is_json
    assert response.get_json()["error"]["code"] == "AI_UNAVAILABLE"
    assert assistant.repository.list_drafts_for_test() == []


@pytest.mark.skipif(
    not os.environ.get("T07_TEST_DATABASE_URL"),
    reason="requires isolated migrated PostgreSQL",
)
def test_postgresql_concurrent_apply_allows_exactly_one_winner():
    engine = sa.create_engine(os.environ["T07_TEST_DATABASE_URL"], pool_pre_ping=True)
    users = sa.Table("users", sa.MetaData(), autoload_with=engine)
    audit_events = sa.Table("audit_events", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        actor_id = connection.execute(
            users.insert().values(
                username=f"t07-{__import__('uuid').uuid4().hex}", password="disabled",
                role="BUSINESS_USER", name="T07", status="active",
                directory_permissions={}, must_change_password=True, version=1,
            ).returning(users.c.id)
        ).scalar_one()
    audit = AuditService(AuditRepository(engine), "test-v1")
    proposals = ProposalService(ProposalsRepository(engine), audit)
    created = proposals.create(
        {
            "title": "原题目", "sourceType": "IDEA", "sourceSummary": "原始设想",
            "researchProblem": "原问题", "objectives": "原目标",
            "researchContent": "原内容", "expectedOutcomes": "原成果",
        }, actor_user_id=actor_id, request_id="req-pg-create",
    )
    assistant = _assistant_service(engine, audit)
    draft = assistant.generate(
        created["businessId"], source_text="测试", selected_file_ids=[],
        proposal_version=1, run_id="run-pg", actor_user_id=actor_id,
        request_id="req-pg-generate",
    )
    barrier = Barrier(2)

    def apply(number):
        barrier.wait()
        try:
            return assistant.apply(
                created["businessId"], draft["id"], fields=["title"],
                proposal_version=1, actor_user_id=actor_id,
                request_id=f"req-pg-apply-{number}",
            )
        except AssistantServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, (1, 2)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert sum(value in {"AI_DRAFT_ALREADY_APPLIED", "VERSION_CONFLICT"} for value in results if isinstance(value, str)) == 1
    assert proposals.get(created["businessId"])["version"] == 2
    with engine.connect() as connection:
        applied_events = connection.scalar(
            sa.select(sa.func.count()).select_from(audit_events).where(
                audit_events.c.action == "assistant_draft_applied",
                audit_events.c.object_id == draft["id"],
            )
        )
    assert applied_events == 1
