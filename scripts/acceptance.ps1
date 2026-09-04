[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OfflineRoot,
    [Parameter(Mandatory = $true)][string]$EvidenceRoot,
    [string]$AppDataRoot,
    [int]$AppPort = 5001
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$Results = New-Object System.Collections.Generic.List[object]
$EvidencePath = $null
$BackupPath = $null
$ExtractedBackup = $null
$Deployment = $null
$AcceptanceOwnershipToken = [Guid]::NewGuid().ToString("N")
$EvidenceOwnershipMarker = $null
$AppDataOwnershipMarker = $null
$ApplicationStartedByAcceptance = $false
$PostgresStartedByAcceptance = $false
$AcceptanceAccountCreated = $false
$CleanupErrors = New-Object System.Collections.Generic.List[string]

function Clear-TransientCredentialEnvironment {
    foreach ($Name in @(
        "MIGRATION_DATABASE_URL", "DATABASE_URL", "FLASK_SECRET_KEY", "PGPASSWORD",
        "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "ACCEPTANCE_PASSWORD"
    )) {
        [Environment]::SetEnvironmentVariable($Name, $null, "Process")
    }
}

function Clear-SensitiveProcessEnvironment {
    foreach ($Name in @([Environment]::GetEnvironmentVariables("Process").Keys)) {
        $Text = [string]$Name
        if ($Text -match '(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|PASS)') {
            [Environment]::SetEnvironmentVariable($Text, $null, "Process")
        }
    }
    Clear-TransientCredentialEnvironment
}

function Invoke-WithTemporaryEnvironment {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Values,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    foreach ($Name in $Values.Keys) {
        [Environment]::SetEnvironmentVariable([string]$Name, [string]$Values[$Name], "Process")
    }
    try { & $Action }
    finally { Clear-TransientCredentialEnvironment }
}

function Assert-LocalNoReparsePath {
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$InspectTree)
    $FullPath = [IO.Path]::GetFullPath($Path)
    if ($FullPath.StartsWith("\\") -or $FullPath.StartsWith("//")) {
        throw "Acceptance paths must not use UNC or network storage: $Path"
    }
    $PathRoot = [IO.Path]::GetPathRoot($FullPath)
    if ([string]::IsNullOrWhiteSpace($PathRoot)) { throw "Acceptance path must be absolute: $Path" }
    if ((New-Object IO.DriveInfo($PathRoot)).DriveType -eq [IO.DriveType]::Network) {
        throw "Acceptance paths must not use a network drive: $Path"
    }
    $Current = $FullPath
    while (-not [string]::IsNullOrWhiteSpace($Current)) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Acceptance path contains a ReparsePoint or junction: $Current"
            }
        }
        $Parent = Split-Path -Parent $Current
        if ([string]::IsNullOrWhiteSpace($Parent) -or $Parent -eq $Current) { break }
        $Current = $Parent
    }
    if ($InspectTree -and (Test-Path -LiteralPath $FullPath -PathType Container)) {
        foreach ($Item in Get-ChildItem -LiteralPath $FullPath -Recurse -Force) {
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Acceptance tree contains a ReparsePoint or junction: $($Item.FullName)"
            }
        }
    }
    return $FullPath
}

function Assert-SafeIdentifier {
    param([Parameter(Mandatory = $true)][string]$Value, [Parameter(Mandatory = $true)][string]$Name)
    if ($Value -notmatch '^[a-z][a-z0-9_]{0,62}$') {
        throw "$Name is not a safe PostgreSQL identifier."
    }
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

function New-AcceptanceOwnershipMarker {
    param([Parameter(Mandatory = $true)][string]$Target)
    $CanonicalTarget = [IO.Path]::GetFullPath($Target)
    $MarkerName = ".acceptance-owner-$([Guid]::NewGuid().ToString("N")).json"
    $Marker = Join-Path (Split-Path -Parent $CanonicalTarget) $MarkerName
    $null = Assert-LocalNoReparsePath -Path $Marker
    if (Test-Path -LiteralPath $Marker) { throw "Acceptance ownership marker already exists." }
    $State = [ordered]@{ target = $CanonicalTarget; token = $AcceptanceOwnershipToken }
    [IO.File]::WriteAllText($Marker, ($State | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    Set-PrivateAcl -Path $Marker
    return $Marker
}

function Invoke-NodeWithSecretFromStdin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$SensitiveInput
    )
    $SensitiveInput | & $Path @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Node acceptance failed with exit code $LASTEXITCODE." }
}

function Assert-AcceptanceOwnershipMarker {
    param(
        [Parameter(Mandatory = $true)][string]$OwnershipRoot,
        [Parameter(Mandatory = $true)][string]$Marker
    )
    $CanonicalTarget = Assert-LocalNoReparsePath -Path $OwnershipRoot -InspectTree
    $null = Assert-LocalNoReparsePath -Path $Marker
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
        throw "Acceptance ownership marker is missing; refusing recursive cleanup."
    }
    try { $State = Get-Content -LiteralPath $Marker -Raw | ConvertFrom-Json }
    catch { throw "Acceptance ownership marker is invalid; refusing recursive cleanup." }
    if ([string]$State.token -ne $AcceptanceOwnershipToken) {
        throw "Acceptance ownership marker token does not match; refusing recursive cleanup."
    }
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath([string]$State.target),
        $CanonicalTarget,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Ownership marker target does not match; refusing recursive cleanup."
    }
}

