[CmdletBinding()]
param(
    [string]$OutputPath,
    [switch]$ConfirmMaintenanceWindow
)

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
    param([AllowEmptyString()][string]$ConfiguredValue, [string]$FallbackName)
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

function Unprotect-Secret {
    param([Parameter(Mandatory = $true)][string]$CipherText)
    $SecureValue = ConvertTo-SecureString $CipherText
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
}

function New-DatabaseUrl {
    param([string]$Role, [string]$Password, [string]$Database, [int]$Port)
    return "postgresql+psycopg://${Role}:$([Uri]::EscapeDataString($Password))@127.0.0.1:$Port/$Database"
}

function Set-PostgresEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$DatabaseUrl,
        [Parameter(Mandatory = $true)]$Deployment
    )
    $Normalized = [Regex]::Replace($DatabaseUrl, '^postgresql(?:\+[^:]+)?://', 'postgresql://')
    try { $Uri = [Uri]$Normalized } catch { throw "DATABASE_URL is not a valid PostgreSQL URL." }
    if ($Uri.Scheme -ne "postgresql" -or [string]::IsNullOrWhiteSpace($Uri.Host)) {
        throw "DATABASE_URL must be a PostgreSQL URL."
    }
    $UserParts = $Uri.UserInfo.Split([char[]]@(':'), 2)
    if ($UserParts.Count -lt 1 -or [string]::IsNullOrWhiteSpace($UserParts[0])) {
        throw "DATABASE_URL must include a PostgreSQL user."
    }
    $env:PGHOST = $Uri.Host
    $env:PGPORT = if ($Uri.IsDefaultPort) { "5432" } else { [string]$Uri.Port }
    $env:PGDATABASE = [Uri]::UnescapeDataString($Uri.AbsolutePath.TrimStart('/'))
    $env:PGUSER = [Uri]::UnescapeDataString($UserParts[0])
    $env:PGPASSWORD = if ($UserParts.Count -eq 2) { [Uri]::UnescapeDataString($UserParts[1]) } else { "" }
    if ([string]::IsNullOrWhiteSpace($env:PGDATABASE)) { throw "DATABASE_URL must include a database name." }
    if (-not [string]::IsNullOrWhiteSpace($Uri.Query) -or -not [string]::IsNullOrWhiteSpace($Uri.Fragment)) {
        throw "DATABASE_URL must not include query parameters or fragments."
    }
    if (
        $env:PGHOST -ne "127.0.0.1" -or
        [int]$env:PGPORT -ne [int]$Deployment.databasePort -or
        $env:PGDATABASE -ne [string]$Deployment.databaseName -or
        $env:PGUSER -ne [string]$Deployment.runtimeRole
    ) {
        throw "DATABASE_URL must match the validated local deployment."
    }
}

function Assert-NoReparsePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Current = [IO.Path]::GetFullPath($Path)
    if ($Current.StartsWith("\\") -or $Current.StartsWith("//")) {
        throw "Backup path must not use UNC or network storage."
    }
    $DriveRoot = [IO.Path]::GetPathRoot($Current)
    if ((New-Object IO.DriveInfo($DriveRoot)).DriveType -eq [IO.DriveType]::Network) {
        throw "Backup path must not use a network drive."
    }
    while (-not [string]::IsNullOrWhiteSpace($Current)) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Backup path contains a junction or reparse point: $Current"
            }
        }
        $Parent = Split-Path -Parent $Current
        if ([string]::IsNullOrWhiteSpace($Parent) -or $Parent -eq $Current) { break }
        $Current = $Parent
    }
}

function Assert-NoReparseTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-NoReparsePath -Path $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return }
    $Pending = New-Object 'System.Collections.Generic.Stack[string]'
    $Pending.Push([IO.Path]::GetFullPath($Path))
    while ($Pending.Count -gt 0) {
        $Current = $Pending.Pop()
        Get-ChildItem -LiteralPath $Current -Force | ForEach-Object {
            if (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Backup tree contains a junction or reparse point: $($_.FullName)"
            }
            if ($_.PSIsContainer) { $Pending.Push($_.FullName) }
        }
    }
}

