#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import check_schema_version, create_runtime_engine, get_migration_database_url
from app.legacy_migration import verify_completed_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a completed legacy migration batch")
    parser.add_argument("source_root")
    parser.add_argument("batch_key")
    parser.add_argument("--storage-root", required=True)
    args = parser.parse_args()
    engine = create_runtime_engine(get_migration_database_url())
    try:
        check_schema_version(engine)
        report = verify_completed_batch(
            engine, args.source_root, args.batch_key,
            storage_root=args.storage_root,
        )
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
