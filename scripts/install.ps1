[CmdletBinding()]
param(
    [string]$OfflineRoot = (Join-Path $PSScriptRoot "..\offline"),
    [string]$VirtualEnvironment = (Join-Path $PSScriptRoot "..\.venv")
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$StagingEnvironment = $null
$NewEnvironmentCreated = $false

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

function Assert-OfflineManifest {
    param([Parameter(Mandatory = $true)][string]$Root)

    $ManifestPath = Join-Path $OfflineRoot "manifest.json"
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
        $ExpectedHash = ([string]$Entry.sha256).ToLowerInvariant()
        if ($ActualHash -ne $ExpectedHash) {
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
    if (-not (Test-Path -LiteralPath $OfflineRoot -PathType Container)) {
        throw "Offline root does not exist: $OfflineRoot"
    }
    $OfflineRoot = (Resolve-Path -LiteralPath $OfflineRoot).Path
    $Wheelhouse = Join-Path $OfflineRoot "wheelhouse"
    $Requirements = Join-Path $OfflineRoot "requirements.txt"
    $RuntimePython = Join-Path $OfflineRoot "runtime\python\python.exe"
    $PostgresPsql = Join-Path $OfflineRoot "runtime\postgresql\bin\psql.exe"
    $PostgresDump = Join-Path $OfflineRoot "runtime\postgresql\bin\pg_dump.exe"
    $PostgresRestore = Join-Path $OfflineRoot "runtime\postgresql\bin\pg_restore.exe"
    foreach ($RequiredFile in @(
        $RuntimePython, $PostgresPsql, $PostgresDump, $PostgresRestore, $Requirements
    )) {
        if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
            throw "Required offline file was not found: $RequiredFile"
        }
    }
    if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) {
        throw "Offline wheelhouse was not found: $Wheelhouse"
    }

    Assert-OfflineManifest -Root $OfflineRoot

    $VirtualEnvironment = [IO.Path]::GetFullPath($VirtualEnvironment)
    if (Test-Path -LiteralPath $VirtualEnvironment) {
        throw "Virtual environment already exists; use upgrade.ps1 for an existing installation."
    }
    $MigrationDatabaseUrl = Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"

    $EnvironmentParent = Split-Path -Parent $VirtualEnvironment
    if (-not (Test-Path -LiteralPath $EnvironmentParent -PathType Container)) {
        New-Item -ItemType Directory -Path $EnvironmentParent -Force | Out-Null
    }
    $StagingEnvironment = Join-Path $EnvironmentParent (
        (Split-Path -Leaf $VirtualEnvironment) + ".installing-" + [Guid]::NewGuid().ToString("N")
    )

    Invoke-Checked -Executable $RuntimePython -Arguments @(
        "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
    )
    Invoke-Checked -Executable $RuntimePython -Arguments @("-m", "venv", $StagingEnvironment)
    $StagingPython = Join-Path $StagingEnvironment "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $StagingPython -PathType Leaf)) {
        throw "Python virtual environment was not created correctly."
    }
    Invoke-Checked -Executable $StagingPython -Arguments @(
        "-m", "pip", "install", "--only-binary=:all:", "--no-index", "--find-links", $Wheelhouse,
        "--requirement", $Requirements
    )

    Move-Item -LiteralPath $StagingEnvironment -Destination $VirtualEnvironment
    $StagingEnvironment = $null
    $NewEnvironmentCreated = $true
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

    Write-Host "Offline dependencies installed and database schema upgraded."
    Write-Host "PostgreSQL client tools: $(Split-Path -Parent $PostgresPsql)"
    exit 0
}
catch {
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    if ($null -ne $StagingEnvironment -and (Test-Path -LiteralPath $StagingEnvironment)) {
        Remove-Item -LiteralPath $StagingEnvironment -Recurse -Force
    }
    if ($NewEnvironmentCreated -and (Test-Path -LiteralPath $VirtualEnvironment)) {
        Remove-Item -LiteralPath $VirtualEnvironment -Recurse -Force
    }
    [Console]::Error.WriteLine("ERROR: {0}", $_.Exception.Message)
    exit 1
}
