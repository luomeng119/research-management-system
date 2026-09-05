#!/usr/bin/env bash
set -euo pipefail

for t12_name in MIGRATION_DATABASE_URL DATABASE_URL \
  T02_ISOLATED_POSTGRES_ROOT T02_ISOLATED_POSTGRES_PORT \
  T02_ISOLATED_POSTGRES_DATABASE; do
  if [[ -z "${!t12_name:-}" ]]; then
    echo "required environment variable is missing: $t12_name" >&2
    exit 64
  fi
done

for t12_command in curl node pg_dump pg_restore createdb psql pg_ctl; do
  if ! command -v "$t12_command" >/dev/null 2>&1; then
    echo "required acceptance command is unavailable: $t12_command" >&2
    exit 69
  fi
done

if [[ ! -x .venv/bin/python ]]; then
  echo "project interpreter is unavailable: .venv/bin/python" >&2
  exit 69
fi
if [[ ! -d node_modules/@playwright/test ]]; then
  echo "Playwright test runtime is unavailable: node_modules/@playwright/test" >&2
  exit 69
fi

t12_python=.venv/bin/python
export PYTHONPATH="$PWD"

validate_t12_database() {
"$t12_python" - "$1" <<'PY'
import os
import sys
from pathlib import Path

import sqlalchemy as sa


expected_data = (Path(os.environ["T02_ISOLATED_POSTGRES_ROOT"]) / "data").resolve()
expected_port = int(os.environ["T02_ISOLATED_POSTGRES_PORT"])
expected_database = sys.argv[1]
if not expected_data.is_dir():
    raise SystemExit(f"isolated PostgreSQL data directory is unavailable: {expected_data}")
pid_lines = (expected_data / "postmaster.pid").read_text(encoding="utf-8").splitlines()
if len(pid_lines) < 4 or int(pid_lines[3]) != expected_port:
    raise SystemExit("isolated PostgreSQL PID record does not match the harness port")
try:
    os.kill(int(pid_lines[0]), 0)
except (OSError, ValueError) as exc:
    raise SystemExit("isolated PostgreSQL process is not active") from exc

for variable in ("MIGRATION_DATABASE_URL", "DATABASE_URL"):
    engine = sa.create_engine(os.environ[variable])
    try:
        with engine.connect() as connection:
            actual_database, actual_port, actual_address, actual_user = connection.execute(
                sa.text(
                    "SELECT current_database(), inet_server_port(), "
                    "host(inet_server_addr()), current_user"
                )
            ).one()
    finally:
        engine.dispose()
    if (
        actual_database != expected_database
        or int(actual_port) != expected_port
        or actual_address not in {"127.0.0.1", "::1"}
        or actual_user
        != f"rm_v1_t02_{'migration' if variable == 'MIGRATION_DATABASE_URL' else 'runtime'}_{expected_port}"
    ):
        raise SystemExit(
            f"{variable} is not bound to the isolated PostgreSQL harness"
        )
PY
}
validate_t12_database "$T02_ISOLATED_POSTGRES_DATABASE"

mkdir -p build
t12_evidence_root=$(mktemp -d "$PWD/build/acceptance-posix.XXXXXX")
chmod 700 "$t12_evidence_root"
t12_runtime_root="$t12_evidence_root/runtime"
mkdir -p "$t12_runtime_root"
chmod 700 "$t12_runtime_root"
t12_baseline="$t12_evidence_root/business-baseline.json"
t12_after="$t12_evidence_root/business-after-browser.json"
t12_readiness="$t12_evidence_root/readiness.json"
t12_outbound="$t12_evidence_root/outbound-requests.json"
t12_browser_result="$t12_evidence_root/browser-result.json"
t12_app_log="$t12_evidence_root/waitress.log"
t12_app_pid=""

