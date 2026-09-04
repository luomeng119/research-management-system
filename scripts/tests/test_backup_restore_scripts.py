import importlib
from pathlib import Path
import sys

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = (SCRIPTS_DIR / "backup.ps1").read_text(encoding="utf-8")
RESTORE_SCRIPT = (SCRIPTS_DIR / "restore.ps1").read_text(encoding="utf-8")


def test_backup_requires_maintenance_window_and_uses_custom_postgres_dump():
    """Catches taking a live-write backup or producing a non-custom database dump."""
    assert "research-management.pid.json" in BACKUP_SCRIPT
    assert "[switch]$ConfirmMaintenanceWindow" in BACKUP_SCRIPT
    assert "refusing to back up while the application is recorded as running" in BACKUP_SCRIPT.lower()
    assert '"--pg-dump-exe"' in BACKUP_SCRIPT
    assert '"--dump-output"' in BACKUP_SCRIPT
    assert "pg_dump" in BACKUP_SCRIPT.lower()


def test_backup_uses_validated_deployment_dpapi_and_only_bundled_pg_dump():
    assert '"deployment.json"' in BACKUP_SCRIPT
    assert "Read-ValidatedDeployment" in BACKUP_SCRIPT
    assert "runtimePasswordProtected" in BACKUP_SCRIPT
    assert "ConvertTo-SecureString" in BACKUP_SCRIPT
    assert 'Get-RequiredEnvironmentValue "DATABASE_URL"' not in BACKUP_SCRIPT
    assert 'Join-Path ([string]$Deployment.postgresRuntime) "bin\\pg_dump.exe"' in BACKUP_SCRIPT
    assert "$env:PG_DUMP_EXE" not in BACKUP_SCRIPT
    assert 'FallbackName "pg_dump.exe"' not in BACKUP_SCRIPT
    assert 'databaseHost -ne "127.0.0.1"' in BACKUP_SCRIPT
    assert "must match the validated local deployment" in BACKUP_SCRIPT
    assert "must not use UNC or network storage" in BACKUP_SCRIPT
    assert "DriveType]::Network" in BACKUP_SCRIPT


def test_backup_always_clears_database_and_postgres_credentials():
    finally_body = BACKUP_SCRIPT.rsplit("finally {", 1)[1]
    for name in (
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "PGPASSWORD",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
    ):
        assert f'[Environment]::SetEnvironmentVariable("{name}", $null, "Process")' in finally_body


def test_backup_exposes_runtime_database_url_only_to_snapshot_process_environment():
    snapshot = BACKUP_SCRIPT.index('"snapshot"')
    set_url = BACKUP_SCRIPT.rindex("$env:DATABASE_URL = $DatabaseUrl", 0, snapshot)
    assert set_url < snapshot
    assert '"--database-url"' not in BACKUP_SCRIPT
    assert '"--dbname=$DatabaseUrl"' not in BACKUP_SCRIPT


def test_backup_cleanup_requires_run_bound_canonical_ownership_markers():
    assert "$RunToken = [Guid]::NewGuid().ToString(\"N\")" in BACKUP_SCRIPT
    assert "New-OwnershipMarker" in BACKUP_SCRIPT
    assert "Assert-OwnershipMarker" in BACKUP_SCRIPT
    assert "canonicalTarget" in BACKUP_SCRIPT
    assert "runToken" in BACKUP_SCRIPT
    assert "Remove-OwnedDirectory" in BACKUP_SCRIPT
    assert "Remove-OwnedFile" in BACKUP_SCRIPT
    assert "$StageRootOwned = $true" in BACKUP_SCRIPT
    assert "$ValidationRootOwned = $true" in BACKUP_SCRIPT
    create_zip = BACKUP_SCRIPT.index("CreateFromDirectory")
    partial_owned = BACKUP_SCRIPT.index("$TemporaryOutputOwned = $true", create_zip)
    assert create_zip < partial_owned
    finally_body = BACKUP_SCRIPT.rsplit("finally {", 1)[1]
    assert "Remove-OwnedDirectory -Target $StageRoot -Marker $StageMarker -Token $RunToken" in finally_body
    assert "Remove-OwnedDirectory -Target $ValidationRoot -Marker $ValidationMarker -Token $RunToken" in finally_body
    assert "Remove-OwnedFile -Target $TemporaryOutputPath -Marker $TemporaryOutputMarker -Token $RunToken" in finally_body