function Remove-OwnedSubtree {
    param(
        [Parameter(Mandatory = $true)][string]$OwnershipRoot,
        [Parameter(Mandatory = $true)][string]$Marker,
        [Parameter(Mandatory = $true)][string]$Target
    )
    Assert-AcceptanceOwnershipMarker -OwnershipRoot $OwnershipRoot -Marker $Marker
    $CanonicalRoot = [IO.Path]::GetFullPath($OwnershipRoot).TrimEnd('\', '/')
    $CanonicalTarget = Assert-LocalNoReparsePath -Path $Target -InspectTree
    $Prefix = $CanonicalRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $CanonicalTarget.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Recursive cleanup target is outside the owned acceptance root."
    }
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}

function Invoke-CheckedScript {
    param([string]$Path, [hashtable]$Parameters = @{})
    & $Path @Parameters
    if ($LASTEXITCODE -ne 0) { throw "Script failed with exit code $LASTEXITCODE: $Path" }
}

function Invoke-CheckedExecutable {
    param([string]$Path, [string[]]$Arguments)
    & $Path @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE: $Path" }
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

function Load-DeploymentEnvironment {
    $DeploymentPath = Join-Path (Join-Path $AppDataRoot "config") "deployment.json"
    $null = Assert-LocalNoReparsePath -Path $DeploymentPath
    if (-not (Test-Path -LiteralPath $DeploymentPath -PathType Leaf)) { throw "Deployment state is missing." }
    try { $Deployment = Get-Content -LiteralPath $DeploymentPath -Raw | ConvertFrom-Json }
    catch { throw "Deployment state is invalid." }
    foreach ($Pair in @(
        @([string]$Deployment.databaseName, "databaseName"),
        @([string]$Deployment.bootstrapRole, "bootstrapRole"),
        @([string]$Deployment.ownerRole, "ownerRole"),
        @([string]$Deployment.runtimeRole, "runtimeRole")
    )) {
        Assert-SafeIdentifier -Value $Pair[0] -Name $Pair[1]
    }
    $UniqueRoles = @($Deployment.bootstrapRole, $Deployment.ownerRole, $Deployment.runtimeRole) | Sort-Object -Unique
    if ([int]$Deployment.schemaVersion -ne 1 -or
        [string]$Deployment.databaseHost -ne "127.0.0.1" -or
        [int]$Deployment.databasePort -lt 1024 -or [int]$Deployment.databasePort -gt 65535 -or
        @($UniqueRoles).Count -ne 3) {
        throw "Deployment state does not describe the isolated local target."
    }
    $ExpectedRuntime = Join-Path (Join-Path $AppDataRoot "runtime") "postgresql"
    $ExpectedData = Join-Path (Join-Path $AppDataRoot "postgresql") "data"
    foreach ($PathPair in @(
        @([string]$Deployment.postgresRuntime, $ExpectedRuntime),
        @([string]$Deployment.postgresData, $ExpectedData)
    )) {
        $Actual = Assert-LocalNoReparsePath -Path $PathPair[0] -InspectTree
        $Expected = [IO.Path]::GetFullPath($PathPair[1])
        if (-not $Actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Deployment path must match this acceptance APP_DATA_ROOT."
        }
    }
    $env:PYTHON_EXE = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $env:PSQL_EXE = Join-Path ([string]$Deployment.postgresRuntime) "bin\psql.exe"
    $env:PG_DUMP_EXE = Join-Path ([string]$Deployment.postgresRuntime) "bin\pg_dump.exe"
    $env:PG_RESTORE_EXE = Join-Path ([string]$Deployment.postgresRuntime) "bin\pg_restore.exe"
    return $Deployment
}

function Get-OwnerDatabaseUrl {
    param([Parameter(Mandatory = $true)]$Deployment)
    $Password = Unprotect-Secret ([string]$Deployment.ownerPasswordProtected)
    try {
        return New-DatabaseUrl -Role ([string]$Deployment.ownerRole) -Password $Password `
            -Database ([string]$Deployment.databaseName) -Port ([int]$Deployment.databasePort)
    }
    finally { $Password = $null }
}

function Get-RuntimeDatabaseUrl {
    param([Parameter(Mandatory = $true)]$Deployment)
    $Password = Unprotect-Secret ([string]$Deployment.runtimePasswordProtected)
    try {
        return New-DatabaseUrl -Role ([string]$Deployment.runtimeRole) -Password $Password `
            -Database ([string]$Deployment.databaseName) -Port ([int]$Deployment.databasePort)
    }
    finally { $Password = $null }
}

function Test-ApplicationHealth {
    $Response = Invoke-WebRequest -Uri "http://127.0.0.1:$AppPort/health/ready" -UseBasicParsing -TimeoutSec 5
    if ([int]$Response.StatusCode -ne 200) { throw "Production readiness endpoint did not return HTTP 200." }
}

function Remove-AcceptanceAccount {
    param([Parameter(Mandatory = $true)]$Deployment)
    if (-not $AcceptanceAccountCreated) { return }
    $DisableCode = @'
import os
import secrets
import sqlalchemy as sa
from app.security.auth import hash_password
engine = sa.create_engine(os.environ["MIGRATION_DATABASE_URL"])
users = sa.Table("users", sa.MetaData(), autoload_with=engine)
with engine.begin() as connection:
    connection.execute(users.update().where(users.c.username == os.environ["ACCEPTANCE_USERNAME"]).values(status="disabled", password=hash_password(secrets.token_urlsafe(48)), version=users.c.version + 1))
'@
    $OwnerUrl = Get-OwnerDatabaseUrl -Deployment $Deployment
    Push-Location $ProjectRoot
    try {
        Invoke-WithTemporaryEnvironment -Values @{
            MIGRATION_DATABASE_URL = $OwnerUrl
            ACCEPTANCE_USERNAME = $env:ACCEPTANCE_USERNAME
        } -Action {
            Invoke-CheckedExecutable -Path $env:PYTHON_EXE -Arguments @("-c", $DisableCode)
        }
        $script:AcceptanceAccountCreated = $false
    }
    finally {
        $OwnerUrl = $null
        Pop-Location
    }
}

function Invoke-AcceptanceProcessCleanup {
    Clear-TransientCredentialEnvironment
    if ($ApplicationStartedByAcceptance) {
        try {
            Invoke-CheckedScript -Path $Stop
            $script:ApplicationStartedByAcceptance = $false
            $script:PostgresStartedByAcceptance = $false
        }
        catch { $CleanupErrors.Add("Application/PostgreSQL cleanup failed: $($_.Exception.Message)") }
    }
    if ($PostgresStartedByAcceptance) {
        try {
            Invoke-CheckedScript -Path $Stop -Parameters @{ DatabaseOnly = $true }
            $script:PostgresStartedByAcceptance = $false
        }
        catch { $CleanupErrors.Add("PostgreSQL cleanup failed: $($_.Exception.Message)") }
    }
}

function Invoke-AcceptanceStep {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][scriptblock]$Action)
    $Started = [DateTime]::UtcNow
    try {
        & $Action
        $Results.Add([ordered]@{
            name = $Name; status = "PASSED"; startedAtUtc = $Started.ToString("o")
            finishedAtUtc = [DateTime]::UtcNow.ToString("o")
        })
    }
    catch {
        $Results.Add([ordered]@{
            name = $Name; status = "FAILED"; startedAtUtc = $Started.ToString("o")
            finishedAtUtc = [DateTime]::UtcNow.ToString("o"); error = $_.Exception.Message
        })
        throw
    }
    finally {
        if ($null -ne $EvidencePath) {
            [ordered]@{ status = "RUNNING"; steps = $Results } |
                ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
        }
    }
}

