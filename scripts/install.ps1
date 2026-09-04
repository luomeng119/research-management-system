[CmdletBinding()]
param(
    [string]$OfflineRoot = (Join-Path $PSScriptRoot "..\offline"),
    [string]$VirtualEnvironment = (Join-Path $PSScriptRoot "..\.venv"),
    [string]$DatabaseName = "research_management",
    [string]$BootstrapRole = "rm_bootstrap",
    [string]$OwnerRole = "rm_owner",
    [string]$RuntimeRole = "rm_runtime",
    [int]$DatabasePort = 55432
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$StagingEnvironment = $null
$NewEnvironmentCreated = $false
$PostgresRuntimeStaging = $null
$PostgresRuntimeCreated = $false
$PostgresDataCreated = $false
$PostgresStarted = $false
$PostgresStopConfirmed = $false
$DeploymentStateCreated = $false
$OwnershipToken = [Guid]::NewGuid().ToString("N")
$PostgresRuntimeMarker = $null
$PostgresDataMarker = $null
$EnvironmentMarker = $null

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

function Invoke-CheckedOutput {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $Output = (& $Executable @Arguments 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE: $Executable"
    }
    return $Output
}

function Assert-DeclaredVersion {
    param([string]$Output, [string]$Declared, [string]$Component)
    $Match = [Regex]::Match($Output, '(?<!\d)(\d+\.\d+(?:\.\d+)*)(?!\d)')
    if (-not $Match.Success) { throw "$Component version probe returned no version." }
    $Actual = $Match.Groups[1].Value
    if ($Actual -ne $Declared -and -not $Actual.StartsWith($Declared + '.', [StringComparison]::Ordinal)) {
        throw "$Component runtime version $Actual does not match declared version $Declared."
    }
    return $Actual
}

function Assert-BundledRuntimeVersions {
    param(
        [object]$Manifest, [string]$RuntimePython, [string]$BundledPostgres,
        [string]$ProbeRoot, [string]$TestRuntime
    )
    if ([string]$Manifest.target.os -ne 'windows' -or [string]$Manifest.target.arch -ne 'x64') {
        throw "Offline manifest target must be Windows x64."
    }
    foreach ($Entry in @($Manifest.files)) {
        if ($Entry.verified -ne $true -or [string]::IsNullOrWhiteSpace([string]$Entry.version) -or
            [string]::IsNullOrWhiteSpace([string]$Entry.source) -or
            @($Entry.verification).Count -eq 0 -or
            [string]$Entry.source -eq 'bundle-input' -or [string]$Entry.version -eq 'bundle-input') {
            throw "Offline manifest contains an unverified or placeholder entry: $($Entry.path)"
        }
    }
    $null = Assert-DeclaredVersion -Output (Invoke-CheckedOutput -Executable $RuntimePython -Arguments @('--version')) `
        -Declared ([string]$Manifest.target.python) -Component 'Python'
    $ProbeEnvironment = Join-Path $ProbeRoot ('.python-runtime-probe-' + [Guid]::NewGuid().ToString('N'))
    $ProbeMarker = New-OwnershipMarker -Target $ProbeEnvironment
    try {
        Invoke-Checked -Executable $RuntimePython -Arguments @('-m', 'venv', $ProbeEnvironment)
        $ProbePython = Join-Path $ProbeEnvironment 'Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $ProbePython -PathType Leaf)) {
            throw 'Bundled Python could not create a Windows virtual environment.'
        }
        Invoke-Checked -Executable $ProbePython -Arguments @('-m', 'ensurepip', '--version')
    }
    finally {
        Remove-OwnedTree -Target $ProbeEnvironment -Marker $ProbeMarker
    }
    $PostgresVersions = @()
    foreach ($Name in @('initdb.exe', 'pg_ctl.exe', 'postgres.exe', 'psql.exe', 'pg_dump.exe', 'pg_restore.exe')) {
        $Executable = Join-Path $BundledPostgres "bin\$Name"
        $PostgresVersions += Assert-DeclaredVersion -Output (Invoke-CheckedOutput -Executable $Executable -Arguments @('--version')) `
            -Declared ([string]$Manifest.target.postgresqlServerVersion) -Component "PostgreSQL $Name"
    }
    if (@($PostgresVersions | Sort-Object -Unique).Count -ne 1 -or
        $PostgresVersions[0] -ne [string]$Manifest.target.postgresqlServerVersion) {
        throw 'PostgreSQL runtime executable versions must be identical and exactly match the manifest.'
    }
    $Node = Join-Path $TestRuntime 'node\node.exe'
    $NodeActual = Assert-DeclaredVersion -Output (Invoke-CheckedOutput -Executable $Node -Arguments @('--version')) `
        -Declared ([string]$Manifest.runtimeValidation.nodeVersion) -Component 'Node.js'
    if ($NodeActual -ne [string]$Manifest.runtimeValidation.nodeVersion) {
        throw 'Node.js runtime must exactly match the manifest version.'
    }
    $PlaywrightVersion = [string]$Manifest.runtimeValidation.playwrightVersion
    foreach ($Package in @('@playwright/test', 'playwright', 'playwright-core')) {
        $PackageJson = Join-Path $TestRuntime "node_modules\$Package\package.json"
        $Metadata = Get-Content -LiteralPath $PackageJson -Raw | ConvertFrom-Json
        if ([string]$Metadata.name -ne $Package -or [string]$Metadata.version -ne $PlaywrightVersion) {
            throw "Playwright package identity or version mismatch: $Package"
        }
    }
    $BrowsersJson = Get-Content -LiteralPath (Join-Path $TestRuntime 'node_modules\playwright-core\browsers.json') -Raw | ConvertFrom-Json
    $BrowserName = [string]$Manifest.runtimeValidation.browserName
    $BrowserRevision = [string]$Manifest.runtimeValidation.chromiumRevision
    $BrowserMetadata = @($BrowsersJson.browsers | Where-Object {
        ([string]$_.name).Replace('-', '_') -eq $BrowserName -and [string]$_.revision -eq $BrowserRevision
    })
    if ($BrowserMetadata.Count -ne 1) { throw 'Playwright browser revision metadata mismatch.' }
    $BrowserRoot = Join-Path $TestRuntime 'playwright-browsers'
    $ExpectedBrowser = [IO.Path]::GetFullPath((Join-Path $BrowserRoot ([string]$Manifest.runtimeValidation.browserExecutable)))
    $null = Assert-LocalNoReparsePath -Path $ExpectedBrowser
    $ExpectedRelative = 'test-runtime/playwright-browsers/' + ([string]$Manifest.runtimeValidation.browserExecutable).Replace('\', '/')
    if (@($Manifest.files | Where-Object { [string]$_.path -eq $ExpectedRelative }).Count -ne 1) {
        throw 'Manifest does not bind the Playwright-selected browser executable.'
    }
    $PlaywrightModule = Join-Path $TestRuntime 'node_modules\playwright'
    $ProbeScript = "const {chromium}=require(process.argv[1]);(async()=>{const executablePath=chromium.executablePath();const browser=await chromium.launch({headless:true,executablePath});await browser.close();process.stdout.write(JSON.stringify({executablePath}));})().catch(e=>{console.error(e);process.exit(1)});"
    $PreviousBrowserPath = [Environment]::GetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', 'Process')
    try {
        $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserRoot
        $ProbeResult = Invoke-CheckedOutput -Executable $Node -Arguments @('-e', $ProbeScript, $PlaywrightModule)
    }
    finally {
        [Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $PreviousBrowserPath, 'Process')
    }
    try { $SelectedBrowser = [IO.Path]::GetFullPath([string](($ProbeResult | ConvertFrom-Json).executablePath)) }
    catch { throw 'Playwright launch probe returned an invalid executable path.' }
    if (-not [string]::Equals($SelectedBrowser, $ExpectedBrowser, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Playwright selected browser does not match the manifest-bound staged executable.'
    }
}

function New-RandomSecret {
    $Bytes = New-Object byte[] 32
    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Protect-Secret {
    param([Parameter(Mandatory = $true)][string]$Value)
    return ConvertFrom-SecureString (ConvertTo-SecureString $Value -AsPlainText -Force)
}

function Assert-LocalNoReparsePath {
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$InspectTree)
    $FullPath = [IO.Path]::GetFullPath($Path)
    if ($FullPath.StartsWith("\\") -or $FullPath.StartsWith("//")) {
        throw "Path must not use UNC or network storage: $Path"
    }
    $PathRoot = [IO.Path]::GetPathRoot($FullPath)
    if ([string]::IsNullOrWhiteSpace($PathRoot)) { throw "Path must be absolute: $Path" }
    $Drive = New-Object IO.DriveInfo($PathRoot)
    if ($Drive.DriveType -eq [IO.DriveType]::Network) {
        throw "Path must not use a network drive: $Path"
    }
    $Current = $FullPath
    while (-not [string]::IsNullOrWhiteSpace($Current)) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Path contains a reparse point or junction: $Current"
            }
        }
        $Parent = Split-Path -Parent $Current
        if ([string]::IsNullOrWhiteSpace($Parent) -or $Parent -eq $Current) { break }
        $Current = $Parent
    }
    if ($InspectTree -and (Test-Path -LiteralPath $FullPath -PathType Container)) {
        foreach ($Item in Get-ChildItem -LiteralPath $FullPath -Recurse -Force) {
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Path tree contains a reparse point or junction: $($Item.FullName)"
            }
        }
    }
    return $FullPath
}

function Set-PrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$Directory)
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($Directory) {
        $Security = New-Object Security.AccessControl.DirectorySecurity
        $Inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        $Rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $Identity.User, [Security.AccessControl.FileSystemRights]::FullControl,
            $Inheritance, [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
    }
    else {
        $Security = New-Object Security.AccessControl.FileSecurity
        $Rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $Identity.User, [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
    }
    $Security.SetOwner($Identity.User)
    $Security.SetAccessRuleProtection($true, $false)
    $Security.AddAccessRule($Rule)
    Set-Acl -LiteralPath $Path -AclObject $Security
    $Verified = Get-Acl -LiteralPath $Path
    $Rules = @($Verified.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if (-not $Verified.AreAccessRulesProtected -or $Rules.Count -ne 1 -or
        $Rules[0].IdentityReference.Value -ne $Identity.User.Value -or
        $Rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        ($Rules[0].FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
            [Security.AccessControl.FileSystemRights]::FullControl) {
        throw "Private ACL verification failed: $Path"
    }
}

function New-OwnershipMarker {
    param([Parameter(Mandatory = $true)][string]$Target)
    $CanonicalTarget = [IO.Path]::GetFullPath($Target)
    $Marker = "$Target.install-owner-$OwnershipToken"
    $null = Assert-LocalNoReparsePath -Path $Marker
    $MarkerState = [ordered]@{ target = $CanonicalTarget; token = $OwnershipToken }
    [IO.File]::WriteAllText(
        $Marker,
        ($MarkerState | ConvertTo-Json -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
    Set-PrivateAcl -Path $Marker
    return $Marker
}

function Assert-OwnershipMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Marker
    )
    $CanonicalTarget = [IO.Path]::GetFullPath($Target)
    $null = Assert-LocalNoReparsePath -Path $Marker
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
        throw "Ownership marker does not belong to this install run; refusing recursive deletion."
    }
    try { $MarkerState = Get-Content -LiteralPath $Marker -Raw | ConvertFrom-Json }
    catch { throw "Ownership marker is invalid; refusing recursive deletion." }
    if ($MarkerState.token -ne $OwnershipToken) {
        throw "Ownership marker token does not match; refusing recursive deletion."
    }
    if (-not [string]::Equals(
            [IO.Path]::GetFullPath([string]$MarkerState.target),
            $CanonicalTarget,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Ownership marker target does not match; refusing recursive deletion."
    }
}

function Move-OwnershipMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Marker,
        [Parameter(Mandatory = $true)][string]$NewTarget
    )
    $null = Assert-LocalNoReparsePath -Path $Marker
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
        throw "Ownership marker does not belong to this install run."
    }
    try { $MarkerState = Get-Content -LiteralPath $Marker -Raw | ConvertFrom-Json }
    catch { throw "Ownership marker is invalid." }
    if ($MarkerState.token -ne $OwnershipToken) {
        throw "Ownership marker target or token mismatch."
    }
    $CanonicalTarget = [IO.Path]::GetFullPath($NewTarget)
    $MarkerState = [ordered]@{ target = $CanonicalTarget; token = $OwnershipToken }
    [IO.File]::WriteAllText(
        $Marker,
        ($MarkerState | ConvertTo-Json -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
    Set-PrivateAcl -Path $Marker
}

function Remove-OwnershipMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Marker
    )
    Assert-OwnershipMarker -Target $Target -Marker $Marker
    Remove-Item -LiteralPath $Marker -Force
}

function Remove-OwnedTree {
    param([Parameter(Mandatory = $true)][string]$Target, [Parameter(Mandatory = $true)][string]$Marker)
    Assert-OwnershipMarker -Target $Target -Marker $Marker
    if (Test-Path -LiteralPath $Target) {
        $null = Assert-LocalNoReparsePath -Path $Target -InspectTree
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
    Remove-Item -LiteralPath $Marker -Force
}

function Assert-SafeIdentifier {
    param([string]$Value, [string]$Name)
    if ($Value -notmatch '^[a-z][a-z0-9_]{0,62}$') {
        throw "$Name must be a lowercase PostgreSQL identifier."
    }
}

function New-DatabaseUrl {
    param([string]$Role, [string]$Password, [string]$Database)
    $EscapedPassword = [Uri]::EscapeDataString($Password)
    return "postgresql+psycopg://${Role}:${EscapedPassword}@127.0.0.1:$DatabasePort/$Database"
}

function Assert-LocalDatabaseUrl {
    param([string]$Value, [string]$ExpectedRole, [string]$VariableName)
    $Normalised = [Regex]::Replace($Value, '^postgresql(?:\+[^:]+)?://', 'postgresql://')
    try { $Uri = [Uri]$Normalised } catch { throw "$VariableName is not a valid PostgreSQL URL." }
    if ($Uri.Scheme -ne "postgresql" -or $Uri.Query -or $Uri.Fragment) {
        throw "$VariableName must not include query parameters or fragments."
    }
    $Parts = $Uri.UserInfo.Split([char[]]@(':'), 2)
    $ActualRole = [Uri]::UnescapeDataString($Parts[0])
    $ActualDatabase = [Uri]::UnescapeDataString($Uri.AbsolutePath.TrimStart('/'))
    if ($Uri.Host -ne "127.0.0.1" -or $Uri.Port -ne $DatabasePort -or
        $ActualDatabase -ne $DatabaseName -or $ActualRole -ne $ExpectedRole) {
        throw "$VariableName must target this local PostgreSQL cluster."
    }
    return [PSCustomObject]@{ Host = $Uri.Host; Port = $Uri.Port; Database = $ActualDatabase; Role = $ActualRole }
}

function Test-TcpPortInUse {
    param([int]$Port)
    $Client = New-Object Net.Sockets.TcpClient
    try {
        $Attempt = $Client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $Attempt.AsyncWaitHandle.WaitOne(500, $false)) { return $false }
        $Client.EndConnect($Attempt)
        return $true
    }
    catch { return $false }
    finally { $Client.Close() }
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
        "runtime/postgresql/bin/initdb.exe",
        "runtime/postgresql/bin/pg_ctl.exe",
        "runtime/postgresql/bin/postgres.exe",
        "runtime/postgresql/bin/psql.exe",
        "runtime/postgresql/bin/pg_dump.exe",
        "runtime/postgresql/bin/pg_restore.exe",
        "runtime/postgresql/bin/libpq.dll",
        "test-runtime/node/node.exe",
        "test-runtime/node_modules/@playwright/test/package.json",
        "test-runtime/node_modules/playwright/package.json",
        "test-runtime/node_modules/playwright-core/package.json",
        "test-runtime/node_modules/playwright-core/browsers.json",
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
    [Environment]::SetEnvironmentVariable("FLASK_SECRET_KEY", $null, "Process")
    [Environment]::SetEnvironmentVariable("PGPASSWORD", $null, "Process")
    Assert-SafeIdentifier -Value $DatabaseName -Name "DatabaseName"
    Assert-SafeIdentifier -Value $BootstrapRole -Name "BootstrapRole"
    Assert-SafeIdentifier -Value $OwnerRole -Name "OwnerRole"
    Assert-SafeIdentifier -Value $RuntimeRole -Name "RuntimeRole"
    $UniqueRoles = @($BootstrapRole, $OwnerRole, $RuntimeRole) | Sort-Object -Unique
    if (@($UniqueRoles).Count -ne 3) {
        throw "BootstrapRole, OwnerRole, and RuntimeRole must all differ."
    }
    if ($DatabasePort -lt 1024 -or $DatabasePort -gt 65535) { throw "DatabasePort must be between 1024 and 65535." }
    if (Test-TcpPortInUse -Port $DatabasePort) { throw "The configured local PostgreSQL port is already in use." }

    if (-not (Test-Path -LiteralPath $OfflineRoot -PathType Container)) {
        throw "Offline root does not exist: $OfflineRoot"
    }
    $OfflineRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $OfflineRoot).Path -InspectTree
    $ConfiguredDataRoot = Get-RequiredEnvironmentValue "APP_DATA_ROOT"
    $null = Assert-LocalNoReparsePath -Path $ConfiguredDataRoot
    New-Item -ItemType Directory -Path $ConfiguredDataRoot -Force | Out-Null
    $DataRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $ConfiguredDataRoot).Path -InspectTree
    if ($null -ne (Get-ChildItem -LiteralPath $DataRoot -Force | Select-Object -First 1)) {
        throw "APP_DATA_ROOT must be empty before installation."
    }
    Set-PrivateAcl -Path $DataRoot -Directory
    $env:APP_DATA_ROOT = $DataRoot
    $Wheelhouse = Join-Path $OfflineRoot "wheelhouse"
    $Requirements = Join-Path $OfflineRoot "requirements.txt"
    $RuntimePython = Join-Path $OfflineRoot "runtime\python\python.exe"
    $BundledPostgres = Join-Path $OfflineRoot "runtime\postgresql"
    $TestRuntime = Join-Path $OfflineRoot "test-runtime"
    foreach ($RequiredFile in @(
        $RuntimePython,
        (Join-Path $BundledPostgres "bin\initdb.exe"),
        (Join-Path $BundledPostgres "bin\pg_ctl.exe"),
        (Join-Path $BundledPostgres "bin\postgres.exe"),
        (Join-Path $BundledPostgres "bin\psql.exe"),
        (Join-Path $BundledPostgres "bin\pg_dump.exe"),
        (Join-Path $BundledPostgres "bin\pg_restore.exe"),
        (Join-Path $BundledPostgres "bin\libpq.dll"),
        (Join-Path $TestRuntime "node\node.exe"),
        (Join-Path $TestRuntime "node_modules\@playwright\test\package.json"),
        (Join-Path $TestRuntime "node_modules\playwright\package.json"),
        (Join-Path $TestRuntime "node_modules\playwright-core\package.json"),
        (Join-Path $TestRuntime "node_modules\playwright-core\browsers.json"),
        $Requirements
    )) {
        if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
            throw "Required offline file was not found: $RequiredFile"
        }
    }
    if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) {
        throw "Offline wheelhouse was not found: $Wheelhouse"
    }
    foreach ($RequiredRuntimeDirectory in @("lib", "share")) {
        $RuntimeDirectory = Join-Path $BundledPostgres $RequiredRuntimeDirectory
        if (-not (Test-Path -LiteralPath $RuntimeDirectory -PathType Container) -or
            $null -eq (Get-ChildItem -LiteralPath $RuntimeDirectory -Force | Select-Object -First 1)) {
            throw "PostgreSQL server runtime directory is missing or empty: $RuntimeDirectory"
        }
    }

    $Manifest = Assert-OfflineManifest -Root $OfflineRoot
    Assert-BundledRuntimeVersions -Manifest $Manifest -RuntimePython $RuntimePython `
        -BundledPostgres $BundledPostgres -ProbeRoot $DataRoot -TestRuntime $TestRuntime

    $ConfigDirectory = Join-Path $DataRoot "config"
    $RunDirectory = Join-Path $DataRoot "run"
    $LogDirectory = Join-Path $DataRoot "logs"
    foreach ($Directory in @($ConfigDirectory, $RunDirectory, $LogDirectory)) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    }
    $DeploymentState = Join-Path $ConfigDirectory "deployment.json"
    if (Test-Path -LiteralPath $DeploymentState) { throw "A local deployment is already configured." }
    $PostgresRuntime = Join-Path (Join-Path $DataRoot "runtime") "postgresql"
    $PostgresData = Join-Path (Join-Path $DataRoot "postgresql") "data"
    if (Test-Path -LiteralPath $PostgresRuntime) { throw "PostgreSQL runtime target already exists." }
    if (Test-Path -LiteralPath $PostgresData) { throw "PostgreSQL data target already exists." }
    $PostgresRuntimeParent = Split-Path -Parent $PostgresRuntime
    New-Item -ItemType Directory -Path $PostgresRuntimeParent -Force | Out-Null
    $PostgresRuntimeStaging = Join-Path $PostgresRuntimeParent (
        "postgresql.installing-" + [Guid]::NewGuid().ToString("N")
    )
    $PostgresRuntimeMarker = New-OwnershipMarker -Target $PostgresRuntimeStaging
    Copy-Item -LiteralPath $BundledPostgres -Destination $PostgresRuntimeStaging -Recurse
    Move-Item -LiteralPath $PostgresRuntimeStaging -Destination $PostgresRuntime
    Move-OwnershipMarker -Marker $PostgresRuntimeMarker -NewTarget $PostgresRuntime
    $PostgresRuntimeStaging = $null
    $PostgresRuntimeCreated = $true

    $InitDbExe = Join-Path $PostgresRuntime "bin\initdb.exe"
    $PgCtlExe = Join-Path $PostgresRuntime "bin\pg_ctl.exe"
    $PostgresExe = Join-Path $PostgresRuntime "bin\postgres.exe"
    $PsqlExe = Join-Path $PostgresRuntime "bin\psql.exe"
    $PostgresLog = Join-Path $LogDirectory "postgresql.log"
    $OwnerPassword = New-RandomSecret
    $RuntimePassword = New-RandomSecret
    $FlaskSecret = New-RandomSecret
    $PostgresDataParent = Split-Path -Parent $PostgresData
    New-Item -ItemType Directory -Path $PostgresDataParent -Force | Out-Null
    Set-PrivateAcl -Path $PostgresDataParent -Directory
    $PostgresDataMarker = New-OwnershipMarker -Target $PostgresData
    $PostgresDataCreated = $true
    Invoke-Checked -Executable $InitDbExe -Arguments @(
        "--pgdata", $PostgresData, "--username", $BootstrapRole,
        "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8"
    )
    $BootstrapCommands = @(
        @{ Database = "postgres"; Sql = "CREATE ROLE $OwnerRole LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '$OwnerPassword';" },
        @{ Database = "postgres"; Sql = "CREATE ROLE $RuntimeRole LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '$RuntimePassword';" },
        @{ Database = "postgres"; Sql = "CREATE DATABASE $DatabaseName OWNER $OwnerRole;" },
        @{ Database = $DatabaseName; Sql = "ALTER SCHEMA public OWNER TO $OwnerRole;" },
        @{ Database = "postgres"; Sql = "ALTER ROLE $BootstrapRole NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS NOLOGIN PASSWORD NULL;" }
    )
    foreach ($BootstrapCommand in $BootstrapCommands) {
        $BootstrapSql = [string]$BootstrapCommand.Sql
        $BootstrapSql | & $PostgresExe @("--single", "-D", $PostgresData, ([string]$BootstrapCommand.Database)) 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "PostgreSQL single-user credential and database bootstrap failed."
        }
    }
    $BootstrapSql = $null
    $BootstrapCommands = $null
    $PgHbaPath = Join-Path $PostgresData "pg_hba.conf"
    $PgHba = @"
