from __future__ import annotations

import hashlib
import json
import math

from app.ai.contract import parse_assistant_content
from app.tests.ai_eval.dataset import DATASET_SHA256, DATASET_VERSION, load_samples


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


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
    if len(records) != 20 or set(record.get("sampleId") for record in records) != set(by_id):
        return {**base, "status": "INVALID_RUN", "reason": "必须一次覆盖冻结的 20 条样本"}

    schema_valid = 0
    forbidden_absent = 0
    latencies = []
    token_limits_ok = True
    output_hashes_ok = True
    for record in records:
        raw = record.get("rawOutput", "")
        try:
            parse_assistant_content(raw)
            schema_valid += 1
        except Exception:
            pass
        sample = by_id[record["sampleId"]]
        forbidden_absent += int(not any(term in raw for term in sample["forbiddenGeneration"]))
        latencies.append(float(record.get("latencySeconds", 999)))
        input_tokens = record.get("inputTokens")
        output_tokens = record.get("outputTokens")
        token_limits_ok = (
            token_limits_ok
            and isinstance(input_tokens, int) and input_tokens <= 8000
            and isinstance(output_tokens, int) and output_tokens <= 2000
        )
        output_hashes_ok = output_hashes_ok and record.get("outputSha256") == hashlib.sha256(raw.encode("utf-8")).hexdigest()

    automatic = {
        "schemaValid": schema_valid,
        "forbiddenAbsent": forbidden_absent,
        "p95LatencySeconds": _p95(latencies),
        "hardTimeoutObserved": max(latencies) if latencies else None,
        "tokenLimitsOk": token_limits_ok,
        "outputHashesOk": output_hashes_ok,
    }
    annotations = annotations or {}
    complete = all(
        sample_id in annotations
        and len(set(annotations[sample_id].get("reviewers", []))) >= 2
        and all(key in annotations[sample_id] for key in (
            "severeHallucination", "atomicFactsCorrect", "totalAtomicFacts",
            "fieldPointsCovered", "totalFieldPoints", "missingCovered", "totalMissing",
        ))
        for sample_id in by_id
    )
    if not complete:
        return {**base, "status": "PENDING_HUMAN_REVIEW", "automatic": automatic}

    values = list(annotations.values())
    severe = sum(bool(value["severeHallucination"]) for value in values)
    facts = _ratio(sum(value["atomicFactsCorrect"] for value in values), sum(value["totalAtomicFacts"] for value in values))
    points = _ratio(sum(value["fieldPointsCovered"] for value in values), sum(value["totalFieldPoints"] for value in values))
    missing = _ratio(sum(value["missingCovered"] for value in values), sum(value["totalMissing"] for value in values))
    human = {
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
    )
    return {**base, "status": "PASSED" if passed else "FAILED", "automatic": automatic, "human": human}
