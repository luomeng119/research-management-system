from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.security.auth import hash_password, normalize_role, validate_password, verify_password

DUMMY_BCRYPT_HASH = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYl6kZ8b4Q8jmQqK8VxQx2gJ0eYp9UuK"


@dataclass(frozen=True)
class AuthenticatedUser:
    user: dict
    password_upgraded: bool


class UsersRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.table = sa.Table("users", sa.MetaData(), autoload_with=engine)

    def get_by_username(self, username: str, connection: Connection | None = None):
        statement = sa.select(self.table).where(self.table.c.username == username.strip())
        if connection is not None:
            row = connection.execute(statement).mappings().first()
            return dict(row) if row else None
        with self.engine.connect() as active:
            row = active.execute(statement).mappings().first()
            return dict(row) if row else None

    def get_by_id(self, user_id: int, connection: Connection | None = None):
        statement = sa.select(self.table).where(self.table.c.id == user_id)
        if connection is not None:
            row = connection.execute(statement).mappings().first()
            return dict(row) if row else None
        with self.engine.connect() as active:
            row = active.execute(statement).mappings().first()
            return dict(row) if row else None

    def authenticate(self, username: str, password: str) -> AuthenticatedUser | None:
        with self.engine.begin() as connection:
            user = self.get_by_username(username, connection)
            if not user or user.get("status") != "active":
                verify_password(password, DUMMY_BCRYPT_HASH)
                return None
            try:
                user["role"] = normalize_role(user["role"])
            except ValueError:
                return None
            valid, upgrade = verify_password(password, user["password"])
            if not valid:
                return None
            if upgrade:
                connection.execute(
                    self.table.update()
                    .where(self.table.c.id == user["id"])
                    .values(password=hash_password(password), version=self.table.c.version + 1)
                )
                user["version"] = int(user["version"]) + 1
            public_user = {key: value for key, value in user.items() if key != "password"}
            return AuthenticatedUser(public_user, upgrade)

    def list_accounts(self):
        columns = [
            column
            for column in self.table.c
            if column.name not in {"password", "directory_permissions"}
        ]
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(*columns).order_by(self.table.c.username)).mappings()]

    def set_status(self, connection: Connection, username: str, status: str) -> dict | None:
        if status not in {"active", "disabled"}:
            raise ValueError("unsupported account status")
        result = connection.execute(
            self.table.update()
            .where(self.table.c.username == username)
            .values(status=status, version=self.table.c.version + 1)
        )
        return self.get_by_username(username, connection) if result.rowcount == 1 else None

    def change_password(
        self, connection: Connection, user_id: int, old_password: str, new_password: str
    ) -> dict | None:
        validate_password(new_password)
        user = self.get_by_id(user_id, connection)
        if not user or user.get("status") != "active":
            return None
        valid, _ = verify_password(old_password, user["password"])
        if not valid:
            return None
        connection.execute(
            self.table.update()
            .where(self.table.c.id == user_id)
            .values(
                password=hash_password(new_password),
                must_change_password=False,
                version=self.table.c.version + 1,
            )
        )
        return self.get_by_id(user_id, connection)

    def reset_password(self, connection: Connection, username: str, password: str) -> dict | None:
        validate_password(password)
        result = connection.execute(
            self.table.update()
            .where(self.table.c.username == username)
            .values(
                password=hash_password(password),
                must_change_password=True,
                version=self.table.c.version + 1,
            )
        )
        return self.get_by_username(username, connection) if result.rowcount == 1 else None
