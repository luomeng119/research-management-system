#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <t02-admin-url>" >&2
  exit 64
fi

t02_admin_hint=${1%%\?*}
t02_database_name=${t02_admin_hint##*/}
if [[ ! "$t02_database_name" =~ (^|_)t02($|_) ]]; then
  echo "refusing non-T02 database identifier: $t02_database_name" >&2
  exit 65
fi

for t02_command in initdb pg_ctl psql createdb; do
  if ! command -v "$t02_command" >/dev/null 2>&1; then
    echo "required PostgreSQL command is unavailable: $t02_command" >&2
    exit 69
  fi
done

if [[ -x .venv/bin/python ]]; then
  t02_python=.venv/bin/python
else
  t02_python=$(command -v python)
fi

t02_root=$(mktemp -d "${TMPDIR:-/tmp}/rm-v1-t02-postgres.XXXXXX")
t02_data="$t02_root/data"
t02_socket="$t02_root/socket"
t02_log="$t02_root/postgres.log"
t02_port=$("$t02_python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
t02_migration_role=rm_v1_t02_migration
t02_runtime_role=rm_v1_t02_runtime
t02_database=rm_v1_t02
t02_started=0

cleanup_t02_postgres() {
  if [[ "$t02_started" -eq 1 ]]; then
    pg_ctl -D "$t02_data" -m fast stop >/dev/null 2>&1 || true
  fi
  rm -rf "$t02_root"
}
trap cleanup_t02_postgres EXIT INT TERM

mkdir -p "$t02_socket"
initdb -D "$t02_data" --auth=trust --no-locale --encoding=UTF8 >/dev/null
pg_ctl -D "$t02_data" -l "$t02_log" \
  -o "-F -p $t02_port -k $t02_socket -h 127.0.0.1" start >/dev/null
t02_started=1

psql -h 127.0.0.1 -p "$t02_port" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE $t02_migration_role LOGIN;" \
  -c "CREATE ROLE $t02_runtime_role LOGIN;" >/dev/null
createdb -h 127.0.0.1 -p "$t02_port" -O "$t02_migration_role" "$t02_database"

export MIGRATION_DATABASE_URL="postgresql+psycopg://$t02_migration_role@127.0.0.1:$t02_port/$t02_database"
export DATABASE_URL="postgresql+psycopg://$t02_runtime_role@127.0.0.1:$t02_port/$t02_database"
export TEST_DATABASE_URL="$DATABASE_URL"

grant_runtime_permissions() {
  psql -h 127.0.0.1 -p "$t02_port" \
    -d "$t02_database" -v ON_ERROR_STOP=1 <<SQL
ALTER SCHEMA public OWNER TO $t02_migration_role;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE, TEMPORARY ON DATABASE $t02_database FROM PUBLIC;
REVOKE CREATE, TEMPORARY ON DATABASE $t02_database FROM $t02_runtime_role;
REVOKE ALL ON SCHEMA public FROM $t02_runtime_role;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE $t02_database TO $t02_runtime_role;
GRANT USAGE ON SCHEMA public TO $t02_runtime_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO $t02_runtime_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO $t02_runtime_role;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_events FROM $t02_runtime_role;
SQL
}

show_revision() {
  psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
    -d "$t02_database" -Atc "SELECT COALESCE(max(version_num), 'base') FROM alembic_version"
}

show_catalog() {
  local t02_table_names t02_index_names t02_constraint_names
  t02_table_names=$(psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
    -d "$t02_database" -Atc \
    "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'")
  t02_index_names=$(psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
    -d "$t02_database" -Atc \
    "SELECT string_agg(indexname, ',' ORDER BY indexname) FROM pg_indexes WHERE schemaname = 'public' AND indexname NOT LIKE '%_pkey'")
  t02_constraint_names=$(psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
    -d "$t02_database" -Atc \
    "SELECT string_agg(conname, ',' ORDER BY conname) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' AND c.contype IN ('c','f','u')")
  echo "schema tables: $t02_table_names"
  echo "schema non-PK indexes: $t02_index_names"
  echo "schema checks/FKs/uniques: $t02_constraint_names"
}

echo "T02 temporary PostgreSQL: isolated cluster=$t02_root port=$t02_port database=$t02_database"
echo "T02 caller URL is used only as a safety identifier; no caller database is contacted"

"$t02_python" -m alembic -c alembic.ini upgrade head
echo "migration roundtrip: upgrade=$(show_revision)"
"$t02_python" -m alembic -c alembic.ini downgrade base
echo "migration roundtrip: downgrade=$(show_revision)"
"$t02_python" -m alembic -c alembic.ini upgrade head
echo "migration roundtrip: re-upgrade=$(show_revision)"

grant_runtime_permissions
show_catalog
"$t02_python" -m pytest app/tests/test_db_contract.py -q

echo "T02 PostgreSQL contract completed; temporary cluster will be removed"
