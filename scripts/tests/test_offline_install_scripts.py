from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def _write_wheel(path: Path, name: str, version: str, *, requires: tuple[str, ...] = ()):
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
    ]
    metadata.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata) + "\n")
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")


def _stage_runtime(offline: Path):
    (offline / "runtime/python").mkdir(parents=True)
    (offline / "runtime/postgresql/bin").mkdir(parents=True)
    (offline / "runtime/postgresql/lib").mkdir(parents=True)
    (offline / "runtime/postgresql/share").mkdir(parents=True)
    (offline / "test-runtime/node").mkdir(parents=True)
    (offline / "test-runtime/node_modules/@playwright/test").mkdir(parents=True)
    (offline / "test-runtime/node_modules/playwright").mkdir(parents=True)
    (offline / "test-runtime/node_modules/playwright-core").mkdir(parents=True)
    (offline / "test-runtime/playwright-browsers/chromium").mkdir(parents=True)
    (offline / "wheelhouse").mkdir(parents=True)
    (offline / "licenses").mkdir(parents=True)
    (offline / "runtime/python/python.exe").write_bytes(b"python-runtime")
    for name in (
        "initdb.exe",
        "pg_ctl.exe",
        "postgres.exe",
        "psql.exe",
        "pg_dump.exe",
        "pg_restore.exe",
        "libpq.dll",
    ):
        (offline / "runtime/postgresql/bin" / name).write_bytes(name.encode())
    (offline / "runtime/postgresql/lib/runtime.lib").write_bytes(b"postgres-lib")
    (offline / "runtime/postgresql/share/postgresql.conf.sample").write_text(
        "# sample", encoding="utf-8"
    )
    (offline / "test-runtime/node/node.exe").write_bytes(b"node-runtime")
    for path in (
        "test-runtime/node_modules/@playwright/test/cli.js",
        "test-runtime/node_modules/@playwright/test/index.mjs",
        "test-runtime/node_modules/@playwright/test/package.json",
        "test-runtime/node_modules/playwright/package.json",
        "test-runtime/node_modules/playwright-core/package.json",
        "test-runtime/playwright-browsers/chromium/chrome.exe",
    ):
        (offline / path).write_text("{}", encoding="utf-8")
    (offline / "licenses/THIRD_PARTY-NOTICES.txt").write_text(
        "notices", encoding="utf-8"
    )


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_offline_bundle", SCRIPTS / "build_offline_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _truncated_amd64_header(path: Path):
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, 0x8664)
    struct.pack_into("<H", payload, 0x86, 1)
    struct.pack_into("<H", payload, 0x94, 0xF0)
    struct.pack_into("<H", payload, 0x98, 0x20B)
    path.write_bytes(payload)


def test_install_fails_closed_and_never_uses_network_package_sources():
    script = _text("install.ps1")

    assert 'Join-Path $OfflineRoot "manifest.json"' in script
    assert 'Join-Path $OfflineRoot "wheelhouse"' in script
    assert 'Join-Path $OfflineRoot "runtime\\python\\python.exe"' in script
    assert 'Join-Path $BundledPostgres "bin\\psql.exe"' in script
    assert 'Join-Path $BundledPostgres "bin\\pg_dump.exe"' in script
    assert 'Join-Path $BundledPostgres "bin\\pg_restore.exe"' in script
    for executable in ("initdb.exe", "pg_ctl.exe", "postgres.exe"):
        assert executable in script
    assert '"--no-index"' in script
    assert '"--find-links"' in script
    assert script.count('"--only-binary=:all:"') == 2
    assert "--index-url" not in script
    assert "Invoke-WebRequest" not in script
    assert "Invoke-RestMethod" not in script
    assert "curl" not in script.lower()
    assert "exit 1" in script


def test_install_verifies_every_manifest_hash_before_creating_environment():
    script = _text("install.ps1")

    verify = script.index("Get-FileHash")
    create_venv = script.index('"-m", "venv"')
    pip_install = script.index('"-m", "pip"')
    assert verify < create_venv < pip_install
    assert "SHA256 mismatch" in script
    assert "manifest contains an unsafe path" in script
    assert "Manifest does not cover required offline file" in script
    assert "ReparsePoint" in script
    assert "size mismatch" in script
    assert "Offline root contains a file absent from the manifest" in script
    assert '".installing-"' in script
    assert "Remove-OwnedTree -Target $StagingEnvironment" in script
    assert "Move-Item -LiteralPath $StagingEnvironment -Destination $VirtualEnvironment" in script


def test_install_generates_migration_credential_for_explicit_alembic_step():
    script = _text("install.ps1")

    assert 'Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"' not in script
    assert "$env:MIGRATION_DATABASE_URL = $MigrationDatabaseUrl" in script
    assert '"-m", "alembic", "upgrade", "head"' in script
    assert '[Environment]::SetEnvironmentVariable("MIGRATION_DATABASE_URL", $null, "Process")' in script
    assert 'Get-RequiredEnvironmentValue "DATABASE_URL"' not in script
    assert "start.ps1" not in script


