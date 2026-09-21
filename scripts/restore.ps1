[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ArchivePath,
    [switch]$ConfirmStaging
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-RequiredEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    $Value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "Required environment variable $Name is not configured." }
    return $Value
}

function Resolve-Executable {
    param([AllowEmptyString()][string]$ConfiguredValue, [string]$FallbackName)
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredValue)) {
        if (Test-Path -LiteralPath $ConfiguredValue -PathType Leaf) { return (Resolve-Path -LiteralPath $ConfiguredValue).Path }
        $ConfiguredCommand = Get-Command $ConfiguredValue -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $ConfiguredCommand) { return $ConfiguredCommand.Source }
        throw "Configured executable was not found: $ConfiguredValue"
    }
    $FallbackCommand = Get-Command $FallbackName -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $FallbackCommand) { throw "Required executable was not found: $FallbackName" }
    return $FallbackCommand.Source
}

function Set-PostgresEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$DatabaseUrl,
        [string]$VariableName = "DATABASE_URL"
    )
    $Normalized = [Regex]::Replace($DatabaseUrl, '^postgresql(?:\+[^:]+)?://', 'postgresql://')
    try { $Uri = [Uri]$Normalized } catch { throw "$VariableName is not a valid PostgreSQL URL." }
    if ($Uri.Scheme -ne "postgresql" -or [string]::IsNullOrWhiteSpace($Uri.Host)) { throw "$VariableName must be a PostgreSQL URL." }
    if (-not [string]::IsNullOrWhiteSpace($Uri.Query) -or -not [string]::IsNullOrWhiteSpace($Uri.Fragment)) {
        throw "$VariableName must not include query parameters or fragments."
    }
    $UserParts = $Uri.UserInfo.Split([char[]]@(':'), 2)
    if ($UserParts.Count -lt 1 -or [string]::IsNullOrWhiteSpace($UserParts[0])) { throw "$VariableName must include a PostgreSQL user." }
    $env:PGHOST = $Uri.Host
    $env:PGPORT = if ($Uri.IsDefaultPort) { "5432" } else { [string]$Uri.Port }
    $env:PGDATABASE = [Uri]::UnescapeDataString($Uri.AbsolutePath.TrimStart('/'))
    $env:PGUSER = [Uri]::UnescapeDataString($UserParts[0])
    $env:PGPASSWORD = if ($UserParts.Count -eq 2) { [Uri]::UnescapeDataString($UserParts[1]) } else { "" }
    if ([string]::IsNullOrWhiteSpace($env:PGDATABASE)) { throw "$VariableName must include a database name." }
    return [PSCustomObject]@{
        Host = $env:PGHOST
        Port = $env:PGPORT
        Database = $env:PGDATABASE
        User = $env:PGUSER
    }
}

function Test-DirectoryEmpty {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    return $null -eq (Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1)
}

function Assert-NoReparsePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Current = [IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrWhiteSpace($Current)) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Restore path contains a junction or reparse point: $Current"
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
                throw "Restore tree contains a junction or reparse point: $($_.FullName)"
            }
            if ($_.PSIsContainer) { $Pending.Push($_.FullName) }
        }
    }
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

