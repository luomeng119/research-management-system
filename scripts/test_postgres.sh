#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <t02-admin-url>" >&2
  exit 64
fi

t02_admin_hint=${1%%\?*}
t02_database_name=${t02_admin_hint##*/}
if [[ ! "$t02_database_name" =~ (^|_)t0(2|3|4|5|6|7)($|_) ]]; then
  echo "refusing database identifier outside the T02-T07 contract: $t02_database_name" >&2
  exit 65
fi
t03_contract=0
if [[ "$t02_database_name" =~ (^|_)t03($|_) ]]; then
  t03_contract=1
fi
t04_contract=0
if [[ "$t02_database_name" =~ (^|_)t04($|_) ]]; then
  t04_contract=1
fi
t05_contract=0
if [[ "$t02_database_name" =~ (^|_)t05($|_) ]]; then
  t05_contract=1
fi
t06_contract=0
if [[ "$t02_database_name" =~ (^|_)t06($|_) ]]; then
  t06_contract=1
fi
t07_contract=0
if [[ "$t02_database_name" =~ (^|_)t07($|_) ]]; then
  t07_contract=1
fi

for t02_command in initdb pg_ctl psql createdb; do
  if ! command -v "$t02_command" >/dev/null 2>&1; then
    echo "required PostgreSQL command is unavailable: $t02_command" >&2
    exit 69
  fi
done

if [[ ! -x .venv/bin/python ]]; then
  echo "project interpreter is unavailable: .venv/bin/python" >&2
  exit 69
fi
t02_python=.venv/bin/python

t02_root=$(mktemp -d "${TMPDIR:-/tmp}/rm-v1-t02-postgres.XXXXXX")
t02_data="$t02_root/data"
t02_socket="$t02_root/socket"
t02_log="$t02_root/postgres.log"
t02_port=$("$t02_python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
t02_migration_role="rm_v1_t02_migration_${t02_port}"
t02_runtime_role="rm_v1_t02_runtime_${t02_port}"
t02_nologin_role="rm_v1_t02_nologin_${t02_port}"
t02_replication_role="rm_v1_t02_replication_${t02_port}"
t02_owner_member_role="rm_v1_t02_owner_member_${t02_port}"
t02_write_member_role="rm_v1_t02_write_member_${t02_port}"
t02_injection_role="rm_v1_t02_injection_${t02_port}"
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
echo "T02 temporary PostgreSQL: isolated cluster=$t02_root port=$t02_port database=$t02_database"
echo "T02 caller URL is used only as a safety identifier; no caller database is contacted"

if [[ "${T02_TEST_FAIL_AT:-}" == "after_cluster_start" ]]; then
  echo "injected failure after_cluster_start" >&2
  exit 70
fi

psql -h 127.0.0.1 -p "$t02_port" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE $t02_migration_role LOGIN;" \
  -c "CREATE ROLE $t02_runtime_role LOGIN;" \
  -c "CREATE ROLE $t02_nologin_role NOLOGIN;" \
  -c "CREATE ROLE $t02_replication_role LOGIN REPLICATION;" \
  -c "CREATE ROLE $t02_owner_member_role LOGIN;" \
  -c "CREATE ROLE $t02_write_member_role LOGIN;" \
  -c "CREATE ROLE $t02_injection_role LOGIN;" \
  -c "GRANT $t02_migration_role TO $t02_owner_member_role;" \
  -c "GRANT pg_write_all_data TO $t02_write_member_role;" >/dev/null
createdb -h 127.0.0.1 -p "$t02_port" -O "$t02_migration_role" "$t02_database"
psql -h 127.0.0.1 -p "$t02_port" -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "ALTER SCHEMA public OWNER TO $t02_migration_role;" >/dev/null

export MIGRATION_DATABASE_URL="postgresql+psycopg://$t02_migration_role@127.0.0.1:$t02_port/$t02_database"
export DATABASE_URL="postgresql+psycopg://$t02_runtime_role@127.0.0.1:$t02_port/$t02_database"
export TEST_DATABASE_URL="$DATABASE_URL"
export T02_RUNTIME_ROLE="$t02_runtime_role"
export T02_NOLOGIN_ROLE="$t02_nologin_role"
export T02_REPLICATION_ROLE="$t02_replication_role"
export T02_OWNER_MEMBER_ROLE="$t02_owner_member_role"
export T02_WRITE_MEMBER_ROLE="$t02_write_member_role"
export T02_INJECTION_ROLE="$t02_injection_role"

expect_provision_rejected() {
  local t02_candidate_role=$1
  local t02_expected_error=$2
  local t02_rejection_output
  if t02_rejection_output=$("$t02_python" scripts/provision_postgres.py \
    --migration-url "$MIGRATION_DATABASE_URL" \
    --runtime-role "$t02_candidate_role" 2>&1); then
    echo "unsafe runtime role was accepted: $t02_candidate_role" >&2
    exit 71
  fi
  if [[ "$t02_rejection_output" != *"$t02_expected_error"* ]]; then
    echo "unexpected rejection for $t02_candidate_role: $t02_rejection_output" >&2
    exit 72
  fi
  echo "runtime role rejected: $t02_candidate_role"
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

"$t02_python" -m alembic -c alembic.ini upgrade head
t02_head_revision=$(show_revision)
echo "migration roundtrip: upgrade=$t02_head_revision"
"$t02_python" -m alembic -c alembic.ini downgrade 0001_v1_core
psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "INSERT INTO proposals (business_id,title,source_type,status,version) VALUES ('T07-UPGRADE-PROBE','升级探针','IDEA','DRAFT',3);" \
  -c "INSERT INTO proposal_ai_drafts (proposal_id,status,provider_kind,model_version,prompt_version,content,accepted_fields) SELECT id,'READY','LOCAL','legacy-local','proposal-v1','{\"title\":\"探针\"}'::jsonb,'[]'::jsonb FROM proposals WHERE business_id='T07-UPGRADE-PROBE';" >/dev/null
"$t02_python" -m alembic -c alembic.ini upgrade head
t07_upgrade_probe=$(psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -Atc "SELECT concat(provider_kind,':',source_proposal_version) FROM proposal_ai_drafts d JOIN proposals p ON p.id=d.proposal_id WHERE p.business_id='T07-UPGRADE-PROBE'")
if [[ "$t07_upgrade_probe" != "LOCAL:0" ]]; then
  echo "0001 to 0002 assistant draft backfill failed: $t07_upgrade_probe" >&2
  exit 78
fi
echo "migration existing-draft upgrade (unknown version is non-applicable): $t07_upgrade_probe"
psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "INSERT INTO proposal_decisions (proposal_id,decision,decision_date,conclusion,basis,idempotency_key,request_fingerprint,result_snapshot) SELECT id,'ESTABLISH',CURRENT_DATE,'downgrade guard','test evidence','t08-migration-guard',repeat('a',64),'{\"project\":{}}'::jsonb FROM proposals WHERE business_id='T07-UPGRADE-PROBE';" >/dev/null
if "$t02_python" -m alembic -c alembic.ini downgrade 0002_proposal_assistant >/dev/null 2>&1; then
  echo "0003 downgrade silently discarded establishment idempotency evidence" >&2
  exit 81
fi
t08_establishment_probe=$(psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -Atc "SELECT count(*) FROM proposal_decisions WHERE idempotency_key='t08-migration-guard'")
if [[ "$t08_establishment_probe" != "1" || "$(show_revision)" != "$t02_head_revision" ]]; then
  echo "0003 downgrade guard did not preserve establishment evidence: count=$t08_establishment_probe revision=$(show_revision)" >&2
  exit 82
fi
psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "DELETE FROM proposal_decisions WHERE idempotency_key='t08-migration-guard';" >/dev/null
echo "migration 0003 lossy-downgrade guard: establishment evidence preserved"
psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "INSERT INTO proposal_ai_drafts (proposal_id,status,provider_kind,model_version,prompt_version,source_proposal_version,content,accepted_fields) SELECT id,'READY','DEEPSEEK','migration-guard','proposal-v1',3,'{\"title\":\"远程证据\"}'::jsonb,'[]'::jsonb FROM proposals WHERE business_id='T07-UPGRADE-PROBE';" >/dev/null
if "$t02_python" -m alembic -c alembic.ini downgrade 0001_v1_core >/dev/null 2>&1; then
  echo "0002 downgrade silently discarded non-LOCAL assistant evidence" >&2
  exit 79
fi
t07_remote_probe=$(psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -Atc "SELECT count(*) FROM proposal_ai_drafts WHERE model_version='migration-guard'")
if [[ "$t07_remote_probe" != "1" || "$(show_revision)" != "$t02_head_revision" ]]; then
  echo "0002 downgrade guard did not preserve remote evidence: count=$t07_remote_probe revision=$(show_revision)" >&2
  exit 80
fi
psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "DELETE FROM proposal_ai_drafts WHERE model_version='migration-guard';" >/dev/null
echo "migration lossy-downgrade guard: remote evidence preserved"
"$t02_python" -m alembic -c alembic.ini upgrade head
"$t02_python" -m alembic -c alembic.ini downgrade base
echo "migration roundtrip: downgrade=$(show_revision)"
"$t02_python" -m alembic -c alembic.ini upgrade head
echo "migration roundtrip: re-upgrade=$(show_revision)"

"$t02_python" scripts/provision_postgres.py \
  --migration-url "$MIGRATION_DATABASE_URL" \
  --runtime-role "$t02_runtime_role"

expect_provision_rejected "$t02_migration_role" "must differ from the migration owner"
expect_provision_rejected "$t02_nologin_role" "must have LOGIN"
expect_provision_rejected "$t02_replication_role" "replication attributes"
expect_provision_rejected "$t02_owner_member_role" "independent leaf role"
expect_provision_rejected "$t02_write_member_role" "independent leaf role"

if t02_injection_output=$("$t02_python" -c \
  'import os; from scripts.provision_postgres import provision; provision(os.environ["MIGRATION_DATABASE_URL"], os.environ["T02_INJECTION_ROLE"], _fail_after_acl=True)' \
  2>&1); then
  echo "ACL failure injection unexpectedly succeeded" >&2
  exit 73
fi
if [[ "$t02_injection_output" != *"injected ACL failure"* ]]; then
  echo "unexpected ACL failure injection output: $t02_injection_output" >&2
  exit 74
fi

t02_direct_acl_count=$(psql -h 127.0.0.1 -p "$t02_port" \
  -U "$t02_migration_role" -d "$t02_database" -Atc \
  "SELECT count(*) FROM (
     SELECT 1 FROM pg_namespace n
     CROSS JOIN LATERAL aclexplode(n.nspacl) acl
     JOIN pg_roles grantee ON grantee.oid = acl.grantee
     WHERE n.nspname = 'public' AND grantee.rolname = '$t02_injection_role'
     UNION ALL
     SELECT 1 FROM pg_class c
     JOIN pg_namespace n ON n.oid = c.relnamespace
     CROSS JOIN LATERAL aclexplode(c.relacl) acl
     JOIN pg_roles grantee ON grantee.oid = acl.grantee
     WHERE n.nspname = 'public' AND grantee.rolname = '$t02_injection_role'
   ) direct_acl")
t02_default_acl_count=$(psql -h 127.0.0.1 -p "$t02_port" \
  -U "$t02_migration_role" -d "$t02_database" -Atc \
  "SELECT count(*) FROM pg_default_acl defaults
   CROSS JOIN LATERAL aclexplode(defaults.defaclacl) acl
   JOIN pg_roles grantee ON grantee.oid = acl.grantee
   WHERE grantee.rolname = '$t02_injection_role'")
if [[ "$t02_direct_acl_count" != "0" || "$t02_default_acl_count" != "0" ]]; then
  echo "ACL failure injection leaked privileges: direct=$t02_direct_acl_count default=$t02_default_acl_count" >&2
  exit 75
fi
t02_rolled_back_permissions=$(psql -h 127.0.0.1 -p "$t02_port" \
  -U "$t02_migration_role" -d "$t02_database" -Atc \
  "SELECT concat_ws(',',
     has_schema_privilege('$t02_injection_role', 'public', 'CREATE'),
     has_table_privilege('$t02_injection_role', 'audit_events', 'SELECT'),
     has_table_privilege('$t02_injection_role', 'audit_events', 'INSERT'),
     has_table_privilege('$t02_injection_role', 'audit_events', 'UPDATE'),
     has_table_privilege('$t02_injection_role', 'audit_events', 'DELETE'),
     has_table_privilege('$t02_injection_role', 'audit_events', 'TRUNCATE'))")
if [[ "$t02_rolled_back_permissions" != "f,f,f,f,f,f" ]]; then
  echo "ACL failure injection retained effective schema/table privileges: $t02_rolled_back_permissions" >&2
  exit 76
fi

t02_future_table="injection_rollback_future_${t02_port}"
psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "CREATE TABLE $t02_future_table (id integer);" >/dev/null
t02_future_permissions=$(psql -h 127.0.0.1 -p "$t02_port" \
  -U "$t02_migration_role" -d "$t02_database" -Atc \
  "SELECT concat_ws(',',
     has_table_privilege('$t02_injection_role', '$t02_future_table', 'SELECT'),
     has_table_privilege('$t02_injection_role', '$t02_future_table', 'INSERT'),
     has_table_privilege('$t02_injection_role', '$t02_future_table', 'UPDATE'),
     has_table_privilege('$t02_injection_role', '$t02_future_table', 'DELETE'),
     has_table_privilege('$t02_injection_role', '$t02_future_table', 'TRUNCATE'))")
if [[ "$t02_future_permissions" != "f,f,f,f,f" ]]; then
  echo "ACL failure injection leaked default table privileges: $t02_future_permissions" >&2
  exit 77
fi
psql -h 127.0.0.1 -p "$t02_port" -U "$t02_migration_role" \
  -d "$t02_database" -v ON_ERROR_STOP=1 \
  -c "DROP TABLE $t02_future_table;" >/dev/null
echo "ACL failure injection rollback: direct=0 default=0 existing=$t02_rolled_back_permissions future_table=$t02_future_permissions"

show_catalog
if [[ "$t05_contract" -eq 1 ]]; then
  T05_TEST_DATABASE_URL="$MIGRATION_DATABASE_URL" \
    "$t02_python" -m pytest app/tests/test_legacy_migration.py -q \
    -k postgresql_concurrent_same_batch_and_identity_sequence
fi
if [[ "$t06_contract" -eq 1 ]]; then
  T06_TEST_DATABASE_URL="$MIGRATION_DATABASE_URL" \
    "$t02_python" -m pytest app/tests/test_proposals.py -q \
    -k postgresql_concurrent_version_and_decision_idempotency
fi
if [[ "$t07_contract" -eq 1 ]]; then
  T07_TEST_DATABASE_URL="$MIGRATION_DATABASE_URL" \
    "$t02_python" -m pytest app/tests/test_assistant.py -q \
    -k postgresql_concurrent_apply_allows_exactly_one_winner
fi
"$t02_python" -m pytest app/tests/test_project_establishment.py -q \
  -k postgresql_concurrent_establishment_is_atomic_and_idempotent
"$t02_python" -m pytest app/tests/test_db_contract.py -q
if [[ "$t03_contract" -eq 1 ]]; then
  "$t02_python" -m pytest app/tests/test_auth_audit_postgres.py -q
fi
if [[ "$t04_contract" -eq 1 ]]; then
  "$t02_python" -m pytest app/tests/test_file_service.py -q -k postgres
fi
echo "T02 PostgreSQL contract completed; temporary cluster will be removed"
