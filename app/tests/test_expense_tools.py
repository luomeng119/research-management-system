from __future__ import annotations

from decimal import Decimal
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import io

import pytest
import sqlalchemy as sa


def test_expense_service_rejects_float_amounts_before_repository_access():
    from app.services.expenses import ExpenseService, ExpenseValidationError

    class NeverCalledRepository:
        engine = None

        def __getattr__(self, name):
            raise AssertionError(f"repository must not be called: {name}")

    service = ExpenseService(NeverCalledRepository())
    with pytest.raises(ExpenseValidationError, match="金额"):
        service.create_invoice(amount=0.1)


def test_runtime_facade_contains_no_sqlite_or_schema_ddl():
    source = (Path(__file__).parents[1] / "expense_db.py").read_text(encoding="utf-8").lower()
    assert "sqlite3" not in source
    assert "create table" not in source
    assert "pragma" not in source
    assert "db_path" not in source


def test_expense_runtime_has_bounded_queries_and_no_legacy_preview_paths():
    app_dir = Path(__file__).parents[1]
    service_source = (app_dir / "services" / "expenses.py").read_text(encoding="utf-8")
    repository_source = (app_dir / "repositories" / "expenses.py").read_text(encoding="utf-8")
    template_source = (app_dir / "templates" / "expense" / "records.html").read_text(encoding="utf-8")
    assert "MAX_PAGE_SIZE" in service_source
    assert ".limit(limit).offset(offset)" in repository_source
    assert "/app/uploads/" not in template_source
    assert "file_path" not in template_source
    assert "controlledFile" in template_source


@pytest.fixture(scope="module")
def expense_service():
    url = os.environ.get("T10_TEST_DATABASE_URL")
    if not url:
        pytest.skip("expense PostgreSQL tests require T10_TEST_DATABASE_URL")
    from app.repositories.expenses import ExpensesRepository
    from app.services.expenses import ExpenseService

    engine = sa.create_engine(url, pool_size=5, max_overflow=5)
    tables = ["object_files", "stored_file_versions", "stored_files", "expense_invoice_item", "expense_invoice", "expense_payment", "expense_reimbursement"]
    with engine.begin() as connection:
        for table in tables:
            connection.execute(sa.text(f"DELETE FROM {table}"))
    service = ExpenseService(ExpensesRepository(engine))
    yield service
    with engine.begin() as connection:
        for table in tables:
            connection.execute(sa.text(f"DELETE FROM {table}"))
    engine.dispose()


def test_postgresql_crud_uses_decimal_dates_items_and_protects_control_fields(expense_service):
    from app.services.expenses import ExpenseValidationError

    rid, number = expense_service.create_reimbursement(title="设备采购", approver="张老师")
    iid = expense_service.create_invoice(
        reimbursement_id=rid, invoice_no="INV-001", date="2026-09-03",
        amount="12.30", tax_amount="0.30", price_ex_tax="12.00",
        items=[{"name": "试剂", "quantity": "2.0000", "unit_price": "6.0000", "amount": "12.00"}],
    )
    pid = expense_service.create_payment(reimbursement_id=rid, amount="12.30", pay_date="2026-09-03")

    assert number.startswith("REI202")
    assert expense_service.get_invoice(iid)["items"][0]["name"] == "试剂"
    assert expense_service.recalculate_total(rid) == 12.3
    assert expense_service.get_payment(pid)["file_path"] is None
    with pytest.raises(ExpenseValidationError, match="专用操作"):
        expense_service.update_invoice(iid, reimbursement_id=None)
    with pytest.raises(ExpenseValidationError, match="专用操作"):
        expense_service.update_reimbursement(rid, status="已确认")


def test_cross_reimbursement_detach_is_rejected_without_partial_write(expense_service):
    from app.services.expenses import ExpenseError

    first, _ = expense_service.create_reimbursement(title="第一项")
    second, _ = expense_service.create_reimbursement(title="第二项")
    iid = expense_service.create_invoice(reimbursement_id=first, amount="9.90")
    with pytest.raises(ExpenseError, match="不属于"):
        expense_service.detach_invoice(second, iid)
    assert expense_service.get_invoice(iid)["reimbursement_id"] == first