def test_backup_copies_only_the_three_business_file_roots_and_seals_sha256_manifest():
    """Catches omitting legacy attachments or recursively copying the backup directory."""
    for relative in ('"data\\files"', '"uploads"', '"documents"', '"data\\documents"', '"data\\templates"'):
        assert relative in BACKUP_SCRIPT
    assert '"data\\research.db"' in BACKUP_SCRIPT
    for suffix in ("research.db", "research.db-wal", "research.db-shm", "research.db-journal"):
        assert suffix in BACKUP_SCRIPT
    assert '"backups"' not in BACKUP_SCRIPT.split("$BusinessRoots = @(", 1)[1].split(")", 1)[0]
    assert '"snapshot"' in BACKUP_SCRIPT
    assert '"--package-root"' in BACKUP_SCRIPT
    assert "verify-package" in BACKUP_SCRIPT
    assert "Backup output cannot be inside a business file root" in BACKUP_SCRIPT
    assert '"--pg-dump-exe"' in BACKUP_SCRIPT
    assert '"--dump-output"' in BACKUP_SCRIPT
    assert "& $PgDumpExe" not in BACKUP_SCRIPT
    assert "legacy project-relative data\\$LegacyName exists outside APP_DATA_ROOT" in BACKUP_SCRIPT


def test_restore_is_explicit_staging_only_and_refuses_nonempty_targets():
    """Catches restoring over a live database or existing business files."""
    assert "[switch]$ConfirmStaging" in RESTORE_SCRIPT
    assert "restore is staging-only" in RESTORE_SCRIPT.lower()
    assert "pg_catalog.pg_class" in RESTORE_SCRIPT
    assert "pg_catalog.pg_proc" in RESTORE_SCRIPT
    assert "pg_catalog.pg_type" in RESTORE_SCRIPT
    assert "must be empty" in RESTORE_SCRIPT.lower()
    assert "Test-DirectoryEmpty" in RESTORE_SCRIPT
    assert "Remove-Item -Recurse" not in RESTORE_SCRIPT
    assert "--clean" not in RESTORE_SCRIPT
    assert '"--single-transaction"' in RESTORE_SCRIPT
    assert '"restore.failed.json"' in RESTORE_SCRIPT
    assert "This staging target is marked failed" in RESTORE_SCRIPT
    for relative in ('"data\\files"', '"uploads"', '"documents"', '"data\\documents"', '"data\\templates"'):
        assert relative in RESTORE_SCRIPT
    assert '"data\\research.db"' in RESTORE_SCRIPT
    assert "legacy project-relative data\\$LegacyName exists outside APP_DATA_ROOT" in RESTORE_SCRIPT
    legacy_guard = RESTORE_SCRIPT.split("$ProjectLegacyDatabase =", 1)[1].split("$FailedMarker =", 1)[0]
    assert "Test-Path -LiteralPath $ProjectLegacyDatabase -PathType Leaf" in legacy_guard
    for suffix in ("research.db", "research.db-wal", "research.db-shm", "research.db-journal"):
        assert suffix in RESTORE_SCRIPT.split("$LegacyRelativeFiles =", 1)[1].split("foreach ($LegacyName", 1)[0]


def test_restore_verifies_archive_before_pg_restore_and_live_state_afterwards():
    """Catches mutating the staging database before checking archive hashes."""
    package_check = RESTORE_SCRIPT.index('"verify-package"')
    pg_restore = RESTORE_SCRIPT.index("pg_restore")
    live_check = RESTORE_SCRIPT.rindex('"verify"')
    assert package_check < pg_restore < live_check
    assert '"--exit-on-error"' in RESTORE_SCRIPT
    assert '"--no-owner"' in RESTORE_SCRIPT
    assert '"--no-privileges"' in RESTORE_SCRIPT
    assert '"--no-password"' in RESTORE_SCRIPT
    assert '"--list"' in RESTORE_SCRIPT


def test_restore_uses_owner_for_restore_then_reprovisions_runtime_acl():
    """Catches restoring as the least-privilege runtime role or skipping ACL repair."""
    assert 'Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"' in RESTORE_SCRIPT
    assert 'Get-RequiredEnvironmentValue "DATABASE_URL"' in RESTORE_SCRIPT
    assert "$RuntimeRole" in RESTORE_SCRIPT
    owner_environment = RESTORE_SCRIPT.index("Set-PostgresEnvironment -DatabaseUrl $MigrationDatabaseUrl")
    empty_check = RESTORE_SCRIPT.index("$TableCountOutput =", owner_environment)
    restore = RESTORE_SCRIPT.index("& $PgRestoreExe", empty_check)
    provision = RESTORE_SCRIPT.index('"provision_postgres.py"', restore)
    live_verify = RESTORE_SCRIPT.rindex('"verify"')
    assert owner_environment < empty_check < restore < provision < live_verify
    assert '"--runtime-role", $RuntimeRole' in RESTORE_SCRIPT
    assert '"--migration-url"' not in RESTORE_SCRIPT
    assert '"--migration-url", $MigrationDatabaseUrl' not in RESTORE_SCRIPT
    assert "DATABASE_URL and MIGRATION_DATABASE_URL must target the same staging database" in RESTORE_SCRIPT
    assert "runtime role must differ" in RESTORE_SCRIPT
    assert "$Uri.Query" in RESTORE_SCRIPT
    assert "$Uri.Fragment" in RESTORE_SCRIPT
    assert "must not include query parameters or fragments" in RESTORE_SCRIPT
    assert "fall back" not in RESTORE_SCRIPT.lower()


