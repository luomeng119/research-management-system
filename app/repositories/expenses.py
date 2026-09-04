from __future__ import annotations

import sqlalchemy as sa


class ExpensesRepository:
    """SQLAlchemy-only persistence for the expense helper.

    Every operation receives the caller's connection.  Transactions belong to
    the service layer.
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        metadata = sa.MetaData()
        self.reimbursements = sa.Table("expense_reimbursement", metadata, autoload_with=engine)
        self.invoices = sa.Table("expense_invoice", metadata, autoload_with=engine)
        self.invoice_items = sa.Table("expense_invoice_item", metadata, autoload_with=engine)
        self.payments = sa.Table("expense_payment", metadata, autoload_with=engine)
        self.files = sa.Table("stored_files", metadata, autoload_with=engine)
        self.file_links = sa.Table("object_files", metadata, autoload_with=engine)

    @staticmethod
    def rows(connection, statement):
        return [dict(row) for row in connection.execute(statement).mappings()]

    @staticmethod
    def one(connection, statement):
        row = connection.execute(statement).mappings().first()
        return dict(row) if row else None

    @staticmethod
    def advisory_lock(connection, key: str) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(key, 0))))

    @staticmethod
    def next_id(connection, table_name: str) -> int:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("PostgreSQL runtime is required")
        return int(connection.scalar(sa.text("SELECT nextval(pg_get_serial_sequence(:table_name, 'id'))").bindparams(table_name=table_name)))

    def get_reimbursement(self, connection, rid: int, *, lock=False):
        statement = self.reimbursement_statement(rid, lock=lock)
        return self.one(connection, statement)

    def reimbursement_statement(self, rid: int, *, lock=False):
        statement = sa.select(self.reimbursements).where(self.reimbursements.c.id == rid)
        if lock:
            statement = statement.with_for_update(of=self.reimbursements)
        return statement

    def list_reimbursements(self, connection, *, status=None, keyword=None, limit=200, offset=0):
        inv_count = sa.select(sa.func.count()).where(self.invoices.c.reimbursement_id == self.reimbursements.c.id).scalar_subquery()
        pay_count = sa.select(sa.func.count()).where(self.payments.c.reimbursement_id == self.reimbursements.c.id).scalar_subquery()
        inv_date = sa.select(sa.func.max(self.invoices.c.date)).where(self.invoices.c.reimbursement_id == self.reimbursements.c.id).scalar_subquery()
        pay_date = sa.select(sa.func.max(self.payments.c.pay_date)).where(self.payments.c.reimbursement_id == self.reimbursements.c.id).scalar_subquery()
        statement = sa.select(
            self.reimbursements,
            inv_count.label("invoice_count"), pay_count.label("payment_count"),
            sa.func.greatest(
                sa.func.coalesce(inv_date, pay_date), sa.func.coalesce(pay_date, inv_date)
            ).label("payment_time"),
        )
        if status:
            statement = statement.where(self.reimbursements.c.status == status)
        if keyword:
            pattern = f"%{keyword}%"
            statement = statement.where(sa.or_(self.reimbursements.c.reimbursement_no.ilike(pattern), self.reimbursements.c.title.ilike(pattern)))
        return self.rows(connection, statement.order_by(self.reimbursements.c.created_at.desc(), self.reimbursements.c.id.desc()).limit(limit).offset(offset))

    def count_reimbursements(self, connection, *, status=None, keyword=None):
        statement = sa.select(sa.func.count()).select_from(self.reimbursements)
        if status:
            statement = statement.where(self.reimbursements.c.status == status)
        if keyword:
            pattern = f"%{keyword}%"
            statement = statement.where(sa.or_(self.reimbursements.c.reimbursement_no.ilike(pattern), self.reimbursements.c.title.ilike(pattern)))
        return int(connection.scalar(statement) or 0)

    def latest_number(self, connection, prefix: str, table, column) -> str | None:
        return connection.scalar(sa.select(column).where(column.like(f"{prefix}%")).order_by(column.desc()).limit(1))

    def insert_reimbursement(self, connection, values: dict) -> int:
        return int(connection.scalar(self.reimbursements.insert().values(**values).returning(self.reimbursements.c.id)))

    def update_reimbursement(self, connection, rid: int, values: dict) -> int:
        return int(connection.execute(self.reimbursements.update().where(self.reimbursements.c.id == rid).values(**values)).rowcount)

    def delete_reimbursement(self, connection, rid: int) -> int:
        return int(connection.execute(self.reimbursements.delete().where(self.reimbursements.c.id == rid)).rowcount)

    def get_invoice(self, connection, iid: int, *, lock=False):
        statement = sa.select(self.invoices).where(self.invoices.c.id == iid)
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.invoices)
        row = self.one(connection, statement)
        if row:
            row["items"] = self.rows(connection, sa.select(self.invoice_items).where(self.invoice_items.c.invoice_id == iid).order_by(self.invoice_items.c.seq, self.invoice_items.c.id))
        return row

    def list_invoices(self, connection, *, reimbursement_id=None, status=None, limit=500, offset=0, lock=False):
        statement = sa.select(self.invoices)
        if reimbursement_id is not None:
            statement = statement.where(self.invoices.c.reimbursement_id == reimbursement_id)
        if status:
            statement = statement.where(self.invoices.c.status == status)
        statement = statement.order_by(self.invoices.c.id) if lock else statement.order_by(
            self.invoices.c.created_at.desc(), self.invoices.c.id.desc()
        )
        statement = statement.limit(limit).offset(offset)
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.invoices)
        rows = self.rows(connection, statement)
        if rows:
            ids = [row["id"] for row in rows]
            items = self.rows(connection, sa.select(self.invoice_items).where(self.invoice_items.c.invoice_id.in_(ids)).order_by(self.invoice_items.c.invoice_id, self.invoice_items.c.seq, self.invoice_items.c.id))
            by_invoice = {iid: [] for iid in ids}
            for item in items:
                by_invoice[item["invoice_id"]].append(item)
            for row in rows:
                row["items"] = by_invoice[row["id"]]
        return rows

    def count_invoices(self, connection, *, reimbursement_id=None, status=None):
        statement = sa.select(sa.func.count()).select_from(self.invoices)
        if reimbursement_id is not None:
            statement = statement.where(self.invoices.c.reimbursement_id == reimbursement_id)
        if status:
            statement = statement.where(self.invoices.c.status == status)
        return int(connection.scalar(statement) or 0)

    def find_duplicate_invoice(self, connection, amount, date, invoice_no):
        return self.one(connection, sa.select(self.invoices.c.id, self.invoices.c.invoice_no).where(
            self.invoices.c.amount == amount, self.invoices.c.date == date, self.invoices.c.invoice_no == invoice_no,
        ).limit(1))

    def insert_invoice_statement(self, values: dict):
        return self.invoices.insert().values(**values)

    def insert_invoice(self, connection, values: dict) -> int:
        return int(connection.scalar(self.invoices.insert().values(**values).returning(self.invoices.c.id)))

    def insert_invoice_items(self, connection, values: list[dict]) -> None:
        if values:
            connection.execute(self.invoice_items.insert(), values)

    def update_invoice(self, connection, iid: int, values: dict) -> int:
        return int(connection.execute(self.invoices.update().where(self.invoices.c.id == iid).values(**values)).rowcount)

    def delete_invoice(self, connection, iid: int) -> int:
        connection.execute(self.invoice_items.delete().where(self.invoice_items.c.invoice_id == iid))
        return int(connection.execute(self.invoices.delete().where(self.invoices.c.id == iid)).rowcount)

    def get_payment(self, connection, pid: int, *, lock=False):
        statement = sa.select(self.payments).where(self.payments.c.id == pid)
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.payments)
        return self.one(connection, statement)

    def list_payments(self, connection, *, reimbursement_id=None, status=None, limit=500, offset=0, lock=False):
        statement = sa.select(self.payments)
        if reimbursement_id is not None:
            statement = statement.where(self.payments.c.reimbursement_id == reimbursement_id)
        if status:
            statement = statement.where(self.payments.c.status == status)
        statement = statement.order_by(self.payments.c.id) if lock else statement.order_by(
            self.payments.c.created_at.desc(), self.payments.c.id.desc()
        )
        statement = statement.limit(limit).offset(offset)
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.payments)
        return self.rows(connection, statement)

    def count_payments(self, connection, *, reimbursement_id=None, status=None):
        statement = sa.select(sa.func.count()).select_from(self.payments)
        if reimbursement_id is not None:
            statement = statement.where(self.payments.c.reimbursement_id == reimbursement_id)
        if status:
            statement = statement.where(self.payments.c.status == status)
        return int(connection.scalar(statement) or 0)

    def lock_and_aggregate_children(self, connection, rid: int):
        for table in (self.invoices, self.payments):
            statement = sa.select(table.c.id).where(table.c.reimbursement_id == rid).order_by(table.c.id)
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(of=table)
            list(connection.scalars(statement))
        invoice = connection.execute(sa.select(
            sa.func.count().label("count"),
            sa.func.coalesce(sa.func.sum(self.invoices.c.amount), 0).label("total"),
        ).where(self.invoices.c.reimbursement_id == rid)).mappings().one()
        payment = connection.execute(sa.select(
            sa.func.count().label("count"),
            sa.func.coalesce(sa.func.sum(self.payments.c.amount), 0).label("total"),
        ).where(self.payments.c.reimbursement_id == rid)).mappings().one()
        return dict(invoice), dict(payment)

    def find_duplicate_payment(self, connection, amount, pay_date):
        return self.one(connection, sa.select(self.payments.c.id, self.payments.c.payment_no).where(
            self.payments.c.amount == amount, self.payments.c.pay_date == pay_date,
        ).limit(1))

    def insert_payment_statement(self, values: dict):
        return self.payments.insert().values(**values)

    def insert_payment(self, connection, values: dict) -> int:
        return int(connection.scalar(self.payments.insert().values(**values).returning(self.payments.c.id)))

    def update_payment(self, connection, pid: int, values: dict) -> int:
        return int(connection.execute(self.payments.update().where(self.payments.c.id == pid).values(**values)).rowcount)

    def delete_payment(self, connection, pid: int) -> int:
        return int(connection.execute(self.payments.delete().where(self.payments.c.id == pid)).rowcount)

    def list_files_for_objects(self, connection, *, object_type: str, object_ids):
        object_ids = [str(value) for value in object_ids]
        if not object_ids:
            return []
        return self.rows(connection, sa.select(
            self.file_links.c.object_id,
            self.files.c.id.label("file_id"),
            self.files.c.original_name,
            self.files.c.media_type,
            self.files.c.version,
            self.files.c.status,
        ).select_from(
            self.file_links.join(self.files, self.files.c.id == self.file_links.c.file_id)
        ).where(
            self.file_links.c.object_type == object_type,
            self.file_links.c.object_id.in_(object_ids),
            self.files.c.status == "ACTIVE",
        ).order_by(self.file_links.c.object_id, self.files.c.business_id))

    def archive_and_unlink_files(self, connection, *, object_type: str, object_id: int):
        statement = sa.select(self.files.c.id).select_from(
            self.file_links.join(self.files, self.files.c.id == self.file_links.c.file_id)
        ).where(
            self.file_links.c.object_type == object_type,
            self.file_links.c.object_id == str(object_id),
        ).order_by(self.files.c.id)
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update(of=self.files)
        file_ids = list(connection.scalars(statement))
        if file_ids:
            values = {"status": "ARCHIVED", "version": self.files.c.version + 1}
            if "updated_at" in self.files.c:
                values["updated_at"] = sa.func.now()
            connection.execute(self.files.update().where(self.files.c.id.in_(file_ids)).values(**values))
            connection.execute(self.file_links.delete().where(
                self.file_links.c.object_type == object_type,
                self.file_links.c.object_id == str(object_id),
            ))
        return len(file_ids)

    def recalculate_total(self, connection, rid: int):
        total = connection.scalar(sa.select(sa.func.coalesce(sa.func.sum(self.invoices.c.amount), 0)).where(self.invoices.c.reimbursement_id == rid))
        self.update_reimbursement(connection, rid, {"total_amount": total})
        return total

    def recalculate_total_statement(self, rid: int):
        total = (
            sa.select(sa.func.coalesce(sa.func.sum(self.invoices.c.amount), 0))
            .where(self.invoices.c.reimbursement_id == rid)
            .scalar_subquery()
        )
        return (
            self.reimbursements.update()
            .where(self.reimbursements.c.id == rid)
            .values(total_amount=total)
        )

    def stats(self, connection):
        draft = connection.scalar(sa.select(sa.func.count(sa.distinct(self.reimbursements.c.id))).select_from(
            self.reimbursements.join(self.invoices, self.invoices.c.reimbursement_id == self.reimbursements.c.id)
        ).where(self.reimbursements.c.status == "草稿"))
        unmatched_i = connection.scalar(sa.select(sa.func.count()).select_from(self.invoices).where(self.invoices.c.status == "未匹配"))
        unmatched_p = connection.scalar(sa.select(sa.func.count()).select_from(self.payments).where(self.payments.c.status == "未匹配"))
        missing_payment = connection.scalar(sa.select(sa.func.count()).select_from(self.reimbursements).where(
            self.reimbursements.c.status.not_in(("已作废", "已完成")),
            sa.exists(sa.select(1).where(self.invoices.c.reimbursement_id == self.reimbursements.c.id)),
            ~sa.exists(sa.select(1).where(self.payments.c.reimbursement_id == self.reimbursements.c.id)),
        ))
        missing_invoice = connection.scalar(sa.select(sa.func.count()).select_from(self.reimbursements).where(
            self.reimbursements.c.status.not_in(("已作废", "已完成")),
            ~sa.exists(sa.select(1).where(self.invoices.c.reimbursement_id == self.reimbursements.c.id)),
            sa.exists(sa.select(1).where(self.payments.c.reimbursement_id == self.reimbursements.c.id)),
        ))
        recent = self.rows(connection, sa.select(self.reimbursements).order_by(self.reimbursements.c.created_at.desc(), self.reimbursements.c.id.desc()).limit(5))
        return {"draft_count": int(draft or 0), "unmatched_invoice_count": int(unmatched_i or 0), "unmatched_payment_count": int(unmatched_p or 0), "missing_payment_count": int(missing_payment or 0), "missing_invoice_count": int(missing_invoice or 0)}, recent
