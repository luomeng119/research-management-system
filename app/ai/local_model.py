from __future__ import annotations

from urllib.parse import urlsplit
import ipaddress

from app.ai.contract import build_messages
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


class LocalProposalAssistant:
    provider_kind = "LOCAL"

    def __init__(self, *, base_url: str, model: str, transport=None) -> None:
        self.base_url = _validated_base_url(base_url)
        if not model:
            raise ValueError("本地模型名称不能为空")
        self.model_version = model
        self._transport = transport or post_json
        self.last_metadata = {}

    def generate(self, source_text: str, *, deadline_seconds: float, cancel_check) -> str:
        response = self._transport(
            url=f"{self.base_url}/v1/chat/completions",
            headers={},
            payload={
                "model": self.model_version,
                "messages": build_messages(source_text),
                "response_format": {"type": "json_object"},
                "max_tokens": 2000,
                "stream": False,
            },
            timeout=min(float(deadline_seconds), 60.0),
            cancel_check=cancel_check,
            **({"no_proxy": True} if self._transport is post_json else {}),
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
            raise RuntimeError("本地模型返回结构无效") from error