cleanup_t12_posix() {
  if [[ -n "$t12_app_pid" ]] && kill -0 "$t12_app_pid" >/dev/null 2>&1; then
    kill "$t12_app_pid" >/dev/null 2>&1 || true
    wait "$t12_app_pid" >/dev/null 2>&1 || true
  fi
}
trap cleanup_t12_posix EXIT INT TERM

stop_t12_waitress() {
  if [[ -z "$t12_app_pid" ]] || ! kill -0 "$t12_app_pid" >/dev/null 2>&1; then
    echo "Waitress was not running at acceptance cleanup" >&2
    return 1
  fi
  kill "$t12_app_pid"
  wait "$t12_app_pid" >/dev/null 2>&1 || true
  if kill -0 "$t12_app_pid" >/dev/null 2>&1; then
    echo "Waitress remained active after acceptance cleanup" >&2
    return 1
  fi
  t12_app_pid=""
}

t12_port=$("$t12_python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
t12_password=$("$t12_python" -c 'import secrets; print("Accept-" + secrets.token_urlsafe(24) + "-7A")')
export APP_DATA_ROOT="$t12_runtime_root"
export APP_BIND_HOST=127.0.0.1
export APP_PORT="$t12_port"
export AI_PROVIDER=DISABLED
export DEPLOYMENT_MODE=PRODUCTION
export FLASK_SECRET_KEY
FLASK_SECRET_KEY=$("$t12_python" -c 'import secrets; print(secrets.token_urlsafe(48))')
export ACCEPTANCE_USERNAME
ACCEPTANCE_USERNAME=$("$t12_python" -c 'import secrets; print("acceptance_" + secrets.token_hex(8))')

printf '%s\n' "$t12_password" | "$t12_python" scripts/acceptance_business.py ensure-user \
  --username "$ACCEPTANCE_USERNAME" --name "张老师"
"$t12_python" scripts/acceptance_business.py seed \
  --username "$ACCEPTANCE_USERNAME" --output "$t12_baseline"

start_t12_waitress() {
env -i \
  PATH="$PATH" \
  HOME="${HOME:-}" \
  TMPDIR="${TMPDIR:-/tmp}" \
  LANG="${LANG:-C.UTF-8}" \
  PYTHONPATH="$PYTHONPATH" \
  DATABASE_URL="$DATABASE_URL" \
  FLASK_SECRET_KEY="$FLASK_SECRET_KEY" \
  APP_DATA_ROOT="$APP_DATA_ROOT" \
  APP_BIND_HOST="$APP_BIND_HOST" \
  APP_PORT="$APP_PORT" \
  AI_PROVIDER="$AI_PROVIDER" \
  DEPLOYMENT_MODE="$DEPLOYMENT_MODE" \
  "$t12_python" run.py >"$t12_app_log" 2>&1 &
t12_app_pid=$!
t12_base_url="http://127.0.0.1:$t12_port"

t12_ready=0
for _ in {1..80}; do
  if ! kill -0 "$t12_app_pid" >/dev/null 2>&1; then
    echo "Waitress exited before readiness; see $t12_app_log" >&2
    exit 70
  fi
  if curl --fail --silent --show-error "$t12_base_url/health/ready" \
      --output "$t12_readiness" 2>/dev/null; then
    t12_ready=1
    break
  fi
  sleep 0.25
done
if [[ "$t12_ready" -ne 1 ]]; then
  echo "Waitress readiness timed out; see $t12_app_log" >&2
  exit 70
fi

"$t12_python" - "$t12_readiness" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
expected = {
    "status": "ready",
    "checks": {"database": "ok", "schema": "ok", "storage": "ok"},
}
if payload != expected:
    raise SystemExit(f"unexpected readiness payload: {payload!r}")
PY
}
start_t12_waitress

printf '%s\n' "$t12_password" | env -i \
  PATH="$PATH" \
  HOME="${HOME:-}" \
  TMPDIR="${TMPDIR:-/tmp}" \
  LANG="${LANG:-C.UTF-8}" \
  ACCEPTANCE_BASE_URL="$t12_base_url" \
  ACCEPTANCE_BASELINE="$t12_baseline" \
  ACCEPTANCE_OUTBOUND="$t12_outbound" \
  ACCEPTANCE_RESULT="$t12_browser_result" \
  ACCEPTANCE_USERNAME="$ACCEPTANCE_USERNAME" \
  node scripts/acceptance_browser.mjs

"$t12_python" scripts/acceptance_business.py verify \
  --expected "$t12_baseline" --output "$t12_after"
"$t12_python" scripts/acceptance_business.py verify-journey \
  --expected "$t12_browser_result" --output "$t12_evidence_root/journey-persistence.json"

stop_t12_waitress

# Snapshot only after the owned service is stopped: database and file writes are quiescent.
t12_backup_root="$t12_evidence_root/backup"
mkdir -m 700 "$t12_backup_root"
cp -R "$t12_runtime_root" "$t12_backup_root/payload"
env -i PATH="$PATH" PYTHONPATH="$PYTHONPATH" \
  PGHOST=127.0.0.1 PGPORT="$T02_ISOLATED_POSTGRES_PORT" \
  PGDATABASE="$T02_ISOLATED_POSTGRES_DATABASE" \
  PGUSER="rm_v1_t02_runtime_$T02_ISOLATED_POSTGRES_PORT" \
  DATABASE_URL="$DATABASE_URL" \
  "$t12_python" scripts/verify_offline.py snapshot \
  --data-root "$t12_runtime_root" --package-root "$t12_backup_root" \
  --output "$t12_backup_root/manifest.json" \
  --pg-dump-exe "$(command -v pg_dump)" --dump-output "$t12_backup_root/database.dump"
"$t12_python" scripts/verify_offline.py verify-package \
  --package-root "$t12_backup_root" --manifest "$t12_backup_root/manifest.json"
pg_restore --list --no-password "$t12_backup_root/database.dump" >/dev/null
ACCEPTANCE_BACKUP_ROOT="$t12_backup_root" "$t12_python" -m pytest \
  scripts/tests/test_verify_offline.py -k real_backup_rejects_corrupted -q

# A distinct database and an exclusively-created directory: never clean the source.
t12_restore_database="rm_v1_t12_restore_$T02_ISOLATED_POSTGRES_PORT"
t12_restore_root="$t12_evidence_root/restored-runtime"
t12_migration_role="rm_v1_t02_migration_$T02_ISOLATED_POSTGRES_PORT"
t12_runtime_role="rm_v1_t02_runtime_$T02_ISOLATED_POSTGRES_PORT"
mkdir -m 700 "$t12_restore_root"
env -i PATH="$PATH" PGHOST=127.0.0.1 PGPORT="$T02_ISOLATED_POSTGRES_PORT" \
  PGUSER="$(id -un)" createdb --no-password --template=template0 \
  --owner="$t12_migration_role" "$t12_restore_database"
assert_t12_restore_empty() {
local t12_restore_tables t12_restore_files
[[ -d "$2" && ! -L "$2" ]] || return 2
if ! t12_restore_tables=$(env -i PATH="$PATH" PGHOST=127.0.0.1 \
  PGPORT="$T02_ISOLATED_POSTGRES_PORT" PGUSER="$t12_migration_role" \
  psql --no-password --dbname="$1" -At -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"); then
  return 2
fi
if ! t12_restore_files=$(ls -A "$2"); then return 2; fi
if [[ "$t12_restore_tables" != "0" || -n "$t12_restore_files" ]]; then
  echo "restore target is not empty; refusing to restore" >&2
  return 1
fi
}
# Read-only negative probes: do not create, erase or restore anything in these targets.
if assert_t12_restore_empty "$T02_ISOLATED_POSTGRES_DATABASE" "$t12_restore_root" \
    2>"$t12_evidence_root/nonempty-database-rejected.log"; then
  echo "nonempty database was accepted" >&2; exit 70
else
  [[ $? -eq 1 ]] || exit 70
fi
if assert_t12_restore_empty "$t12_restore_database" "$t12_backup_root/payload" \
    2>"$t12_evidence_root/nonempty-files-rejected.log"; then
  echo "nonempty file directory was accepted" >&2; exit 70
else
  [[ $? -eq 1 ]] || exit 70
fi
"$t12_python" scripts/verify_offline.py verify --data-root "$t12_runtime_root" \
  --manifest "$t12_backup_root/manifest.json"
assert_t12_restore_empty "$t12_restore_database" "$t12_restore_root"
env -i PATH="$PATH" PGHOST=127.0.0.1 PGPORT="$T02_ISOLATED_POSTGRES_PORT" \
  PGUSER="$(id -un)" psql --no-password --dbname="$t12_restore_database" \
  -v ON_ERROR_STOP=1 -c "ALTER SCHEMA public OWNER TO $t12_migration_role" >/dev/null
env -i PATH="$PATH" PGHOST=127.0.0.1 PGPORT="$T02_ISOLATED_POSTGRES_PORT" \
  PGUSER="$t12_migration_role" pg_restore --dbname="$t12_restore_database" \
  --exit-on-error --single-transaction --no-owner --no-privileges --no-password \
  "$t12_backup_root/database.dump"
cp -R "$t12_backup_root/payload/." "$t12_restore_root/"
export MIGRATION_DATABASE_URL="postgresql+psycopg://$t12_migration_role@127.0.0.1:$T02_ISOLATED_POSTGRES_PORT/$t12_restore_database"
export DATABASE_URL="postgresql+psycopg://$t12_runtime_role@127.0.0.1:$T02_ISOLATED_POSTGRES_PORT/$t12_restore_database"
export APP_DATA_ROOT="$t12_restore_root"
"$t12_python" scripts/provision_postgres.py --runtime-role "$t12_runtime_role"
"$t12_python" scripts/verify_offline.py verify --data-root "$t12_restore_root" \
  --manifest "$t12_backup_root/manifest.json"
"$t12_python" scripts/acceptance_business.py verify \
  --expected "$t12_baseline" --output "$t12_evidence_root/business-after-restore.json"
"$t12_python" scripts/acceptance_business.py verify-journey \
  --expected "$t12_browser_result" --output "$t12_evidence_root/journey-after-restore.json"

validate_t12_database "$t12_restore_database"
pg_ctl -D "$T02_ISOLATED_POSTGRES_ROOT/data" -m fast restart
validate_t12_database "$t12_restore_database"
"$t12_python" scripts/verify_offline.py verify --data-root "$t12_restore_root" \
  --manifest "$t12_backup_root/manifest.json"
t12_app_log="$t12_evidence_root/waitress-after-restore.log"
t12_readiness="$t12_evidence_root/readiness-after-restore.json"
start_t12_waitress
printf '%s\n' "$t12_password" | env -i \
  PATH="$PATH" HOME="${HOME:-}" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-C.UTF-8}" \
  ACCEPTANCE_BASE_URL="$t12_base_url" ACCEPTANCE_BASELINE="$t12_baseline" \
  ACCEPTANCE_OUTBOUND="$t12_evidence_root/outbound-after-restore.json" \
  ACCEPTANCE_RESULT="$t12_evidence_root/browser-after-restore.json" \
  ACCEPTANCE_USERNAME="$ACCEPTANCE_USERNAME" ACCEPTANCE_PHASE=after-restore \
  ACCEPTANCE_PREVIOUS_RESULT="$t12_browser_result" \
  node scripts/acceptance_browser.mjs
t12_password=""
stop_t12_waitress

echo "POSIX production-chain acceptance passed: PostgreSQL + Waitress + Chromium"
echo "Evidence retained at: $t12_evidence_root"
