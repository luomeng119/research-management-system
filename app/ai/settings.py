from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def _is_literal_loopback(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            return False
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except (TypeError, ValueError):
        return False


def validate_assistant_settings(config) -> None:
    mode = str(config.get("DEPLOYMENT_MODE", "DEVELOPMENT")).upper()
    provider = str(config.get("AI_PROVIDER", "DISABLED")).upper()
    key = config.get("DEEPSEEK_API_KEY")
    if provider not in {"DISABLED", "LOCAL", "DEEPSEEK"}:
        raise RuntimeError("AI_PROVIDER 配置无效")
    if mode == "PRODUCTION" and (provider == "DEEPSEEK" or key):
        raise RuntimeError("正式离线环境禁止配置或调用 DeepSeek")
    if provider == "LOCAL" and not _is_literal_loopback(
        str(config.get("LOCAL_MODEL_BASE_URL", ""))
    ):
        raise RuntimeError("本地模型地址必须使用字面回环地址")
    if mode not in {"DEVELOPMENT", "PRODUCTION", "TESTING"}:
        raise RuntimeError("DEPLOYMENT_MODE 配置无效，必须显式选择开发或正式模式")
