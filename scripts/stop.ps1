[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

try {
    $ConfiguredDataRoot = [Environment]::GetEnvironmentVariable("APP_DATA_ROOT")
    if ([string]::IsNullOrWhiteSpace($ConfiguredDataRoot)) {
        throw "Required environment variable APP_DATA_ROOT is not configured."
    }
    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) {
        throw "APP_DATA_ROOT must reference an existing directory."
    }

    $DataRoot = (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path
    $env:APP_DATA_ROOT = $DataRoot
    $PidFile = Join-Path (Join-Path $DataRoot "run") "research-management.pid.json"
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        throw "PID file was not found; the application is not recorded as running."
    }

    try {
        $PidState = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
        $ProcessId = [int]$PidState.pid
        $RecordedStart = [DateTime]::Parse(
            [string]$PidState.startedAtUtc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
    }
    catch {
        throw "PID file is invalid; refusing to stop any process."
    }

    $ServerProcess = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $ServerProcess) {
        Remove-Item -LiteralPath $PidFile -Force
        throw "Recorded PID $ProcessId is no longer running; stale PID file removed."
    }

    $ActualStart = $ServerProcess.StartTime.ToUniversalTime()
    if ($ActualStart.Ticks -ne $RecordedStart.ToUniversalTime().Ticks) {
        throw "PID $ProcessId belongs to a different process instance; refusing to stop it."
    }

    Stop-Process -Id $ProcessId -ErrorAction Stop
    $Deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        if ([DateTime]::UtcNow -ge $Deadline) {
            throw "Timed out waiting for PID $ProcessId to stop."
        }
        Start-Sleep -Milliseconds 200
    }

    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Application process $ProcessId stopped."
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: {0}", $_.Exception.Message)
    exit 1
}
