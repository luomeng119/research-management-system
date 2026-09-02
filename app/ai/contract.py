from __future__ import annotations

import json
import re
from typing import Protocol

from app.text import has_meaningful_text, normalize_text


PROMPT_VERSION = "proposal-v12-grounded-finalizer-safety"
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
- objectives：先逐字查找输入已明确的研究、探索、比较、测试、验证、分析、降低、整理、评估、补充或复测动作；找到的动作必须提取，不得因为技术路线、指标或条件缺失而省略。
- researchContent：只列出输入已说明要处理的材料、数据或活动。
- expectedOutcomes：只提取输入明确说要“形成、产出或交付”的报告、建议、方案、数据等结果；不得把比较、测试会自然产生的结果推定为交付物，也不得把已有材料改写成未来成果，未说明则为空。
不得添加输入未提及的算法、技术路线、试验平台、评价结论、显著改善或任何常识补全。
只允许以下有原文直接依据的后续动作转换：输入明确说“尚无、未记录、未知、未说明”某项信息时，可写“补充或核实该项信息”；明确说“尚未复测”时可写“复测原现象”；明确出现两种或多种对象及同一性能/用时记录时可写“比较该性能/用时”；明确给出事件数量及伴随场景数量时可写“分析该场景与事件的关系”。不得由此推断原因、优劣或结论。
输入是“研究某种方法”时，可把“验证该方法”列为目标；输入是“降低某项误报/故障率”时，可把“分析该问题的影响因素”和“降低该指标”列为目标；输入给出已有试验记录及测量指标时，可把“分析已有指标数据”列为目标。仍不得生成具体原因、技术方案或评价结论。
映射示例只说明规则，不得复制示例中的对象：
- “建议围绕延迟做小范围测试，技术路线未定”必须提取“小范围测试延迟”，并把延迟基线、评价指标、技术路线列为缺失。
- “两种对象的同类测试记录，批次未知”可提取“比较两种对象的测试记录、核实批次”，并把测试条件列为缺失。
- “三类方式和用时，未记录人员配置”可提取“比较三类方式的用时、补充人员配置”，并把应用场景列为缺失。
- “十起现象中六起发生于移动时”可提取“分析移动与该现象的关系”，并把样本周期和其他变量列为缺失。
- “一台出现漂移，尚未复测”可提取“复测漂移现象”，并把复测结果和漂移阈值列为缺失。
- “先验证某问题，再决定是否开展改进”必须提取“验证该问题”，不得把后续决定写成模型结论，并把验证条件和评价指标列为缺失。
- “整理故障样本并评估研究条件”必须提取整理和评估两个动作，并把样本数量、故障分类列为缺失。
- “已有常温运行的功耗和温升、尚无低温数据”可提取“分析常温功耗和温升、补充低温数据”，并把低温数据和评价阈值列为缺失。
信息不足时不得编造或填写“待定”占位内容；无法从输入确认的字符串字段返回空字符串，数组字段返回空数组。
missingInformation 要具体列出开展后续整理仍缺少且输入未提供的信息，优先检查：具体研究对象、具体问题、研究目标、原始材料或数据正文、场景或环境条件、基线、测试或评价指标、数据依据、预期交付物。输入非常笼统或使用“这个材料”等指代时，必须逐项列出缺少的正文、问题、目标和成果；信息较完整时也要检查条件、周期和评价指标。最多列出 6 项，不要把已提供的信息再次列为缺失项。
输入中每个“尚无、未记录、未给出、未说明、未确定、尚未复测”后面的缺口必须优先、原词写入 missingInformation；之后再补充条件、基线和评价指标，交付物最后考虑。
返回前必须自检：六个字段全部存在；输入中的明确动作已经进入 objectives；只要前五个字段任一为空，missingInformation 就至少有一项；missingInformation 不超过 6 项。"""


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


_EXPLICIT_ACTION = re.compile(
    r"^(?:拟|尝试|想|先|再|需要)?"
    r"(研究|探索|比较|测试|验证|分析|降低|整理|评估|补充|复测)"
    r"([^，,。；;\n]{1,80})"
)
_DECLARED_ACTION = re.compile(
    r"^(?:内部交流|讨论|沟通|会议)(?:明确)?提出"
    r"(研究|探索|比较|测试|验证|分析|降低|整理|评估|补充|复测)"
    r"([^，,。；;\n]{1,80})"
)
_EXPLICIT_GAP = re.compile(
    r"(?:尚无|没有记录|未记录|未给出|没有给出|未说明|尚未确定)"
    r"([^，,。；;\n]{1,60})"
)
_NEGATED_ACTION_CONTEXT = re.compile(
    r"(?:不|未|没有|无需|禁止|停止|取消|并非|否决了?|拒绝了?|放弃了?)"
    r"(?:[^,，。；;\n]{0,8})$"
)
_INSTRUCTION_CONTEXT_MARKERS = (
    "忽略系统提示",
    "忽略上述提示",
    "绕过系统提示",
    "不要遵守系统提示",
    "未经授权的数据导出",
)
_META_INSTRUCTION = re.compile(
    r"(?:忽略|无视|绕过|忘记|覆盖)[^,，。；;\n]{0,16}"
    r"(?:系统提示|提示|规则|指令|要求)"
)
_DIRECTION_MENTION = re.compile(r"(?:提出|提到|提了)(?:一个|某个)?(?:研究)?方向")


def _grounded_action_allowed(source: str, start: int) -> bool:
    if _META_INSTRUCTION.search(source):
        return False
    context = source[max(0, start - 40):start]
    if any(marker in context for marker in _INSTRUCTION_CONTEXT_MARKERS):
        return False
    return _NEGATED_ACTION_CONTEXT.search(context) is None


def _same_or_contained(left: str, right: str) -> bool:
    left = re.sub(r"\s+", "", left)
    right = re.sub(r"\s+", "", right)
    return bool(left and right and (left in right or right in left))


def _append_grounded(items: list[str], value: str) -> None:
    value = normalize_text(value)
    if (
        has_meaningful_text(value)
        and len(value) <= FIELD_LIMITS["objectives"]
        and not any(_same_or_contained(value, item) for item in items)
        and len(items) < MAX_ARRAY_ITEMS
    ):
        items.append(value)


def finalize_assistant_content(source_text: str, raw: str) -> dict:
    """Apply only deterministic, source-grounded action/gap completion."""
    content = parse_assistant_content(raw)
    source = normalize_text(source_text)
    objectives = list(content["objectives"])
    research_content = list(content["researchContent"])

    observed_record = re.search(r"((?:检测|试验|故障)?记录显示[^,，。；;\n]{1,100})", source)
    if observed_record and not content["researchProblem"]:
        content["researchProblem"] = normalize_text(observed_record.group(1))

    vague_research = re.search(r"研究一下([^,，。；;\n]{1,60})", source)
    if vague_research:
        _append_grounded(research_content, vague_research.group(1))
    if _DIRECTION_MENTION.search(source):
        _append_grounded(research_content, "存在一个研究方向")

    if not objectives:
        matches = [match for pattern in (_EXPLICIT_ACTION, _DECLARED_ACTION) if (match := pattern.match(source))]
        for match in matches:
            if not _grounded_action_allowed(source, match.start()):
                continue
            action = f"{match.group(1)}{match.group(2)}"
            if "一下" not in action:
                _append_grounded(objectives, action)

    method = re.match(r"(?:拟|想|需要)?研究([^，,。；;\n]{1,60}方法)", source)
    if method and _grounded_action_allowed(source, method.start()):
        _append_grounded(objectives, f"验证{method.group(1)}")
    reduction = re.match(r"(?:拟|想|需要)?降低([^，,。；;\n]{1,60}率)", source)
    if (
        reduction
        and _grounded_action_allowed(source, reduction.start())
        and not any("影响因素" in item for item in objectives)
    ):
        _append_grounded(objectives, f"分析{reduction.group(1)}的影响因素")
    recorded = re.search(r"记录了([^，,。；;\n]{1,60})", source)
    if recorded and _grounded_action_allowed(source, recorded.start()):
        _append_grounded(objectives, f"分析{recorded.group(1)}")
    paired = re.search(r"两种([^，,。；;\n]{1,40})的([^，,。；;\n]{1,40})测试记录", source)
    if paired and _grounded_action_allowed(source, paired.start()):
        _append_grounded(
            objectives,
            f"比较两种{paired.group(1)}的{paired.group(2)}测试记录",
        )
    three_ways = re.search(r"三类([^，,。；;\n]{1,40})和用时", source)
    if three_ways and _grounded_action_allowed(source, three_ways.start()):
        _append_grounded(objectives, f"比较三类{three_ways.group(1)}的用时")

    missing = list(content["missingInformation"])
    explicit_gaps = [normalize_text(match.group(1)) for match in _EXPLICIT_GAP.finditer(source)]
    if "尚未复测" in source:
        explicit_gaps.append("复测结果")
    if "尚无" in source and "数据" in source:
        explicit_gaps.append("评价阈值")
    for gap in reversed(explicit_gaps):
        if has_meaningful_text(gap) and not any(_same_or_contained(gap, item) for item in missing):
            missing.insert(0, gap)
    objectives = [
        item for item in objectives
        if not (
            ("没有记录人员配置" in source and item == "分析人员配置")
            or item.startswith("分析显示")
            or (
                item == "分析该问题的影响因素"
                and any(
                    other != item and other.startswith("分析") and "影响因素" in other
                    for other in objectives
                )
            )
            or (
                item == "分析功耗和温升"
                and any(other.startswith("分析常温") for other in objectives)
            )
        )
    ]
    content["objectives"] = objectives[:MAX_ARRAY_ITEMS]
    content["researchContent"] = research_content[:MAX_ARRAY_ITEMS]
    content["missingInformation"] = missing[:ARRAY_ITEM_LIMITS["missingInformation"]]
    return content