def test_manual_match_is_atomic_and_records_both_sides(expense_service):
    iid = expense_service.create_invoice(amount="88.00")
    pid = expense_service.create_payment(amount="88.00", pay_date="2026-09-03")
    rid = expense_service.manual_match([iid], [pid], title="手工整理", approver="李老师")

    invoice = expense_service.get_invoice(iid)
    payment = expense_service.get_payment(pid)
    assert invoice["reimbursement_id"] == payment["reimbursement_id"] == rid
    assert invoice["matched_payment_ids"] == [pid]
    assert payment["matched_invoice_ids"] == [iid]


def test_manual_match_cannot_overwrite_an_existing_pair(expense_service):
    from app.services.expenses import ExpenseError

    first_invoice = expense_service.create_invoice(amount="14.00")
    first_payment = expense_service.create_payment(amount="14.00", pay_date="2026-09-03")
    rid = expense_service.manual_match([first_invoice], [first_payment], title="已匹配")
    second_payment = expense_service.create_payment(amount="14.00", pay_date="2026-09-03")
    with pytest.raises(ExpenseError, match="状态不可匹配"):
        expense_service.manual_match([first_invoice], [second_payment], rid=rid)
    assert expense_service.get_payment(first_payment)["matched_invoice_ids"] == [first_invoice]
    assert expense_service.get_payment(second_payment)["reimbursement_id"] is None


def test_concurrent_number_generation_has_no_duplicates(expense_service):
    def create(index):
        return expense_service.create_reimbursement(title=f"并发 {index}")[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = list(pool.map(create, range(2)))
    assert len(set(numbers)) == 2


def test_concurrent_payment_number_generation_has_no_duplicates(expense_service):
    def create(_index):
        pid = expense_service.create_payment(amount="1.00", pay_date="2026-09-03")
        return expense_service.get_payment(pid)["payment_no"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = list(pool.map(create, range(2)))
    assert len(set(numbers)) == 2


def test_auto_match_rechecks_under_lock_and_does_not_reuse_records(expense_service):
    iid = expense_service.create_invoice(amount="66.60")
    pid = expense_service.create_payment(amount="66.60", pay_date="2026-09-03")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: expense_service.auto_match(approver="王老师"), range(2)))

    assert sum(result["total_payments_matched"] for result in results) == 1
    assert expense_service.get_invoice(iid)["matched_payment_ids"] == [pid]
    assert expense_service.get_payment(pid)["matched_invoice_ids"] == [iid]


def test_documents_json_updates_are_serialized_by_row_lock(expense_service):
    rid, _ = expense_service.create_reimbursement(title="单据并发")

    def add(index):
        return expense_service.add_document(rid, {"doc_type": "差旅费报销凭证", "fields": {"序号": str(index)}})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, range(2)))
    assert len(expense_service.get_documents(rid)) == 2


def test_first_attachment_failure_rolls_back_business_row(expense_service):
    from app.services.expenses import ExpenseService

    class Writer:
        def __init__(self, connection): self.connection = connection
        def execute(self, statement, parameters=None): self.connection.execute(statement, parameters or {})

    class FailingAtomicFiles:
        def __init__(self, engine): self.engine = engine
        def upload_new_object(self, stream, **kwargs):
            with self.engine.begin() as connection:
                kwargs["create_metadata"](Writer(connection))
                raise RuntimeError("injected file failure")

    service = ExpenseService(expense_service.repository, file_service=FailingAtomicFiles(expense_service.repository.engine))
    with service.repository.engine.connect() as connection:
        before = connection.scalar(sa.select(sa.func.count()).select_from(service.repository.invoices))
    with pytest.raises(RuntimeError, match="injected"):
        service.create_invoice_with_upload(
            stream=None, original_name="proof.png", actor_user_id=1, request_id="rollback",
            amount="1.00", date="2026-09-03",
        )
    with service.repository.engine.connect() as connection:
        after = connection.scalar(sa.select(sa.func.count()).select_from(service.repository.invoices))
    assert after == before


def test_manual_match_failure_injection_rolls_back_all_relations(expense_service, monkeypatch):
    iid = expense_service.create_invoice(amount="21.00")
    pid = expense_service.create_payment(amount="21.00", pay_date="2026-09-03")
    original = expense_service.repository.update_payment

    def fail_after_invoice(*_args, **_kwargs):
        raise RuntimeError("injected relation failure")

    monkeypatch.setattr(expense_service.repository, "update_payment", fail_after_invoice)
    with pytest.raises(RuntimeError, match="injected relation failure"):
        expense_service.manual_match([iid], [pid], title="rollback")
    monkeypatch.setattr(expense_service.repository, "update_payment", original)
    assert expense_service.get_invoice(iid)["reimbursement_id"] is None
    assert expense_service.get_payment(pid)["reimbursement_id"] is None