def test_install_reprobes_bundled_runtime_versions_and_python_ensurepip():
    script = _text("install.ps1")

    manifest = script.index("Assert-OfflineManifest -Root $OfflineRoot")
    runtime_probe = script.index("Assert-BundledRuntimeVersions -Manifest")
    initdb = script.index('"--encoding=UTF8"')
    assert manifest < runtime_probe < initdb
    assert '"--version"' in script
    assert "$Manifest.target.python" in script
    assert "$Manifest.target.postgresqlServerVersion" in script
    assert '"-m", "ensurepip", "--version"' in script


def test_upgrade_refuses_to_run_while_recorded_server_is_active():
    script = _text("upgrade.ps1")

    assert '"research-management.pid.json"' in script
    assert "Get-Process -Id" in script
    assert "Stop the application before upgrading" in script
    assert "TcpClient" in script
    assert "Get-CimInstance Win32_Process" in script
    assert '".upgrading-"' in script
    assert '".previous-"' in script
    assert "Move-Item -LiteralPath $VirtualEnvironment -Destination $PreviousEnvironment" in script
    catch_body = script.split("catch {", 1)[1]
    assert "Move-Item -LiteralPath $PreviousEnvironment -Destination $VirtualEnvironment" not in catch_body
    assert 'Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"' not in script
    assert "ownerPasswordProtected" in script
    assert "ConvertTo-SecureString" in script
    assert "PostgreSQL cluster must be stopped before upgrading" in script
    assert '"--no-index"' in script
    assert script.count('"--only-binary=:all:"') == 1
    assert '"-m", "alembic", "upgrade", "head"' in script
    assert "start.ps1" not in script


def test_upgrade_is_backup_first_staged_and_fail_safe_after_schema_change():
    script = _text("upgrade.ps1")

    backup = script.rindex("Invoke-PreUpgradeBackup -DataRoot")
    staging = script.index('"-m", "venv", $StagingEnvironment', backup)
    schema_flag = script.index("$SchemaMayBeCommitted = $true", staging)
    migration = script.index('"-m", "alembic", "upgrade", "head"', schema_flag)
    revision_check = script.index("Assert-DatabaseRevision", migration)
    switch_old = script.index("Move-Item -LiteralPath $VirtualEnvironment -Destination $PreviousEnvironment", revision_check)
    switch_new = script.index("Move-Item -LiteralPath $StagingEnvironment -Destination $VirtualEnvironment", switch_old)
    assert backup < staging < schema_flag < migration < revision_check < switch_old < switch_new
    assert "-Executable $StagingPython" in script
    assert "[A-Za-z0-9_.-]+" in script
    assert "$BackupCompleted" in script
    assert "$SchemaMayBeCommitted" in script
    assert "$VenvSwitched" in script
    assert '"upgrade-state.json"' in script
    assert '"AFTER_BACKUP", "AFTER_SCHEMA", "AFTER_VENV_SWITCH"' in script
    assert "Automatic rollback is disabled" in script
    assert "Pre-upgrade backup:" in script
    assert "oldRevision = $OldRevision" in script
    assert "expectedHead = $ExpectedHead" in script
    assert "recoveryRequired = $RecoveryRequired" in script
    assert '"FAILED_POSTGRES_RUNNING"' in script
    assert '"FAILED_POSTGRES_STATE_UNKNOWN"' in script
    assert '"FAILED_STOPPED"' in script
    catch_body = script.split("\ncatch {\n    $OriginalError", 1)[1]
    assert "Old active virtual environment quarantined" not in catch_body
    assert "Move-Item -LiteralPath $VirtualEnvironment" not in catch_body


def test_upgrade_failure_distinguishes_pg_ctl_status_exit_codes():
    script = _text("upgrade.ps1")
    helper = script.split("function Get-PostgresRuntimeState", 1)[1].split("function ", 1)[0]
    assert '0 { return "RUNNING" }' in helper
    assert '3 { return "STOPPED" }' in helper
    assert 'default { return "UNKNOWN" }' in helper
    catch_body = script.split("\ncatch {\n    $OriginalError", 1)[1]
    assert '$PostgresStateAfterFailure -eq "STOPPED"' in catch_body
    assert '$PostgresStateAfterFailure -eq "RUNNING"' in catch_body
    assert '"FAILED_POSTGRES_STATE_UNKNOWN"' in catch_body


def test_upgrade_and_start_refuse_incomplete_upgrade_state():
    upgrade = _text("upgrade.ps1")
    start = _text("start.ps1")
    for script in (upgrade, start):
        assert '"upgrade-state.json"' in script
        assert 'status -ne "COMPLETED"' in script
        assert "incomplete upgrade state" in script.lower()


