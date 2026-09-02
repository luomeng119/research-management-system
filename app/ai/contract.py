from __future__ import annotations

import json
from typing import Protocol

from app.text import has_meaningful_text, normalize_text


PROMPT_VERSION = "proposal-v5-grounded-sparse"
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
ARRAY_ITEM_LIMITS = {"missingInformation": 6}
MAX_RAW_OUTPUT_BYTES = 64_000
MAX_PROMPT_BYTES = 8_000
APPLICABLE_FIELDS = CONTENT_FIELDS[:5]
ARRAY_TOTAL_LIMITS = {
    "objectives": 10_000,
    "researchContent": 50_000,
    "expectedOutcomes": 10_000,
    "missingInformation": 10_000,
}

SYSTEM_PROMPT = """你是科研提案整理助手，只整理用户提供的事实，不作立项、专家推荐或合规判断。
必须只返回一个 JSON 对象，不得包含 Markdown、解释或额外字段。以下六个字段每次都必须出现，即使值为空也不得省略：
title（字符串）、researchProblem（字符串）、objectives（字符串数组）、researchContent（字符串数组）、expectedOutcomes（字符串数组）、missingInformation（字符串数组）。
逐字段只做事实映射：
- title：研究对象和动作已明确时，用输入中的词汇概括；否则为空。
- researchProblem：只改写输入已指出的问题、现象或比较关系，不得加入“优化、影响正常运行、较高”等输入没有的判断。
- objectives：先逐字查找输入已明确的比较、测试、验证、分析、降低、整理或评估动作；找到的动作必须提取，不得因为技术路线、指标或条件缺失而省略。
- researchContent：只列出输入已说明要处理的材料、数据或活动。
- expectedOutcomes：只提取输入明确说要“形成、产出或交付”的报告、建议、方案、数据等结果；不得把比较、测试会自然产生的结果推定为交付物，也不得把已有材料改写成未来成果，未说明则为空。
不得添加输入未提及的算法、技术路线、试验平台、评价结论、显著改善或任何常识补全。
信息不足时不得编造或填写“待定”占位内容；无法从输入确认的字符串字段返回空字符串，数组字段返回空数组。
missingInformation 要具体列出开展后续整理仍缺少且输入未提供的信息，优先检查：具体研究对象、场景或环境条件、基线、测试或评价指标、数据或材料依据；未明说交付物时再列预期交付物。最多列出 6 项，不要把已提供的信息再次列为缺失项。"""


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
    if not has_meaningful_text(source_text):
        raise AssistantContractError("请选择需要整理的文字", "AI_INPUT_INVALID")
    source_text = normalize_text(source_text)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": source_text},
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
            item_limit = ARRAY_ITEM_LIMITS.get(field, MAX_ARRAY_ITEMS)
            if (
                not isinstance(item, list)
                or len(item) > item_limit
            ):
                raise _invalid(f"{field} 必须是受限数组")
            cleaned = []
            for part in item:
                if not has_meaningful_text(part):
                    raise _invalid(f"{field} 包含无效条目")
                text = normalize_text(part)
                if len(text) > FIELD_LIMITS[field]:
                    raise _invalid(f"{field} 条目超过限制")
                cleaned.append(text)
            if len("\n".join(cleaned)) > ARRAY_TOTAL_LIMITS[field]:
                raise _invalid(f"{field} 总长度超过限制")
            normalized[field] = cleaned
        else:
            if not isinstance(item, str):
                raise _invalid(f"{field} 必须是文字")
            text = normalize_text(item) if has_meaningful_text(item) else ""
            if len(text) > FIELD_LIMITS[field]:
                raise _invalid(f"{field} 超过限制")
            normalized[field] = text
    if (
        any(not normalized[field] for field in APPLICABLE_FIELDS)
        and not normalized["missingInformation"]
    ):
        raise _invalid("存在空建议时必须列出缺失信息")
    return normalized