def test_first_expense_attachment_uses_controlled_file_service_atomically(expense_service, tmp_path):
    from app.repositories.files import FilesRepository
    from app.services.expenses import ExpenseService
    from app.services.files import FileService

    class AuditRecorder:
        def record(self, connection, **event):
            return event

    engine = expense_service.repository.engine
    username = f"expense-file-{os.getpid()}"
    with engine.begin() as connection:
        user_id = connection.scalar(sa.text(
            "INSERT INTO users (username,password,role,name) VALUES (:username,'x','user','张老师') RETURNING id"
        ), {"username": username})
    files = FileService(
        FilesRepository(engine), AuditRecorder(),
        storage_root=tmp_path / "files", max_bytes=1024 * 1024, preview_max_bytes=1024 * 1024,
    )
    service = ExpenseService(expense_service.repository, file_service=files)
    try:
        iid, stored = service.create_invoice_with_upload(
            io.BytesIO(b"\x89PNG\r\n\x1a\ncontrolled"), "invoice.png",
            actor_user_id=user_id, request_id="expense-upload", amount="7.50", date="2026-09-03",
        )
        assert stored["storagePath"].startswith("2026/")
        invoice = service.get_invoice(iid)
        assert invoice["file_path"] is None
        listed = files.list_for_object(object_type="INVOICE", object_id=str(iid))
        assert len(listed) == 1
        file_id = listed[0]["fileId"]
        assert service.get_invoice(iid)["files"][0]["fileId"] == file_id
        opened = files.open_version_stream(file_id, 1, object_type="INVOICE", object_id=str(iid))
        try:
            assert opened["stream"].read().startswith(b"\x89PNG")
        finally:
            opened["stream"].close()
        service.delete_invoice(iid)
        assert service.get_invoice(iid) is None
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT count(*) FROM object_files WHERE object_type='INVOICE' AND object_id=:id"), {"id": str(iid)}) == 0
            assert connection.scalar(sa.text("SELECT status FROM stored_files WHERE id=:id"), {"id": file_id}) == "ARCHIVED"
    finally:
        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM object_files WHERE object_type='INVOICE' AND object_id=:id"), {"id": str(locals().get("iid", -1))})
            connection.execute(sa.text("DELETE FROM stored_file_versions WHERE file_id IN (SELECT id FROM stored_files WHERE created_by=:id)"), {"id": user_id})
            connection.execute(sa.text("DELETE FROM stored_files WHERE created_by=:id"), {"id": user_id})
            connection.execute(sa.text("DELETE FROM expense_invoice WHERE id=:id"), {"id": locals().get("iid", -1)})
            connection.execute(sa.text("DELETE FROM users WHERE id=:id"), {"id": user_id})


@pytest.mark.parametrize("doc_type", ["差旅费报销凭证", "因公出差审批单", "伙食补助费申报表"])
def test_three_shipped_docx_templates_generate_real_word_files(doc_type):
    from docx import Document
    from app.document_engine import DocumentFiller

    generated = DocumentFiller().generate(doc_type, {}, {"session": {}, "invoices": [], "payments": []})
    document = Document(io.BytesIO(generated))
    assert generated.startswith(b"PK")
    assert document.paragraphs or document.tables


def test_json_only_purchase_template_and_template_traversal_fail_clearly():
    from app.document_engine import DocumentFiller

    filler = DocumentFiller()
    with pytest.raises(FileNotFoundError, match="docx"):
        filler.generate("科研物资采购申请单", {}, {})
    with pytest.raises(FileNotFoundError, match="模板不存在"):
        filler.generate("../科研物资采购申请单", {}, {})


def test_multiple_documents_are_really_present_in_merged_word():
    from docx import Document
    from flask import Flask
    from app.routes.documents import _merge_docx_and_images

    parts = []
    for token in ("FIRST-UNIQUE-CONTENT", "SECOND-UNIQUE-CONTENT"):
        document = Document()
        document.add_paragraph(token)
        output = io.BytesIO()
        document.save(output)
        parts.append((token, output.getvalue()))
    app = Flask(__name__)
    with app.app_context():
        merged = Document(io.BytesIO(_merge_docx_and_images(parts, [], [])))
    text = "\n".join(paragraph.text for paragraph in merged.paragraphs)
    assert "FIRST-UNIQUE-CONTENT" in text
    assert "SECOND-UNIQUE-CONTENT" in text