function Read-ValidatedDeployment {
    param([Parameter(Mandatory = $true)][string]$DataRoot)
    $DeploymentPath = Join-Path (Join-Path $DataRoot "config") "deployment.json"
    Assert-NoReparsePath -Path $DeploymentPath
    if (-not (Test-Path -LiteralPath $DeploymentPath -PathType Leaf)) {
        throw "Deployment state was not found; run install.ps1 before backup.ps1."
    }
    try { $State = Get-Content -LiteralPath $DeploymentPath -Raw | ConvertFrom-Json }
    catch { throw "Deployment state is invalid; refusing to back up." }
    foreach ($Identifier in @($State.bootstrapRole, $State.ownerRole, $State.runtimeRole, $State.databaseName)) {
        if ([string]$Identifier -notmatch '^[a-z][a-z0-9_]{0,62}$') {
            throw "Deployment state contains an invalid PostgreSQL identifier."
        }
    }
    $UniqueRoles = @($State.bootstrapRole, $State.ownerRole, $State.runtimeRole) | Sort-Object -Unique
    if (
        [int]$State.schemaVersion -ne 1 -or
        [string]$State.databaseHost -ne "127.0.0.1" -or
        [int]$State.databasePort -lt 1024 -or [int]$State.databasePort -gt 65535 -or
        @($UniqueRoles).Count -ne 3 -or
        [string]::IsNullOrWhiteSpace([string]$State.runtimePasswordProtected)
    ) {
        throw "Deployment state does not describe a supported local installation."
    }
    $ExpectedRuntime = [IO.Path]::GetFullPath((Join-Path (Join-Path $DataRoot "runtime") "postgresql"))
    $ExpectedData = [IO.Path]::GetFullPath((Join-Path (Join-Path $DataRoot "postgresql") "data"))
    foreach ($Pair in @(@([string]$State.postgresRuntime, $ExpectedRuntime), @([string]$State.postgresData, $ExpectedData))) {
        Assert-NoReparseTree -Path $Pair[0]
        $Actual = (Resolve-Path -LiteralPath $Pair[0]).Path
        if (-not $Actual.Equals($Pair[1], [StringComparison]::OrdinalIgnoreCase)) {
            throw "Deployment path must match the expected APP_DATA_ROOT location."
        }
    }
    return $State
}

function Assert-OwnershipMarker {
    param([string]$Target, [string]$Marker, [string]$Token)
    Assert-NoReparsePath -Path $Target
    Assert-NoReparsePath -Path $Marker
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
        throw "Backup ownership marker is missing; refusing cleanup."
    }
    try { $Ownership = Get-Content -LiteralPath $Marker -Raw | ConvertFrom-Json }
    catch { throw "Backup ownership marker is invalid; refusing cleanup." }
    $CanonicalTarget = [IO.Path]::GetFullPath($Target)
    if (
        [string]$Ownership.runToken -ne $Token -or
        -not ([string]$Ownership.canonicalTarget).Equals($CanonicalTarget, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Backup ownership marker does not match this run and canonical target."
    }
}

function New-OwnershipMarker {
    param([string]$Target, [string]$Marker, [string]$Token)
    Assert-NoReparsePath -Path $Target
    Assert-NoReparsePath -Path $Marker
    if (Test-Path -LiteralPath $Marker) { throw "Backup ownership marker already exists." }
    [ordered]@{
        runToken = $Token
        canonicalTarget = [IO.Path]::GetFullPath($Target)
    } | ConvertTo-Json | Set-Content -LiteralPath $Marker -Encoding UTF8
    Assert-OwnershipMarker -Target $Target -Marker $Marker -Token $Token
}

function Remove-OwnedDirectory {
    param([string]$Target, [string]$Marker, [string]$Token)
    Assert-OwnershipMarker -Target $Target -Marker $Marker -Token $Token
    Assert-NoReparseTree -Path $Target
    [IO.Directory]::Delete($Target, $true)
    [IO.File]::Delete($Marker)
}

