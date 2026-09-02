"""Read-only legacy SQLite migration into the V1 PostgreSQL schema."""

from .core import (
    BatchConflict,
    ConversionIssue,
    SourceSafetyError,
    StructuralMigrationError,
    build_manifest,
    convert_bool,
    convert_date,
    convert_datetime,
    convert_decimal,
    convert_json,
    discover_sources,
    inventory_attachments,
    migrate_legacy,
    verify_completed_batch,
)

__all__ = [
    "BatchConflict", "ConversionIssue", "SourceSafetyError", "build_manifest",
    "convert_bool", "convert_date", "convert_datetime", "convert_decimal",
    "convert_json", "discover_sources", "inventory_attachments", "migrate_legacy",
    "StructuralMigrationError", "verify_completed_batch",
]
