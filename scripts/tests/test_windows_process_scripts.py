from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
START_SCRIPT = (SCRIPTS_DIR / "start.ps1").read_text(encoding="utf-8")
STOP_SCRIPT = (SCRIPTS_DIR / "stop.ps1").read_text(encoding="utf-8")


def test_start_requires_deployment_inputs_and_defaults_to_safe_local_mode():
    for variable in (
        "FLASK_SECRET_KEY",
        "DATABASE_URL",
        "APP_DATA_ROOT",
    ):
        assert f'Get-RequiredEnvironmentValue "{variable}"' in START_SCRIPT
    assert 'Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"' not in START_SCRIPT

    assert '$BindHost = "127.0.0.1"' in START_SCRIPT
    assert '$env:DEPLOYMENT_MODE = "PRODUCTION"' in START_SCRIPT
    assert '$env:DEPLOYMENT_MODE.Trim().ToUpperInvariant() -ne "PRODUCTION"' in START_SCRIPT
    assert "DEPLOYMENT_MODE must be PRODUCTION" in START_SCRIPT
    assert '$env:AI_PROVIDER = "DISABLED"' in START_SCRIPT
    assert "$env:APP_BIND_HOST" in START_SCRIPT
    assert "$env:APP_PORT" in START_SCRIPT
    assert "$DataRoot = Assert-LocalNoReparsePath" in START_SCRIPT
    assert "$env:APP_DATA_ROOT = $DataRoot" in START_SCRIPT


def test_start_launches_one_waitress_process_without_migration_credentials():
    launch = "Start-Process -FilePath $WaitressExe"
    assert launch in START_SCRIPT
    clear_migration_credential = (
        '[Environment]::SetEnvironmentVariable('
        '"MIGRATION_DATABASE_URL", $null, "Process")'
    )
    assert clear_migration_credential in START_SCRIPT
    assert START_SCRIPT.index(clear_migration_credential) < START_SCRIPT.index(launch)
    assert "-m alembic" not in START_SCRIPT
    assert "upgrade head" not in START_SCRIPT
    assert '"--call"' in START_SCRIPT
    assert '"app:create_app"' in START_SCRIPT
    assert '"--threads=4"' in START_SCRIPT
    assert "inference_server" not in START_SCRIPT
    assert "node.exe" not in START_SCRIPT.lower()


def test_start_reports_success_only_after_http_readiness():
    launch = START_SCRIPT.index("Start-Process -FilePath $WaitressExe")
    readiness = START_SCRIPT.index("Invoke-WebRequest -Uri $ReadyUri")
    pid_write = START_SCRIPT.index("$PidState | ConvertTo-Json")
    success = START_SCRIPT.index('Write-Host "Application started')

    assert launch < readiness < pid_write < success
    assert '$ReadyUri = "http://${ReadyHost}:$Port/health/ready"' in START_SCRIPT
    assert '$ReadyHost -eq "0.0.0.0"' in START_SCRIPT
    assert '$ReadyHost = "127.0.0.1"' in START_SCRIPT
    assert "$ReadyStatus = [int]$ReadyResponse.StatusCode" in START_SCRIPT
    assert "$ReadyStatus -eq 200" in START_SCRIPT
    assert "$FailureResponse.StatusCode" in START_SCRIPT
    assert "Readiness check returned HTTP $ReadyStatus" in START_SCRIPT
    assert "Readiness check did not return HTTP 200" in START_SCRIPT
    assert "Stop-Process -Id $ServerProcess.Id" in START_SCRIPT
    assert "Remove-Item -LiteralPath $PidFile" in START_SCRIPT


def test_pid_and_logs_are_kept_under_data_root():
    assert '$RunDirectory = Join-Path $DataRoot "run"' in START_SCRIPT
    assert '$LogDirectory = Join-Path $DataRoot "logs"' in START_SCRIPT
    assert '"research-management.pid.json"' in START_SCRIPT
    assert '"server.out.log"' in START_SCRIPT
    assert '"server.err.log"' in START_SCRIPT


def test_start_failure_removes_postgres_pid_only_after_confirmed_stop():
    assert "function Get-PostgresRuntimeState" in START_SCRIPT
    helper = START_SCRIPT.split("function Get-PostgresRuntimeState", 1)[1].split("function ", 1)[0]
    assert '0 { return "RUNNING" }' in helper
    assert '3 { return "STOPPED" }' in helper
    assert 'default { return "UNKNOWN" }' in helper
    catch_body = START_SCRIPT.split("\ncatch {\n    $OriginalError", 1)[1]
    stop = catch_body.index('"stop", "-D", $PostgresData')
    status = catch_body.index("Get-PostgresRuntimeState", stop)
    confirmed = catch_body.index('$StopExitCode -eq 0 -and $PostgresState -eq "STOPPED"', status)
    remove = catch_body.index("Remove-Item -LiteralPath $PostgresPidState", confirmed)
    assert stop < status < confirmed < remove
    assert "PostgreSQL cleanup was not confirmed" in catch_body


def test_stop_targets_only_verified_recorded_pid():
    assert "$DataRoot = Assert-LocalNoReparsePath" in STOP_SCRIPT
    assert "$env:APP_DATA_ROOT = $DataRoot" in STOP_SCRIPT
    assert "Get-Process -Id $ProcessId" in STOP_SCRIPT
    assert "Stop-Process -Id $ProcessId" in STOP_SCRIPT
    assert "startedAtUtc" in STOP_SCRIPT
    assert "different process instance" in STOP_SCRIPT
    assert "Get-Process python" not in STOP_SCRIPT
    assert "taskkill" not in STOP_SCRIPT.lower()
    assert "-Name" not in STOP_SCRIPT


def test_scripts_report_failure_with_nonzero_exit():
    assert "exit 1" in START_SCRIPT
    assert "exit 1" in STOP_SCRIPT