function Remove-OwnedFile {
    param([string]$Target, [string]$Marker, [string]$Token)
    Assert-OwnershipMarker -Target $Target -Marker $Marker -Token $Token
    if (Test-Path -LiteralPath $Target -PathType Leaf) { [IO.File]::Delete($Target) }
    [IO.File]::Delete($Marker)
}

function Get-ResolvedOrProjectedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $FullPath = [IO.Path]::GetFullPath($Path)
    $Missing = New-Object System.Collections.Generic.List[string]
    $Current = $FullPath
    while (-not (Test-Path -LiteralPath $Current)) {
        $Missing.Insert(0, (Split-Path -Leaf $Current))
        $Parent = Split-Path -Parent $Current
        if ([string]::IsNullOrWhiteSpace($Parent) -or $Parent -eq $Current) { throw "Could not resolve path ancestry: $Path" }
        $Current = $Parent
    }
    Assert-NoReparsePath -Path $Current
    $Resolved = (Resolve-Path -LiteralPath $Current).Path
    foreach ($Part in $Missing) { $Resolved = Join-Path $Resolved $Part }
    return [IO.Path]::GetFullPath($Resolved)
}

function Copy-DirectoryContents {
    param([string]$Source, [string]$Destination)
    Assert-NoReparseTree -Path $Source
    Assert-NoReparseTree -Path $Destination
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Assert-NoReparsePath -Path $Destination
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return }
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

function Copy-SingleFileIfPresent {
    param([string]$Source, [string]$Destination)
    Assert-NoReparsePath -Path $Source
    Assert-NoReparsePath -Path $Destination
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { return }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Assert-NoReparsePath -Path (Split-Path -Parent $Destination)
    Copy-Item -LiteralPath $Source -Destination $Destination
}

