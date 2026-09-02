from __future__ import annotations

from app.ai.contract import build_messages
from app.ai.http_transport import post_json


DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"


class DeepSeekProposalAssistant:
    provider_kind = "DEEPSEEK"

    def __init__(self, *, api_key: str, model: str, transport=None) -> None:
        if not api_key:
            raise RuntimeError("DeepSeek API Key 未配置")
        if not model:
            raise RuntimeError("DeepSeek 模型未配置")
        self.api_key = api_key
        self.model_version = model
        self._transport = transport or post_json
        self.last_metadata = {}

    def generate(self, source_text: str, *, deadline_seconds: float, cancel_check) -> str:
        payload = {
            "model": self.model_version,
            "messages": build_messages(source_text),
            "response_format": {"type": "json_object"},
            # V4 enables high-effort thinking by default. This bounded
            # extraction task needs the JSON answer, not hidden reasoning that
            # can consume the entire 2,000-token output budget.
            "thinking": {"type": "disabled"},
            "max_tokens": 2000,
            "stream": False,
        }
        response = self._transport(
            url=DEEPSEEK_CHAT_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload=payload,
            timeout=min(float(deadline_seconds), 60.0),
            cancel_check=cancel_check,
        )
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        self.last_metadata = {
            "model": response.get("model") or self.model_version,
            "inputTokens": usage.get("prompt_tokens"),
            "outputTokens": usage.get("completion_tokens"),
        }
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("DeepSeek 返回结构无效") from error
