# -*- coding: utf-8 -*-
"""Compatibility facade for the PostgreSQL expense service."""
from __future__ import annotations

import json
from pathlib import Path

from flask import current_app


def _service():
    service = current_app.extensions.get("expense_service")
    if service is None:
        raise RuntimeError("expense service is unavailable")
    return service


def init_db():
    """Retained as a no-op: Alembic owns the PostgreSQL schema."""
    return None


def get_all_reimbursements(status=None, keyword=None, limit=200, offset=0):
    return _service().list_reimbursements(status, keyword, limit=limit, offset=offset)


def get_reimbursement_by_id(rid): return _service().get_reimbursement(rid)
def create_reimbursement(title="", approver="", remark="", reimbursement_type="采购报销"): return _service().create_reimbursement(title=title, approver=approver, remark=remark, reimbursement_type=reimbursement_type)
def update_reimbursement(rid, **fields): return _service().update_reimbursement(rid, **fields)
def delete_reimbursement(rid): return _service().delete_reimbursement(rid)
def toggle_reimbursement_paid(rid): return _service().toggle_paid(rid)
def add_invoice(record_id=None, reimbursement_id=None, **fields): return _service().create_invoice(reimbursement_id=reimbursement_id, **fields)
def get_invoices(reimbursement_id=None, status=None, limit=500, offset=0):
    return _service().list_invoices(reimbursement_id, status, limit=limit, offset=offset)
def get_invoice_by_id(iid): return _service().get_invoice(iid)
def update_invoice(iid, **fields): return _service().update_invoice(iid, **fields)
def delete_invoice(iid): return _service().delete_invoice(iid)
def add_payment(reimbursement_id=None, **fields): return _service().create_payment(reimbursement_id=reimbursement_id, **fields)
def get_payments(reimbursement_id=None, status=None, limit=500, offset=0):
    return _service().list_payments(reimbursement_id, status, limit=limit, offset=offset)
def get_reimbursement_children(rid): return _service().collect_reimbursement_children(rid)
def get_payment_by_id(pid): return _service().get_payment(pid)
def update_payment(pid, **fields): return _service().update_payment(pid, **fields)
def delete_payment(pid): return _service().delete_payment(pid)
def get_unmatched_invoices(): return _service().list_invoices(status="未匹配")
def get_unmatched_payments(): return _service().list_payments(status="未匹配")
def recalculate_reimbursement_total(rid): return _service().recalculate_total(rid)
def get_reimbursement_documents(rid): return _service().get_documents(rid)
def add_document_to_reimbursement(rid, doc): return _service().add_document(rid, doc)
def update_document_in_reimbursement(rid, doc_id, updates): return _service().update_document(rid, doc_id, updates)
def delete_document_from_reimbursement(rid, doc_id): return _service().delete_document(rid, doc_id)
def get_expense_stats(): return _service().stats()
def find_duplicate_invoice(amount, invoice_date, invoice_no): return _service().find_duplicate_invoice(amount, invoice_date, invoice_no)
def find_duplicate_payment(amount, pay_date): return _service().find_duplicate_payment(amount, pay_date)


def _templates_root() -> Path:
    return Path(__file__).resolve().parent / "document_templates"


def get_document_templates():
    templates = []
    root = _templates_root()
    if not root.is_dir():
        return templates
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        meta_path = directory / "template.json"
        if not directory.is_dir() or not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict):
            templates.append(meta)
    return templates


def get_document_template(doc_type):
    name = str(doc_type or "")
    if not name or Path(name).name != name or any(marker in name for marker in ("/", "\\", "..")):
        return None
    try:
        meta = json.loads((_templates_root() / name / "template.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None
