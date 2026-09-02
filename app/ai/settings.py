from __future__ import annotations

def validate_assistant_settings(config) -> None:
    mode = str(config.get("DEPLOYMENT_MODE", "DEVELOPMENT")).upper()
    provider = str(config.get("AI_PROVIDER", "DISABLED")).upper()
    if provider not in {"DISABLED", "DEEPSEEK"}:
        raise RuntimeError("AI_PROVIDER 配置无效，V1 仅支持 DISABLED 或 DEEPSEEK")
    if mode not in {"DEVELOPMENT", "PRODUCTION", "TESTING"}:
        raise RuntimeError("DEPLOYMENT_MODE 配置无效，必须显式选择开发或正式模式")
