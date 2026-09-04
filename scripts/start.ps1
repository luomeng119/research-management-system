[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-RequiredEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)

    $Value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Required environment variable $Name is not configured."
    }
    return $Value
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
        if ($null -ne $ConfiguredCommand) {
            return $ConfiguredCommand.Source
        }
        throw "Configured executable was not found: $ConfiguredValue"
    }

    $FallbackCommand = Get-Command $FallbackName -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $FallbackCommand) {
        throw "Required executable was not found: $FallbackName"
    }
    return $FallbackCommand.Source
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $null = Get-RequiredEnvironmentValue "FLASK_SECRET_KEY"
    $null = Get-RequiredEnvironmentValue "DATABASE_URL"
    $ConfiguredDataRoot = Get-RequiredEnvironmentValue "APP_DATA_ROOT"

    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) {
        throw "APP_DATA_ROOT must reference an existing directory."
    }
    $DataRoot = (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path
    $env:APP_DATA_ROOT = $DataRoot

    $ProbePath = Join-Path $DataRoot (".write-test-{0}" -f [Guid]::NewGuid().ToString("N"))
    try {
        [System.IO.File]::WriteAllText($ProbePath, "ok")
    }
    finally {
        if (Test-Path -LiteralPath $ProbePath) {
            Remove-Item -LiteralPath $ProbePath -Force
        }
    }

    $RunDirectory = Join-Path $DataRoot "run"
    $LogDirectory = Join-Path $DataRoot "logs"
    New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null

    $PidFile = Join-Path $RunDirectory "research-management.pid.json"
    if (Test-Path -LiteralPath $PidFile) {
        try {
            $ExistingState = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
            $ExistingProcess = Get-Process -Id ([int]$ExistingState.pid) -ErrorAction SilentlyContinue
        }
        catch {
            $ExistingProcess = $null
        }
        if ($null -ne $ExistingProcess) {
            throw "The application is already running with PID $($ExistingProcess.Id)."
        }
        Remove-Item -LiteralPath $PidFile -Force
    }

    if ([string]::IsNullOrWhiteSpace($env:DEPLOYMENT_MODE)) {
        $env:DEPLOYMENT_MODE = "PRODUCTION"
    }
    elseif ($env:DEPLOYMENT_MODE.Trim().ToUpperInvariant() -ne "PRODUCTION") {
        throw "DEPLOYMENT_MODE must be PRODUCTION when using this startup script."
    }

    if ([string]::IsNullOrWhiteSpace($env:AI_PROVIDER)) {
        $env:AI_PROVIDER = "DISABLED"
    }

    # The long-running web process must never inherit the schema-owner credential.
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")

    $BindHost = $env:APP_BIND_HOST
    if ([string]::IsNullOrWhiteSpace($BindHost)) {
        $BindHost = "127.0.0.1"
    }
    $PortText = $env:APP_PORT
    if ([string]::IsNullOrWhiteSpace($PortText)) {
        $PortText = "5001"
    }
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
        if (Test-Path -LiteralPath $SiblingWaitress -PathType Leaf) {
            $ConfiguredWaitress = $SiblingWaitress
        }
    }
    $WaitressExe = Resolve-Executable -ConfiguredValue $ConfiguredWaitress -FallbackName "waitress-serve.exe"

    Push-Location $ProjectRoot
    try {
        $StdoutLog = Join-Path $LogDirectory "server.out.log"
        $StderrLog = Join-Path $LogDirectory "server.err.log"
        $Arguments = @(
            "--host=$BindHost",
            "--port=$Port",
            "--threads=4",
            "--call",
            "app:create_app"
        )
        $ServerProcess = Start-Process -FilePath $WaitressExe `
            -ArgumentList $Arguments `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError $StderrLog `
            -WindowStyle Hidden `
            -PassThru

        $ReadyHost = $BindHost
        if ($ReadyHost -eq "0.0.0.0") {
            $ReadyHost = "127.0.0.1"
        }
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
                if ($_.Exception.PSObject.Properties.Name -contains "Response") {
                    $FailureResponse = $_.Exception.Response
                }
                if ($null -ne $FailureResponse) {
                    $ReadyStatus = [int]$FailureResponse.StatusCode
                }
                # Connection failures are retried until the startup deadline.
            }
            if ($ReadyStatus -eq 200) {
                $IsReady = $true
                break
            }
            if ($null -ne $ReadyStatus) {
                Stop-Process -Id $ServerProcess.Id -ErrorAction SilentlyContinue
                if (Test-Path -LiteralPath $PidFile) {
                    Remove-Item -LiteralPath $PidFile -Force
                }
                throw "Readiness check returned HTTP $ReadyStatus; the new Waitress process was stopped."
            }
            Start-Sleep -Milliseconds 500
        }

        if (-not $IsReady) {
            Stop-Process -Id $ServerProcess.Id -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $PidFile) {
                Remove-Item -LiteralPath $PidFile -Force
            }
            throw "Readiness check did not return HTTP 200 within 30 seconds; the new Waitress process was stopped."
        }

        $PidState = [ordered]@{
            pid = $ServerProcess.Id
            startedAtUtc = $ServerProcess.StartTime.ToUniversalTime().ToString("o")
        }
        try {
            $PidState | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8
        }
        catch {
            Stop-Process -Id $ServerProcess.Id -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $PidFile) {
                Remove-Item -LiteralPath $PidFile -Force
            }
            throw "Waitress started but its PID state could not be recorded; the new process was stopped."
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "Application started on http://${BindHost}:$Port/ (PID $($ServerProcess.Id))."
    Write-Host "Runtime logs: $LogDirectory"
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: {0}", $_.Exception.Message)
    exit 1
}
