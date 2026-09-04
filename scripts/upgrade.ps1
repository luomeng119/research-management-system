[CmdletBinding()]
param(
    [string]$OfflineRoot = (Join-Path $PSScriptRoot "..\offline"),
    [string]$VirtualEnvironment = (Join-Path $PSScriptRoot "..\.venv"),
    [ValidateSet("", "AFTER_BACKUP", "AFTER_SCHEMA", "AFTER_VENV_SWITCH")]
    [string]$FaultInjectionPoint = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$StagingEnvironment = $null
$PreviousEnvironment = $null
$PostgresStarted = $false
$PgCtlExe = $null
$PostgresData = $null
$BackupCompleted = $false
$SchemaMayBeCommitted = $false
$VenvSwitched = $false
$PreUpgradeBackup = $null
$UpgradeStatePath = $null
$MayWriteUpgradeState = $false
$OldRevision = $null
$ExpectedHead = $null
$RecoveryRequired = $false

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
    if (-not (Test-Path -LiteralPath $DeploymentPath -PathType Leaf)) { throw "Deployment state was not found; run install.ps1 before upgrade.ps1." }
    try { $State = Get-Content -LiteralPath $DeploymentPath -Raw | ConvertFrom-Json } catch { throw "Deployment state is invalid; refusing to upgrade." }
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
    param([string]$Role, [string]$Password, [string]$Database, [int]$Port)
    return "postgresql+psycopg://${Role}:$([Uri]::EscapeDataString($Password))@127.0.0.1:$Port/$Database"
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

function Invoke-Captured {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $Output = @(& $Executable @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE: $Executable" }
    return (($Output | ForEach-Object { [string]$_ }) -join "`n").Trim()
}

function Get-PostgresRuntimeState {
    param([string]$Executable, [string]$DataRoot)
    & $Executable @("status", "-D", $DataRoot) *> $null
    switch ($LASTEXITCODE) {
        0 { return "RUNNING" }
        3 { return "STOPPED" }
        default { return "UNKNOWN" }
    }
}

function Invoke-UpgradeFault {
    param([Parameter(Mandatory = $true)][string]$Point)
    if ($FaultInjectionPoint -eq $Point) { throw "Injected upgrade failure at $Point." }
}

function Write-UpgradeState {
    param([Parameter(Mandatory = $true)][string]$Status)
    if ([string]::IsNullOrWhiteSpace($UpgradeStatePath) -or -not $MayWriteUpgradeState) { return }
    $Payload = [ordered]@{
        schemaVersion = 1
        status = $Status
        backupCompleted = $BackupCompleted
        schemaMayBeCommitted = $SchemaMayBeCommitted
        venvSwitched = $VenvSwitched
        backupPath = $PreUpgradeBackup
        stagingEnvironment = $StagingEnvironment
        previousEnvironment = $PreviousEnvironment
        oldRevision = $OldRevision
        expectedHead = $ExpectedHead
        recoveryRequired = $RecoveryRequired
        updatedAtUtc = [DateTime]::UtcNow.ToString("o")
    }
    $TemporaryState = "$UpgradeStatePath.tmp-$([Guid]::NewGuid().ToString('N'))"
    $Payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $TemporaryState -Encoding UTF8
    Move-Item -LiteralPath $TemporaryState -Destination $UpgradeStatePath -Force
}

function Get-PostgresVersion {
    param([Parameter(Mandatory = $true)][string]$Executable)
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw "PostgreSQL executable is missing: $Executable" }
    $Output = Invoke-Captured -Executable $Executable -Arguments @("--version")
    if ($Output -notmatch '(?i)PostgreSQL\)?\s+([0-9]+(?:\.[0-9]+){1,2})') {
        throw "Could not parse PostgreSQL version from $Executable."
    }
    return $Matches[1]
}

function Assert-PostgresRuntimeVersionMatch {
    param($Manifest, [string]$InstalledRuntime, [string]$BundleRoot)
    $Declared = [string]$Manifest.target.postgresqlServerVersion
    if ([string]::IsNullOrWhiteSpace($Declared)) { throw "Manifest PostgreSQL version is missing." }
    $InstalledExe = Join-Path $InstalledRuntime "bin\postgres.exe"
    $BundledExe = Join-Path $BundleRoot "runtime\postgresql\bin\postgres.exe"
    $InstalledVersion = Get-PostgresVersion -Executable $InstalledExe
    $BundledVersion = Get-PostgresVersion -Executable $BundledExe
    if ($InstalledVersion -ne $Declared -or $BundledVersion -ne $Declared -or $InstalledVersion -ne $BundledVersion) {
        throw "PostgreSQL runtime upgrades are not supported; installed, bundle, and manifest versions must match exactly."
    }
}

function Assert-PostgresDataMajor {
    param([string]$DataRoot, [string]$PostgresExecutable)
    $VersionFile = Join-Path $DataRoot "PG_VERSION"
    if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) { throw "PostgreSQL data PG_VERSION is missing." }
    $DataMajor = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
    if ($DataMajor -notmatch '^[0-9]+$') { throw "PostgreSQL data PG_VERSION is invalid." }
    $BinaryVersion = Get-PostgresVersion -Executable $PostgresExecutable
    $BinaryMajor = $BinaryVersion.Split('.')[0]
    if ($DataMajor -ne $BinaryMajor) {
        throw "PostgreSQL data major version $DataMajor does not match binary version $BinaryVersion."
    }
}

