from __future__ import annotations

import argparse
import os
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def _psycopg_url(value: str) -> str:
    split = urlsplit(value)
    if split.query or split.fragment:
        raise ValueError(
            "migration URL must not include query parameters or fragments"
        )
    url = make_url(value)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("migration URL must use PostgreSQL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def provision(
    migration_url: str,
    runtime_role: str,
    *,
    _fail_after_acl: bool = False,
) -> tuple[str, str]:
    with psycopg.connect(_psycopg_url(migration_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, current_database(), "
                "pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
            )
            migration_owner, database_name, database_owner = cursor.fetchone()
            if migration_owner != database_owner:
                raise RuntimeError(
                    "migration URL must connect as the existing database owner"
                )
            cursor.execute(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname = 'public'"
            )
            if cursor.fetchone() != (migration_owner,):
                raise RuntimeError(
                    "migration owner must already own the public schema"
                )
            cursor.execute(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
                "rolbypassrls, rolreplication "
                "FROM pg_roles WHERE rolname = %s",
                (runtime_role,),
            )
            runtime_attributes = cursor.fetchone()
            if runtime_attributes is None:
                raise RuntimeError(f"runtime role does not exist: {runtime_role}")
            if runtime_role == migration_owner:
                raise RuntimeError("runtime role must differ from the migration owner")
            can_login, *forbidden_attributes = runtime_attributes
            if not can_login:
                raise RuntimeError("runtime role must have LOGIN")
            if any(forbidden_attributes):
                raise RuntimeError(
                    "runtime role must not have superuser, role creation, database "
                    "creation, row-security bypass, or replication attributes"
                )
            cursor.execute(
                "SELECT pg_get_userbyid(roleid) FROM pg_auth_members "
                "WHERE member = (SELECT oid FROM pg_roles WHERE rolname = %s)",
                (runtime_role,),
            )
            if cursor.fetchall():
                raise RuntimeError(
                    "runtime role must be an independent leaf role with no memberships"
                )

            owner = sql.Identifier(migration_owner)
            database = sql.Identifier(database_name)
            runtime = sql.Identifier(runtime_role)
            direct_acl_statements = (
                sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC;").format(database),
                sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM {};").format(database, runtime),
                sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC;"),
                sql.SQL("REVOKE ALL ON SCHEMA public FROM {};").format(runtime),
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;"),
                sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;"),
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {};").format(database, runtime),
                sql.SQL("GRANT USAGE ON SCHEMA public TO {};").format(runtime),
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    "IN SCHEMA public TO {};"
                ).format(runtime),
                sql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {};"
                ).format(runtime),
                sql.SQL("REVOKE ALL ON TABLE audit_events FROM {};").format(runtime),
                sql.SQL("GRANT INSERT, SELECT ON TABLE audit_events TO {};").format(runtime),
            )
            default_table_acl_statements = (
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "REVOKE ALL ON TABLES FROM PUBLIC;"
                ).format(owner),
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {};"
                ).format(owner, runtime),
            )
            default_sequence_acl_statements = (
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "REVOKE ALL ON SEQUENCES FROM PUBLIC;"
                ).format(owner),
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {};"
                ).format(owner, runtime),
            )
            for statement in direct_acl_statements + default_table_acl_statements:
                cursor.execute(statement)
            if _fail_after_acl:
                raise RuntimeError("injected ACL failure")
            for statement in default_sequence_acl_statements:
                cursor.execute(statement)

            cursor.execute(
                "SELECT "
                "has_database_privilege(%s, current_database(), 'CREATE'), "
                "has_database_privilege(%s, current_database(), 'TEMPORARY'), "
                "has_schema_privilege(%s, 'public', 'CREATE'), "
                "has_table_privilege(%s, 'public.audit_events', 'SELECT'), "
                "has_table_privilege(%s, 'public.audit_events', 'INSERT'), "
                "has_table_privilege(%s, 'public.audit_events', 'UPDATE'), "
                "has_table_privilege(%s, 'public.audit_events', 'DELETE'), "
                "has_table_privilege(%s, 'public.audit_events', 'TRUNCATE')",
                (runtime_role,) * 8,
            )
            expected_permissions = (
                False,
                False,
                False,
                True,
                True,
                False,
                False,
                False,
            )
            if cursor.fetchone() != expected_permissions:
                raise RuntimeError(
                    "runtime role effective privileges do not match the required boundary"
                )
    return migration_owner, database_name


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the production PostgreSQL migration/runtime role boundary."
    )
    parser.add_argument(
        "--migration-url",
        default=os.environ.get("MIGRATION_DATABASE_URL"),
        help="Database-owner URL; defaults to MIGRATION_DATABASE_URL.",
    )
    parser.add_argument("--runtime-role", required=True)
    args = parser.parse_args()
    if not args.migration_url:
        parser.error("--migration-url or MIGRATION_DATABASE_URL is required")
    owner, database = provision(args.migration_url, args.runtime_role)
    print(
        f"PostgreSQL privileges provisioned: database={database} "
        f"migration_owner={owner} runtime_role={args.runtime_role}"
    )


if __name__ == "__main__":
    main()