def test_upgrade_checks_pgdata_major_and_reprovisions_acl_before_final_revision_check():
    script = _text("upgrade.ps1")
    assert '"PG_VERSION"' in script
    assert "PostgreSQL data major version" in script
    migration = script.index('"-m", "alembic", "upgrade", "head"')
    provision = script.index('"provision_postgres.py"', migration)
    revision = script.index("Assert-DatabaseRevision", provision)
    assert migration < provision < revision
    assert '"--runtime-role", ([string]$Deployment.runtimeRole)' in script
    assert '"--migration-url"' not in script


def test_upgrade_rejects_any_postgresql_runtime_version_change_before_backup():
    script = _text("upgrade.ps1")

    version_check = script.rindex("Assert-PostgresRuntimeVersionMatch -Manifest")
    backup = script.rindex("Invoke-PreUpgradeBackup -DataRoot")
    assert version_check < backup
    assert "$Manifest.target.postgresqlServerVersion" in script
    assert 'Join-Path $InstalledRuntime "bin\\postgres.exe"' in script
    assert 'Join-Path $BundleRoot "runtime\\postgresql\\bin\\postgres.exe"' in script
    assert "PostgreSQL runtime upgrades are not supported" in script


def test_bundle_builder_rejects_missing_runtime(tmp_path: Path):
    offline = tmp_path / "offline"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_offline_bundle.py"),
            "--offline-root",
            str(offline),
            "--no-download",
            "--python-source",
            "python.org CPython 3.13 x64",
            "--postgres-source",
            "postgresql.org PostgreSQL x64",
            "--postgres-version",
            "16.4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "runtime/python/python.exe" in result.stderr
    assert not (offline / "manifest.json").exists()