def test_legacy_page_urls_redirect_instead_of_missing_template_500(tmp_path):
    from app import create_app

    app = create_app({
        "TESTING": True, "SECRET_KEY": "expense-route-test",
        "DATA_DIR": str(tmp_path), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False, "AI_PROVIDER": "DISABLED",
    })
    client = app.test_client()
    with client.session_transaction() as state:
        state["user"] = "teacher"
    for path in ("/expense/upload", "/expense/payments", "/expense/pending", "/expense/fill"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/expense/records")


def test_expense_api_and_page_url_contract_is_still_registered(tmp_path):
    from app import create_app

    app = create_app({
        "TESTING": True, "SECRET_KEY": "expense-url-test",
        "DATA_DIR": str(tmp_path), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False, "AI_PROVIDER": "DISABLED",
    })
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert {
        "/expense/", "/expense/records", "/expense/upload", "/expense/payments",
        "/expense/pending", "/expense/fill", "/expense/approvals",
        "/expense/documents/", "/expense/documents/new/<int:rid>",
        "/expense/documents/templates", "/expense/documents/print/<int:rid>",
        "/expense/api/reimbursements", "/expense/api/invoices", "/expense/api/payments",
        "/expense/api/manual_match", "/expense/api/match",
    } <= rules


def test_upload_survives_ocr_outage_and_internal_errors_do_not_leak(tmp_path, monkeypatch):
    from app import create_app
    import app.routes.expense as expense_routes

    class FakeExpenseService:
        def find_duplicate_invoice(self, *_args): return None
        def create_invoice_with_upload(self, stream, original_name, **kwargs):
            assert stream.read().startswith(b"\x89PNG")
            return 17, {"fileId": "00000000-0000-0000-0000-000000000017", "versionNo": 1, "originalName": original_name}
        def list_reimbursements(self, *_args, **_kwargs):
            raise RuntimeError("/secret/internal/database/path")

    app = create_app({
        "TESTING": True, "SECRET_KEY": "expense-upload-test",
        "DATA_DIR": str(tmp_path), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False, "AI_PROVIDER": "DISABLED",
        "EXPENSE_SERVICE": FakeExpenseService(),
    })
    monkeypatch.setattr(expense_routes, "recognize_file", lambda _path: (_ for _ in ()).throw(RuntimeError("OCR offline")))
    client = app.test_client()
    with client.session_transaction() as state:
        state.update({"user": "teacher", "user_id": 1, "name": "张老师"})
    response = client.post(
        "/expense/api/upload", data={"type": "invoice", "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nmanual"), "invoice.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["manual_required"] is True

    leaked = client.get("/expense/api/reimbursements")
    assert leaked.status_code == 500
    assert "/secret/" not in leaked.get_data(as_text=True)


def test_postgresql_route_contract_supports_crud_edit_and_confirm(expense_service, tmp_path):
    from app import create_app

    app = create_app({
        "TESTING": True, "SECRET_KEY": "expense-pg-route-test",
        "DATA_DIR": str(tmp_path), "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False, "AI_PROVIDER": "DISABLED",
        "EXPENSE_SERVICE": expense_service,
    })
    client = app.test_client()
    with client.session_transaction() as state:
        state.update({"user": "teacher", "user_id": 1, "name": "张老师"})

    created = client.post("/expense/api/reimbursements", json={"title": "路由验证"})
    assert created.status_code == 200
    payload = created.get_json()
    assert payload["success"] is True
    rid = payload["id"]
    iid = expense_service.create_invoice(reimbursement_id=rid, amount="31.20", invoice_no="ROUTE-INV")
    expense_service.create_payment(reimbursement_id=rid, amount="31.20", pay_date="2026-09-03")

    edited = client.put(f"/expense/api/invoices/{iid}", json={"amount": 31.2, "seller": "<img src=x onerror=alert(1)>"})
    assert edited.status_code == 200
    detail = client.get(f"/expense/api/reimbursements/{rid}").get_json()
    assert detail["success"] is True
    assert detail["invoices"][0]["seller"] == "<img src=x onerror=alert(1)>"
    assert detail["invoices"][0]["file_path"] is None
    assert detail["invoices"][0]["ocr_text"] == ""

    confirmed = client.post(f"/expense/api/reimbursements/{rid}/confirm")
    assert confirmed.status_code == 200
    assert expense_service.get_reimbursement(rid)["status"] == "已确认"
    rejected = client.put(f"/expense/api/invoices/{iid}", json={"amount": 32.0})
    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == "INVALID_STATUS"