function Test-PathWithin {
    param([string]$Candidate, [string]$Root)
    Assert-NoReparsePath -Path $Candidate
    Assert-NoReparsePath -Path $Root
    $FullCandidate = Get-ResolvedOrProjectedPath -Path $Candidate
    $FullRoot = (Get-ResolvedOrProjectedPath -Path $Root).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
    return $FullCandidate.StartsWith($FullRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

$StageRoot = $null
$ValidationRoot = $null
$TemporaryOutputPath = $null
$TemporaryOutputMarker = $null
$TemporaryOutputOwned = $false
$StageMarker = $null
$StageRootOwned = $false
$ValidationMarker = $null
$ValidationRootOwned = $false
$RunToken = [Guid]::NewGuid().ToString("N")
$RuntimePassword = $null
$DatabaseUrl = $null
try {
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    foreach ($PostgresVariable in @("PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER")) {
        [Environment]::SetEnvironmentVariable($PostgresVariable, $null, "Process")
    }
    if (-not $ConfirmMaintenanceWindow) {
        throw "Backup requires -ConfirmMaintenanceWindow after all business writes have stopped."
    }
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $ConfiguredDataRoot = Get-RequiredEnvironmentValue "APP_DATA_ROOT"
    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) {
        throw "APP_DATA_ROOT must reference an existing directory."
    }
    $DataRoot = (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path
    Assert-NoReparsePath -Path $DataRoot
    $env:APP_DATA_ROOT = $DataRoot
    $Deployment = Read-ValidatedDeployment -DataRoot $DataRoot
    $RuntimePassword = Unprotect-Secret ([string]$Deployment.runtimePasswordProtected)
    $DatabaseUrl = New-DatabaseUrl `
        -Role ([string]$Deployment.runtimeRole) `
        -Password $RuntimePassword `
        -Database ([string]$Deployment.databaseName) `
        -Port ([int]$Deployment.databasePort)
    $LegacyRelativeFiles = @("research.db", "research.db-wal", "research.db-shm", "research.db-journal")
    foreach ($LegacyName in $LegacyRelativeFiles) {
        $ConfiguredLegacyDatabase = Get-ResolvedOrProjectedPath -Path (Join-Path (Join-Path $DataRoot "data") $LegacyName)
        $ProjectLegacyDatabase = Get-ResolvedOrProjectedPath -Path (Join-Path (Join-Path $ProjectRoot "data") $LegacyName)
        if (
            -not $ConfiguredLegacyDatabase.Equals($ProjectLegacyDatabase, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $ProjectLegacyDatabase -PathType Leaf)
        ) {
            throw "A legacy project-relative data\$LegacyName exists outside APP_DATA_ROOT. Align the legacy model path before backup; refusing to create an incomplete archive."
        }
    }
    $PidFile = Join-Path (Join-Path $DataRoot "run") "research-management.pid.json"
    if (Test-Path -LiteralPath $PidFile -PathType Leaf) {
        throw "Refusing to back up while the application is recorded as running. Stop it and use a maintenance window."
    }

    $BackupDirectory = Join-Path $DataRoot "backups"
    New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
    Assert-NoReparseTree -Path $BackupDirectory
    $BusinessRoots = @(
        @{ Relative = "data\files"; Source = (Join-Path (Join-Path $DataRoot "data") "files") },
        @{ Relative = "uploads"; Source = (Join-Path $DataRoot "uploads") },
        @{ Relative = "documents"; Source = (Join-Path $DataRoot "documents") },
        @{ Relative = "data\documents"; Source = (Join-Path (Join-Path $DataRoot "data") "documents") },
        @{ Relative = "data\templates"; Source = (Join-Path (Join-Path $DataRoot "data") "templates") },
        @{ Relative = "data\legacy-folder-archive"; Source = (Join-Path (Join-Path $DataRoot "data") "legacy-folder-archive") }
    )
    $BusinessFiles = @(
        "data\research.db",
        "data\research.db-wal",
        "data\research.db-shm",
        "data\research.db-journal"
    )
    if ([string]::IsNullOrWhiteSpace($OutputPath)) {
        $OutputPath = Join-Path $BackupDirectory ("research-management-{0}.zip" -f [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))
    }
    $OutputPath = [IO.Path]::GetFullPath($OutputPath)
    if (Test-Path -LiteralPath $OutputPath) { throw "Backup output already exists: $OutputPath" }
    foreach ($Definition in $BusinessRoots) {
        if (Test-PathWithin -Candidate $OutputPath -Root $Definition.Source) {
            throw "Backup output cannot be inside a business file root."
        }
    }
    $OutputDirectory = Split-Path -Parent $OutputPath
    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) { throw "Backup output must have a parent directory." }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    Assert-NoReparsePath -Path $OutputDirectory
    $TemporaryOutputPath = Join-Path $OutputDirectory (".partial-{0}.zip" -f [Guid]::NewGuid().ToString("N"))
    Assert-NoReparsePath -Path $TemporaryOutputPath

    $StageRoot = Join-Path $BackupDirectory (".backup-staging-{0}" -f [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $StageRoot | Out-Null
    Assert-NoReparseTree -Path $StageRoot
    $StageMarker = "$StageRoot.ownership.json"
    New-OwnershipMarker -Target $StageRoot -Marker $StageMarker -Token $RunToken
    $StageRootOwned = $true
    $PayloadRoot = Join-Path $StageRoot "payload"
    foreach ($Definition in $BusinessRoots) {
        Copy-DirectoryContents -Source $Definition.Source -Destination (Join-Path $PayloadRoot $Definition.Relative)
    }
    foreach ($RelativeFile in $BusinessFiles) {
        Copy-SingleFileIfPresent -Source (Join-Path $DataRoot $RelativeFile) -Destination (Join-Path $PayloadRoot $RelativeFile)
    }
    Assert-NoReparseTree -Path $StageRoot

    Set-PostgresEnvironment -DatabaseUrl $DatabaseUrl -Deployment $Deployment
    $PgDumpExe = Join-Path ([string]$Deployment.postgresRuntime) "bin\pg_dump.exe"
    Assert-NoReparsePath -Path $PgDumpExe
    if (-not (Test-Path -LiteralPath $PgDumpExe -PathType Leaf)) {
        throw "Bundled pg_dump.exe is missing from the validated deployment."
    }
    $DumpPath = Join-Path $StageRoot "database.dump"
    $ConfiguredPython = $env:PYTHON_EXE
    $DefaultVenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if ([string]::IsNullOrWhiteSpace($ConfiguredPython) -and (Test-Path -LiteralPath $DefaultVenvPython -PathType Leaf)) {
        $ConfiguredPython = $DefaultVenvPython
    }
    $PythonExe = Resolve-Executable -ConfiguredValue $ConfiguredPython -FallbackName "python.exe"
    $Verifier = Join-Path $PSScriptRoot "verify_offline.py"
    $Manifest = Join-Path $StageRoot "manifest.json"
    $env:DATABASE_URL = $DatabaseUrl
    & $PythonExe @($Verifier, "snapshot", "--data-root", $DataRoot, "--package-root", $StageRoot, "--output", $Manifest, "--pg-dump-exe", $PgDumpExe, "--dump-output", $DumpPath)
    if ($LASTEXITCODE -ne 0) { throw "Backup snapshot verification failed." }
    & $PythonExe @($Verifier, "verify-package", "--package-root", $StageRoot, "--manifest", $Manifest)
    if ($LASTEXITCODE -ne 0) { throw "Backup package verification failed." }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    # This GUID path is owned only by this invocation. The final path is published
    # later with a no-overwrite move, avoiding races with another backup process.
    [IO.Compression.ZipFile]::CreateFromDirectory($StageRoot, $TemporaryOutputPath, [IO.Compression.CompressionLevel]::Optimal, $false)
    $TemporaryOutputMarker = "$TemporaryOutputPath.ownership.json"
    New-OwnershipMarker -Target $TemporaryOutputPath -Marker $TemporaryOutputMarker -Token $RunToken
    $TemporaryOutputOwned = $true
    $ValidationRoot = Join-Path $BackupDirectory (".backup-validation-{0}" -f [Guid]::NewGuid().ToString("N"))
    & $PythonExe @($Verifier, "extract", "--archive", $TemporaryOutputPath, "--destination", $ValidationRoot)
    if ($LASTEXITCODE -ne 0) { throw "Final backup archive extraction check failed." }
    $ValidationMarker = "$ValidationRoot.ownership.json"
    New-OwnershipMarker -Target $ValidationRoot -Marker $ValidationMarker -Token $RunToken
    $ValidationRootOwned = $true
    & $PythonExe @($Verifier, "verify-package", "--package-root", $ValidationRoot, "--manifest", (Join-Path $ValidationRoot "manifest.json"))
    if ($LASTEXITCODE -ne 0) { throw "Final backup archive verification failed." }
    Assert-OwnershipMarker -Target $TemporaryOutputPath -Marker $TemporaryOutputMarker -Token $RunToken
    [IO.File]::Move($TemporaryOutputPath, $OutputPath)
    [IO.File]::Delete($TemporaryOutputMarker)
    $TemporaryOutputOwned = $false
    Write-Host "Verified maintenance-window backup created: $OutputPath"
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: {0}", $_.Exception.Message)
    exit 1
}
finally {
    $RuntimePassword = $null
    $DatabaseUrl = $null
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("PGPASSWORD", $null, "Process")
    [Environment]::SetEnvironmentVariable("PGHOST", $null, "Process")
    [Environment]::SetEnvironmentVariable("PGPORT", $null, "Process")
    [Environment]::SetEnvironmentVariable("PGDATABASE", $null, "Process")
    [Environment]::SetEnvironmentVariable("PGUSER", $null, "Process")
    if ($TemporaryOutputOwned) {
        Remove-OwnedFile -Target $TemporaryOutputPath -Marker $TemporaryOutputMarker -Token $RunToken
    }
    if ($ValidationRootOwned) {
        Remove-OwnedDirectory -Target $ValidationRoot -Marker $ValidationMarker -Token $RunToken
    }
    if ($StageRootOwned) {
        Remove-OwnedDirectory -Target $StageRoot -Marker $StageMarker -Token $RunToken
    }
}
