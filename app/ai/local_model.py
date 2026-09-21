from __future__ import annotations

from urllib.parse import urlsplit
import ipaddress
from threading import local

from app.ai.contract import build_messages, proposal_output_schema
from app.ai.http_transport import post_json


def _validated_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname
            and ipaddress.ip_address(parsed.hostname).is_loopback
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and parsed.port is not None
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("本地模型必须使用带端口的字面回环地址")
    return value.rstrip("/")


def _runtime_output_schema() -> dict:
    """Constrain shape without expanding llama.cpp's bounded repetition grammar.

    The business parser still enforces every length and item bound after generation.
    """
    repetition_limits = {"minLength", "maxLength", "minItems", "maxItems"}

    def strip_limits(value):
        if isinstance(value, dict):
            return {key: strip_limits(item) for key, item in value.items()
                    if key not in repetition_limits}
        if isinstance(value, list):
            return [strip_limits(item) for item in value]
        return value

    return strip_limits(proposal_output_schema())


class LocalProposalAssistant:
    provider_kind = "LOCAL"

    def __init__(self, *, base_url: str, model: str, transport=None) -> None:
        self.base_url = _validated_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("本地模型名称不能为空")
        self.model_version = model
        self._transport = transport or post_json
        self._metadata = local()

    @property
    def last_metadata(self) -> dict:
        return dict(getattr(self._metadata, "value", {}))

    def generate(self, source_text: str, *, deadline_seconds: float, cancel_check) -> str:
        self._metadata.value = {}
        response = self._transport(
            url=f"{self.base_url}/v1/chat/completions",
            headers={},
            payload={
                "model": self.model_version,
                "messages": build_messages(source_text),
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "proposal_assistant", "strict": True,
                    "schema": _runtime_output_schema(),
                }},
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "max_tokens": 2000,
                "stream": False,
            },
            timeout=min(float(deadline_seconds), 60.0),
            cancel_check=cancel_check,
            no_proxy=True,
        )
        try:
            if not isinstance(response, dict) or response.get("error") is not None:
                raise ValueError("invalid response")
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("expected one choice")
            choice = choices[0]
            message = choice["message"]
            if (choice.get("finish_reason") != "stop"
                    or message.get("role") != "assistant"
                    or message.get("tool_calls")):
                raise ValueError("incomplete assistant message")
            raw = message["content"]
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("empty output text")
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise RuntimeError("本地模型返回结构无效") from error
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        def token_count(key):
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None
        model = response.get("model")
        self._metadata.value = {
            "model": model if isinstance(model, str) and model else self.model_version,
            "inputTokens": token_count("prompt_tokens"),
            "outputTokens": token_count("completion_tokens"),
        }
        return raw
