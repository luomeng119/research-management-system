from __future__ import annotations

import hashlib
import json
import math
import re

from app.ai.contract import parse_assistant_content
from app.tests.ai_eval.dataset import DATASET_SHA256, DATASET_VERSION, load_samples


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _missing_item_is_request(item: str, forbidden_term: str) -> bool:
    if forbidden_term not in item:
        return True
    text = re.sub(r"\s+", "", item)
    term = re.escape(forbidden_term)
    label = rf"(?:具体)?{term}(?:或(?:类型|型号|规格))?"
    return any(re.fullmatch(pattern, text) is not None for pattern in (
        label,
        rf"(?:请提供|请补充|需补充|需要补充|待补充|待核实|需核实){label}(?:信息)?",
        rf"{label}(?:未提供|未说明|缺失|待补充|待核实)",
    ))


def _valid_review_annotation(sample: dict, value: dict) -> bool:
    if not isinstance(value, dict) or type(value.get("severeHallucination")) is not bool:
        return False
    reviewers = value.get("reviewers")
    if (
        not isinstance(reviewers, list)
        or len({item.strip() for item in reviewers if isinstance(item, str) and item.strip()}) < 2
    ):
        return False
    count_keys = (
        "atomicFactsCorrect", "totalAtomicFacts", "fieldPointsCovered",
        "totalFieldPoints", "missingCovered", "totalMissing",
    )
    if any(type(value.get(key)) is not int or value[key] < 0 for key in count_keys):
        return False
    if (
        value["atomicFactsCorrect"] > value["totalAtomicFacts"]
        or value["fieldPointsCovered"] > value["totalFieldPoints"]
        or value["missingCovered"] > value["totalMissing"]
    ):
        return False
    return (
        value["totalFieldPoints"] == len(sample["expectedFieldPoints"])
        and value["totalMissing"] == len(sample["requiredMissingInformation"])
    )


def evaluate(records: list[dict], annotations: dict | None = None) -> dict:
    samples = load_samples()
    by_id = {sample["id"]: sample for sample in samples}
    provider_kinds = {record.get("providerKind") for record in records}
    base = {
        "datasetVersion": DATASET_VERSION,
        "datasetSha256": DATASET_SHA256,
        "sampleCount": len(records),
    }
    if not records or "FAKE" in provider_kinds:
        return {**base, "status": "NOT_RUN", "reason": "真实模型未运行，Fake Adapter 不计入模型质量"}
    if (
        provider_kinds not in ({"DEEPSEEK"}, {"LOCAL"})
        or len(records) != 20
        or set(record.get("sampleId") for record in records) != set(by_id)
    ):
        return {**base, "status": "INVALID_RUN", "reason": "必须一次覆盖冻结的 20 条样本"}

    schema_valid = 0
    forbidden_absent = 0
    latencies = []
    token_limits_ok = True
    output_hashes_ok = True
    provider_output_hashes_ok = True
    provider_outputs_auditable = True
    for record in records:
        raw = record.get("finalizedOutput", record.get("rawOutput", ""))
        parsed = None
        try:
            parsed = parse_assistant_content(raw)
            schema_valid += 1
        except Exception:
            pass
        sample = by_id[record["sampleId"]]
        # Asking the user to supply a missing forbidden item (for example a
        # material name) is not the same as generating that item. Only score
        # model assertions/suggestions; keep raw fallback for invalid output.
        forbidden_text = raw
        if parsed is not None:
            forbidden_text = json.dumps(
                {key: value for key, value in parsed.items() if key != "missingInformation"},
                ensure_ascii=False,
            )
        forbidden_found = any(
            term in forbidden_text for term in sample["forbiddenGeneration"]
        )
        if parsed is not None and not forbidden_found:
            forbidden_found = any(
                term in item and not _missing_item_is_request(item, term)
                for item in parsed["missingInformation"]
                for term in sample["forbiddenGeneration"]
            )
        forbidden_absent += int(not forbidden_found)
        latencies.append(float(record.get("latencySeconds", 999)))
        input_tokens = record.get("inputTokens")
        output_tokens = record.get("outputTokens")
        token_limits_ok = (
            token_limits_ok
            and type(input_tokens) is int and 0 <= input_tokens <= 8000
            and type(output_tokens) is int and 0 <= output_tokens <= 2000
        )
        expected_hash = record.get(
            "finalizedOutputSha256", record.get("outputSha256")
        )
        output_hashes_ok = output_hashes_ok and expected_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if record.get("providerRawOutputAvailable") is True:
            provider_raw = record.get("providerRawOutput")
            provider_output_hashes_ok = (
                provider_output_hashes_ok
                and isinstance(provider_raw, str)
                and record.get("providerOutputSha256")
                == hashlib.sha256(provider_raw.encode("utf-8")).hexdigest()
            )
        else:
            provider_outputs_auditable = False

    automatic = {
        "schemaValid": schema_valid,
        "forbiddenAbsent": forbidden_absent,
        "p95LatencySeconds": _p95(latencies),
        "hardTimeoutObserved": max(latencies) if latencies else None,
        "tokenLimitsOk": token_limits_ok,
        "outputHashesOk": output_hashes_ok,
        "providerOutputHashesOk": provider_output_hashes_ok,
        "providerOutputsAuditable": provider_outputs_auditable,
    }
    annotations = annotations or {}
    complete = set(annotations) == set(by_id) and all(
        sample_id in annotations
        and isinstance(annotations[sample_id], dict)
        and len(set(annotations[sample_id].get("reviewers", []))) >= 2
        and all(key in annotations[sample_id] for key in (
            "severeHallucination", "atomicFactsCorrect", "totalAtomicFacts",
            "fieldPointsCovered", "totalFieldPoints", "missingCovered", "totalMissing",
        ))
        for sample_id in by_id
    )
    if not complete:
        if set(annotations) == set(by_id):
            return {**base, "status": "INVALID_REVIEW", "automatic": automatic}
        return {**base, "status": "PENDING_REVIEW", "automatic": automatic}

    if not all(
        _valid_review_annotation(by_id[sample_id], annotations[sample_id])
        for sample_id in by_id
    ):
        return {**base, "status": "INVALID_REVIEW", "automatic": automatic}

    values = list(annotations.values())
    severe = sum(bool(value["severeHallucination"]) for value in values)
    facts = _ratio(sum(value["atomicFactsCorrect"] for value in values), sum(value["totalAtomicFacts"] for value in values))
    points = _ratio(sum(value["fieldPointsCovered"] for value in values), sum(value["totalFieldPoints"] for value in values))
    missing = _ratio(sum(value["missingCovered"] for value in values), sum(value["totalMissing"] for value in values))
    review = {
        "severeHallucinations": severe,
        "atomicFactAccuracy": facts,
        "fieldPointCoverage": points,
        "missingInformationRecall": missing,
    }
    passed = (
        schema_valid == 20 and forbidden_absent == 20 and severe == 0
        and facts is not None and facts >= 0.95
        and points is not None and points >= 0.90
        and missing is not None and missing >= 0.90
        and automatic["p95LatencySeconds"] <= 45
        and automatic["hardTimeoutObserved"] <= 60
        and token_limits_ok and output_hashes_ok
        and provider_output_hashes_ok and provider_outputs_auditable
    )
    return {
        **base,
        "status": "PASSED" if passed else "FAILED",
        "automatic": automatic,
        "review": review,
    }