function Invoke-PreUpgradeBackup {
    param([string]$DataRoot)
    $BackupDirectory = Join-Path $DataRoot "backups"
    New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
    $script:PreUpgradeBackup = Join-Path $BackupDirectory (
        "pre-upgrade-{0}-{1}.zip" -f [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"), [Guid]::NewGuid().ToString("N")
    )
    $BackupScript = Join-Path $PSScriptRoot "backup.ps1"
    $PowerShellExe = (Get-Process -Id $PID).Path
    Invoke-Checked -Executable $PowerShellExe -Arguments @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $BackupScript,
        "-OutputPath", $script:PreUpgradeBackup, "-ConfirmMaintenanceWindow"
    )
    if (-not (Test-Path -LiteralPath $script:PreUpgradeBackup -PathType Leaf)) {
        throw "Pre-upgrade backup did not publish a verified archive."
    }
    $script:BackupCompleted = $true
    Write-UpgradeState -Status "BACKUP_VERIFIED"
}

function Get-AlembicHead {
    param([string]$Python)
    $Output = Invoke-Captured -Executable $Python -Arguments @("-m", "alembic", "heads")
    $MatchesFound = @([Regex]::Matches($Output, '(?m)^([A-Za-z0-9_.-]+)\s+\(head\)\s*$'))
    if ($MatchesFound.Count -ne 1) { throw "Upgrade package must contain exactly one Alembic head." }
    return $MatchesFound[0].Groups[1].Value
}

function Get-DatabaseRevision {
    param([string]$Python)
    $Code = "from sqlalchemy import create_engine,text; import os; e=create_engine(os.environ['MIGRATION_DATABASE_URL']); c=e.connect(); rows=c.execute(text('SELECT version_num FROM alembic_version')).scalars().all(); c.close(); e.dispose(); assert len(rows)==1; print(rows[0])"
    return (Invoke-Captured -Executable $Python -Arguments @("-c", $Code)).Trim()
}

