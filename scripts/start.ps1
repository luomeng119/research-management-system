[CmdletBinding()]
param(
    [switch]$DatabaseOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$ServerProcess = $null
$PostgresStartedByThisRun = $false
$PgCtlExe = $null
$PostgresData = $null
$PostgresPidState = $null

function Assert-LocalNoReparsePath {
    param([string]$Path, [switch]$InspectTree)
    $FullPath = [IO.Path]::GetFullPath($Path)
    if ($FullPath.StartsWith("\\") -or $FullPath.StartsWith("//")) { throw "Path must not use UNC or network storage." }
    $Root = [IO.Path]::GetPathRoot($FullPath)
    if ((New-Object IO.DriveInfo($Root)).DriveType -eq [IO.DriveType]::Network) { throw "Path must not use a network drive." }
    $Current = $FullPath
    while ($Current) {
        if (Test-Path -LiteralPath $Current) {
            if (((Get-Item -LiteralPath $Current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Path contains a ReparsePoint or junction: $Current"
            }
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
    if (-not (Test-Path -LiteralPath $DeploymentPath -PathType Leaf)) { throw "Deployment state was not found; run install.ps1 first." }
    try { $State = Get-Content -LiteralPath $DeploymentPath -Raw | ConvertFrom-Json } catch { throw "Deployment state is invalid." }
    foreach ($Identifier in @($State.bootstrapRole, $State.ownerRole, $State.runtimeRole, $State.databaseName)) {
        if ([string]$Identifier -notmatch '^[a-z][a-z0-9_]{0,62}$') { throw "Deployment state contains an invalid PostgreSQL identifier." }
    }
    $UniqueRoles = @($State.bootstrapRole, $State.ownerRole, $State.runtimeRole) | Sort-Object -Unique
    if ([int]$State.schemaVersion -ne 1 -or [string]$State.databaseHost -ne "127.0.0.1" -or
        [int]$State.databasePort -lt 1024 -or [int]$State.databasePort -gt 65535 -or
        @($UniqueRoles).Count -ne 3) {
        throw "Deployment state does not describe a supported local installation."
    }
    $ExpectedRuntime = Join-Path (Join-Path $DataRoot "runtime") "postgresql"
    $ExpectedData = Join-Path (Join-Path $DataRoot "postgresql") "data"
    foreach ($Pair in @(@([string]$State.postgresRuntime, $ExpectedRuntime), @([string]$State.postgresData, $ExpectedData))) {
        $Actual = Assert-LocalNoReparsePath -Path $Pair[0] -InspectTree
        $Expected = [IO.Path]::GetFullPath($Pair[1])
        if (-not $Actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) { throw "Deployment path must match the expected APP_DATA_ROOT location." }
    }
    return $State
}

function Get-RequiredEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    $Value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Required environment variable $Name is not configured."
    }
    return $Value
}

function Unprotect-Secret {
    param([Parameter(Mandatory = $true)][string]$CipherText)
    $SecureValue = ConvertTo-SecureString $CipherText
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
}

function New-DatabaseUrl {
    param([string]$Role, [string]$Password, [string]$Database, [int]$DatabasePort)
    return "postgresql+psycopg://${Role}:$([Uri]::EscapeDataString($Password))@127.0.0.1:$DatabasePort/$Database"
}

function Resolve-Executable {
    param(
        [AllowEmptyString()][string]$ConfiguredValue,
        [Parameter(Mandatory = $true)][string]$FallbackName
    )
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredValue)) {
        if (Test-Path -LiteralPath $ConfiguredValue -PathType Leaf) {
            return (Resolve-Path -LiteralPath $ConfiguredValue).Path
        }
        $ConfiguredCommand = Get-Command $ConfiguredValue -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $ConfiguredCommand) { return $ConfiguredCommand.Source }
        throw "Configured executable was not found: $ConfiguredValue"
    }
    $FallbackCommand = Get-Command $FallbackName -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $FallbackCommand) { throw "Required executable was not found: $FallbackName" }
    return $FallbackCommand.Source
}

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE: $Executable" }
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
    catch { throw "$Description PID state is invalid." }
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $Process) { return $null }
    if ($Process.StartTime.ToUniversalTime().Ticks -ne $RecordedStart.ToUniversalTime().Ticks) {
        throw "$Description PID $ProcessId belongs to a different process instance."
    }
    return $Process
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $ConfiguredDataRoot = Get-RequiredEnvironmentValue "APP_DATA_ROOT"
    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) {
        throw "APP_DATA_ROOT must reference an existing directory."
    }
    $DataRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path -InspectTree
    $env:APP_DATA_ROOT = $DataRoot
    $Deployment = Read-ValidatedDeployment -DataRoot $DataRoot
    $DatabasePort = [int]$Deployment.databasePort
    if ($DatabasePort -lt 1 -or $DatabasePort -gt 65535) { throw "Deployment database port is invalid." }
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("FLASK_SECRET_KEY", $null, "Process")
    foreach ($PostgresVariable in @("PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER")) {
        [Environment]::SetEnvironmentVariable($PostgresVariable, $null, "Process")
    }

    $PostgresRuntime = (Resolve-Path -LiteralPath ([string]$Deployment.postgresRuntime)).Path
    $PostgresData = (Resolve-Path -LiteralPath ([string]$Deployment.postgresData)).Path
    $PgCtlExe = Join-Path $PostgresRuntime "bin\pg_ctl.exe"
    if (-not (Test-Path -LiteralPath $PgCtlExe -PathType Leaf)) { throw "Bundled pg_ctl.exe is missing." }

    $RunDirectory = Join-Path $DataRoot "run"
    $LogDirectory = Join-Path $DataRoot "logs"
    New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    $PostgresPidState = Join-Path $RunDirectory "postgresql.pid.json"
    $PostmasterPid = Join-Path $PostgresData "postmaster.pid"
    $RecordedPostgres = $null
    if (Test-Path -LiteralPath $PostgresPidState -PathType Leaf) {
        try { $RecordedPostgresState = Get-Content -LiteralPath $PostgresPidState -Raw | ConvertFrom-Json }
        catch { throw "PostgreSQL PID state is invalid; refusing to start another cluster." }
        if (-not ([string]$RecordedPostgresState.dataRoot).Equals($PostgresData, [StringComparison]::OrdinalIgnoreCase)) {
            throw "PostgreSQL PID state dataRoot does not match this deployment."
        }
        $RecordedPostgres = Get-VerifiedProcess -State $RecordedPostgresState -Description "PostgreSQL"
        if ($null -ne $RecordedPostgres) {
            if (-not (Test-Path -LiteralPath $PostmasterPid -PathType Leaf)) {
                throw "Recorded PostgreSQL is active but postmaster.pid is missing."
            }
            $PostmasterProcessId = [int]((Get-Content -LiteralPath $PostmasterPid -TotalCount 1).Trim())
            if ($PostmasterProcessId -ne $RecordedPostgres.Id) {
                throw "PostgreSQL PID state does not match this cluster's postmaster.pid."
            }
        }
        else {
            throw "PostgreSQL PID state is stale; resolve it before startup."
        }
    }
    if ($null -eq $RecordedPostgres) {
        Invoke-Checked -Executable $PgCtlExe -Arguments @(
            "start", "-D", $PostgresData, "-l", (Join-Path $LogDirectory "postgresql.log"),
            "-w", "-o", "-h 127.0.0.1 -p $DatabasePort"
        )
        $PostgresStartedByThisRun = $true
        if (-not (Test-Path -LiteralPath $PostmasterPid -PathType Leaf)) {
            throw "PostgreSQL started but did not create postmaster.pid."
        }
        $PostmasterProcessId = [int]((Get-Content -LiteralPath $PostmasterPid -TotalCount 1).Trim())
        $PostgresProcess = Get-Process -Id $PostmasterProcessId -ErrorAction SilentlyContinue
        if ($null -eq $PostgresProcess) { throw "PostgreSQL postmaster process was not found after startup." }
        [ordered]@{
            pid = $PostgresProcess.Id
            startedAtUtc = $PostgresProcess.StartTime.ToUniversalTime().ToString("o")
            dataRoot = $PostgresData
        } | ConvertTo-Json | Set-Content -LiteralPath $PostgresPidState -Encoding UTF8
    }

    if ($DatabaseOnly) {
        Write-Host "Local PostgreSQL started on 127.0.0.1:$DatabasePort."
        exit 0
    }

    $RuntimePassword = Unprotect-Secret ([string]$Deployment.runtimePasswordProtected)
    $FlaskSecret = Unprotect-Secret ([string]$Deployment.flaskSecretProtected)
    $env:DATABASE_URL = New-DatabaseUrl `
        -Role ([string]$Deployment.runtimeRole) `
        -Password $RuntimePassword `
        -Database ([string]$Deployment.databaseName) `
        -DatabasePort $DatabasePort
    $env:FLASK_SECRET_KEY = $FlaskSecret
    $null = Get-RequiredEnvironmentValue "FLASK_SECRET_KEY"
    $null = Get-RequiredEnvironmentValue "DATABASE_URL"

    $PidFile = Join-Path $RunDirectory "research-management.pid.json"
    if (Test-Path -LiteralPath $PidFile) {
        try {
            $ExistingState = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
            $ExistingProcess = Get-VerifiedProcess -State $ExistingState -Description "Application"
        }
        catch { throw "Application PID state is invalid; refusing to start another process." }
        if ($null -ne $ExistingProcess) { throw "The application is already running with PID $($ExistingProcess.Id)." }
        throw "Application PID state is stale; resolve it before startup."
    }

    if ([string]::IsNullOrWhiteSpace($env:DEPLOYMENT_MODE)) { $env:DEPLOYMENT_MODE = "PRODUCTION" }
    elseif ($env:DEPLOYMENT_MODE.Trim().ToUpperInvariant() -ne "PRODUCTION") {
        throw "DEPLOYMENT_MODE must be PRODUCTION when using this startup script."
    }
    if ([string]::IsNullOrWhiteSpace($env:AI_PROVIDER)) { $env:AI_PROVIDER = "DISABLED" }

    $BindHost = $env:APP_BIND_HOST
    if ([string]::IsNullOrWhiteSpace($BindHost)) { $BindHost = "127.0.0.1" }
    $PortText = $env:APP_PORT
    if ([string]::IsNullOrWhiteSpace($PortText)) { $PortText = "5001" }
    $Port = 0
    if (-not [int]::TryParse($PortText, [ref]$Port) -or $Port -lt 1 -or $Port -gt 65535) {
        throw "APP_PORT must be an integer between 1 and 65535."
    }

    $ConfiguredPython = $env:PYTHON_EXE
    $DefaultVenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if ([string]::IsNullOrWhiteSpace($ConfiguredPython) -and (Test-Path -LiteralPath $DefaultVenvPython -PathType Leaf)) {
        $ConfiguredPython = $DefaultVenvPython
    }
    $PythonExe = Resolve-Executable -ConfiguredValue $ConfiguredPython -FallbackName "python.exe"
    $ConfiguredWaitress = $env:WAITRESS_EXE
    if ([string]::IsNullOrWhiteSpace($ConfiguredWaitress)) {
        $SiblingWaitress = Join-Path (Split-Path -Parent $PythonExe) "waitress-serve.exe"
        if (Test-Path -LiteralPath $SiblingWaitress -PathType Leaf) { $ConfiguredWaitress = $SiblingWaitress }
    }
    $WaitressExe = Resolve-Executable -ConfiguredValue $ConfiguredWaitress -FallbackName "waitress-serve.exe"

    Push-Location $ProjectRoot
    try {
        $StdoutLog = Join-Path $LogDirectory "server.out.log"
        $StderrLog = Join-Path $LogDirectory "server.err.log"
        $Arguments = @("--host=$BindHost", "--port=$Port", "--threads=4", "--call", "app:create_app")
        $ServerProcess = Start-Process -FilePath $WaitressExe `
            -ArgumentList $Arguments -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog `
            -WindowStyle Hidden -PassThru

        $ReadyHost = $BindHost
        if ($ReadyHost -eq "0.0.0.0") { $ReadyHost = "127.0.0.1" }
        $ReadyUri = "http://${ReadyHost}:$Port/health/ready"
        $ReadyDeadline = [DateTime]::UtcNow.AddSeconds(30)
        $IsReady = $false
        while ([DateTime]::UtcNow -lt $ReadyDeadline) {
            $ServerProcess.Refresh()
            if ($ServerProcess.HasExited) {
                throw "Waitress exited during startup with code $($ServerProcess.ExitCode). See $StderrLog."
            }
            $ReadyStatus = $null
            try {
                $ReadyResponse = Invoke-WebRequest -Uri $ReadyUri -Method Get -UseBasicParsing -TimeoutSec 2
                $ReadyStatus = [int]$ReadyResponse.StatusCode
            }
            catch {
                $FailureResponse = $null
                if ($_.Exception.PSObject.Properties.Name -contains "Response") { $FailureResponse = $_.Exception.Response }
                if ($null -ne $FailureResponse) { $ReadyStatus = [int]$FailureResponse.StatusCode }
            }
            if ($ReadyStatus -eq 200) { $IsReady = $true; break }
            if ($null -ne $ReadyStatus) {
                Stop-Process -Id $ServerProcess.Id -ErrorAction SilentlyContinue
                if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
                throw "Readiness check returned HTTP $ReadyStatus; the new Waitress process was stopped."
            }
            Start-Sleep -Milliseconds 500
        }
        if (-not $IsReady) {
            Stop-Process -Id $ServerProcess.Id -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
            throw "Readiness check did not return HTTP 200 within 30 seconds; the new Waitress process was stopped."
        }
        $PidState = [ordered]@{
            pid = $ServerProcess.Id
            startedAtUtc = $ServerProcess.StartTime.ToUniversalTime().ToString("o")
        }
        try { $PidState | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8 }
        catch {
            Stop-Process -Id $ServerProcess.Id -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
            throw "Waitress started but its PID state could not be recorded; the new process was stopped."
        }
    }
    finally { Pop-Location }

    Write-Host "Application started on http://${BindHost}:$Port/ (PID $($ServerProcess.Id))."
    Write-Host "Runtime logs: $LogDirectory"
    exit 0
}
catch {
    $OriginalError = $_.Exception.Message
    if ($null -ne $ServerProcess) { Stop-Process -Id $ServerProcess.Id -ErrorAction SilentlyContinue }
    if ($PostgresStartedByThisRun -and $null -ne $PgCtlExe -and $null -ne $PostgresData) {
        try { & $PgCtlExe @("stop", "-D", $PostgresData, "-m", "fast", "-w") | Out-Null } catch {}
        if ($null -ne $PostgresPidState -and (Test-Path -LiteralPath $PostgresPidState)) {
            Remove-Item -LiteralPath $PostgresPidState -Force -ErrorAction SilentlyContinue
        }
    }
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    [Console]::Error.WriteLine("ERROR: {0}", $OriginalError)
    exit 1
}