def test_bundle_builder_rejects_placeholder_executables_instead_of_writing_manifest(tmp_path: Path):
    offline = tmp_path / "offline"
    _stage_runtime(offline)
    _write_wheel(offline / "wheelhouse/example-1.0-py3-none-any.whl", "example", "1.0")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n", encoding="utf-8")

    command = [
        sys.executable,
        str(SCRIPTS / "build_offline_bundle.py"),
        "--offline-root",
        str(offline),
        "--requirements",
        str(requirements),
        "--no-download",
        "--python-source",
        "python.org CPython 3.13 x64",
        "--postgres-source",
        "postgresql.org PostgreSQL x64",
        "--postgres-version",
        "16.4",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert "valid PE/AMD64" in result.stderr
    assert not (offline / "manifest.json").exists()


def test_pe_validation_rejects_fake_bytes_and_truncated_amd64_header(tmp_path: Path):
    builder = _load_builder()
    executable = tmp_path / "runtime.exe"
    executable.write_bytes(b"fake")
    with pytest.raises(ValueError, match="valid PE/AMD64"):
        builder._assert_pe_amd64(executable)
    _truncated_amd64_header(executable)
    with pytest.raises(ValueError, match="valid PE/AMD64"):
        builder._assert_pe_amd64(executable)


def test_declared_runtime_version_must_match_probe_output():
    builder = _load_builder()
    assert builder._assert_declared_version("Python 3.13.7", "3.13", "Python") == "3.13.7"
    with pytest.raises(ValueError, match="declared version 3.13"):
        builder._assert_declared_version("Python 3.12.9", "3.13", "Python")


def test_playwright_packages_and_actual_selected_browser_revision_must_match(tmp_path: Path):
    builder = _load_builder()
    root = tmp_path / "test-runtime"
    for package in ("@playwright/test", "playwright", "playwright-core"):
        package_root = root / "node_modules" / package
        package_root.mkdir(parents=True)
        (package_root / "package.json").write_text(
            json.dumps({"name": package, "version": "1.55.0"}), encoding="utf-8"
        )
    (root / "node_modules/playwright-core/browsers.json").write_text(
        json.dumps({"browsers": [{"name": "chromium", "revision": "1187"}]}),
        encoding="utf-8",
    )
    browser = root / "playwright-browsers/chromium-1187"
    browser.mkdir(parents=True)
    executable = browser / "chrome.exe"
    executable.write_bytes(b"selected by real Playwright probe")
    version, revisions = builder._inspect_playwright_runtime(root)
    assert version == "1.55.0"
    selected = builder._validate_playwright_selection(
        root / "playwright-browsers", revisions, executable
    )
    assert selected == ("chromium", "1187", "chromium-1187/chrome.exe")

    (root / "node_modules/playwright/package.json").write_text(
        json.dumps({"name": "playwright", "version": "1.54.0"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Playwright package versions disagree"):
        builder._inspect_playwright_runtime(root)

    (root / "node_modules/playwright/package.json").write_text(
        json.dumps({"name": "playwright", "version": "1.55.0"}), encoding="utf-8"
    )
    browser.rename(root / "playwright-browsers/chromium-9999")
    with pytest.raises(ValueError, match="Chromium revision"):
        builder._validate_playwright_selection(
            root / "playwright-browsers", revisions,
            root / "playwright-browsers/chromium-9999/chrome.exe",
        )


def test_bundle_builder_rejects_offline_root_symlink_before_resolving(tmp_path: Path):
    target = tmp_path / "real-offline"
    target.mkdir()
    offline = tmp_path / "offline-link"
    offline.symlink_to(target, target_is_directory=True)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "build_offline_bundle.py"),
            "--offline-root", str(offline), "--requirements", str(requirements),
            "--no-download", "--python-source", "python.org CPython 3.13 x64",
            "--postgres-source", "postgresql.org PostgreSQL x64",
            "--postgres-version", "16.4",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "symbolic link or reparse point" in result.stderr


def test_bundle_builder_rejects_placeholder_browser_runtime(tmp_path: Path):
    offline = tmp_path / "offline"
    _stage_runtime(offline)
    (offline / "test-runtime/playwright-browsers/chromium/chrome.exe").unlink()
    (offline / "test-runtime/playwright-browsers/README.txt").write_text(
        "placeholder", encoding="utf-8"
    )
    _write_wheel(offline / "wheelhouse/example-1.0-py3-none-any.whl", "example", "1.0")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_offline_bundle.py"),
            "--offline-root",
            str(offline),
            "--requirements",
            str(requirements),
            "--no-download",
            "--python-source",
            "python source",
            "--postgres-source",
            "postgres source",
            "--postgres-version",
            "16.4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Windows Chromium executable" in result.stderr
    assert not (offline / "manifest.json").exists()


def test_bundle_builder_rejects_wheelhouse_that_does_not_cover_lock(tmp_path: Path):
    offline = tmp_path / "offline"
    _stage_runtime(offline)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("missing-package==9.9\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_offline_bundle.py"),
            "--offline-root",
            str(offline),
            "--requirements",
            str(requirements),
            "--no-download",
            "--python-source",
            "python source",
            "--postgres-source",
            "postgres source",
            "--postgres-version",
            "16.4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "missing-package==9.9" in result.stderr
    assert not (offline / "manifest.json").exists()


def test_bundle_builder_rejects_invalid_or_incompatible_wheel(tmp_path: Path):
    offline = tmp_path / "offline"
    _stage_runtime(offline)
    (offline / "wheelhouse/example-1.0-cp313-cp313-macosx_14_0_arm64.whl").write_bytes(
        b"not-a-wheel"
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_offline_bundle.py"),
            "--offline-root",
            str(offline),
            "--requirements",
            str(requirements),
            "--no-download",
            "--python-source",
            "python source",
            "--postgres-source",
            "postgres source",
            "--postgres-version",
            "16.4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "wheel" in result.stderr.lower()
    assert not (offline / "manifest.json").exists()


def test_bundle_builder_resolves_transitive_dependencies_offline(tmp_path: Path):
    offline = tmp_path / "offline"
    _stage_runtime(offline)
    _write_wheel(
        offline / "wheelhouse/example-1.0-py3-none-any.whl",
        "example",
        "1.0",
        requires=("dependency==2.0",),
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_offline_bundle.py"),
            "--offline-root",
            str(offline),
            "--requirements",
            str(requirements),
            "--no-download",
            "--python-source",
            "python source",
            "--postgres-source",
            "postgres source",
            "--postgres-version",
            "16.4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "dependency" in (result.stdout + result.stderr).lower()
    assert not (offline / "manifest.json").exists()


def test_bundle_builder_accepts_older_cp_abi3_wheel_for_python_313():
    sys.path.insert(0, str(SCRIPTS))
    try:
        from build_offline_bundle import _tag_is_windows_cp313_compatible

        assert _tag_is_windows_cp313_compatible("cp39", "abi3", "win_amd64")
        assert not _tag_is_windows_cp313_compatible("cp39", "cp39", "win_amd64")
        assert not _tag_is_windows_cp313_compatible("cp313", "cp313", "win32")
    finally:
        sys.path.pop(0)


def test_bundle_builder_rejects_stale_manifest_temp_file(tmp_path: Path):
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / "manifest.json.tmp").write_text("stale", encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_offline_bundle.py"),
            "--offline-root",
            str(offline),
            "--requirements",
            str(requirements),
            "--no-download",
            "--python-source",
            "python source",
            "--postgres-source",
            "postgres source",
            "--postgres-version",
            "16.4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "stale manifest temporary file" in result.stderr.lower()
    assert not (offline / "manifest.json").exists()
