[CmdletBinding()]
param(
    [string]$OfflineRoot = (Join-Path $PSScriptRoot "..\offline"),
    [string]$VirtualEnvironment = (Join-Path $PSScriptRoot "..\.venv")
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$StagingEnvironment = $null
$PreviousEnvironment = $null

function Get-RequiredEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    $Value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Required environment variable $Name is not configured."
    }
    return $Value
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE: $Executable"
    }
}

function Get-SafeBundleFiles {
    param([Parameter(Mandatory = $true)][string]$Root)
    $Pending = New-Object 'System.Collections.Generic.Stack[string]'
    $Pending.Push($Root)
    while ($Pending.Count -gt 0) {
        $Directory = $Pending.Pop()
        $DirectoryItem = Get-Item -LiteralPath $Directory -Force
        if (($DirectoryItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Offline bundle contains a reparse point: $($DirectoryItem.FullName)"
        }
        foreach ($Child in Get-ChildItem -LiteralPath $Directory -Force) {
            if (($Child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Offline bundle contains a reparse point: $($Child.FullName)"
            }
            if ($Child.PSIsContainer) {
                $Pending.Push($Child.FullName)
            }
            else {
                Write-Output $Child
            }
        }
    }
}

function Test-TcpPortInUse {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][int]$Port
    )
    $Client = New-Object Net.Sockets.TcpClient
    try {
        $Attempt = $Client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $Attempt.AsyncWaitHandle.WaitOne(1000, $false)) {
            return $false
        }
        $Client.EndConnect($Attempt)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $Client.Close()
    }
}