function Test-PathWithin {
    param([string]$Candidate, [string]$Root)
    $FullCandidate = Get-ResolvedOrProjectedPath -Path $Candidate
    $FullRoot = (Get-ResolvedOrProjectedPath -Path $Root).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
    return $FullCandidate.StartsWith($FullRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Copy-DirectoryContents {
    param([string]$Source, [string]$Destination)
    Assert-NoReparseTree -Path $Source
    Assert-NoReparsePath -Path $Destination
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { throw "Backup payload root is missing: $Source" }
    Assert-NoReparseTree -Path $Destination
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
    Assert-NoReparseTree -Path $Destination
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

$ExtractRoot = $null
$FailedMarker = $null
$StagingMutationStarted = $false
try {
    if (-not $ConfirmStaging) {
        throw "Restore is staging-only. Supply -ConfirmStaging and use an empty staging database and APP_DATA_ROOT."
    }
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $ArchivePath = (Resolve-Path -LiteralPath $ArchivePath).Path
    $ConfiguredDataRoot = Get-RequiredEnvironmentValue "APP_DATA_ROOT"
    $RuntimeDatabaseUrl = Get-RequiredEnvironmentValue "DATABASE_URL"
    $MigrationDatabaseUrl = Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"
    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) { throw "APP_DATA_ROOT must reference an existing staging directory." }
    $DataRoot = (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path
    Assert-NoReparseTree -Path $DataRoot
    $env:APP_DATA_ROOT = $DataRoot
    $LegacyRelativeFiles = @("research.db", "research.db-wal", "research.db-shm", "research.db-journal")
    foreach ($LegacyName in $LegacyRelativeFiles) {
        $ConfiguredLegacyDatabase = Get-ResolvedOrProjectedPath -Path (Join-Path (Join-Path $DataRoot "data") $LegacyName)
        $ProjectLegacyDatabase = Get-ResolvedOrProjectedPath -Path (Join-Path (Join-Path $ProjectRoot "data") $LegacyName)
        if (
            -not $ConfiguredLegacyDatabase.Equals($ProjectLegacyDatabase, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $ProjectLegacyDatabase -PathType Leaf)
        ) {
            throw "A legacy project-relative data\$LegacyName exists outside APP_DATA_ROOT. Restore refuses to overwrite or merge that separate data store."
        }
    }
    $FailedMarker = Join-Path $DataRoot "restore.failed.json"
    if (Test-Path -LiteralPath $FailedMarker) {
        throw "This staging target is marked failed and cannot be reused. Discard it and create a new empty staging target."
    }
    $PidFile = Join-Path (Join-Path $DataRoot "run") "research-management.pid.json"
    if (Test-Path -LiteralPath $PidFile -PathType Leaf) { throw "Restore target is recorded as running; staging must be stopped." }

    $TargetRoots = @(
        (Join-Path (Join-Path $DataRoot "data") "files"),
        (Join-Path $DataRoot "uploads"),
        (Join-Path $DataRoot "documents"),
        (Join-Path (Join-Path $DataRoot "data") "documents"),
        (Join-Path (Join-Path $DataRoot "data") "templates"),
        (Join-Path (Join-Path $DataRoot "data") "legacy-folder-archive")
    )
    $TargetFiles = @(
        (Join-Path (Join-Path $DataRoot "data") "research.db"),
        (Join-Path (Join-Path $DataRoot "data") "research.db-wal"),
        (Join-Path (Join-Path $DataRoot "data") "research.db-shm"),
        (Join-Path (Join-Path $DataRoot "data") "research.db-journal")
    )
    foreach ($Target in $TargetRoots) {
        Assert-NoReparseTree -Path $Target
        if (-not (Test-DirectoryEmpty -Path $Target)) { throw "Restore business-file target must be empty: $Target" }
    }
    foreach ($TargetFile in $TargetFiles) {
        Assert-NoReparsePath -Path $TargetFile
        if (Test-Path -LiteralPath $TargetFile) { throw "Restore business-file target must be empty: $TargetFile" }
    }

    $RuntimeConnection = Set-PostgresEnvironment -DatabaseUrl $RuntimeDatabaseUrl -VariableName "DATABASE_URL"
    $RuntimeRole = $RuntimeConnection.User
    $MigrationConnection = Set-PostgresEnvironment -DatabaseUrl $MigrationDatabaseUrl -VariableName "MIGRATION_DATABASE_URL"
    if (
        -not $RuntimeConnection.Host.Equals($MigrationConnection.Host, [StringComparison]::OrdinalIgnoreCase) -or
        $RuntimeConnection.Port -ne $MigrationConnection.Port -or
        $RuntimeConnection.Database -ne $MigrationConnection.Database
    ) {
        throw "DATABASE_URL and MIGRATION_DATABASE_URL must target the same staging database."
    }
    if ($RuntimeRole.Equals($MigrationConnection.User, [StringComparison]::OrdinalIgnoreCase)) {
        throw "DATABASE_URL runtime role must differ from the MIGRATION_DATABASE_URL owner role."
    }
    $PsqlExe = Resolve-Executable -ConfiguredValue $env:PSQL_EXE -FallbackName "psql.exe"
    $SchemaObjectQuery = @"
SELECT
    (SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public')
  + (SELECT count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public')
  + (SELECT count(*) FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public');
"@
    $TableCountOutput = & $PsqlExe @("--no-password", "--tuples-only", "--no-align", "--command=$SchemaObjectQuery")
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the staging database." }
    $TableCount = 0
    if (-not [int]::TryParse(([string]$TableCountOutput).Trim(), [ref]$TableCount) -or $TableCount -ne 0) {
        throw "Restore database must be empty."
    }

    $ConfiguredPython = $env:PYTHON_EXE
    $DefaultVenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if ([string]::IsNullOrWhiteSpace($ConfiguredPython) -and (Test-Path -LiteralPath $DefaultVenvPython -PathType Leaf)) { $ConfiguredPython = $DefaultVenvPython }
    $PythonExe = Resolve-Executable -ConfiguredValue $ConfiguredPython -FallbackName "python.exe"
    $Verifier = Join-Path $PSScriptRoot "verify_offline.py"
    $ExtractRoot = Join-Path $DataRoot (".restore-package-{0}" -f [Guid]::NewGuid().ToString("N"))
    if (-not (Test-PathWithin -Candidate $ExtractRoot -Root $DataRoot)) { throw "Restore extraction root must remain inside APP_DATA_ROOT." }
    Assert-NoReparsePath -Path $ArchivePath
    Assert-NoReparsePath -Path $ExtractRoot
    & $PythonExe @($Verifier, "begin-restore", "--data-root", $DataRoot)
    if ($LASTEXITCODE -ne 0) { throw "Could not create the exclusive restore marker; refusing to write." }
    $StagingMutationStarted = $true
    # Recheck while holding the exclusive marker: pre-lock checks may be stale.
    foreach ($Target in $TargetRoots) {
        Assert-NoReparseTree -Path $Target
        if (-not (Test-DirectoryEmpty -Path $Target)) { throw "Restore business-file target must be empty: $Target" }
    }
    foreach ($TargetFile in $TargetFiles) {
        Assert-NoReparsePath -Path $TargetFile
        if (Test-Path -LiteralPath $TargetFile) { throw "Restore business-file target must be empty: $TargetFile" }
    }
    $TableCountOutput = & $PsqlExe @("--no-password", "--tuples-only", "--no-align", "--command=$SchemaObjectQuery")
    if ($LASTEXITCODE -ne 0) { throw "Could not recheck the staging database." }
    $TableCount = 0
    if (-not [int]::TryParse(([string]$TableCountOutput).Trim(), [ref]$TableCount) -or $TableCount -ne 0) {
        throw "Restore database must still be empty after acquiring its marker."
    }
    & $PythonExe @($Verifier, "extract", "--archive", $ArchivePath, "--destination", $ExtractRoot)
    if ($LASTEXITCODE -ne 0) { throw "Backup archive extraction failed." }
    Assert-NoReparseTree -Path $ExtractRoot
    $Manifest = Join-Path $ExtractRoot "manifest.json"
    & $PythonExe @($Verifier, "verify-package", "--package-root", $ExtractRoot, "--manifest", $Manifest)
    if ($LASTEXITCODE -ne 0) { throw "Backup package verification failed before restore." }

    $PgRestoreExe = Resolve-Executable -ConfiguredValue $env:PG_RESTORE_EXE -FallbackName "pg_restore.exe"
    $DumpPath = Join-Path $ExtractRoot "database.dump"
    $null = & $PgRestoreExe @("--list", "--no-password", $DumpPath)
    if ($LASTEXITCODE -ne 0) { throw "database.dump is not a readable PostgreSQL custom-format archive." }
    & $PgRestoreExe @("--dbname=$($MigrationConnection.Database)", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--no-password", $DumpPath)
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE. Discard this staging target." }

    $Provisioner = Join-Path $PSScriptRoot "provision_postgres.py"
    & $PythonExe @($Provisioner, "--runtime-role", $RuntimeRole)
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL runtime ACL provisioning failed. Discard this staging target." }
    $null = Set-PostgresEnvironment -DatabaseUrl $RuntimeDatabaseUrl -VariableName "DATABASE_URL"

    Copy-DirectoryContents -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\files") -Destination $TargetRoots[0]
    Copy-DirectoryContents -Source (Join-Path (Join-Path $ExtractRoot "payload") "uploads") -Destination $TargetRoots[1]
    Copy-DirectoryContents -Source (Join-Path (Join-Path $ExtractRoot "payload") "documents") -Destination $TargetRoots[2]
    Copy-DirectoryContents -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\documents") -Destination $TargetRoots[3]
    Copy-DirectoryContents -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\templates") -Destination $TargetRoots[4]
    Copy-DirectoryContents -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\legacy-folder-archive") -Destination $TargetRoots[5]
    Copy-SingleFileIfPresent -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\research.db") -Destination $TargetFiles[0]
    Copy-SingleFileIfPresent -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\research.db-wal") -Destination $TargetFiles[1]
    Copy-SingleFileIfPresent -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\research.db-shm") -Destination $TargetFiles[2]
    Copy-SingleFileIfPresent -Source (Join-Path (Join-Path $ExtractRoot "payload") "data\research.db-journal") -Destination $TargetFiles[3]
    & $PythonExe @($Verifier, "verify", "--data-root", $DataRoot, "--manifest", $Manifest, "--complete-restore")
    if ($LASTEXITCODE -ne 0) { throw "Restored staging data failed verification; do not cut over." }

    Write-Host "Restore verified in the empty staging target. It is now eligible for a separate cutover decision."
    exit 0
}
catch {
    if ($StagingMutationStarted -and $null -ne $FailedMarker) {
        $FailureRecord = @{
            status = "FAILED"
            recordedAtUtc = [DateTime]::UtcNow.ToString("o")
            action = "Discard this staging target and create a new empty target."
        } | ConvertTo-Json
        [IO.File]::WriteAllText($FailedMarker, $FailureRecord, (New-Object Text.UTF8Encoding($false)))
    }
    [Console]::Error.WriteLine("ERROR: {0}", $_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $ExtractRoot -and (Test-Path -LiteralPath $ExtractRoot -PathType Container)) {
        [IO.Directory]::Delete($ExtractRoot, $true)
    }
}