function Assert-DatabaseRevision {
    param([string]$Python, [string]$Expected)
    $Actual = Get-DatabaseRevision -Python $Python
    if ($Actual -ne $Expected) { throw "Database revision $Actual does not match the new unique head $Expected." }
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
        "runtime/postgresql/bin/initdb.exe",
        "runtime/postgresql/bin/pg_ctl.exe",
        "runtime/postgresql/bin/postgres.exe",
        "runtime/postgresql/bin/psql.exe",
        "runtime/postgresql/bin/pg_dump.exe",
        "runtime/postgresql/bin/pg_restore.exe",
        "runtime/postgresql/bin/libpq.dll",
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
    return $Manifest
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
    foreach ($SensitiveName in @("MIGRATION_DATABASE_URL", "DATABASE_URL", "FLASK_SECRET_KEY", "PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER")) {
        [Environment]::SetEnvironmentVariable($SensitiveName, $null, "Process")
    }
    $ConfiguredDataRoot = Get-RequiredEnvironmentValue "APP_DATA_ROOT"
    if (-not (Test-Path -LiteralPath $ConfiguredDataRoot -PathType Container)) { throw "APP_DATA_ROOT must reference an existing directory." }
    $DataRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path -InspectTree
    $Deployment = Read-ValidatedDeployment -DataRoot $DataRoot
    $PostgresData = (Resolve-Path -LiteralPath ([string]$Deployment.postgresData)).Path
    $PostgresRuntime = (Resolve-Path -LiteralPath ([string]$Deployment.postgresRuntime)).Path
    $PgCtlExe = Join-Path $PostgresRuntime "bin\pg_ctl.exe"
    if (-not (Test-Path -LiteralPath $PgCtlExe -PathType Leaf)) { throw "Bundled pg_ctl.exe is missing." }
    & $PgCtlExe @("status", "-D", $PostgresData) *> $null
    if ($LASTEXITCODE -eq 0) { throw "PostgreSQL cluster must be stopped before upgrading." }
    $PostgresPidState = Join-Path (Join-Path $DataRoot "run") "postgresql.pid.json"
    if (Test-Path -LiteralPath $PostgresPidState -PathType Leaf) { throw "PostgreSQL PID state exists; resolve it before upgrading." }

    $PidFile = Join-Path (Join-Path $DataRoot "run") "research-management.pid.json"
    if (Test-Path -LiteralPath $PidFile -PathType Leaf) {
        try {
            $PidState = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
            $RecordedProcess = Get-Process -Id ([int]$PidState.pid) -ErrorAction SilentlyContinue
        }
        catch { throw "PID file is invalid; refusing to upgrade." }
        if ($null -ne $RecordedProcess) { throw "Stop the application before upgrading." }
        throw "A stale PID file exists; run stop.ps1 and resolve it before upgrading."
    }
    $BindHost = $env:APP_BIND_HOST
    if ([string]::IsNullOrWhiteSpace($BindHost) -or $BindHost -eq "0.0.0.0") { $BindHost = "127.0.0.1" }
    $Port = 5001
    if (-not [string]::IsNullOrWhiteSpace($env:APP_PORT)) {
        if (-not [int]::TryParse($env:APP_PORT, [ref]$Port) -or $Port -lt 1 -or $Port -gt 65535) { throw "APP_PORT must be an integer between 1 and 65535." }
    }
    if (Test-TcpPortInUse -HostName $BindHost -Port $Port) { throw "The configured application port is active; refusing a hot upgrade." }
    $WaitressProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and $_.CommandLine -match "waitress" -and $_.CommandLine -match "app:create_app"
    })
    if ($WaitressProcesses.Count -gt 0) { throw "A Waitress application process is active; refusing a hot upgrade." }

    if (-not (Test-Path -LiteralPath $OfflineRoot -PathType Container)) { throw "Offline root does not exist: $OfflineRoot" }
    $OfflineRoot = (Resolve-Path -LiteralPath $OfflineRoot).Path
    $Manifest = Assert-OfflineManifest -Root $OfflineRoot
    Assert-PostgresRuntimeVersionMatch -Manifest $Manifest -InstalledRuntime $PostgresRuntime -BundleRoot $OfflineRoot
    Assert-PostgresDataMajor -DataRoot $PostgresData -PostgresExecutable (Join-Path $PostgresRuntime "bin\postgres.exe")
    $Wheelhouse = Join-Path $OfflineRoot "wheelhouse"
    $Requirements = Join-Path $OfflineRoot "requirements.txt"
    $RuntimePython = Join-Path $OfflineRoot "runtime\python\python.exe"
    $VirtualEnvironment = [IO.Path]::GetFullPath($VirtualEnvironment)
    $VenvPython = Join-Path $VirtualEnvironment "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) { throw "Installed virtual environment was not found; run install.ps1 first." }

    $UpgradeStatePath = Join-Path (Join-Path $DataRoot "run") "upgrade-state.json"
    if (Test-Path -LiteralPath $UpgradeStatePath -PathType Leaf) {
        try { $ExistingUpgradeState = Get-Content -LiteralPath $UpgradeStatePath -Raw | ConvertFrom-Json }
        catch { throw "Existing upgrade state is invalid; refusing to overwrite it." }
        if ([string]$ExistingUpgradeState.status -ne "COMPLETED") {
            throw "An incomplete upgrade state exists; recovery must be resolved before another upgrade."
        }
    }
    $MayWriteUpgradeState = $true
    Write-UpgradeState -Status "PREPARING_BACKUP"
    Invoke-Checked -Executable $PgCtlExe -Arguments @(
        "start", "-D", $PostgresData, "-l", (Join-Path (Join-Path $DataRoot "logs") "postgresql.log"),
        "-w", "-o", "-h 127.0.0.1 -p $([int]$Deployment.databasePort)"
    )
    $PostgresStarted = $true
    $env:PYTHON_EXE = $VenvPython
    Invoke-PreUpgradeBackup -DataRoot $DataRoot
    [Environment]::SetEnvironmentVariable("PYTHON_EXE", $null, "Process")
    Invoke-UpgradeFault -Point "AFTER_BACKUP"

    $EnvironmentParent = Split-Path -Parent $VirtualEnvironment
    $EnvironmentName = Split-Path -Leaf $VirtualEnvironment
    $StagingEnvironment = Join-Path $EnvironmentParent ($EnvironmentName + ".upgrading-" + [Guid]::NewGuid().ToString("N"))
    $PreviousEnvironment = Join-Path $EnvironmentParent (
        $EnvironmentName + ".previous-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss") + "-" + [Guid]::NewGuid().ToString("N")
    )
    Write-UpgradeState -Status "BUILDING_STAGING_VENV"
    Invoke-Checked -Executable $RuntimePython -Arguments @("-m", "venv", $StagingEnvironment)
    $StagingPython = Join-Path $StagingEnvironment "Scripts\python.exe"
    Invoke-Checked -Executable $StagingPython -Arguments @(
        "-m", "pip", "install", "--only-binary=:all:", "--no-index", "--find-links", $Wheelhouse, "--requirement", $Requirements
    )

    $OwnerPassword = Unprotect-Secret ([string]$Deployment.ownerPasswordProtected)
    $MigrationDatabaseUrl = New-DatabaseUrl -Role ([string]$Deployment.ownerRole) -Password $OwnerPassword -Database ([string]$Deployment.databaseName) -Port ([int]$Deployment.databasePort)
    $env:MIGRATION_DATABASE_URL = $MigrationDatabaseUrl
    Push-Location $ProjectRoot
    try {
        $ExpectedHead = Get-AlembicHead -Python $StagingPython
        $OldRevision = Get-DatabaseRevision -Python $StagingPython
        $SchemaMayBeCommitted = $true
        Write-UpgradeState -Status "SCHEMA_MIGRATION_STARTED"
        Invoke-Checked -Executable $StagingPython -Arguments @("-m", "alembic", "upgrade", "head")
        $Provisioner = Join-Path $PSScriptRoot "provision_postgres.py"
        Invoke-Checked -Executable $StagingPython -Arguments @(
            $Provisioner, "--runtime-role", ([string]$Deployment.runtimeRole)
        )
        Assert-DatabaseRevision -Python $StagingPython -Expected $ExpectedHead
        Write-UpgradeState -Status "SCHEMA_AT_NEW_HEAD"
        Invoke-UpgradeFault -Point "AFTER_SCHEMA"
    }
    finally { Pop-Location }

    Move-Item -LiteralPath $VirtualEnvironment -Destination $PreviousEnvironment
    Move-Item -LiteralPath $StagingEnvironment -Destination $VirtualEnvironment
    $StagingEnvironment = $null
    $VenvSwitched = $true
    Write-UpgradeState -Status "VENV_SWITCHED"
    Invoke-UpgradeFault -Point "AFTER_VENV_SWITCH"
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    Invoke-Checked -Executable $PgCtlExe -Arguments @("stop", "-D", $PostgresData, "-m", "fast", "-w")
    $PostgresStarted = $false
    Write-UpgradeState -Status "COMPLETED"
    Write-Host "Offline dependency and schema upgrade completed."
    Write-Host "Previous virtual environment retained for manual recovery only: $PreviousEnvironment"
    Write-Host "Pre-upgrade backup: $PreUpgradeBackup"
    exit 0
}
catch {
    $OriginalError = $_.Exception.Message
    $StopError = $null
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
    if ($PostgresStarted -and $null -ne $PgCtlExe -and $null -ne $PostgresData) {
        try {
            Invoke-Checked -Executable $PgCtlExe -Arguments @("stop", "-D", $PostgresData, "-m", "fast", "-w")
            $PostgresStarted = $false
        }
        catch { $StopError = $_.Exception.Message }
    }
    $PostgresStateAfterFailure = "UNKNOWN"
    if ($null -ne $PgCtlExe -and $null -ne $PostgresData) {
        try { $PostgresStateAfterFailure = Get-PostgresRuntimeState -Executable $PgCtlExe -DataRoot $PostgresData }
        catch { $PostgresStateAfterFailure = "UNKNOWN" }
    }
    if ($PostgresStateAfterFailure -eq "STOPPED") { $PostgresStarted = $false }
    $RecoveryRequired = $true
    $FailureStatus = if ($PostgresStateAfterFailure -eq "STOPPED") {
        "FAILED_STOPPED"
    }
    elseif ($PostgresStateAfterFailure -eq "RUNNING") {
        "FAILED_POSTGRES_RUNNING"
    }
    else {
        "FAILED_POSTGRES_STATE_UNKNOWN"
    }
    try { Write-UpgradeState -Status $FailureStatus } catch { [Console]::Error.WriteLine("STATE ERROR: {0}", $_.Exception.Message) }
    if ($SchemaMayBeCommitted) {
        [Console]::Error.WriteLine("Automatic rollback is disabled because the schema may have committed. The application remains stopped.")
    }
    elseif (-not $VenvSwitched) {
        [Console]::Error.WriteLine("The database revision was not marked changed and the active virtual environment was not switched.")
    }
    if ($BackupCompleted) { [Console]::Error.WriteLine("Pre-upgrade backup: {0}", $PreUpgradeBackup) }
    if ($null -ne $StopError) { [Console]::Error.WriteLine("POSTGRES STOP ERROR: {0}", $StopError) }
    [Console]::Error.WriteLine("ERROR: {0}", $OriginalError)
    exit 1
}
finally {
    $OwnerPassword = $null
    $MigrationDatabaseUrl = $null
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
    foreach ($SensitiveName in @("MIGRATION_DATABASE_URL", "DATABASE_URL", "PYTHON_EXE", "PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER")) {
        [Environment]::SetEnvironmentVariable($SensitiveName, $null, "Process")
    }
}