function Assert-OfflineManifest {
    param([Parameter(Mandatory = $true)][string]$Root)
    $ManifestPath = Join-Path $Root "manifest.json"
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Offline manifest was not found: $ManifestPath"
    }
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ([int]$Manifest.schemaVersion -ne 1) {
        throw "Unsupported offline manifest schema."
    }
    $RootPrefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $ActualPaths = @{}
    foreach ($ActualFile in @(Get-SafeBundleFiles -Root $Root)) {
        $ActualRelative = $ActualFile.FullName.Substring($RootPrefix.Length).Replace('\', '/')
        if ($ActualRelative -ne "manifest.json") {
            $ActualPaths[$ActualRelative] = $true
        }
    }
    $ManifestPaths = @{}
    foreach ($Entry in $Manifest.files) {
        $Relative = [string]$Entry.path
        $NormalisedRelative = $Relative.Replace('\', '/')
        $Segments = $Relative -split '[/\\]'
        if ([string]::IsNullOrWhiteSpace($Relative) -or
            [IO.Path]::IsPathRooted($Relative) -or
            $Segments -contains "..") {
            throw "Offline manifest contains an unsafe path: $Relative"
        }
        $Candidate = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
        if (-not $Candidate.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Offline manifest contains an unsafe path: $Relative"
        }
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            throw "Offline file is missing: $Relative"
        }
        $CandidateItem = Get-Item -LiteralPath $Candidate
        if (($CandidateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Offline manifest may not reference a reparse point: $Relative"
        }
        if ([long]$CandidateItem.Length -ne [long]$Entry.size) {
            throw "Offline file size mismatch: $Relative"
        }
        $ActualHash = (Get-FileHash -LiteralPath $Candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualHash -ne ([string]$Entry.sha256).ToLowerInvariant()) {
            throw "SHA256 mismatch for offline file: $Relative"
        }
        if ($ManifestPaths.ContainsKey($NormalisedRelative)) {
            throw "Offline manifest contains a duplicate path: $Relative"
        }
        $ManifestPaths[$NormalisedRelative] = $true
    }
    foreach ($RequiredManifestPath in @(
        "requirements.txt",
        "runtime/python/python.exe",
        "runtime/postgresql/bin/psql.exe",
        "runtime/postgresql/bin/pg_dump.exe",
        "runtime/postgresql/bin/pg_restore.exe",
        "licenses/THIRD_PARTY-NOTICES.txt"
    )) {
        if (-not $ManifestPaths.ContainsKey($RequiredManifestPath)) {
            throw "Manifest does not cover required offline file: $RequiredManifestPath"
        }
    }
    $WheelEntries = @($ManifestPaths.Keys | Where-Object { $_ -like "wheelhouse/*.whl" })
    if ($WheelEntries.Count -eq 0) {
        throw "Manifest does not cover any offline wheel files."
    }
    foreach ($ActualPath in $ActualPaths.Keys) {
        if (-not $ManifestPaths.ContainsKey($ActualPath)) {
            throw "Offline root contains a file absent from the manifest: $ActualPath"
        }
    }
    foreach ($ManifestPathEntry in $ManifestPaths.Keys) {
        if (-not $ActualPaths.ContainsKey($ManifestPathEntry)) {
            throw "Manifest contains a path absent from the offline root: $ManifestPathEntry"
        }
    }
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $ConfiguredDataRoot = Get-RequiredEnvironmentValue "APP_DATA_ROOT"
    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) {
        throw "APP_DATA_ROOT must reference an existing directory."
    }
    $DataRoot = (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path
    $PidFile = Join-Path (Join-Path $DataRoot "run") "research-management.pid.json"
    if (Test-Path -LiteralPath $PidFile -PathType Leaf) {
        try {
            $PidState = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
            $RecordedProcess = Get-Process -Id ([int]$PidState.pid) -ErrorAction SilentlyContinue
        }
        catch {
            throw "PID file is invalid; refusing to upgrade."
        }
        if ($null -ne $RecordedProcess) {
            throw "Stop the application before upgrading."
        }
        throw "A stale PID file exists; run stop.ps1 and resolve it before upgrading."
    }

    $BindHost = $env:APP_BIND_HOST
    if ([string]::IsNullOrWhiteSpace($BindHost) -or $BindHost -eq "0.0.0.0") {
        $BindHost = "127.0.0.1"
    }
    $Port = 5001
    if (-not [string]::IsNullOrWhiteSpace($env:APP_PORT)) {
        if (-not [int]::TryParse($env:APP_PORT, [ref]$Port) -or $Port -lt 1 -or $Port -gt 65535) {
            throw "APP_PORT must be an integer between 1 and 65535."
        }
    }
    if (Test-TcpPortInUse -HostName $BindHost -Port $Port) {
        throw "The configured application port is active; refusing a hot upgrade."
    }
    $WaitressProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
        $_.CommandLine -match "waitress" -and
        $_.CommandLine -match "app:create_app"
    })
    if ($WaitressProcesses.Count -gt 0) {
        throw "A Waitress application process is active; refusing a hot upgrade."
    }

    if (-not (Test-Path -LiteralPath $OfflineRoot -PathType Container)) {
        throw "Offline root does not exist: $OfflineRoot"
    }
    $OfflineRoot = (Resolve-Path -LiteralPath $OfflineRoot).Path
    Assert-OfflineManifest -Root $OfflineRoot
    $Wheelhouse = Join-Path $OfflineRoot "wheelhouse"
    $Requirements = Join-Path $OfflineRoot "requirements.txt"
    $RuntimePython = Join-Path $OfflineRoot "runtime\python\python.exe"
    $VirtualEnvironment = [IO.Path]::GetFullPath($VirtualEnvironment)
    $VenvPython = Join-Path $VirtualEnvironment "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "Installed virtual environment was not found; run install.ps1 first."
    }
    $MigrationDatabaseUrl = Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"

    $EnvironmentParent = Split-Path -Parent $VirtualEnvironment
    $EnvironmentName = Split-Path -Leaf $VirtualEnvironment
    $StagingEnvironment = Join-Path $EnvironmentParent (
        $EnvironmentName + ".upgrading-" + [Guid]::NewGuid().ToString("N")
    )
    $PreviousEnvironment = Join-Path $EnvironmentParent (
        $EnvironmentName + ".previous-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss") +
        "-" + [Guid]::NewGuid().ToString("N")
    )
    Invoke-Checked -Executable $RuntimePython -Arguments @("-m", "venv", $StagingEnvironment)
    $StagingPython = Join-Path $StagingEnvironment "Scripts\python.exe"
    Invoke-Checked -Executable $StagingPython -Arguments @(
        "-m", "pip", "install", "--only-binary=:all:", "--no-index", "--find-links", $Wheelhouse,
        "--requirement", $Requirements
    )

    Move-Item -LiteralPath $VirtualEnvironment -Destination $PreviousEnvironment
    Move-Item -LiteralPath $StagingEnvironment -Destination $VirtualEnvironment
    $StagingEnvironment = $null
    $VenvPython = Join-Path $VirtualEnvironment "Scripts\python.exe"
    Invoke-Checked -Executable $VenvPython -Arguments @(
        "-m", "pip", "install", "--force-reinstall", "--only-binary=:all:", "--no-index", "--find-links",
        $Wheelhouse, "--requirement", $Requirements
    )
    Push-Location $ProjectRoot
    try {
        $env:MIGRATION_DATABASE_URL = $MigrationDatabaseUrl
        Invoke-Checked -Executable $VenvPython -Arguments @("-m", "alembic", "upgrade", "head")
    }
    finally {
        [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
        Pop-Location
    }

    Write-Host "Offline dependency and schema upgrade completed."
    Write-Host "Previous virtual environment retained for recovery: $PreviousEnvironment"
    exit 0
}
catch {
    $OriginalError = $_.Exception.Message
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    try {
        if ($null -ne $StagingEnvironment -and (Test-Path -LiteralPath $StagingEnvironment)) {
            Remove-Item -LiteralPath $StagingEnvironment -Recurse -Force
        }
        if ($null -ne $PreviousEnvironment -and (Test-Path -LiteralPath $PreviousEnvironment)) {
            if (Test-Path -LiteralPath $VirtualEnvironment) {
                Remove-Item -LiteralPath $VirtualEnvironment -Recurse -Force
            }
            Move-Item -LiteralPath $PreviousEnvironment -Destination $VirtualEnvironment
        }
    }
    catch {
        [Console]::Error.WriteLine("ROLLBACK ERROR: {0}", $_.Exception.Message)
    }
    [Console]::Error.WriteLine("ERROR: {0}", $OriginalError)
    exit 1
}