try {
    Clear-SensitiveProcessEnvironment
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $OfflineRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $OfflineRoot).Path -InspectTree
    $EvidenceRoot = Assert-LocalNoReparsePath -Path $EvidenceRoot
    if (Test-Path -LiteralPath $EvidenceRoot) {
        if (-not (Test-Path -LiteralPath $EvidenceRoot -PathType Container) -or
            $null -ne (Get-ChildItem -LiteralPath $EvidenceRoot -Force | Select-Object -First 1)) {
            throw "EvidenceRoot must be a new or empty local directory; use a new run directory for every acceptance run."
        }
    }
    New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
    $EvidenceRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $EvidenceRoot).Path -InspectTree
    Set-PrivateAcl -Path $EvidenceRoot -Directory
    $EvidenceOwnershipMarker = New-AcceptanceOwnershipMarker -Target $EvidenceRoot
    $EvidencePath = Join-Path $EvidenceRoot "evidence.json"
    if ([string]::IsNullOrWhiteSpace($AppDataRoot)) { $AppDataRoot = Join-Path $EvidenceRoot "runtime" }
    $AppDataRoot = Assert-LocalNoReparsePath -Path $AppDataRoot
    $EvidencePrefix = $EvidenceRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $AppDataRoot.StartsWith($EvidencePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "APP_DATA_ROOT must be inside EvidenceRoot."
    }
    if (Test-Path -LiteralPath $AppDataRoot) {
        if ($null -ne (Get-ChildItem -LiteralPath $AppDataRoot -Force | Select-Object -First 1)) {
            throw "Acceptance APP_DATA_ROOT must be empty."
        }
    }
    else { New-Item -ItemType Directory -Path $AppDataRoot -Force | Out-Null }
    $AppDataRoot = Assert-LocalNoReparsePath -Path (Resolve-Path -LiteralPath $AppDataRoot).Path -InspectTree
    Set-PrivateAcl -Path $AppDataRoot -Directory
    $AppDataOwnershipMarker = New-AcceptanceOwnershipMarker -Target $AppDataRoot
    $env:APP_DATA_ROOT = $AppDataRoot
    $env:APP_BIND_HOST = "127.0.0.1"
    $env:APP_PORT = [string]$AppPort
    $env:AI_PROVIDER = "DISABLED"
    $OutboundPath = Join-Path $EvidenceRoot "outbound-requests.json"
    $NodeExe = Join-Path $OfflineRoot "test-runtime\node\node.exe"
    $PlaywrightCli = Join-Path $OfflineRoot "test-runtime\node_modules\@playwright\test\cli.js"
    $PlaywrightModule = Join-Path $OfflineRoot "test-runtime\node_modules\@playwright\test\index.mjs"
    $BrowserRoot = Join-Path $OfflineRoot "test-runtime\playwright-browsers"
    foreach ($RequiredBrowserFile in @($NodeExe, $PlaywrightCli, $PlaywrightModule)) {
        if (-not (Test-Path -LiteralPath $RequiredBrowserFile -PathType Leaf)) {
            throw "Required offline Playwright runtime is missing: $RequiredBrowserFile"
        }
    }
    if (-not (Test-Path -LiteralPath $BrowserRoot -PathType Container)) {
        throw "Required offline Playwright runtime is missing: $BrowserRoot"
    }

    $Install = Join-Path $PSScriptRoot "install.ps1"
    $Start = Join-Path $PSScriptRoot "start.ps1"
    $Stop = Join-Path $PSScriptRoot "stop.ps1"
    $Backup = Join-Path $PSScriptRoot "backup.ps1"
    $Restore = Join-Path $PSScriptRoot "restore.ps1"
    $Verifier = Join-Path $PSScriptRoot "verify_offline.py"
    $BusinessHelper = Join-Path $PSScriptRoot "acceptance_business.py"
    $BusinessBaselinePath = Join-Path $EvidenceRoot "business-baseline.json"
    $BusinessBeforeBackupPath = Join-Path $EvidenceRoot "business-before-backup.json"
    $BusinessAfterRestorePath = Join-Path $EvidenceRoot "business-after-restore.json"
    $AcceptancePassword = "Accept-$([Guid]::NewGuid().ToString('N'))-7"
    $env:ACCEPTANCE_USERNAME = "zhanglaoshi"

    Invoke-AcceptanceStep "01-install" {
        Invoke-CheckedScript -Path $Install -Parameters @{ OfflineRoot = $OfflineRoot }
        $script:Deployment = Load-DeploymentEnvironment
        $script:PostgresStartedByAcceptance = $true
        Invoke-CheckedScript -Path $Start -Parameters @{ DatabaseOnly = $true }
        $SeedCode = @'
import os
import sqlalchemy as sa
from app.security.auth import BUSINESS_USER, hash_password
engine = sa.create_engine(os.environ["MIGRATION_DATABASE_URL"])
users = sa.Table("users", sa.MetaData(), autoload_with=engine)
with engine.begin() as connection:
    existing = connection.execute(sa.select(users.c.id).where(users.c.username == os.environ["ACCEPTANCE_USERNAME"])).first()
    if existing is None:
        connection.execute(users.insert().values(username=os.environ["ACCEPTANCE_USERNAME"], password=hash_password(os.environ["ACCEPTANCE_PASSWORD"]), role=BUSINESS_USER, name="张老师", status="active", must_change_password=False, version=1))
    else:
        connection.execute(users.update().where(users.c.id == existing.id).values(password=hash_password(os.environ["ACCEPTANCE_PASSWORD"]), role=BUSINESS_USER, name="张老师", status="active", must_change_password=False, version=users.c.version + 1))
'@
        $OwnerUrl = Get-OwnerDatabaseUrl -Deployment $Deployment
        Push-Location $ProjectRoot
        try {
            Invoke-WithTemporaryEnvironment -Values @{
                MIGRATION_DATABASE_URL = $OwnerUrl
                ACCEPTANCE_USERNAME = $env:ACCEPTANCE_USERNAME
                ACCEPTANCE_PASSWORD = $AcceptancePassword
            } -Action {
                Invoke-CheckedExecutable -Path $env:PYTHON_EXE -Arguments @("-c", $SeedCode)
            }
            $script:AcceptanceAccountCreated = $true
            $RuntimeUrl = Get-RuntimeDatabaseUrl -Deployment $Deployment
            $FlaskSecret = Unprotect-Secret ([string]$Deployment.flaskSecretProtected)
            try {
                Invoke-WithTemporaryEnvironment -Values @{
                    DATABASE_URL = $RuntimeUrl
                    FLASK_SECRET_KEY = $FlaskSecret
                } -Action {
                    Invoke-CheckedExecutable -Path $env:PYTHON_EXE -Arguments @(
                        $BusinessHelper, "seed", "--username", $env:ACCEPTANCE_USERNAME,
                        "--output", $BusinessBaselinePath
                    )
                }
            }
            finally { $RuntimeUrl = $null; $FlaskSecret = $null }
        }
        finally {
            $OwnerUrl = $null
            Pop-Location
        }
        Invoke-CheckedScript -Path $Stop -Parameters @{ DatabaseOnly = $true }
        $script:PostgresStartedByAcceptance = $false
    }

    Invoke-AcceptanceStep "02-start" {
        $script:Deployment = Load-DeploymentEnvironment
        $script:PostgresStartedByAcceptance = $true
        $script:ApplicationStartedByAcceptance = $true
        Invoke-CheckedScript -Path $Start
        Clear-TransientCredentialEnvironment
    }

    Invoke-AcceptanceStep "03-health" {
        Test-ApplicationHealth
    }

    Invoke-AcceptanceStep "04-core-e2e" {
        $TestPath = Join-Path $EvidenceRoot "offline-core.spec.mjs"
        $PlaywrightModuleUri = ([Uri]$PlaywrightModule).AbsoluteUri
        $TestSource = @"
import { chromium } from '$PlaywrightModuleUri';
import fs from 'node:fs';

let password = '';
for await (const chunk of process.stdin) password += chunk;
password = password.replace(/\r?\n`$/, '');
if (!password) throw new Error('Acceptance password was not supplied on stdin.');

const browserEnvironment = {};
for (const name of ['SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LOCALAPPDATA', 'PROGRAMDATA', 'PATH']) {
  if (process.env[name]) browserEnvironment[name] = process.env[name];
}
const outbound = [];
const phase = process.env.ACCEPTANCE_PHASE || 'unknown';
const resultPath = '$($EvidenceRoot.Replace('\','/'))/playwright-results-' + phase + '.json';
const baseline = JSON.parse(fs.readFileSync('$($BusinessBaselinePath.Replace('\','/'))', 'utf8'));
const ids = baseline.identities;
const browser = await chromium.launch({ env: browserEnvironment });
const context = await browser.newContext({ baseURL: 'http://127.0.0.1:$AppPort' });
const page = await context.newPage();
let status = 'FAILED';
try {
  page.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol) && !['127.0.0.1', 'localhost'].includes(url.hostname)) outbound.push(request.url());
  });
  await page.goto('/auth/login');
  await page.locator('input[name="username"]').fill(process.env.ACCEPTANCE_USERNAME);
  await page.locator('input[name="password"]').fill(password);
  password = '';
  await page.locator('button[type="submit"]').click();
  if (/\/auth\/login/.test(page.url())) throw new Error('Acceptance login failed.');
  async function open(path, marker) {
    const response = await page.goto(path);
    if (!response || response.status() >= 400) throw new Error('Route failed: ' + path);
    if (/\/auth\/login/.test(page.url())) throw new Error('Route redirected to login: ' + path);
    await page.getByText(marker, { exact: false }).first().waitFor({ state: 'visible', timeout: 10000 });
  }
  await open('/proposals/' + encodeURIComponent(ids.proposalBusinessId), 'V1验收-智能保障设备适配研究-20260904');
  await page.getByText('v1-proposal-source.txt', { exact: true }).waitFor({ state: 'visible' });
  await page.getByText('立项', { exact: true }).first().waitFor({ state: 'visible' });
  await open('/projects/' + encodeURIComponent(ids.projectRegistryId) + '/overview', 'V1验收-智能保障设备适配研究-20260904');
  await page.locator('#projectStatusLabel').filter({ hasText: '已结题' }).waitFor({ state: 'visible' });
  await page.getByText('一般科研项目', { exact: true }).waitFor({ state: 'visible' });
  await page.getByText('V1验收-阶段进展记录-20260904', { exact: true }).first().waitFor({ state: 'visible' });
  const projectResponse = await page.request.get('/api/projects/' + encodeURIComponent(ids.projectRegistryId));
  if (!projectResponse.ok()) throw new Error('Project detail API failed.');
  const projectRecord = await projectResponse.json();
  if (projectRecord.businessId !== ids.projectBusinessId || projectRecord.sourceProposalId !== ids.proposalInternalId) throw new Error('Project-to-proposal relationship mismatch.');
  const projectFilesResponse = await page.request.get('/api/files?objectType=PROJECT&objectId=' + encodeURIComponent(ids.projectBusinessId));
  if (!projectFilesResponse.ok()) throw new Error('Project attachment API failed.');
  const projectFiles = (await projectFilesResponse.json()).files || [];
  if (!projectFiles.some(file => file.fileId === ids.projectFileId && file.originalName === 'v1-project-record.txt' && Number(file.versionNo) === 1)) throw new Error('Project controlled attachment mismatch.');
  await page.getByRole('tab', { name: /成果/ }).click();
  await page.getByText('V1验收-适配研究报告-20260904', { exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('tab', { name: '结题', exact: true }).click();
  await page.getByText('完成研究目标并形成可复核报告', { exact: true }).waitFor({ state: 'visible' });
  await open('/projects/detail/' + encodeURIComponent(ids.projectBusinessId), 'V1验收-科研保障设备-20260904');
  await page.getByText('一号科研实验室', { exact: true }).waitFor({ state: 'visible' });
  await open('/experts/groups/edit/' + encodeURIComponent(ids.expertGroupId), 'V1验收-科研论证专家组-20260904');
  const selectedMemberRow = page.locator('.card', { hasText: '已选择成员' }).locator('tbody tr').filter({ hasText: '张老师' });
  await selectedMemberRow.getByText('张老师', { exact: true }).waitFor({ state: 'visible' });
  await selectedMemberRow.getByText('第一研究室', { exact: true }).waitFor({ state: 'visible' });
  await selectedMemberRow.getByText('科研设备适配', { exact: true }).waitFor({ state: 'visible' });
  await open('/equipment', 'V1验收-科研保障设备-20260904');
  await open('/expense/records', 'V1验收-设备采购登记-20260904');
  await open('/standards/', 'V1验收-科研设备试验记录规范-20260904');
  await open('/templates/?cat=' + encodeURIComponent('科研模板'), 'V1验收-科研项目记录模板-20260904');
  await open('/tables/' + encodeURIComponent(ids.genericTableId), 'V1验收-智能保障设备适配研究-20260904');
  await open('/utils/', '文档校对');
  await page.locator('.container').getByRole('button', { name: '当前未启用', exact: true }).waitFor({ state: 'visible' });
  if (outbound.length !== 0) throw new Error('Non-local browser requests were observed.');
  status = 'PASSED';
} finally {
  password = '';
  fs.writeFileSync(process.env.OUTBOUND_EVIDENCE, JSON.stringify(outbound, null, 2));
  fs.writeFileSync(resultPath, JSON.stringify({ status }, null, 2));
  await browser.close();
}
"@
        [IO.File]::WriteAllText($TestPath, $TestSource, (New-Object Text.UTF8Encoding($false)))
        $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserRoot
        $env:OUTBOUND_EVIDENCE = $OutboundPath
        $env:ACCEPTANCE_PHASE = "before-backup"
        Invoke-NodeWithSecretFromStdin -Path $NodeExe -Arguments @($TestPath) -SensitiveInput $AcceptancePassword
        [Environment]::SetEnvironmentVariable("ACCEPTANCE_PHASE", $null, "Process")
    }

    Invoke-AcceptanceStep "05-backup" {
        Clear-TransientCredentialEnvironment
        Invoke-CheckedScript -Path $Stop -Parameters @{ KeepPostgresRunning = $true }
        $script:ApplicationStartedByAcceptance = $false
        $script:Deployment = Load-DeploymentEnvironment
        $script:BackupPath = Join-Path $EvidenceRoot "acceptance-backup.zip"
        $RuntimeUrl = Get-RuntimeDatabaseUrl -Deployment $Deployment
        $FlaskSecret = Unprotect-Secret ([string]$Deployment.flaskSecretProtected)
        try {
            Invoke-WithTemporaryEnvironment -Values @{
                DATABASE_URL = $RuntimeUrl
                FLASK_SECRET_KEY = $FlaskSecret
            } -Action {
                Invoke-CheckedExecutable -Path $env:PYTHON_EXE -Arguments @(
                    $BusinessHelper, "verify", "--expected", $BusinessBaselinePath,
                    "--output", $BusinessBeforeBackupPath
                )
                Invoke-CheckedScript -Path $Backup -Parameters @{
                    OutputPath = $script:BackupPath; ConfirmMaintenanceWindow = $true
                }
            }
        }
        finally { $RuntimeUrl = $null; $FlaskSecret = $null }
    }

    Invoke-AcceptanceStep "06-stop" {
        Invoke-CheckedScript -Path $Stop -Parameters @{ DatabaseOnly = $true }
        $script:PostgresStartedByAcceptance = $false
    }

    Invoke-AcceptanceStep "07-clear-test-data" {
        $script:Deployment = Load-DeploymentEnvironment
        $script:PostgresStartedByAcceptance = $true
        Invoke-CheckedScript -Path $Start -Parameters @{ DatabaseOnly = $true }
        $DatabaseName = [string]$Deployment.databaseName
        $OwnerRole = [string]$Deployment.ownerRole
        Assert-SafeIdentifier -Value $DatabaseName -Name "databaseName"
        Assert-SafeIdentifier -Value $OwnerRole -Name "ownerRole"
        $SchemaResetSql = "BEGIN; DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION `"$OwnerRole`"; COMMIT;"
        $OwnerPassword = Unprotect-Secret ([string]$Deployment.ownerPasswordProtected)
        try {
            Invoke-WithTemporaryEnvironment -Values @{
                PGHOST = "127.0.0.1"
                PGPORT = [string]$Deployment.databasePort
                PGDATABASE = $DatabaseName
                PGUSER = $OwnerRole
                PGPASSWORD = $OwnerPassword
            } -Action {
                Invoke-CheckedExecutable -Path $env:PSQL_EXE -Arguments @(
                    "--no-password", "--set=ON_ERROR_STOP=1", "--command=$SchemaResetSql"
                )
            }
        }
        finally { $OwnerPassword = $null }
        foreach ($BusinessRoot in @("data\files", "uploads", "documents", "data\documents", "data\templates")) {
            $Target = Join-Path $AppDataRoot $BusinessRoot
            if (Test-Path -LiteralPath $Target) {
                Remove-OwnedSubtree -OwnershipRoot $AppDataRoot -Marker $AppDataOwnershipMarker -Target $Target
            }
        }
    }

    Invoke-AcceptanceStep "08-restore" {
        $script:Deployment = Load-DeploymentEnvironment
        $OwnerUrl = Get-OwnerDatabaseUrl -Deployment $Deployment
        $RuntimeUrl = Get-RuntimeDatabaseUrl -Deployment $Deployment
        try {
            Invoke-WithTemporaryEnvironment -Values @{
                MIGRATION_DATABASE_URL = $OwnerUrl
                DATABASE_URL = $RuntimeUrl
            } -Action {
                Invoke-CheckedScript -Path $Restore -Parameters @{
                    ArchivePath = $script:BackupPath; ConfirmStaging = $true
                }
            }
        }
        finally { $OwnerUrl = $null; $RuntimeUrl = $null }
    }

    Invoke-AcceptanceStep "09-restart" {
        Clear-TransientCredentialEnvironment
        Invoke-CheckedScript -Path $Stop -Parameters @{ DatabaseOnly = $true }
        $script:PostgresStartedByAcceptance = $false
        $script:Deployment = Load-DeploymentEnvironment
        $script:PostgresStartedByAcceptance = $true
        $script:ApplicationStartedByAcceptance = $true
        Invoke-CheckedScript -Path $Start
        Clear-TransientCredentialEnvironment
        Test-ApplicationHealth
    }

    Invoke-AcceptanceStep "10-consistency" {
        $script:Deployment = Load-DeploymentEnvironment
        $script:ExtractedBackup = Join-Path $EvidenceRoot "verified-backup"
        Invoke-CheckedExecutable -Path $env:PYTHON_EXE -Arguments @(
            $Verifier, "extract", "--archive", $script:BackupPath, "--destination", $script:ExtractedBackup
        )
        $RuntimeUrl = Get-RuntimeDatabaseUrl -Deployment $Deployment
        $FlaskSecret = Unprotect-Secret ([string]$Deployment.flaskSecretProtected)
        try {
            Invoke-WithTemporaryEnvironment -Values @{
                DATABASE_URL = $RuntimeUrl
                FLASK_SECRET_KEY = $FlaskSecret
            } -Action {
                Invoke-CheckedExecutable -Path $env:PYTHON_EXE -Arguments @(
                    $Verifier, "verify", "--data-root", $AppDataRoot, "--manifest", (Join-Path $script:ExtractedBackup "manifest.json")
                )
                Invoke-CheckedExecutable -Path $env:PYTHON_EXE -Arguments @(
                    $BusinessHelper, "verify", "--expected", $BusinessBaselinePath,
                    "--output", $BusinessAfterRestorePath
                )
            }
        }
        finally { $RuntimeUrl = $null; $FlaskSecret = $null }
        $env:ACCEPTANCE_PHASE = "after-restore"
        Invoke-NodeWithSecretFromStdin -Path $NodeExe -Arguments @($TestPath) -SensitiveInput $AcceptancePassword
        [Environment]::SetEnvironmentVariable("ACCEPTANCE_PHASE", $null, "Process")
    }

    Invoke-AcceptanceStep "11-outbound-scan" {
        if (-not (Test-Path -LiteralPath $OutboundPath -PathType Leaf)) { throw "Outbound request evidence is missing." }
        $Outbound = @(Get-Content -LiteralPath $OutboundPath -Raw | ConvertFrom-Json)
        if ($Outbound.Count -ne 0) { throw "Non-local browser requests were observed during offline acceptance." }
    }

    Invoke-AcceptanceStep "12-cleanup" {
        $script:Deployment = Load-DeploymentEnvironment
        Remove-AcceptanceAccount -Deployment $Deployment
        Clear-TransientCredentialEnvironment
        Invoke-CheckedScript -Path $Stop
        $script:ApplicationStartedByAcceptance = $false
        $script:PostgresStartedByAcceptance = $false
        Assert-AcceptanceOwnershipMarker -OwnershipRoot $AppDataRoot -Marker $AppDataOwnershipMarker
        Assert-AcceptanceOwnershipMarker -OwnershipRoot $EvidenceRoot -Marker $EvidenceOwnershipMarker
        Remove-Item -LiteralPath $AppDataOwnershipMarker -Force
        Remove-Item -LiteralPath $EvidenceOwnershipMarker -Force
    }

    [ordered]@{ status = "PASSED"; finishedAtUtc = [DateTime]::UtcNow.ToString("o"); steps = $Results } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
    Write-Host "Offline acceptance passed. Evidence: $EvidencePath"
    exit 0
}
catch {
    $OriginalError = $_.Exception.Message
    if ($AcceptanceAccountCreated -and $null -ne $Deployment) {
        if (-not $PostgresStartedByAcceptance) {
            try {
                $script:PostgresStartedByAcceptance = $true
                Invoke-CheckedScript -Path $Start -Parameters @{ DatabaseOnly = $true }
            }
            catch { $CleanupErrors.Add("PostgreSQL could not be started for account cleanup: $($_.Exception.Message)") }
        }
        if ($PostgresStartedByAcceptance) {
            try { Remove-AcceptanceAccount -Deployment $Deployment }
            catch { $CleanupErrors.Add("Acceptance account cleanup failed: $($_.Exception.Message)") }
        }
    }
    Invoke-AcceptanceProcessCleanup
    if ($null -ne $EvidencePath) {
        [ordered]@{
            status = "FAILED"
            finishedAtUtc = [DateTime]::UtcNow.ToString("o")
            steps = $Results
            cleanupErrors = $CleanupErrors
        } |
            ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
    }
    [Console]::Error.WriteLine("ERROR: {0}", $OriginalError)
    foreach ($CleanupError in $CleanupErrors) {
        [Console]::Error.WriteLine("CLEANUP ERROR: {0}", $CleanupError)
    }
    exit 1
}
finally {
    Clear-TransientCredentialEnvironment
    [Environment]::SetEnvironmentVariable("ACCEPTANCE_PASSWORD", $null, "Process")
    [Environment]::SetEnvironmentVariable("ACCEPTANCE_PHASE", $null, "Process")
    $AcceptancePassword = $null
}
