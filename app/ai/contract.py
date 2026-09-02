from __future__ import annotations

import json
from typing import Protocol


PROMPT_VERSION = "proposal-v1"
CONTENT_FIELDS = (
    "title",
    "researchProblem",
    "objectives",
    "researchContent",
    "expectedOutcomes",
    "missingInformation",
)
ARRAY_FIELDS = frozenset(CONTENT_FIELDS[2:])
FIELD_LIMITS = {
    "title": 200,
    "researchProblem": 10_000,
    "objectives": 2_000,
    "researchContent": 5_000,
    "expectedOutcomes": 2_000,
    "missingInformation": 2_000,
}
MAX_ARRAY_ITEMS = 20
MAX_RAW_OUTPUT_BYTES = 64_000
MAX_PROMPT_BYTES = 8_000
REQUIRED_ARRAY_FIELDS = frozenset({"objectives", "researchContent", "expectedOutcomes"})
ARRAY_TOTAL_LIMITS = {
    "objectives": 10_000,
    "researchContent": 50_000,
    "expectedOutcomes": 10_000,
    "missingInformation": 10_000,
}

SYSTEM_PROMPT = """你是科研提案整理助手，只整理用户提供的事实，不作立项、专家推荐或合规判断。
必须只返回一个 JSON 对象，不得包含 Markdown、解释或额外字段。对象字段固定为：
title（字符串）、researchProblem（字符串）、objectives（字符串数组）、researchContent（字符串数组）、expectedOutcomes（字符串数组）、missingInformation（字符串数组）。
信息不足时不得编造，应把缺失项写入 missingInformation。"""


class AssistantContractError(ValueError):
    def __init__(self, message: str, code: str = "AI_OUTPUT_INVALID") -> None:
        super().__init__(message)
        self.code = code


class ProposalAssistant(Protocol):
    provider_kind: str
    model_version: str

    def generate(self, source_text: str, *, deadline_seconds: float, cancel_check) -> str:
        """Return one plain JSON object as text. Implementations must not retry."""


def build_messages(source_text: str) -> list[dict[str, str]]:
    if not isinstance(source_text, str) or not source_text.strip():
        raise AssistantContractError("请选择需要整理的文字", "AI_INPUT_INVALID")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": source_text.strip()},
    ]
    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROMPT_BYTES:
        raise AssistantContractError("输入超过 8000 token 的保守限制", "AI_INPUT_TOO_LARGE")
    return messages


def _invalid(message: str) -> AssistantContractError:
    return AssistantContractError(message)


def parse_assistant_content(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid("模型未返回内容")
    if len(raw.encode("utf-8")) > MAX_RAW_OUTPUT_BYTES:
        raise _invalid("模型输出超过限制")
    stripped = raw.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise _invalid("模型输出不得包含代码围栏")
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise _invalid("模型输出包含重复字段")
            value[key] = item
        return value

    try:
        value = json.loads(stripped, object_pairs_hook=unique_object)
    except AssistantContractError:
        raise
    except (TypeError, ValueError) as error:
        raise _invalid("模型输出不是有效 JSON") from error
    if not isinstance(value, dict) or set(value) != set(CONTENT_FIELDS):
        raise _invalid("模型输出字段不符合契约")
    normalized = {}
    for field in CONTENT_FIELDS:
        item = value[field]
        if field in ARRAY_FIELDS:
            if (
                not isinstance(item, list)
                or len(item) > MAX_ARRAY_ITEMS
                or (field in REQUIRED_ARRAY_FIELDS and not item)
            ):
                raise _invalid(f"{field} 必须是受限数组")
            cleaned = []
            for part in item:
                if not isinstance(part, str) or not part.strip():
                    raise _invalid(f"{field} 包含无效条目")
                text = part.strip()
                if len(text) > FIELD_LIMITS[field]:
                    raise _invalid(f"{field} 条目超过限制")
                cleaned.append(text)
            if len("\n".join(cleaned)) > ARRAY_TOTAL_LIMITS[field]:
                raise _invalid(f"{field} 总长度超过限制")
            normalized[field] = cleaned
        else:
            if not isinstance(item, str) or not item.strip():
                raise _invalid(f"{field} 必须是非空文字")
            text = item.strip()
            if len(text) > FIELD_LIMITS[field]:
                raise _invalid(f"{field} 超过限制")
            normalized[field] = text
    return normalized
