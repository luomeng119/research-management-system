from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


ROOT_PATTERN = re.compile(r"isolated cluster=(\S+)")


def _run_runner(database_name: str, *, fail_at: str | None = None):
    env = os.environ.copy()
    env.pop("T02_TEST_FAIL_AT", None)
    if fail_at:
        env["T02_TEST_FAIL_AT"] = fail_at
    return subprocess.run(
        ["bash", "scripts/test_postgres.sh", f"postgresql+psycopg://sentinel.invalid:1/{database_name}"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )


def _cluster_path(result: subprocess.CompletedProcess[str]) -> Path:
    match = ROOT_PATTERN.search(result.stdout)
    assert match, result.stdout + result.stderr
    return Path(match.group(1))


def test_non_t02_identifier_is_rejected_before_any_postgresql_command(tmp_path):
    forbidden_bin = tmp_path / "bin"
    forbidden_bin.mkdir()
    marker = tmp_path / "postgres-command-ran"
    for command in ("initdb", "pg_ctl", "psql", "createdb"):
        executable = forbidden_bin / command
        executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
        executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{forbidden_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", "scripts/test_postgres.sh", "postgresql+psycopg://sentinel.invalid:1/production"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 65
    assert "outside the T02/T03/T04 contract" in result.stderr
    assert not marker.exists()


def test_unreachable_caller_url_succeeds_and_cleans_temporary_cluster():
    result = _run_runner("rm_v1_t02_safety_success")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "caller URL is used only as a safety identifier" in result.stdout
    assert not _cluster_path(result).exists()


def test_t03_identifier_runs_auth_audit_contract_and_cleans_cluster():
    result = _run_runner("rm_v1_t03_safety_success")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert not _cluster_path(result).exists()


def test_t04_identifier_runs_file_contract_and_cleans_cluster():
    result = _run_runner("rm_v1_t04_safety_success")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
    assert not _cluster_path(result).exists()


def test_controlled_failure_cleans_temporary_cluster():
    result = _run_runner("rm_v1_t02_safety_failure", fail_at="after_cluster_start")

    assert result.returncode != 0
    assert "injected failure after_cluster_start" in result.stderr
    assert not _cluster_path(result).exists()
