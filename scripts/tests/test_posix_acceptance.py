from pathlib import Path
import json
import re
import subprocess
import os
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_acceptance_python_selection_fails_closed_before_database_access(tmp_path):
    # Ignoring an explicit interpreter must not silently use the development venv.
    for selected, expected in ((str(tmp_path / "missing"), 69), ("", 69),
                               (str(tmp_path), 69), (sys.executable, 64)):
        result = subprocess.run(
            ["bash", str(SCRIPTS / "acceptance_posix.sh")], cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "ACCEPTANCE_PYTHON": selected},
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == expected, result.stderr
        assert not (tmp_path / "build").exists()
    default = subprocess.run(
        ["bash", str(SCRIPTS / "acceptance_posix.sh")], cwd=tmp_path,
        env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=10,
    )
    assert default.returncode == 69
    assert ".venv/bin/python" in default.stderr


def test_download_navigation_classification_is_exact_and_one_to_one():
    source = _text("acceptance_browser.mjs")
    code = re.search(r"  for \(const url of verifiedDownloadUrls\) \{.*?\n  \}", source, re.S)[0]
    aborted = {"type": "requestfailed", "navigation": True, "method": "GET",
               "message": "net::ERR_ABORTED", "url": "http://127.0.0.1:12345/download"}
    cases = [
        ([], [aborted], 1),
        ([aborted["url"]], [aborted], 0),
        ([aborted["url"]], [aborted, aborted], 1),
        ([aborted["url"]], [{**aborted, "navigation": False}], 1),
        ([aborted["url"]], [{**aborted, "method": "POST"}], 1),
        ([aborted["url"]], [{**aborted, "url": "http://127.0.0.1:12345/other"}], 1),
        ([aborted["url"]], [{**aborted, "message": "net::ERR_CONNECTION_RESET"}], 1),
    ]
    for urls, errors, remaining in cases:
        result = subprocess.run(["node", "-e", """
const fs = require('fs'); const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const state = {verifiedDownloadUrls: input.urls, browserErrors: input.errors,
  expectedDownloadTransitions: []};
vm.runInNewContext(input.code, state);
process.stdout.write(JSON.stringify(state));
"""], input=json.dumps({"code": code, "urls": urls, "errors": errors}),
            text=True, capture_output=True, check=True, timeout=10)
        state = json.loads(result.stdout)
        assert len(state["browserErrors"]) == remaining
        assert len(state["expectedDownloadTransitions"]) == len(errors) - remaining


def test_postgres_harness_runs_optional_command_without_eval_before_cleanup():
    source = _text("test_postgres.sh")
    assert 't02_after_command=("$@")' in source
    assert '"${t02_after_command[@]}"' in source
    assert "eval " not in source
    assert source.index('"${t02_after_command[@]}"') < source.index(
        'echo "T02 PostgreSQL contract completed'
    )
    for name in (
        "T02_ISOLATED_POSTGRES_ROOT",
        "T02_ISOLATED_POSTGRES_PORT",
        "T02_ISOLATED_POSTGRES_DATABASE",
    ):
        assert f'export {name}=' in source


def test_posix_acceptance_uses_formal_runtime_and_retains_evidence():
    source = _text("acceptance_posix.sh")
    for name in (
        "MIGRATION_DATABASE_URL",
        "DATABASE_URL",
        "T02_ISOLATED_POSTGRES_ROOT",
        "T02_ISOLATED_POSTGRES_PORT",
        "T02_ISOLATED_POSTGRES_DATABASE",
    ):
        assert name in source
    assert 'required environment variable is missing: $t12_name' in source
    assert '"$t12_python" run.py' in source
    assert "/health/ready" in source
    assert '"database": "ok"' in source
    assert '"schema": "ok"' in source
    assert '"storage": "ok"' in source
    assert 'acceptance_business.py ensure-user' in source
    assert 'acceptance_business.py seed' in source
    assert 'acceptance_business.py verify' in source
    assert 'acceptance_browser.mjs' in source
    assert "TESTING" not in source
    assert "server.py" not in source
    assert "sqlite" not in source.casefold()
    assert "APP_DATA_ROOT" in source
    assert "AI_PROVIDER=DISABLED" in source
    assert "trap cleanup_t12_posix" in source
    assert 'expected_data / "postmaster.pid"' in source
    assert "host(inet_server_addr())" in source
    assert "actual_user" in source
    assert 'env -i \\\n' in source
    assert 'ACCEPTANCE_USERNAME=$("$t12_python"' in source
    assert "stop_t12_waitress" in source
    assert source.index("stop_t12_waitress\n") < source.index(
        'echo "POSIX production-chain acceptance passed'
    )


def test_posix_browser_checks_authenticated_business_surfaces_and_outbound_requests():
    source = _text("acceptance_browser.mjs")
    assert "process.stdin" in source
    assert "/auth/login" in source
    assert "ACCEPTANCE_BASELINE" in source
    assert "ACCEPTANCE_OUTBOUND" in source
    assert "ACCEPTANCE_RESULT" in source
    for path in (
        "/proposals/",
        "/projects/",
        "/projects/detail/",
        "/experts/groups/edit/",
        "/equipment",
        "/expense/records",
        "/standards/",
        "/templates/",
        "/tables/",
        "/utils/",
    ):
        assert path in source
    assert "Non-local browser requests were observed" in source
    assert "chromium.launch({ env: browserEnvironment })" in source
    assert source.index("try {") < source.index("chromium.launch")
    assert source.index("try {") < source.index("JSON.parse")
    assert "context.route('**/*'" in source
    assert "route.abort('blockedbyclient')" in source
    assert "context.routeWebSocket(/.*/" in source
    assert "Non-local WebSocket blocked" in source
    assert "websocket.connectToServer()" in source
    assert "serviceWorkers: 'block'" in source
    assert "Invalid password was accepted" in source


def test_acceptance_journey_uses_current_status_transition_modal_contract():
    source = _text("acceptance_journey.mjs")
    assert "#statusTransitionModal" in source
    assert "#statusTransitionReason" in source
    assert "#statusTransitionConfirm" in source
    assert "page.once('dialog'" not in source


def test_acceptance_user_password_is_read_from_stdin_and_not_argv():
    source = _text("acceptance_business.py")
    assert 'subparsers.add_parser("ensure-user")' in source
    assert "sys.stdin.readline()" in source
    assert 'add_argument("--password"' not in source
    assert "hash_password(password)" in source