def test_provision_cli_reads_owner_url_from_environment_without_command_line_secret(monkeypatch):
    """Catches requiring the owner password in the process argument list."""
    module = importlib.import_module("scripts.provision_postgres")
    observed = {}

    def provision(migration_url, runtime_role):
        observed.update(migration_url=migration_url, runtime_role=runtime_role)
        return "owner_role", "research_db"

    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://owner:secret@db/research_db")
    monkeypatch.setattr(module, "provision", provision)
    monkeypatch.setattr(sys, "argv", ["provision_postgres.py", "--runtime-role", "runtime_role"])
    module.main()

    assert observed == {
        "migration_url": "postgresql+psycopg://owner:secret@db/research_db",
        "runtime_role": "runtime_role",
    }


def test_provision_cli_fails_closed_without_owner_credential(monkeypatch):
    module = importlib.import_module("scripts.provision_postgres")
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["provision_postgres.py", "--runtime-role", "runtime_role"])

    with pytest.raises(SystemExit) as failure:
        module.main()

    assert failure.value.code == 2


@pytest.mark.parametrize(
    "suffix",
    ["?host=other", "?port=6543", "?dbname=otherdb", "#unexpected"],
)
def test_provision_rejects_url_components_that_can_change_the_effective_target(suffix):
    module = importlib.import_module("scripts.provision_postgres")

    with pytest.raises(ValueError, match="query parameters or fragments"):
        module._psycopg_url(
            "postgresql+psycopg://owner:secret@localhost/research_db" + suffix
        )


def test_database_credentials_are_not_passed_on_process_command_lines():
    """Catches exposing the PostgreSQL password in pg_dump/pg_restore/psql arguments."""
    for script in (BACKUP_SCRIPT, RESTORE_SCRIPT):
        assert "$env:PGPASSWORD" in script
        assert "--dbname=$DatabaseUrl" not in script
        assert "-d $DatabaseUrl" not in script


def test_scripts_reject_reparse_points_before_copy_or_extract():
    """Catches copying through a junction outside the verified runtime tree."""
    assert "Assert-NoReparsePath" in BACKUP_SCRIPT
    assert "FileAttributes]::ReparsePoint" in BACKUP_SCRIPT
    assert BACKUP_SCRIPT.index("Assert-NoReparsePath") < BACKUP_SCRIPT.index("Copy-DirectoryContents")
    assert "Assert-NoReparsePath" in RESTORE_SCRIPT
    assert "FileAttributes]::ReparsePoint" in RESTORE_SCRIPT
    assert RESTORE_SCRIPT.index("Assert-NoReparsePath") < RESTORE_SCRIPT.index('"extract"')
    for script in (BACKUP_SCRIPT, RESTORE_SCRIPT):
        tree_function = script.split("function Assert-NoReparseTree", 1)[1].split("function ", 1)[0]
        assert "Get-ChildItem -LiteralPath $Current -Force" in tree_function
        assert "-Recurse" not in tree_function
        assert "System.Collections.Generic.Stack[string]" in tree_function
        assert tree_function.index("FileAttributes]::ReparsePoint") < tree_function.index("$Pending.Push($_.FullName)")


def test_final_zip_is_reextracted_and_verified_and_partial_output_is_removed():
    """Catches announcing success for a truncated archive produced during compression."""
    create = BACKUP_SCRIPT.index("CreateFromDirectory")
    extract = BACKUP_SCRIPT.index('"extract"', create)
    verify = BACKUP_SCRIPT.index('"verify-package"', extract)
    publish = BACKUP_SCRIPT.index("[IO.File]::Move($TemporaryOutputPath, $OutputPath)")
    success = BACKUP_SCRIPT.index("Verified maintenance-window backup created")
    assert create < extract < verify < publish < success
    assert "[IO.File]::Delete($OutputPath)" not in BACKUP_SCRIPT
    assert "Remove-OwnedFile -Target $TemporaryOutputPath" in BACKUP_SCRIPT
    assert '".partial-{0}.zip"' in BACKUP_SCRIPT
    created_guard = BACKUP_SCRIPT.index("$TemporaryOutputOwned = $true")
    assert create < created_guard
