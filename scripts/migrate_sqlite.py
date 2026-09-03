#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import check_schema_version, create_runtime_engine, get_migration_database_url
from app.legacy_migration import migrate_legacy, plan_legacy_binaries


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate the three read-only legacy SQLite databases")
    parser.add_argument("source_root")
    parser.add_argument("batch_key")
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-file-bytes", type=int, default=100 * 1024 * 1024)
    args = parser.parse_args()
    engine = create_runtime_engine(get_migration_database_url())
    try:
        check_schema_version(engine)
        if args.dry_run:
            report = plan_legacy_binaries(
                engine, args.source_root, args.storage_root,
                max_bytes=args.max_file_bytes,
            )
        else:
            report = migrate_legacy(
                engine, args.source_root, args.batch_key,
                storage_root=args.storage_root,
                max_file_bytes=args.max_file_bytes,
            )
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
