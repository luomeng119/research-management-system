from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_bundle_contract_requires_complete_postgresql_server_runtime():
    builder = _text("build_offline_bundle.py")
    for executable in (
        "initdb.exe",
        "pg_ctl.exe",
        "postgres.exe",
        "psql.exe",
        "pg_dump.exe",
        "pg_restore.exe",
    ):
        assert f'"runtime/postgresql/bin/{executable}"' in builder
    assert '"runtime/postgresql/bin/libpq.dll"' in builder
    assert '"runtime/postgresql/lib"' in builder
    assert '"runtime/postgresql/share"' in builder
    assert 'component="postgresql-server-runtime"' in builder
    assert '"postgresqlServerVersion"' in builder
    assert '"postgresqlClientVersion"' not in builder
    assert '"test-runtime/node/node.exe"' in builder
    assert '"test-runtime/node_modules/@playwright/test/cli.js"' in builder
    assert '"test-runtime/playwright-browsers"' in builder


def test_install_bootstraps_local_cluster_without_putting_secrets_in_argv():
    install = _text("install.ps1")
    assert 'Get-RequiredEnvironmentValue "APP_DATA_ROOT"' in install
    assert 'Join-Path (Join-Path $DataRoot "postgresql") "data"' in install
    assert '"deployment.json"' in install
    assert "ConvertFrom-SecureString" in install
    assert "ConvertTo-SecureString" in install
    assert "SetAccessRuleProtection" in install
    assert "initdb.exe" in install
    assert "pg_ctl.exe" in install
    assert "postgres.exe" in install
    assert "libpq.dll" in install
    assert '"lib"' in install
    assert '"share"' in install
    assert "PostgreSQL server runtime directory is missing or empty" in install
    assert '"--auth-local=trust"' in install
    assert '"--auth-host=reject"' in install
    assert '"--single", "-D", $PostgresData, ([string]$BootstrapCommand.Database)' in install
    assert "$BootstrapSql | & $PostgresExe" in install
    assert '"pg_hba.conf"' in install
    assert "scram-sha-256" in install
    assert "PasswordFile" not in install
    assert "BootstrapSqlFile" not in install
    assert '"--command=' not in install
    assert "--password=$" not in install
    assert "--pwfile" not in install


def test_install_validates_exact_local_database_boundary_and_separate_roles():
    install = _text("install.ps1")
    assert "Assert-LocalDatabaseUrl" in install
    assert "$Uri.Query" in install
    assert "$Uri.Fragment" in install
    assert "must not include query parameters or fragments" in install
    assert "must target this local PostgreSQL cluster" in install
    assert "BootstrapRole, OwnerRole, and RuntimeRole must all differ" in install
    assert "MIGRATION_DATABASE_URL and DATABASE_URL must target the same local database" in install
    assert '"-m", "alembic", "upgrade", "head"' in install
    provision = install.index('"provision_postgres.py"')
    alembic = install.index('"-m", "alembic", "upgrade", "head"')
    assert alembic < provision


def test_install_demotes_separate_bootstrap_role_before_network_start():
    install = _text("install.ps1")
    assert '[string]$BootstrapRole = "rm_bootstrap"' in install
    assert "BootstrapRole, OwnerRole, and RuntimeRole must all differ" in install
    assert '"--username", $BootstrapRole' in install
    assert "CREATE ROLE $OwnerRole LOGIN NOSUPERUSER" in install
    assert "ALTER ROLE $BootstrapRole NOSUPERUSER" in install
    assert "NOLOGIN PASSWORD NULL" in install
    demotion = install.index("ALTER ROLE $BootstrapRole NOSUPERUSER")
    network_start = install.index('"start", "-D", $PostgresData')
    assert demotion < network_start


def test_install_claims_created_targets_before_fallible_initialization_and_acl():
    install = _text("install.ps1")
    assert install.index("$PostgresDataCreated = $true") < install.index(
        "Invoke-Checked -Executable $InitDbExe"
    )
    assert install.index("$DeploymentStateCreated = $true") < install.index(
        "Set-PrivateAcl -Path $DeploymentState"
    )


