from __future__ import annotations

import json
from pathlib import Path


DATASET_VERSION = "proposal-assistant-eval-v1"
DATASET_SHA256 = "88e8f2bb7453c2c880ece6b2c44f1f047b1ef44b81ca8bf867c145fb6d7c1893"


def load_samples() -> list[dict]:
    return json.loads(
        Path(__file__).with_name("samples.json").read_text(encoding="utf-8")
    )
