from __future__ import annotations

from app.ai.local_model import LocalProposalAssistant


def validate_assistant_settings(config) -> None:
    mode = str(config.get("DEPLOYMENT_MODE", "DEVELOPMENT")).upper()
    provider = str(config.get("AI_PROVIDER", "DISABLED")).upper()
    if mode not in {"DEVELOPMENT", "PRODUCTION", "TESTING"}:
        raise RuntimeError("DEPLOYMENT_MODE 配置无效，必须显式选择开发或正式模式")
    if provider not in {"DISABLED", "LOCAL"}:
        raise RuntimeError("AI_PROVIDER 配置无效，当前仅支持 DISABLED 或 LOCAL")
    if provider == "LOCAL":
        try:
            LocalProposalAssistant(
                base_url=config.get("LOCAL_MODEL_BASE_URL"),
                model=config.get("LOCAL_MODEL_NAME"),
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"AI_PROVIDER LOCAL 配置无效：{error}") from error