local all all scram-sha-256
host all all 127.0.0.1/32 scram-sha-256
host all all ::1/128 scram-sha-256
"@
    [IO.File]::WriteAllText($PgHbaPath, $PgHba, (New-Object Text.UTF8Encoding($false)))
    $PostgresStarted = $true
    Invoke-Checked -Executable $PgCtlExe -Arguments @(
        "start", "-D", $PostgresData, "-l", $PostgresLog, "-w",
        "-o", "-h 127.0.0.1 -p $DatabasePort"
    )

    $MigrationDatabaseUrl = New-DatabaseUrl -Role $OwnerRole -Password $OwnerPassword -Database $DatabaseName
    $RuntimeDatabaseUrl = New-DatabaseUrl -Role $RuntimeRole -Password $RuntimePassword -Database $DatabaseName
    $MigrationTarget = Assert-LocalDatabaseUrl -Value $MigrationDatabaseUrl -ExpectedRole $OwnerRole -VariableName "MIGRATION_DATABASE_URL"
    $RuntimeTarget = Assert-LocalDatabaseUrl -Value $RuntimeDatabaseUrl -ExpectedRole $RuntimeRole -VariableName "DATABASE_URL"
    if ($MigrationTarget.Host -ne $RuntimeTarget.Host -or $MigrationTarget.Port -ne $RuntimeTarget.Port -or
        $MigrationTarget.Database -ne $RuntimeTarget.Database) {
        throw "MIGRATION_DATABASE_URL and DATABASE_URL must target the same local database."
    }

    $VirtualEnvironment = [IO.Path]::GetFullPath($VirtualEnvironment)
    $null = Assert-LocalNoReparsePath -Path $VirtualEnvironment
    if (Test-Path -LiteralPath $VirtualEnvironment) {
        throw "Virtual environment already exists; use upgrade.ps1 for an existing installation."
    }
    $EnvironmentParent = Split-Path -Parent $VirtualEnvironment
    if (-not (Test-Path -LiteralPath $EnvironmentParent -PathType Container)) {
        New-Item -ItemType Directory -Path $EnvironmentParent -Force | Out-Null
    }
    $StagingEnvironment = Join-Path $EnvironmentParent (
        (Split-Path -Leaf $VirtualEnvironment) + ".installing-" + [Guid]::NewGuid().ToString("N")
    )
    $EnvironmentMarker = New-OwnershipMarker -Target $StagingEnvironment

    Invoke-Checked -Executable $RuntimePython -Arguments @(
        "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
    )
    Invoke-Checked -Executable $RuntimePython -Arguments @("-m", "venv", $StagingEnvironment)
    $StagingPython = Join-Path $StagingEnvironment "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $StagingPython -PathType Leaf)) {
        throw "Python virtual environment was not created correctly."
    }
    Invoke-Checked -Executable $StagingPython -Arguments @("-m", "ensurepip", "--version")
    Invoke-Checked -Executable $StagingPython -Arguments @(
        "-m", "pip", "install", "--only-binary=:all:", "--no-index", "--find-links", $Wheelhouse,
        "--requirement", $Requirements
    )

    Move-Item -LiteralPath $StagingEnvironment -Destination $VirtualEnvironment
    Move-OwnershipMarker -Marker $EnvironmentMarker -NewTarget $VirtualEnvironment
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
        $Provisioner = Join-Path $PSScriptRoot "provision_postgres.py"
        Invoke-Checked -Executable $VenvPython -Arguments @($Provisioner, "--runtime-role", $RuntimeRole)
    }
    finally {
        [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
        Pop-Location
    }

    Invoke-Checked -Executable $PgCtlExe -Arguments @("stop", "-D", $PostgresData, "-m", "fast", "-w")
    $PostgresStarted = $false
    $PostgresStopConfirmed = $true
    $State = [ordered]@{
        schemaVersion = 1
        databaseHost = "127.0.0.1"
        databasePort = $DatabasePort
        databaseName = $DatabaseName
        bootstrapRole = $BootstrapRole
        ownerRole = $OwnerRole
        runtimeRole = $RuntimeRole
        ownerPasswordProtected = Protect-Secret -Value $OwnerPassword
        runtimePasswordProtected = Protect-Secret -Value $RuntimePassword
        flaskSecretProtected = Protect-Secret -Value $FlaskSecret
        postgresRuntime = $PostgresRuntime
        postgresData = $PostgresData
    }
    $State | ConvertTo-Json | Set-Content -LiteralPath $DeploymentState -Encoding UTF8
    $DeploymentStateCreated = $true
    Set-PrivateAcl -Path $DeploymentState

    Assert-OwnershipMarker -Target $PostgresRuntime -Marker $PostgresRuntimeMarker
    Assert-OwnershipMarker -Target $PostgresData -Marker $PostgresDataMarker
    Assert-OwnershipMarker -Target $VirtualEnvironment -Marker $EnvironmentMarker
    Remove-OwnershipMarker -Marker $PostgresRuntimeMarker -Target $PostgresRuntime
    Remove-OwnershipMarker -Marker $PostgresDataMarker -Target $PostgresData
    Remove-OwnershipMarker -Marker $EnvironmentMarker -Target $VirtualEnvironment

    Write-Host "Offline application and local PostgreSQL server installed."
    Write-Host "Deployment state: $DeploymentState"
    exit 0
}
catch {
    $OriginalError = $_.Exception.Message
    [Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
    [Environment]::SetEnvironmentVariable("PGPASSWORD", $null, "Process")
    $PostgresStopConfirmed = -not $PostgresStarted
    if ($PostgresStarted -and $null -ne $PgCtlExe -and (Test-Path -LiteralPath $PgCtlExe)) {
        try {
            & $PgCtlExe @("stop", "-D", $PostgresData, "-m", "immediate", "-w") | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $PostgresStopConfirmed = $true
                $PostgresStarted = $false
            }
            else {
                [Console]::Error.WriteLine(
                    "ERROR: PostgreSQL cleanup stop failed; PGDATA and runtime were retained (exit code {0}).",
                    $LASTEXITCODE
                )
            }
        }
        catch {
            [Console]::Error.WriteLine(
                "ERROR: PostgreSQL cleanup stop failed; PGDATA and runtime were retained: {0}",
                $_.Exception.Message
            )
        }
    }
    if ($null -ne $StagingEnvironment -and $null -ne $EnvironmentMarker) {
        Remove-OwnedTree -Target $StagingEnvironment -Marker $EnvironmentMarker
    }
    if ($NewEnvironmentCreated -and $null -ne $EnvironmentMarker) {
        Remove-OwnedTree -Target $VirtualEnvironment -Marker $EnvironmentMarker
    }
    if ($PostgresStopConfirmed -and $PostgresDataCreated -and $null -ne $PostgresDataMarker) {
        Remove-OwnedTree -Target $PostgresData -Marker $PostgresDataMarker
    }
    if ($PostgresStopConfirmed -and $PostgresRuntimeCreated -and $null -ne $PostgresRuntimeMarker) {
        Remove-OwnedTree -Target $PostgresRuntime -Marker $PostgresRuntimeMarker
    }
    if ($PostgresStopConfirmed -and $null -ne $PostgresRuntimeStaging -and $null -ne $PostgresRuntimeMarker) {
        Remove-OwnedTree -Target $PostgresRuntimeStaging -Marker $PostgresRuntimeMarker
    }
    if ($DeploymentStateCreated -and (Test-Path -LiteralPath $DeploymentState)) {
        Remove-Item -LiteralPath $DeploymentState -Force
    }
    [Console]::Error.WriteLine("ERROR: {0}", $OriginalError)
    exit 1
}
