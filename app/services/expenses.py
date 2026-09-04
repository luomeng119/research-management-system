from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
import json
import math
import uuid


class ExpenseError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ExpenseValidationError(ExpenseError):
    pass


class ExpenseService:
    REIMBURSEMENT_FIELDS = frozenset({"title", "remark", "approver"})
    INVOICE_FIELDS = frozenset({
        "invoice_no", "date", "amount", "tax_amount", "price_ex_tax", "buyer", "seller",
        "content", "spec", "invoice_type", "tax_rate", "confidence", "train_no",
        "departure_station", "arrival_station", "departure_date", "seat_type", "passenger_name",
        "id_card_no", "flight_no", "departure_airport", "arrival_airport", "departure_time",
        "departure_city", "arrival_city",
    })
    PAYMENT_FIELDS = frozenset({"payment_no", "amount", "pay_date", "payer"})
    TYPES = frozenset({"采购报销", "出差报销"})
    MAX_PAGE_SIZE = 500
    CHILD_BATCH_SIZE = 200
    MAX_REIMBURSEMENT_CHILDREN = 2000
    MAX_DOCUMENTS = 100
    MAX_DOCUMENT_BYTES = 256_000
    MAX_MATCH_CANDIDATES = 20
    MAX_MATCH_COMBINATIONS = 50_000

    def __init__(self, repository, audit_service=None, file_service=None) -> None:
        self.repository = repository
        self.audit_service = audit_service
        self.file_service = file_service

    @staticmethod
    def amount(value, *, field="金额", scale=2) -> Decimal:
        if isinstance(value, (float, bool)):
            raise ExpenseValidationError("INVALID_AMOUNT", f"{field}格式无效")
        try:
            amount = Decimal(str(value if value not in (None, "") else "0"))
        except (InvalidOperation, ValueError):
            raise ExpenseValidationError("INVALID_AMOUNT", f"{field}格式无效")
        if not amount.is_finite():
            raise ExpenseValidationError("INVALID_AMOUNT", f"{field}格式无效")
        quantum = Decimal(1).scaleb(-scale)
        if amount != amount.quantize(quantum):
            raise ExpenseValidationError("INVALID_AMOUNT", f"{field}最多 {scale} 位小数")
        return amount

    @staticmethod
    def _date(value, *, field="日期"):
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            raise ExpenseValidationError("INVALID_DATE", f"{field}格式无效")
        if str(parsed) != str(value):
            raise ExpenseValidationError("INVALID_DATE", f"{field}格式无效")
        return parsed

    @staticmethod
    def _text(value, *, field="字段", maximum=4000):
        value = str(value or "").strip()
        if len(value) > maximum or "\x00" in value:
            raise ExpenseValidationError("INVALID_TEXT", f"{field}内容无效")
        return value

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @classmethod
    def _legacy(cls, value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

    @classmethod
    def public_row(cls, row):
        if not row:
            return None
        result = {key: cls._legacy(value) for key, value in row.items()}
        if "file_path" in result:
            result["file_path"] = None
        if "ocr_text" in result:
            result["ocr_text"] = ""
        card = str(result.get("id_card_no") or "")
        if len(card) > 7:
            result["id_card_no"] = f"{card[:3]}****{card[-4:]}"
        return result

    def _with_files(self, connection, rows, object_type):
        public = [self.public_row(row) for row in rows]
        grouped = {str(row["id"]): [] for row in public}
        if self.file_service is not None:
            for item in self.repository.list_files_for_objects(
                connection, object_type=object_type, object_ids=grouped
            ):
                grouped.setdefault(str(item["object_id"]), []).append({
                    "fileId": str(item["file_id"]),
                    "originalName": item["original_name"],
                    "mediaType": item["media_type"],
                    "versionNo": int(item["version"]),
                    "status": item["status"],
                })
        for row in public:
            row["files"] = grouped.get(str(row["id"]), [])
        return public

    def _audit(self, connection, *, operation, object_type, object_id, actor_user_id=None, request_id="expense-internal", object_count=1):
        if not self.audit_service:
            return
        if actor_user_id is None:
            try:
                from flask import has_request_context, session
                actor_user_id = int(session.get("user_id") or 1) if has_request_context() else 1
            except (RuntimeError, TypeError, ValueError):
                actor_user_id = 1
        self.audit_service.record(
            connection, event_name="expense_operation", user_id=actor_user_id,
            object_type=object_type, object_id=str(object_id), result="SUCCESS",
            request_id=str(request_id), duration_ms=0,
            properties={"operation": operation, "object_count": int(object_count)},
        )

    @staticmethod
    def _require_draft(row):
        if row.get("status") != "草稿":
            raise ExpenseError("INVALID_STATUS", "只有草稿报销项可以修改整理关系", 409)

    @staticmethod
    def _next_number(repository, connection, *, prefix, table, column):
        repository.advisory_lock(connection, f"expense-number:{prefix}")
        latest = repository.latest_number(connection, prefix, table, column)
        try:
            sequence = int(str(latest)[len(prefix):]) + 1 if latest else 1
        except ValueError:
            sequence = 1
        return f"{prefix}{sequence:03d}"

    def _create_reimbursement(self, connection, *, title="", approver="", remark="", reimbursement_type="采购报销"):
        if reimbursement_type not in self.TYPES:
            raise ExpenseValidationError("INVALID_TYPE", "报销类型无效")
        now = self._now()
        prefix = f"REI{now.strftime('%Y%m%d')}"
        number = self._next_number(self.repository, connection, prefix=prefix, table=self.repository.reimbursements, column=self.repository.reimbursements.c.reimbursement_no)
        rid = self.repository.insert_reimbursement(connection, {
            "reimbursement_no": number, "title": self._text(title, field="标题", maximum=200),
            "total_amount": Decimal("0.00"), "status": "草稿",
            "remark": self._text(remark, field="备注"), "approver": self._text(approver, field="经办人", maximum=100),
            "created_at": now, "updated_at": now, "reimbursement_type": reimbursement_type,
            "is_paid": False, "documents": [],
        })
        return rid, number

    def create_reimbursement(self, **fields):
        with self.repository.engine.begin() as connection:
            result = self._create_reimbursement(connection, **fields)
            self._audit(connection, operation="CREATE", object_type="EXPENSE", object_id=result[0])
            return result

    def list_reimbursements(self, status=None, keyword=None, *, limit=200, offset=0):
        limit = int(limit)
        if limit < 1 or limit > self.MAX_PAGE_SIZE or int(offset) < 0:
            raise ExpenseValidationError("INVALID_PAGE", "分页参数无效")
        with self.repository.engine.connect() as connection:
            return [self.public_row(row) for row in self.repository.list_reimbursements(connection, status=status, keyword=self._text(keyword, maximum=100) if keyword else None, limit=limit, offset=int(offset))]

    def page_reimbursements(self, status=None, keyword=None, *, limit=200, offset=0):
        rows = self.list_reimbursements(status, keyword, limit=limit, offset=offset)
        clean_keyword = self._text(keyword, maximum=100) if keyword else None
        with self.repository.engine.connect() as connection:
            total = self.repository.count_reimbursements(connection, status=status, keyword=clean_keyword)
        return rows, total, int(offset) + len(rows) if int(offset) + len(rows) < total else None

    def get_reimbursement(self, rid):
        with self.repository.engine.connect() as connection:
            return self.public_row(self.repository.get_reimbursement(connection, int(rid)))

    def update_reimbursement(self, rid, **fields):
        forbidden = set(fields) - self.REIMBURSEMENT_FIELDS
        if forbidden:
            raise ExpenseValidationError("PROTECTED_FIELD", "状态、类型、金额和支付标记只能通过专用操作修改")
        values = {key: self._text(value, field=key, maximum=4000 if key == "remark" else 200) for key, value in fields.items()}
        values["updated_at"] = self._now()
        with self.repository.engine.begin() as connection:
            if not self.repository.get_reimbursement(connection, int(rid), lock=True):
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            self.repository.update_reimbursement(connection, int(rid), values)
            self._audit(connection, operation="UPDATE", object_type="EXPENSE", object_id=rid)

    def toggle_type(self, rid):
        with self.repository.engine.begin() as connection:
            row = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not row:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            if row["status"] != "草稿":
                raise ExpenseError("INVALID_STATUS", "已确认的报销项不可切换类型", 409)
            value = "出差报销" if row.get("reimbursement_type") == "采购报销" else "采购报销"
            self.repository.update_reimbursement(connection, int(rid), {"reimbursement_type": value, "updated_at": self._now()})
            self._audit(connection, operation="TOGGLE_TYPE", object_type="EXPENSE", object_id=rid)
            return value

    def toggle_paid(self, rid):
        with self.repository.engine.begin() as connection:
            row = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not row:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            value = not bool(row.get("is_paid"))
            self.repository.update_reimbursement(connection, int(rid), {"is_paid": value, "updated_at": self._now()})
            self._audit(connection, operation="TOGGLE_PAID", object_type="EXPENSE", object_id=rid)
            return int(value)

    def _invoice_values(self, fields, *, include_defaults=True):
        unknown = set(fields) - self.INVOICE_FIELDS - {"items", "ocr_text", "file_path", "reimbursement_id"}
        if unknown:
            raise ExpenseValidationError("INVALID_FIELD", "发票字段无效")
        values = {key: self._text(fields.get(key), field=key) for key in self.INVOICE_FIELDS if key in fields and key not in {"amount", "tax_amount", "price_ex_tax", "date", "departure_date", "departure_time"}}
        for key in ("amount", "tax_amount", "price_ex_tax"):
            if include_defaults or key in fields:
                labels = {"amount": "金额", "tax_amount": "税额", "price_ex_tax": "不含税金额"}
                values[key] = self.amount(fields.get(key, "0"), field=labels[key])
        for key in ("date", "departure_date"):
            if include_defaults or key in fields:
                values[key] = self._date(fields.get(key), field=key)
        if "departure_time" in fields:
            raw = fields.get("departure_time")
            try:
                values["departure_time"] = datetime.fromisoformat(str(raw)) if raw else None
            except ValueError:
                raise ExpenseValidationError("INVALID_DATE", "departure_time 格式无效")
        return values

    def _invoice_items(self, invoice_id, items):
        if not isinstance(items, list) or len(items) > 200:
            raise ExpenseValidationError("INVALID_ITEMS", "发票明细无效")
        result = []
        for seq, item in enumerate(items):
            if not isinstance(item, dict):
                raise ExpenseValidationError("INVALID_ITEMS", "发票明细无效")
            result.append({
                "invoice_id": invoice_id, "seq": seq,
                "name": self._text(item.get("name"), maximum=500), "spec": self._text(item.get("spec"), maximum=500),
                "unit": self._text(item.get("unit"), maximum=100),
                "quantity": self.amount(item.get("quantity", "0"), field="数量", scale=4),
                "unit_price": self.amount(item.get("unit_price", "0"), field="单价", scale=4),
                "amount": self.amount(item.get("amount", "0"), field="明细金额"),
                "tax_rate": self._text(item.get("tax_rate"), maximum=50),
                "tax_amount": self.amount(item.get("tax_amount", "0"), field="明细税额"),
            })
        return result

    def create_invoice(self, reimbursement_id=None, **fields):
        values = self._invoice_values(fields)
        values.update({"reimbursement_id": int(reimbursement_id) if reimbursement_id is not None else None, "ocr_text": self._text(fields.get("ocr_text"), field="OCR", maximum=100_000), "file_path": None, "status": "已匹配" if reimbursement_id is not None else "未匹配", "matched_payment_ids": [], "created_at": self._now()})
        with self.repository.engine.begin() as connection:
            if reimbursement_id is not None:
                reimbursement = self.repository.get_reimbursement(connection, int(reimbursement_id), lock=True)
                if not reimbursement:
                    raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
                self._require_draft(reimbursement)
            iid = self.repository.insert_invoice(connection, values)
            self.repository.insert_invoice_items(connection, self._invoice_items(iid, fields.get("items", [])))
            if reimbursement_id is not None:
                self.repository.recalculate_total(connection, int(reimbursement_id))
            self._audit(connection, operation="CREATE", object_type="INVOICE", object_id=iid)
            return iid

    def create_invoice_with_upload(self, stream, original_name, *, actor_user_id, request_id, reimbursement_id=None, **fields):
        if self.file_service is None:
            raise ExpenseError("FILE_SERVICE_UNAVAILABLE", "附件服务不可用，请先手工录入", 503)
        values = self._invoice_values(fields)
        values.update({"reimbursement_id": int(reimbursement_id) if reimbursement_id is not None else None, "ocr_text": self._text(fields.get("ocr_text"), field="OCR", maximum=100_000), "file_path": None, "status": "已匹配" if reimbursement_id is not None else "未匹配", "matched_payment_ids": [], "created_at": self._now()})
        with self.repository.engine.connect() as connection:
            iid = self.repository.next_id(connection, "expense_invoice")
        values["id"] = iid
        items = self._invoice_items(iid, fields.get("items", []))

        def create_metadata(writer):
            writer.execute(self.repository.insert_invoice_statement(values))
            if items:
                writer.execute(self.repository.invoice_items.insert(), items)

        result = self.file_service.upload_new_object(
            stream, original_name=original_name, object_type="INVOICE", object_id=str(iid),
            create_metadata=create_metadata, actor_user_id=int(actor_user_id), request_id=str(request_id),
        )
        return iid, result

    def list_invoices(self, reimbursement_id=None, status=None, *, limit=500, offset=0):
        if int(limit) < 1 or int(limit) > self.MAX_PAGE_SIZE:
            raise ExpenseValidationError("INVALID_PAGE", "分页参数无效")
        with self.repository.engine.connect() as connection:
            rows = self.repository.list_invoices(connection, reimbursement_id=int(reimbursement_id) if reimbursement_id is not None else None, status=status, limit=int(limit), offset=int(offset))
            return self._with_files(connection, rows, "INVOICE")

    def page_invoices(self, reimbursement_id=None, status=None, *, limit=100, offset=0):
        rows = self.list_invoices(reimbursement_id, status, limit=limit, offset=offset)
        with self.repository.engine.connect() as connection:
            total = self.repository.count_invoices(connection, reimbursement_id=int(reimbursement_id) if reimbursement_id is not None else None, status=status)
        return rows, total, int(offset) + len(rows) if int(offset) + len(rows) < total else None

    def get_invoice(self, iid):
        with self.repository.engine.connect() as connection:
            row = self.repository.get_invoice(connection, int(iid))
            return self._with_files(connection, [row], "INVOICE")[0] if row else None

    def _lock_invoice_parent_first(self, connection, iid):
        probe = self.repository.get_invoice(connection, int(iid))
        if not probe:
            raise ExpenseError("NOT_FOUND", "发票不存在", 404)
        expected_rid = probe.get("reimbursement_id")
        reimbursement = None
        if expected_rid is not None:
            reimbursement = self.repository.get_reimbursement(connection, int(expected_rid), lock=True)
        current = self.repository.get_invoice(connection, int(iid), lock=True)
        if not current or current.get("reimbursement_id") != expected_rid:
            raise ExpenseError("CONCURRENT_CHANGE", "发票状态已变更，请重试", 409)
        return current, reimbursement

    def update_invoice(self, iid, **fields):
        if set(fields) & {"status", "reimbursement_id", "matched_payment_ids", "file_path", "ocr_text"}:
            raise ExpenseValidationError("PROTECTED_FIELD", "匹配状态和附件只能通过专用操作修改")
        values = self._invoice_values(fields, include_defaults=False)
        with self.repository.engine.begin() as connection:
            current, reimbursement = self._lock_invoice_parent_first(connection, iid)
            if reimbursement is not None:
                self._require_draft(reimbursement)
            self.repository.update_invoice(connection, int(iid), values)
            if current.get("reimbursement_id") and "amount" in values:
                self.repository.recalculate_total(connection, current["reimbursement_id"])
            self._audit(connection, operation="UPDATE", object_type="INVOICE", object_id=iid)

    def create_payment(self, reimbursement_id=None, **fields):
        unknown = set(fields) - self.PAYMENT_FIELDS - {"ocr_text", "file_path"}
        if unknown:
            raise ExpenseValidationError("INVALID_FIELD", "支付字段无效")
        now = self._now()
        with self.repository.engine.begin() as connection:
            if reimbursement_id is not None:
                reimbursement = self.repository.get_reimbursement(connection, int(reimbursement_id), lock=True)
                if not reimbursement:
                    raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
                self._require_draft(reimbursement)
            prefix = f"PAY{now.strftime('%Y%m%d')}"
            number = self._text(fields.get("payment_no"), maximum=100) or self._next_number(self.repository, connection, prefix=prefix, table=self.repository.payments, column=self.repository.payments.c.payment_no)
            pid = self.repository.insert_payment(connection, {
                "reimbursement_id": int(reimbursement_id) if reimbursement_id is not None else None,
                "payment_no": number, "amount": self.amount(fields.get("amount", "0")),
                "pay_date": self._date(fields.get("pay_date"), field="支付日期"), "payer": self._text(fields.get("payer"), maximum=200),
                "ocr_text": self._text(fields.get("ocr_text"), maximum=100_000), "file_path": None,
                "status": "已匹配" if reimbursement_id is not None else "未匹配", "matched_invoice_ids": [], "created_at": now,
            })
            self._audit(connection, operation="CREATE", object_type="PAYMENT", object_id=pid)
            return pid

    def create_payment_with_upload(self, stream, original_name, *, actor_user_id, request_id, reimbursement_id=None, **fields):
        if self.file_service is None:
            raise ExpenseError("FILE_SERVICE_UNAVAILABLE", "附件服务不可用，请先手工录入", 503)
        now = self._now()
        with self.repository.engine.connect() as connection:
            pid = self.repository.next_id(connection, "expense_payment")
        number = self._text(fields.get("payment_no"), maximum=100) or f"PAY{now.strftime('%Y%m%d')}{pid:06d}"
        values = {
            "id": pid, "reimbursement_id": int(reimbursement_id) if reimbursement_id is not None else None,
            "payment_no": number, "amount": self.amount(fields.get("amount", "0")),
            "pay_date": self._date(fields.get("pay_date"), field="支付日期"), "payer": self._text(fields.get("payer"), maximum=200),
            "ocr_text": self._text(fields.get("ocr_text"), maximum=100_000), "file_path": None,
            "status": "已匹配" if reimbursement_id is not None else "未匹配", "matched_invoice_ids": [], "created_at": now,
        }

        def create_metadata(writer):
            writer.execute(self.repository.insert_payment_statement(values))

        result = self.file_service.upload_new_object(
            stream, original_name=original_name, object_type="PAYMENT", object_id=str(pid),
            create_metadata=create_metadata, actor_user_id=int(actor_user_id), request_id=str(request_id),
        )
        return pid, result

    def list_payments(self, reimbursement_id=None, status=None, *, limit=500, offset=0):
        if int(limit) < 1 or int(limit) > self.MAX_PAGE_SIZE:
            raise ExpenseValidationError("INVALID_PAGE", "分页参数无效")
        with self.repository.engine.connect() as connection:
            rows = self.repository.list_payments(connection, reimbursement_id=int(reimbursement_id) if reimbursement_id is not None else None, status=status, limit=int(limit), offset=int(offset))
            return self._with_files(connection, rows, "PAYMENT")

    def page_payments(self, reimbursement_id=None, status=None, *, limit=100, offset=0):
        rows = self.list_payments(reimbursement_id, status, limit=limit, offset=offset)
        with self.repository.engine.connect() as connection:
            total = self.repository.count_payments(connection, reimbursement_id=int(reimbursement_id) if reimbursement_id is not None else None, status=status)
        return rows, total, int(offset) + len(rows) if int(offset) + len(rows) < total else None

    def collect_reimbursement_children(self, reimbursement_id):
        """Collect a bounded complete child set using fixed-size database pages."""
        rid = int(reimbursement_id)
        with self.repository.engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            invoice_total = self.repository.count_invoices(connection, reimbursement_id=rid)
            payment_total = self.repository.count_payments(connection, reimbursement_id=rid)
            if invoice_total + payment_total > self.MAX_REIMBURSEMENT_CHILDREN:
                raise ExpenseError(
                    "REIMBURSEMENT_RECORD_LIMIT",
                    f"报销项发票和支付记录合计超过 V1 上限 {self.MAX_REIMBURSEMENT_CHILDREN} 条，请拆分报销项",
                    409,
                )

            def collect(repository_method, object_type, total):
                rows = []
                for offset in range(0, total, self.CHILD_BATCH_SIZE):
                    page = repository_method(
                        connection, reimbursement_id=rid,
                        limit=min(self.CHILD_BATCH_SIZE, total - offset), offset=offset,
                    )
                    if not page:
                        raise ExpenseError("CONCURRENT_CHANGE", "报销明细已变更，请重试", 409)
                    rows.extend(self._with_files(connection, page, object_type))
                if len(rows) != total:
                    raise ExpenseError("CONCURRENT_CHANGE", "报销明细已变更，请重试", 409)
                return rows

            return (
                collect(self.repository.list_invoices, "INVOICE", invoice_total),
                collect(self.repository.list_payments, "PAYMENT", payment_total),
            )

    def get_payment(self, pid):
        with self.repository.engine.connect() as connection:
            row = self.repository.get_payment(connection, int(pid))
            return self._with_files(connection, [row], "PAYMENT")[0] if row else None

    def _lock_payment_parent_first(self, connection, pid):
        probe = self.repository.get_payment(connection, int(pid))
        if not probe:
            raise ExpenseError("NOT_FOUND", "支付记录不存在", 404)
        expected_rid = probe.get("reimbursement_id")
        reimbursement = None
        if expected_rid is not None:
            reimbursement = self.repository.get_reimbursement(connection, int(expected_rid), lock=True)
        current = self.repository.get_payment(connection, int(pid), lock=True)
        if not current or current.get("reimbursement_id") != expected_rid:
            raise ExpenseError("CONCURRENT_CHANGE", "支付记录状态已变更，请重试", 409)
        return current, reimbursement

    def update_payment(self, pid, **fields):
        if set(fields) & {"status", "reimbursement_id", "matched_invoice_ids", "file_path", "ocr_text"}:
            raise ExpenseValidationError("PROTECTED_FIELD", "匹配状态和附件只能通过专用操作修改")
        unknown = set(fields) - self.PAYMENT_FIELDS
        if unknown:
            raise ExpenseValidationError("INVALID_FIELD", "支付字段无效")
        values = {}
        for key, value in fields.items():
            if key == "amount": values[key] = self.amount(value)
            elif key == "pay_date": values[key] = self._date(value, field="支付日期")
            else: values[key] = self._text(value, maximum=200)
        with self.repository.engine.begin() as connection:
            current, reimbursement = self._lock_payment_parent_first(connection, pid)
            if reimbursement is not None:
                self._require_draft(reimbursement)
            self.repository.update_payment(connection, int(pid), values)
            self._audit(connection, operation="UPDATE", object_type="PAYMENT", object_id=pid)

    def attach_invoice(self, rid, iid):
        with self.repository.engine.begin() as connection:
            reimbursement = self.repository.get_reimbursement(connection, int(rid), lock=True)
            invoice = self.repository.get_invoice(connection, int(iid), lock=True)
            if not reimbursement or not invoice:
                raise ExpenseError("NOT_FOUND", "报销项或发票不存在", 404)
            self._require_draft(reimbursement)
            owner = invoice.get("reimbursement_id")
            if owner not in (None, int(rid)):
                raise ExpenseError("CROSS_REIMBURSEMENT", "发票已属于其他报销项", 409)
            self.repository.update_invoice(connection, int(iid), {"reimbursement_id": int(rid), "status": "已匹配"})
            self.repository.recalculate_total(connection, int(rid))
            self._audit(connection, operation="ATTACH", object_type="INVOICE", object_id=iid)

    def detach_invoice(self, rid, iid):
        with self.repository.engine.begin() as connection:
            reimbursement = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not reimbursement:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            self._require_draft(reimbursement)
            invoice_probe = self.repository.get_invoice(connection, int(iid))
            invoice = self.repository.get_invoice(connection, int(iid), lock=True)
            if not invoice or invoice.get("reimbursement_id") != int(rid):
                raise ExpenseError("CROSS_REIMBURSEMENT", "发票不属于该报销项", 409)
            if invoice_probe and invoice_probe.get("matched_payment_ids") != invoice.get("matched_payment_ids"):
                raise ExpenseError("CONCURRENT_CHANGE", "匹配关系已变更，请重试", 409)
            for pid in sorted(int(value) for value in (invoice.get("matched_payment_ids") or [])):
                payment = self.repository.get_payment(connection, int(pid), lock=True)
                if not payment or payment.get("reimbursement_id") != int(rid):
                    raise ExpenseError("MATCH_INCONSISTENT", "匹配关系不一致，请重新整理", 409)
                remaining = [value for value in (payment.get("matched_invoice_ids") or []) if int(value) != int(iid)]
                values = {"matched_invoice_ids": remaining}
                if not remaining:
                    values.update({"reimbursement_id": None, "status": "未匹配"})
                self.repository.update_payment(connection, int(pid), values)
            self.repository.update_invoice(connection, int(iid), {"reimbursement_id": None, "status": "未匹配", "matched_payment_ids": []})
            self.repository.recalculate_total(connection, int(rid))
            self._audit(connection, operation="DETACH", object_type="INVOICE", object_id=iid)

    def attach_payment(self, rid, pid):
        with self.repository.engine.begin() as connection:
            reimbursement = self.repository.get_reimbursement(connection, int(rid), lock=True)
            payment = self.repository.get_payment(connection, int(pid), lock=True)
            if not reimbursement or not payment:
                raise ExpenseError("NOT_FOUND", "报销项或支付记录不存在", 404)
            self._require_draft(reimbursement)
            owner = payment.get("reimbursement_id")
            if owner not in (None, int(rid)):
                raise ExpenseError("CROSS_REIMBURSEMENT", "支付记录已属于其他报销项", 409)
            self.repository.update_payment(connection, int(pid), {"reimbursement_id": int(rid), "status": "已匹配"})
            self._audit(connection, operation="ATTACH", object_type="PAYMENT", object_id=pid)

    def detach_payment(self, rid, pid):
        with self.repository.engine.begin() as connection:
            reimbursement = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not reimbursement:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            self._require_draft(reimbursement)
            payment_probe = self.repository.get_payment(connection, int(pid))
            invoice_ids = sorted(int(value) for value in ((payment_probe or {}).get("matched_invoice_ids") or []))
            invoices = [self.repository.get_invoice(connection, iid, lock=True) for iid in invoice_ids]
            payment = self.repository.get_payment(connection, int(pid), lock=True)
            if not payment or payment.get("reimbursement_id") != int(rid):
                raise ExpenseError("CROSS_REIMBURSEMENT", "支付记录不属于该报销项", 409)
            if payment.get("matched_invoice_ids") != ((payment_probe or {}).get("matched_invoice_ids") or []):
                raise ExpenseError("CONCURRENT_CHANGE", "匹配关系已变更，请重试", 409)
            for iid, invoice in zip(invoice_ids, invoices):
                if not invoice or invoice.get("reimbursement_id") != int(rid):
                    raise ExpenseError("MATCH_INCONSISTENT", "匹配关系不一致，请重新整理", 409)
                remaining = [value for value in (invoice.get("matched_payment_ids") or []) if int(value) != int(pid)]
                values = {"matched_payment_ids": remaining}
                if not remaining:
                    values.update({"reimbursement_id": None, "status": "未匹配"})
                self.repository.update_invoice(connection, int(iid), values)
            self.repository.update_payment(connection, int(pid), {"reimbursement_id": None, "status": "未匹配", "matched_invoice_ids": []})
            self.repository.recalculate_total(connection, int(rid))
            self._audit(connection, operation="DETACH", object_type="PAYMENT", object_id=pid)

    def manual_match(self, invoice_ids, payment_ids, rid=None, *, title="", approver=""):
        invoice_ids = [int(value) for value in dict.fromkeys(invoice_ids or [])]
        payment_ids = [int(value) for value in dict.fromkeys(payment_ids or [])]
        if not invoice_ids or not payment_ids or len(invoice_ids) + len(payment_ids) > 100:
            raise ExpenseValidationError("INVALID_SELECTION", "请选择有效的发票和支付记录")
        with self.repository.engine.begin() as connection:
            if rid is None:
                rid, _ = self._create_reimbursement(connection, title=title, approver=approver)
            reimbursement = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not reimbursement:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            self._require_draft(reimbursement)
            invoices = [self.repository.get_invoice(connection, iid, lock=True) for iid in sorted(invoice_ids)]
            payments = [self.repository.get_payment(connection, pid, lock=True) for pid in sorted(payment_ids)]
            if any(row is None or row.get("reimbursement_id") not in (None, int(rid)) for row in invoices + payments):
                raise ExpenseError("CROSS_REIMBURSEMENT", "所选记录属于其他报销项", 409)
            if any(
                row.get("status") not in {"未匹配", "已匹配"}
                or bool(row.get("matched_payment_ids") or row.get("matched_invoice_ids"))
                for row in invoices + payments
            ):
                raise ExpenseError("INVALID_STATUS", "所选记录状态不可匹配", 409)
            if sum(row["amount"] for row in invoices) != sum(row["amount"] for row in payments):
                raise ExpenseValidationError("AMOUNT_MISMATCH", "发票与支付金额必须相等")
            for iid in invoice_ids:
                self.repository.update_invoice(connection, iid, {"reimbursement_id": int(rid), "status": "已匹配", "matched_payment_ids": payment_ids})
            for pid in payment_ids:
                self.repository.update_payment(connection, pid, {"reimbursement_id": int(rid), "status": "已匹配", "matched_invoice_ids": invoice_ids})
            self.repository.recalculate_total(connection, int(rid))
            self._audit(connection, operation="MANUAL_MATCH", object_type="EXPENSE", object_id=rid, object_count=len(invoice_ids) + len(payment_ids))
            return int(rid)

    @staticmethod
    def _subset(candidates, target, max_work):
        work = 0
        for size in range(1, min(len(candidates), 10) + 1):
            count = math.comb(len(candidates), size)
            if work + count > max_work:
                raise ExpenseError("MATCH_LIMIT", "候选记录较多，请使用手工匹配", 409)
            for combo in combinations(candidates, size):
                work += 1
                if sum(row["amount"] for row in combo) == target:
                    return [row["id"] for row in combo]
        return None

    def auto_match(self, *, approver=""):
        with self.repository.engine.begin() as connection:
            self.repository.advisory_lock(connection, "expense-auto-match")
            invoices = self.repository.list_invoices(connection, status="未匹配", limit=self.MAX_MATCH_CANDIDATES + 1)
            payments = self.repository.list_payments(connection, status="未匹配", limit=self.MAX_MATCH_CANDIDATES + 1)
            if len(invoices) > self.MAX_MATCH_CANDIDATES or len(payments) > self.MAX_MATCH_CANDIDATES:
                raise ExpenseError("MATCH_LIMIT", "候选记录较多，请使用手工匹配", 409)
            unused = {row["id"]: row for row in invoices if row.get("reimbursement_id") is None and row["amount"] > 0}
            matches = []
            for payment in sorted(payments, key=lambda row: row["amount"], reverse=True):
                if payment.get("reimbursement_id") is not None or payment["amount"] <= 0:
                    continue
                ids = self._subset(list(unused.values()), payment["amount"], self.MAX_MATCH_COMBINATIONS)
                if not ids:
                    continue
                travel = any(unused[iid].get("invoice_type") in {"火车票", "航空行程单", "出租车发票", "网约车"} for iid in ids)
                rid, _ = self._create_reimbursement(connection, title=f"{self._now().strftime('%Y年%m月')}报销", approver=approver, reimbursement_type="出差报销" if travel else "采购报销")
                locked_invoices = [self.repository.get_invoice(connection, iid, lock=True) for iid in sorted(ids)]
                locked_payment = self.repository.get_payment(connection, int(payment["id"]), lock=True)
                if any(not row or row.get("status") != "未匹配" or row.get("reimbursement_id") is not None for row in locked_invoices):
                    raise ExpenseError("CONCURRENT_CHANGE", "发票状态已变更，请重试", 409)
                if not locked_payment or locked_payment.get("status") != "未匹配" or locked_payment.get("reimbursement_id") is not None:
                    raise ExpenseError("CONCURRENT_CHANGE", "支付记录状态已变更，请重试", 409)
                if sum(row["amount"] for row in locked_invoices) != locked_payment["amount"]:
                    raise ExpenseError("CONCURRENT_CHANGE", "金额已变更，请重试", 409)
                for iid in ids:
                    self.repository.update_invoice(connection, iid, {"reimbursement_id": rid, "status": "已匹配", "matched_payment_ids": [payment["id"]]})
                    unused.pop(iid, None)
                self.repository.update_payment(connection, payment["id"], {"reimbursement_id": rid, "status": "已匹配", "matched_invoice_ids": ids})
                self.repository.recalculate_total(connection, rid)
                self._audit(connection, operation="AUTO_MATCH", object_type="EXPENSE", object_id=rid, object_count=1 + len(ids))
                matches.append({"reimbursement_id": rid, "payment": self.public_row(payment), "invoices": [self.public_row(row) for row in invoices if row["id"] in ids]})
            return {"new_matches": matches, "total_invoices_matched": sum(len(row["invoices"]) for row in matches), "total_payments_matched": len(matches)}

    def confirm(self, rid):
        with self.repository.engine.begin() as connection:
            reimbursement = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not reimbursement:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            self._require_draft(reimbursement)
            invoice_totals, payment_totals = self.repository.lock_and_aggregate_children(connection, int(rid))
            if not invoice_totals["count"]:
                raise ExpenseValidationError("NO_INVOICE", "报销项没有发票")
            if not payment_totals["count"]:
                raise ExpenseValidationError("NO_PAYMENT", "报销项没有支付记录")
            if invoice_totals["total"] != payment_totals["total"]:
                raise ExpenseValidationError("AMOUNT_MISMATCH", "发票与支付金额必须相等")
            self.repository.recalculate_total(connection, int(rid))
            self.repository.update_reimbursement(connection, int(rid), {"status": "已确认", "confirmed_at": self._now(), "updated_at": self._now()})
            self._audit(connection, operation="CONFIRM", object_type="EXPENSE", object_id=rid)

    def persist_generated_documents(self, rid, documents, *, actor_user_id, request_id):
        if self.file_service is None:
            raise ExpenseError("FILE_SERVICE_UNAVAILABLE", "附件服务不可用", 503)
        documents = list(documents or [])
        if len(documents) != 2:
            raise ExpenseValidationError("INCOMPLETE_DOCUMENT_SET", "必须完整生成结算单和审批单")
        items = []
        for original_name, source in documents:
            name = self._text(original_name, field="文件名", maximum=200)
            if not name.lower().endswith(".docx") or not hasattr(source, "read"):
                raise ExpenseValidationError("INVALID_DOCUMENT", "生成文档无效")
            source.seek(0)
            items.append((source, name))

        def finalize(connection):
            row = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not row or row["status"] not in {"已确认", "已生成文档"}:
                raise ExpenseError("INVALID_STATUS", "请先确认报销项", 409)
            self.repository.update_reimbursement(connection, int(rid), {"status": "已生成文档", "updated_at": self._now()})
            self._audit(
                connection, operation="GENERATE_DOCUMENTS", object_type="EXPENSE",
                object_id=rid, actor_user_id=actor_user_id, request_id=request_id,
                object_count=len(items),
            )

        return self.file_service.upload_expense_documents_atomic(
            items, object_id=str(rid), actor_user_id=int(actor_user_id),
            request_id=str(request_id), finalize_metadata=finalize,
        )

    def delete_reimbursement(self, rid):
        with self.repository.engine.begin() as connection:
            row = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not row:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            if row["status"] != "草稿":
                raise ExpenseError("INVALID_STATUS", "只有草稿状态的报销项可以删除", 409)
            invoices = self.repository.list_invoices(connection, reimbursement_id=int(rid), lock=True)
            payments = self.repository.list_payments(connection, reimbursement_id=int(rid), lock=True)
            for invoice in invoices:
                self.repository.update_invoice(connection, invoice["id"], {"reimbursement_id": None, "status": "未匹配", "matched_payment_ids": []})
            for payment in payments:
                self.repository.update_payment(connection, payment["id"], {"reimbursement_id": None, "status": "未匹配", "matched_invoice_ids": []})
            archived = self.repository.archive_and_unlink_files(
                connection, object_type="EXPENSE", object_id=int(rid)
            )
            self.repository.delete_reimbursement(connection, int(rid))
            self._audit(connection, operation="DELETE", object_type="EXPENSE", object_id=rid, object_count=1 + archived)

    def delete_invoice(self, iid):
        with self.repository.engine.begin() as connection:
            probe = self.repository.get_invoice(connection, int(iid))
            if not probe:
                raise ExpenseError("NOT_FOUND", "发票不存在", 404)
            rid = probe.get("reimbursement_id")
            if rid:
                self._require_draft(self.repository.get_reimbursement(connection, int(rid), lock=True))
            row = self.repository.get_invoice(connection, int(iid), lock=True)
            if not row or row.get("reimbursement_id") != rid or row.get("matched_payment_ids") != probe.get("matched_payment_ids"):
                raise ExpenseError("CONCURRENT_CHANGE", "发票状态已变更，请重试", 409)
            payment_ids = sorted(int(value) for value in (row.get("matched_payment_ids") or []))
            payments = [self.repository.get_payment(connection, pid, lock=True) for pid in payment_ids]
            archived = self.repository.archive_and_unlink_files(
                connection, object_type="INVOICE", object_id=int(iid)
            )
            for pid, payment in zip(payment_ids, payments):
                if payment:
                    remaining = [value for value in (payment.get("matched_invoice_ids") or []) if int(value) != int(iid)]
                    values = {"matched_invoice_ids": remaining}
                    if not remaining:
                        values.update({"reimbursement_id": None, "status": "未匹配"})
                    self.repository.update_payment(connection, int(pid), values)
            self.repository.delete_invoice(connection, int(iid))
            if rid:
                self.repository.recalculate_total(connection, rid)
            self._audit(connection, operation="DELETE", object_type="INVOICE", object_id=iid, object_count=1 + archived)

    def delete_payment(self, pid):
        with self.repository.engine.begin() as connection:
            probe = self.repository.get_payment(connection, int(pid))
            if not probe:
                raise ExpenseError("NOT_FOUND", "支付记录不存在", 404)
            rid = probe.get("reimbursement_id")
            if rid:
                self._require_draft(self.repository.get_reimbursement(connection, int(rid), lock=True))
            invoice_ids = sorted(int(value) for value in (probe.get("matched_invoice_ids") or []))
            invoices = [self.repository.get_invoice(connection, iid, lock=True) for iid in invoice_ids]
            row = self.repository.get_payment(connection, int(pid), lock=True)
            if not row or row.get("reimbursement_id") != rid or row.get("matched_invoice_ids") != probe.get("matched_invoice_ids"):
                raise ExpenseError("CONCURRENT_CHANGE", "支付记录状态已变更，请重试", 409)
            archived = self.repository.archive_and_unlink_files(
                connection, object_type="PAYMENT", object_id=int(pid)
            )
            for iid, invoice in zip(invoice_ids, invoices):
                if invoice:
                    remaining = [value for value in (invoice.get("matched_payment_ids") or []) if int(value) != int(pid)]
                    values = {"matched_payment_ids": remaining}
                    if not remaining:
                        values.update({"reimbursement_id": None, "status": "未匹配"})
                    self.repository.update_invoice(connection, int(iid), values)
            self.repository.delete_payment(connection, int(pid))
            if rid:
                self.repository.recalculate_total(connection, rid)
            self._audit(connection, operation="DELETE", object_type="PAYMENT", object_id=pid, object_count=1 + archived)

    def recalculate_total(self, rid):
        with self.repository.engine.begin() as connection:
            if not self.repository.get_reimbursement(connection, int(rid), lock=True):
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            return self._legacy(self.repository.recalculate_total(connection, int(rid)))

    def get_documents(self, rid):
        with self.repository.engine.connect() as connection:
            row = self.repository.get_reimbursement(connection, int(rid))
            if not row:
                return []
            return row.get("documents") or []

    def page_documents(self, rid, *, limit=100, offset=0):
        limit, offset = int(limit), int(offset)
        if limit < 1 or limit > self.MAX_DOCUMENTS or offset < 0:
            raise ExpenseValidationError("INVALID_PAGE", "分页参数无效")
        documents = self.get_documents(rid)
        total = len(documents)
        rows = documents[offset:offset + limit]
        next_offset = offset + len(rows) if offset + len(rows) < total else None
        return rows, total, next_offset

    def _documents_update(self, rid, operation):
        with self.repository.engine.begin() as connection:
            row = self.repository.get_reimbursement(connection, int(rid), lock=True)
            if not row:
                raise ExpenseError("NOT_FOUND", "报销项不存在", 404)
            documents = list(row.get("documents") or [])
            result = operation(documents)
            if len(result) > self.MAX_DOCUMENTS or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > self.MAX_DOCUMENT_BYTES:
                raise ExpenseValidationError("DOCUMENTS_TOO_LARGE", "单据内容超过限制")
            self.repository.update_reimbursement(connection, int(rid), {"documents": result, "updated_at": self._now()})
            self._audit(connection, operation="UPDATE_DOCUMENTS", object_type="EXPENSE", object_id=rid)
            return result

    def add_document(self, rid, doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("fields", {}), dict):
            raise ExpenseValidationError("INVALID_DOCUMENT", "单据内容无效")
        clean = {"id": self._text(doc.get("id") or uuid.uuid4().hex, maximum=64), "template_id": self._text(doc.get("template_id"), maximum=200), "doc_type": self._text(doc.get("doc_type"), maximum=200), "filled_by": self._text(doc.get("filled_by"), maximum=100), "filled_at": self._now().strftime("%Y-%m-%d %H:%M:%S"), "fields": {self._text(key, maximum=200): self._text(value, maximum=4000) for key, value in doc.get("fields", {}).items()}}
        return self._documents_update(rid, lambda docs: docs + [clean])

    def update_document(self, rid, doc_id, updates):
        if not isinstance(updates, dict) or not isinstance(updates.get("fields", {}), dict):
            raise ExpenseValidationError("INVALID_DOCUMENT", "单据内容无效")
        def change(docs):
            found = False
            for doc in docs:
                if doc.get("id") == doc_id:
                    found = True
                    doc.setdefault("fields", {}).update({self._text(key, maximum=200): self._text(value, maximum=4000) for key, value in updates.get("fields", {}).items()})
                    doc["updated_at"] = self._now().strftime("%Y-%m-%d %H:%M:%S")
            if not found:
                raise ExpenseError("NOT_FOUND", "单据不存在", 404)
            return docs
        return self._documents_update(rid, change)

    def delete_document(self, rid, doc_id):
        def remove(docs):
            filtered = [doc for doc in docs if doc.get("id") != doc_id]
            if len(filtered) == len(docs):
                raise ExpenseError("NOT_FOUND", "单据不存在", 404)
            return filtered
        return self._documents_update(rid, remove)

    def stats(self):
        with self.repository.engine.connect() as connection:
            stats, recent = self.repository.stats(connection)
            return stats, [self.public_row(row) for row in recent]

    def find_duplicate_invoice(self, amount, invoice_date, invoice_no):
        with self.repository.engine.connect() as connection:
            return self.public_row(self.repository.find_duplicate_invoice(connection, self.amount(amount), self._date(invoice_date), self._text(invoice_no, maximum=100)))

    def find_duplicate_payment(self, amount, pay_date):
        with self.repository.engine.connect() as connection:
            return self.public_row(self.repository.find_duplicate_payment(connection, self.amount(amount), self._date(pay_date)))