def test_install_rejects_nonlocal_or_reparse_data_root_and_verifies_private_acl():
    install = _text("install.ps1")
    assert "Assert-LocalNoReparsePath" in install
    assert "DriveType" in install
    assert "Network" in install
    assert "ReparsePoint" in install
    assert "APP_DATA_ROOT must be empty before installation" in install
    assert "SetAccessRuleProtection" in install
    assert "AreAccessRulesProtected" in install
    assert "Private ACL verification failed" in install
    assert "Set-PrivateAcl -Path $DataRoot -Directory" in install


def test_install_cleanup_requires_per_run_ownership_markers():
    install = _text("install.ps1")
    assert "$OwnershipToken = [Guid]::NewGuid().ToString(\"N\")" in install
    assert "New-OwnershipMarker" in install
    assert "Remove-OwnedTree" in install
    assert "Ownership marker does not belong to this install run" in install
    assert install.index("New-OwnershipMarker -Target $PostgresData") < install.index(
        "Invoke-Checked -Executable $InitDbExe"
    )
    assert "Remove-OwnedTree -Target $PostgresData" in install
    assert "Remove-Item -LiteralPath $PostgresData -Recurse" not in install


def test_install_secures_postgres_parent_before_data_marker():
    install = _text("install.ps1")
    parent_create = install.index("New-Item -ItemType Directory -Path $PostgresDataParent")
    parent_acl = install.index("Set-PrivateAcl -Path $PostgresDataParent -Directory")
    marker = install.index("New-OwnershipMarker -Target $PostgresData")
    assert parent_create < parent_acl < marker


def test_bootstrap_transfers_target_public_schema_before_demoting_bootstrap():
    install = _text("install.ps1")
    assert 'Database = $DatabaseName; Sql = "ALTER SCHEMA public OWNER TO $OwnerRole;"' in install
    schema_owner = install.index("ALTER SCHEMA public OWNER TO $OwnerRole")
    demotion = install.index("ALTER ROLE $BootstrapRole NOSUPERUSER")
    assert schema_owner < demotion
    assert '"--single", "-D", $PostgresData, ([string]$BootstrapCommand.Database)' in install


def test_install_retains_database_and_runtime_when_emergency_stop_fails():
    install = _text("install.ps1")
    assert "$PostgresStopConfirmed = $false" in install
    assert "PostgreSQL cleanup stop failed; PGDATA and runtime were retained" in install
    assert "if ($PostgresStopConfirmed -and $PostgresDataCreated" in install
    assert "if ($PostgresStopConfirmed -and $PostgresRuntimeCreated" in install


def test_ownership_marker_binds_canonical_target_and_is_moved_then_removed():
    install = _text("install.ps1")
    assert "ConvertTo-Json" in install
    assert "target = $CanonicalTarget" in install
    assert "token = $OwnershipToken" in install
    assert "Ownership marker target does not match" in install
    assert "Move-OwnershipMarker -Marker $PostgresRuntimeMarker -NewTarget $PostgresRuntime" in install
    assert "Move-OwnershipMarker -Marker $EnvironmentMarker -NewTarget $VirtualEnvironment" in install
    assert "Remove-OwnershipMarker -Marker $PostgresRuntimeMarker" in install
    assert "Remove-OwnershipMarker -Marker $PostgresDataMarker" in install
    assert "Remove-OwnershipMarker -Marker $EnvironmentMarker" in install


def test_start_and_stop_manage_only_the_recorded_local_postgresql_cluster():
    start = _text("start.ps1")
    stop = _text("stop.ps1")
    assert '"deployment.json"' in start
    assert "pg_ctl.exe" in start
    assert '"postgresql.pid.json"' in start
    assert "postmaster.pid" in start
    assert start.index("pg_ctl.exe") < start.index("Start-Process -FilePath $WaitressExe")
    assert "[switch]$DatabaseOnly" in start
    assert "[switch]$KeepPostgresRunning" in stop
    assert "[switch]$DatabaseOnly" in stop
    assert '"postgresql.pid.json"' in stop
    assert "postmaster.pid" in stop
    assert "different process instance" in stop
    assert '"stop", "-D", $PostgresData' in stop
    assert "taskkill" not in stop.lower()
    assert "Get-Process postgres" not in stop


