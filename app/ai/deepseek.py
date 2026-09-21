from __future__ import annotations

from threading import local

from app.ai.contract import build_messages, proposal_output_schema
from app.ai.http_transport import post_json


DEEPSEEK_RESPONSES_URL = "https://api.deepseek.com/responses"


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
        self._metadata = local()

    @property
    def last_metadata(self) -> dict:
        return dict(getattr(self._metadata, "value", {}))

    def generate(self, source_text: str, *, deadline_seconds: float, cancel_check) -> str:
        self._metadata.value = {}
        payload = {
            "model": self.model_version,
            "input": build_messages(source_text),
            "text": {"format": {
                "type": "json_schema", "name": "proposal_assistant",
                "schema": proposal_output_schema(),
            }},
            "reasoning": {"effort": "none"},
            "temperature": 0,
            "max_output_tokens": 2000,
            "stream": False,
        }
        response = self._transport(
            url=DEEPSEEK_RESPONSES_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload=payload,
            timeout=min(float(deadline_seconds), 60.0),
            cancel_check=cancel_check,
        )
        try:
            if (
                not isinstance(response, dict) or response.get("status") != "completed"
                or response.get("error") is not None
                or response.get("incomplete_details") is not None
            ):
                raise ValueError("response not completed")
            output = response["output"]
            if not isinstance(output, list) or len(output) != 1:
                raise ValueError("expected one assistant message")
            message = output[0]
            if (message["type"] != "message" or message["status"] != "completed"
                    or message["role"] != "assistant"):
                raise ValueError("invalid assistant message")
            parts = message["content"]
            if not isinstance(parts, list) or not parts:
                raise ValueError("empty content")
            for part in parts:
                if part["type"] != "output_text" or not isinstance(part["text"], str):
                    raise ValueError("invalid output text")
            raw = "".join(part["text"] for part in parts)
            if not raw.strip():
                raise ValueError("empty output text")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise RuntimeError("DeepSeek 返回结构无效") from error
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        def token_count(key):
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None
        self._metadata.value = {
            "model": response.get("model") or self.model_version,
            "inputTokens": token_count("input_tokens"),
            "outputTokens": token_count("output_tokens"),
        }
        return raw
