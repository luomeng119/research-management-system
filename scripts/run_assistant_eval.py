from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

# Support the documented direct entrypoint from the repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_evaluation_record(
    *, sample_id: str, provider_raw: str, finalized: str, model: str,
    metadata: dict, latency_seconds: float, error: dict | None,
    provider_output_available: bool = True,
) -> dict:
    return {
        "sampleId": sample_id,
        "providerKind": "DEEPSEEK",
        "modelVersion": metadata.get("model") or model,
        "providerRawOutput": provider_raw if provider_output_available else None,
        "providerRawOutputAvailable": provider_output_available,
        "providerOutputSha256": (
            hashlib.sha256(provider_raw.encode("utf-8")).hexdigest()
            if provider_output_available else None
        ),
        "finalizedOutput": finalized,
        "finalizedOutputSha256": hashlib.sha256(finalized.encode("utf-8")).hexdigest(),
        "latencySeconds": round(latency_seconds, 3),
        "inputTokens": metadata.get("inputTokens"),
        "outputTokens": metadata.get("outputTokens"),
        "error": error,
    }


def main() -> int:
    from app.ai.deepseek import DeepSeekProposalAssistant
    from app.ai.contract import PROMPT_VERSION, finalize_assistant_content
    from app.tests.ai_eval.dataset import DATASET_SHA256, DATASET_VERSION, load_samples
    from app.tests.ai_eval.scorer import evaluate

    parser = argparse.ArgumentParser(description="Run the frozen proposal assistant evaluation")
    parser.add_argument("--output", required=True, help="JSON result path")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))
    args = parser.parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        parser.error("DEEPSEEK_API_KEY is required")

    provider = DeepSeekProposalAssistant(api_key=key, model=args.model)
    records = []
    for sample in load_samples():
        started = time.monotonic()
        raw = ""
        provider_raw = ""
        error = None
        provider_output_available = False
        try:
            provider_raw = provider.generate(
                sample["input"], deadline_seconds=60, cancel_check=lambda: False
            )
            provider_output_available = True
            raw = json.dumps(
                finalize_assistant_content(sample["input"], provider_raw),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except Exception as caught:
            error = {"type": type(caught).__name__, "message": str(caught)[:160]}
        metadata = dict(provider.last_metadata)
        records.append(_build_evaluation_record(
            sample_id=sample["id"], provider_raw=provider_raw, finalized=raw,
            model=args.model, metadata=metadata,
            latency_seconds=time.monotonic() - started, error=error,
            provider_output_available=provider_output_available,
        ))
    result = {
        "runAt": datetime.now(timezone.utc).isoformat(),
        "datasetVersion": DATASET_VERSION,
        "datasetSha256": DATASET_SHA256,
        "promptVersion": PROMPT_VERSION,
        "records": records,
        "report": evaluate(records),
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    print(json.dumps({
        "output": str(destination),
        "status": result["report"]["status"],
        "samples": len(records),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