def test_deployment_readers_bind_state_to_exact_local_nonreparse_paths():
    for name in ("start.ps1", "stop.ps1", "upgrade.ps1"):
        script = _text(name)
        assert "Assert-LocalNoReparsePath" in script
        assert "DriveType" in script
        assert "Network" in script
        assert "ReparsePoint" in script
        assert 'Join-Path (Join-Path $DataRoot "runtime") "postgresql"' in script
        assert 'Join-Path (Join-Path $DataRoot "postgresql") "data"' in script
        assert "must match the expected APP_DATA_ROOT location" in script
        assert "bootstrapRole" in script
        assert "^[a-z][a-z0-9_]{0,62}$" in script
    start = _text("start.ps1")
    stop = _text("stop.ps1")
    assert "PostgreSQL PID state dataRoot does not match" in start
    assert "PostgreSQL PID state dataRoot does not match" in stop


def test_postgresql_processes_do_not_inherit_application_or_owner_secrets():
    install = _text("install.ps1")
    start = _text("start.ps1")
    upgrade = _text("upgrade.ps1")
    for script in (install, start, upgrade):
        clear_owner = script.index(
            '[Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")'
        )
        pg_start = script.index('"start", "-D", $PostgresData')
        assert clear_owner < pg_start
    assert start.index(
        '[Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")'
    ) < start.index('"start", "-D", $PostgresData')
    assert start.index('"PGPASSWORD"') < start.index(
        "Start-Process -FilePath $WaitressExe"
    )
    assert start.index("if ($DatabaseOnly)") < start.index(
        "$RuntimePassword = Unprotect-Secret"
    )


def test_upgrade_uses_protected_local_owner_credential_and_stopped_cluster():
    upgrade = _text("upgrade.ps1")
    assert '"deployment.json"' in upgrade
    assert "ConvertTo-SecureString" in upgrade
    assert "SecureStringToBSTR" in upgrade
    assert 'Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"' not in upgrade
    assert "pg_ctl.exe" in upgrade
    assert "PostgreSQL cluster must be stopped before upgrading" in upgrade
    assert '[Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")' in upgrade


def test_acceptance_runs_fixed_fail_fast_production_sequence_with_evidence():
    acceptance = _text("acceptance.ps1")
    ordered = [
        'Invoke-AcceptanceStep "01-install"',
        'Invoke-AcceptanceStep "02-start"',
        'Invoke-AcceptanceStep "03-health"',
        'Invoke-AcceptanceStep "04-core-e2e"',
        'Invoke-AcceptanceStep "05-backup"',
        'Invoke-AcceptanceStep "06-stop"',
        'Invoke-AcceptanceStep "07-clear-test-data"',
        'Invoke-AcceptanceStep "08-restore"',
        'Invoke-AcceptanceStep "09-restart"',
        'Invoke-AcceptanceStep "10-consistency"',
        'Invoke-AcceptanceStep "11-outbound-scan"',
    ]
    positions = [acceptance.index(marker) for marker in ordered]
    assert positions == sorted(positions)
    assert "$ErrorActionPreference = \"Stop\"" in acceptance
    assert "evidence.json" in acceptance
    assert "outbound-requests.json" in acceptance
    assert "health/ready" in acceptance
    assert "@playwright\\test\\index.mjs" in acceptance
    assert "app:create_app" not in acceptance
    assert "application.run" not in acceptance
    assert "sqlite" not in acceptance.lower()
    assert "EvidenceRoot" in acceptance
    assert "APP_DATA_ROOT must be inside EvidenceRoot" in acceptance


def test_acceptance_requires_real_offline_browser_runtime():
    acceptance = _text("acceptance.ps1")
    assert '"test-runtime\\node\\node.exe"' in acceptance
    assert '"test-runtime\\node_modules\\@playwright\\test\\cli.js"' in acceptance
    assert '"test-runtime\\playwright-browsers"' in acceptance
    assert "Required offline Playwright runtime is missing" in acceptance


def test_acceptance_roots_are_local_nonreparse_and_recursive_cleanup_is_owned():
    acceptance = _text("acceptance.ps1")
    assert "Assert-LocalNoReparsePath" in acceptance
    assert "DriveType" in acceptance
    assert "Network" in acceptance
    assert "ReparsePoint" in acceptance
    assert "New-AcceptanceOwnershipMarker" in acceptance
    assert "Assert-AcceptanceOwnershipMarker" in acceptance
    assert "target = $CanonicalTarget" in acceptance
    assert "token = $AcceptanceOwnershipToken" in acceptance
    assert "Remove-OwnedSubtree" in acceptance
    assert "Ownership marker target does not match" in acceptance
    assert "Remove-Item -LiteralPath $Target -Recurse -Force" in acceptance
    assert (
        acceptance.index("Assert-AcceptanceOwnershipMarker -OwnershipRoot $OwnershipRoot")
        < acceptance.index("Remove-Item -LiteralPath $Target -Recurse -Force")
    )
    assert "if (Test-Path -LiteralPath $Target) { Remove-Item" not in acceptance


