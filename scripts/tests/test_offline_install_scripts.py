from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path


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
    assert "Move-Item -LiteralPath $PreviousEnvironment -Destination $VirtualEnvironment" in script
    assert 'Get-RequiredEnvironmentValue "MIGRATION_DATABASE_URL"' not in script
    assert "ownerPasswordProtected" in script
    assert "ConvertTo-SecureString" in script
    assert "PostgreSQL cluster must be stopped before upgrading" in script
    assert '"--no-index"' in script
    assert script.count('"--only-binary=:all:"') == 2
    assert '"-m", "alembic", "upgrade", "head"' in script
    assert "start.ps1" not in script


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


def test_bundle_builder_writes_deterministic_complete_manifest(tmp_path: Path):
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
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    manifest_bytes = (offline / "manifest.json").read_bytes()
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert (offline / "manifest.json").read_bytes() == manifest_bytes

    manifest = json.loads(manifest_bytes)
    assert manifest["schemaVersion"] == 1
    assert manifest["target"] == {
        "os": "windows",
        "arch": "x64",
        "python": "3.13",
        "postgresqlServerVersion": "16.4",
    }
    assert manifest["sources"]["python"] == "python.org CPython 3.13 x64"
    assert (
        manifest["sources"]["postgresqlServerRuntime"]
        == "postgresql.org PostgreSQL x64"
    )
    paths = [entry["path"] for entry in manifest["files"]]
    assert paths == sorted(paths)
    actual_paths = sorted(
        path.relative_to(offline).as_posix()
        for path in offline.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    assert paths == actual_paths
    assert "requirements.txt" in paths
    assert "runtime/python/python.exe" in paths
    assert "runtime/postgresql/bin/pg_dump.exe" in paths
    assert "runtime/postgresql/bin/initdb.exe" in paths
    assert "runtime/postgresql/bin/pg_ctl.exe" in paths
    assert "runtime/postgresql/bin/postgres.exe" in paths
    assert "test-runtime/node/node.exe" in paths
    assert "test-runtime/node_modules/@playwright/test/cli.js" in paths
    assert "test-runtime/playwright-browsers/chromium/chrome.exe" in paths
    assert "wheelhouse/example-1.0-py3-none-any.whl" in paths
    for entry in manifest["files"]:
        file_path = offline / entry["path"]
        assert entry["sha256"] == hashlib.sha256(file_path.read_bytes()).hexdigest()
        assert entry["size"] == file_path.stat().st_size
        assert "verified" not in entry
        assert entry["version"]
        assert entry["source"]


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
