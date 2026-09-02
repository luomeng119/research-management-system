from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from app.ai.deepseek import DeepSeekProposalAssistant
from app.tests.ai_eval.dataset import DATASET_SHA256, DATASET_VERSION, load_samples
from app.tests.ai_eval.scorer import evaluate


def main() -> int:
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
        error = None
        try:
            raw = provider.generate(
                sample["input"], deadline_seconds=60, cancel_check=lambda: False
            )
        except Exception as caught:
            error = {"type": type(caught).__name__, "message": str(caught)[:160]}
        metadata = dict(provider.last_metadata)
        records.append({
            "sampleId": sample["id"],
            "providerKind": "DEEPSEEK",
            "modelVersion": metadata.get("model") or args.model,
            "rawOutput": raw,
            "outputSha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "latencySeconds": round(time.monotonic() - started, 3),
            "inputTokens": metadata.get("inputTokens"),
            "outputTokens": metadata.get("outputTokens"),
            "error": error,
        })
    result = {
        "runAt": datetime.now(timezone.utc).isoformat(),
        "datasetVersion": DATASET_VERSION,
        "datasetSha256": DATASET_SHA256,
        "promptVersion": "proposal-v1",
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