def test_acceptance_limits_password_visibility_and_disables_playwright_artifacts():
    acceptance = _text("acceptance.ps1")
    assert "Clear-SensitiveProcessEnvironment" in acceptance
    for sensitive_name in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH", "PASS"):
        assert sensitive_name in acceptance
    assert "Invoke-WithTemporaryEnvironment" in acceptance
    assert "Invoke-NodeWithSecretFromStdin" in acceptance
    assert "$SensitiveInput | & $Path" in acceptance
    assert "process.stdin" in acceptance
    assert "chromium.launch({ env: browserEnvironment })" in acceptance
    assert "ACCEPTANCE_PASSWORD = $AcceptancePassword" not in acceptance[
        acceptance.index('Invoke-AcceptanceStep "04-core-e2e"'):
        acceptance.index('Invoke-AcceptanceStep "05-backup"')
    ]
    assert "retain-on-failure" not in acceptance
    assert 'SetEnvironmentVariable("ACCEPTANCE_PASSWORD", $null, "Process")' in acceptance
    assert "Remove-AcceptanceAccount" in acceptance


def test_acceptance_restarts_database_after_restore_and_rechecks_health():
    acceptance = _text("acceptance.ps1")
    restart = acceptance.index('Invoke-AcceptanceStep "09-restart"')
    consistency = acceptance.index('Invoke-AcceptanceStep "10-consistency"')
    restart_body = acceptance[restart:consistency]
    stop = restart_body.index(
        'Invoke-CheckedScript -Path $Stop -Parameters @{ DatabaseOnly = $true }'
    )
    start = restart_body.index("Invoke-CheckedScript -Path $Start")
    health = restart_body.index("Test-ApplicationHealth")
    assert stop < start < health


def test_acceptance_failure_cleanup_uses_only_recorded_process_state():
    acceptance = _text("acceptance.ps1")
    assert "$ApplicationStartedByAcceptance = $false" in acceptance
    assert "$PostgresStartedByAcceptance = $false" in acceptance
    assert "Invoke-AcceptanceProcessCleanup" in acceptance
    assert "cleanupErrors" in acceptance
    assert "taskkill" not in acceptance.lower()
    assert "Get-Process" not in acceptance


def test_acceptance_strictly_validates_and_safely_quotes_postgresql_identifiers():
    acceptance = _text("acceptance.ps1")
    assert "Assert-SafeIdentifier" in acceptance
    assert "^[a-z][a-z0-9_]{0,62}$" in acceptance
    assert '$SchemaResetSql = "BEGIN; DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION `\"$OwnerRole`\"; COMMIT;"' in acceptance
    assert acceptance.count("Invoke-CheckedExecutable -Path $env:PSQL_EXE") == 1
    assert "--command=$SchemaResetSql" in acceptance
    assert "--set=owner_role" not in acceptance
    assert "--command=DROP DATABASE $DatabaseName" not in acceptance
    assert "CREATE SCHEMA public AUTHORIZATION $OwnerRole" not in acceptance


def test_acceptance_roots_and_markers_have_verified_current_user_private_dacl():
    acceptance = _text("acceptance.ps1")
    assert "function Set-PrivateAcl" in acceptance
    assert "SetAccessRuleProtection($true, $false)" in acceptance
    assert "AreAccessRulesProtected" in acceptance
    assert "Private ACL verification failed" in acceptance
    assert "Set-PrivateAcl -Path $EvidenceRoot -Directory" in acceptance
    assert "Set-PrivateAcl -Path $AppDataRoot -Directory" in acceptance
    assert "Set-PrivateAcl -Path $Marker" in acceptance
    marker_function = acceptance[
        acceptance.index("function New-AcceptanceOwnershipMarker"):
        acceptance.index("function Assert-AcceptanceOwnershipMarker")
    ]
    assert "[Guid]::NewGuid().ToString(\"N\")" in marker_function
    assert '$AcceptanceOwnershipToken.json' not in marker_function
