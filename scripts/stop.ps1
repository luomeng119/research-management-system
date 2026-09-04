[CmdletBinding()]
param(
    [switch]$KeepPostgresRunning,
    [switch]$DatabaseOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Assert-LocalNoReparsePath {
    param([string]$Path, [switch]$InspectTree)
    $FullPath = [IO.Path]::GetFullPath($Path)
    if ($FullPath.StartsWith("\\") -or $FullPath.StartsWith("//")) { throw "Path must not use UNC or network storage." }
    $Root = [IO.Path]::GetPathRoot($FullPath)
    if ((New-Object IO.DriveInfo($Root)).DriveType -eq [IO.DriveType]::Network) { throw "Path must not use a network drive." }
    $Current = $FullPath
    while ($Current) {
        if (Test-Path -LiteralPath $Current) {
            if (((Get-Item -LiteralPath $Current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Path contains a ReparsePoint or junction." }
        }
        $Parent = Split-Path -Parent $Current
        if (-not $Parent -or $Parent -eq $Current) { break }
        $Current = $Parent
    }
    if ($InspectTree -and (Test-Path -LiteralPath $FullPath -PathType Container)) {
        foreach ($Item in Get-ChildItem -LiteralPath $FullPath -Recurse -Force) {
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Path tree contains a ReparsePoint or junction." }
        }
    }
    return $FullPath
}

function Read-ValidatedDeployment {
    param([string]$DataRoot)
    $DeploymentPath = Join-Path (Join-Path $DataRoot "config") "deployment.json"
    if (-not (Test-Path -LiteralPath $DeploymentPath -PathType Leaf)) { throw "Deployment state was not found." }
    try { $State = Get-Content -LiteralPath $DeploymentPath -Raw | ConvertFrom-Json } catch { throw "Deployment state is invalid." }
    foreach ($Identifier in @($State.bootstrapRole, $State.ownerRole, $State.runtimeRole, $State.databaseName)) {
        if ([string]$Identifier -notmatch '^[a-z][a-z0-9_]{0,62}$') { throw "Deployment state contains an invalid PostgreSQL identifier." }
    }
    $UniqueRoles = @($State.bootstrapRole, $State.ownerRole, $State.runtimeRole) | Sort-Object -Unique
    if ([int]$State.schemaVersion -ne 1 -or [string]$State.databaseHost -ne "127.0.0.1" -or [int]$State.databasePort -lt 1024 -or [int]$State.databasePort -gt 65535 -or @($UniqueRoles).Count -ne 3) { throw "Deployment state does not describe a supported local installation." }
    $ExpectedRuntime = Join-Path (Join-Path $DataRoot "runtime") "postgresql"
    $ExpectedData = Join-Path (Join-Path $DataRoot "postgresql") "data"
    foreach ($Pair in @(@([string]$State.postgresRuntime, $ExpectedRuntime), @([string]$State.postgresData, $ExpectedData))) {
        $Actual = Assert-LocalNoReparsePath -Path $Pair[0] -InspectTree
        if (-not $Actual.Equals([IO.Path]::GetFullPath($Pair[1]), [StringComparison]::OrdinalIgnoreCase)) { throw "Deployment path must match the expected APP_DATA_ROOT location." }
    }
    return $State
}

function Get-VerifiedProcess {
    param([Parameter(Mandatory = $true)]$State, [string]$Description)
    try {
        $ProcessId = [int]$State.pid
        $RecordedStart = [DateTime]::Parse(
            [string]$State.startedAtUtc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
    }
    catch { throw "$Description PID state is invalid; refusing to stop any process." }
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $Process) { throw "Recorded $Description PID $ProcessId is no longer running." }
    if ($Process.StartTime.ToUniversalTime().Ticks -ne $RecordedStart.ToUniversalTime().Ticks) {
        throw "$Description PID $ProcessId belongs to a different process instance; refusing to stop it."
    }
    return $Process
}

try {
    if ($KeepPostgresRunning -and $DatabaseOnly) { throw "KeepPostgresRunning and DatabaseOnly cannot be combined." }
    $ConfiguredDataRoot = [Environment]::GetEnvironmentVariable("APP_DATA_ROOT", "Process")
    if ([string]::IsNullOrWhiteSpace($ConfiguredDataRoot)) {
        throw "Required environment variable APP_DATA_ROOT is not configured."
    }
    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) {
        throw "APP_DATA_ROOT must reference an existing directory."
    }
    $DataRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path -InspectTree
    $env:APP_DATA_ROOT = $DataRoot
    $RunDirectory = Join-Path $DataRoot "run"

    if (-not $DatabaseOnly) {
        $PidFile = Join-Path $RunDirectory "research-management.pid.json"
        if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
            throw "PID file was not found; refusing to infer an application process."
        }
        try { $PidState = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json }
        catch { throw "PID file is invalid; refusing to stop any process." }
        $ServerProcess = Get-VerifiedProcess -State $PidState -Description "Application"
        $ProcessId = $ServerProcess.Id
        Stop-Process -Id $ProcessId -ErrorAction Stop
        $Deadline = [DateTime]::UtcNow.AddSeconds(10)
        while ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            if ([DateTime]::UtcNow -ge $Deadline) { throw "Timed out waiting for PID $ProcessId to stop." }
            Start-Sleep -Milliseconds 200
        }
        Remove-Item -LiteralPath $PidFile -Force
        Write-Host "Application process $ProcessId stopped."
    }

    if ($KeepPostgresRunning) { exit 0 }

    $Deployment = Read-ValidatedDeployment -DataRoot $DataRoot
    $PostgresRuntime = (Resolve-Path -LiteralPath ([string]$Deployment.postgresRuntime)).Path
    $PostgresData = (Resolve-Path -LiteralPath ([string]$Deployment.postgresData)).Path
    $PgCtlExe = Join-Path $PostgresRuntime "bin\pg_ctl.exe"
    if (-not (Test-Path -LiteralPath $PgCtlExe -PathType Leaf)) { throw "Bundled pg_ctl.exe is missing." }
    $PostgresPidState = Join-Path $RunDirectory "postgresql.pid.json"
    if (-not (Test-Path -LiteralPath $PostgresPidState -PathType Leaf)) {
        throw "PostgreSQL PID state was not found; refusing to infer a database process."
    }
    try { $RecordedPostgresState = Get-Content -LiteralPath $PostgresPidState -Raw | ConvertFrom-Json }
    catch { throw "PostgreSQL PID state is invalid; refusing to stop any process." }
    if (-not ([string]$RecordedPostgresState.dataRoot).Equals($PostgresData, [StringComparison]::OrdinalIgnoreCase)) {
        throw "PostgreSQL PID state dataRoot does not match this deployment."
    }
    $PostgresProcess = Get-VerifiedProcess -State $RecordedPostgresState -Description "PostgreSQL"
    $PostmasterPid = Join-Path $PostgresData "postmaster.pid"
    if (-not (Test-Path -LiteralPath $PostmasterPid -PathType Leaf)) {
        throw "postmaster.pid is missing; refusing to stop an unverified cluster."
    }
    $PostmasterProcessId = [int]((Get-Content -LiteralPath $PostmasterPid -TotalCount 1).Trim())
    if ($PostmasterProcessId -ne $PostgresProcess.Id) {
        throw "PostgreSQL PID belongs to a different process instance; refusing to stop it."
    }
    & $PgCtlExe @("stop", "-D", $PostgresData, "-m", "fast", "-w") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "pg_ctl could not stop the recorded PostgreSQL cluster." }
    Remove-Item -LiteralPath $PostgresPidState -Force
    Write-Host "Local PostgreSQL cluster stopped."
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: {0}", $_.Exception.Message)
    exit 1
}
