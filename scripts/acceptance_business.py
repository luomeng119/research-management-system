#!/usr/bin/env python3
"""Seed and verify the fixed, non-empty V1 business acceptance chain."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path


ACCEPTANCE_MARKER = "V1验收-智能保障设备适配研究-20260904"
EQUIPMENT_MARKER = "V1验收-科研保障设备-20260904"
EXPENSE_MARKER = "V1验收-设备采购登记-20260904"
EXPERT_GROUP_MARKER = "V1验收-科研论证专家组-20260904"
EQUIPMENT_LOCATION = "一号科研实验室"
STANDARD_MARKER = "V1验收-科研设备试验记录规范-20260904"
TEMPLATE_FOLDER = "科研模板"
TEMPLATE_MARKER = "V1验收-科研项目记录模板-20260904"
TABLE_MARKER = "V1验收-科研试验记录表-20260904"
INVOICE_V1 = b"V1 acceptance invoice attachment version 1\n"
INVOICE_V2 = b"V1 acceptance invoice attachment version 2 - verified\n"
EXPECTED_INVOICE_HASHES = {
    "1": hashlib.sha256(INVOICE_V1).hexdigest(),
    "2": hashlib.sha256(INVOICE_V2).hexdigest(),
}

REQUIRED_POSITIVE_COUNTS = (
    "proposals",
    "argumentations",
    "establishmentDecisions",
    "projects",
    "progressRecords",
    "outputs",
    "closures",
    "experts",
    "expertGroupMembers",
    "equipment",
    "equipmentGroupMembers",
    "reimbursements",
    "invoices",
    "payments",
    "controlledFiles",
    "fileVersions",
    "proposalFiles",
    "projectFiles",
    "standards",
    "researchTemplates",
    "genericTables",
    "genericTableRows",
)
REQUIRED_RELATIONS = (
    "proposalToProject",
    "expertToGroup",
    "equipmentToProjectGroup",
    "invoiceToReimbursement",
    "paymentToReimbursement",
    "fileToInvoice",
    "proposalToFile",
    "projectToFile",
    "standardToFile",
    "templateToFile",
    "genericTableToRow",
)


class AcceptanceContractError(RuntimeError):
    pass


def validate_snapshot(snapshot: dict) -> None:
    if snapshot.get("schemaVersion") != 1 or snapshot.get("marker") != ACCEPTANCE_MARKER:
        raise AcceptanceContractError("business snapshot schema or marker is invalid")
    identities = snapshot.get("identities")
    if not isinstance(identities, dict) or any(not identities.get(key) for key in (
        "proposalInternalId", "proposalBusinessId", "projectRegistryId", "projectBusinessId", "expertId",
        "expertGroupId", "equipmentId", "equipmentGroupId", "reimbursementId",
        "invoiceId", "paymentId", "fileId", "proposalFileId", "projectFileId",
        "standardId", "templateId", "genericTableId", "genericTableVersionId",
    )):
        raise AcceptanceContractError("business snapshot identities are incomplete")
    states = snapshot.get("states") or {}
    if states.get("proposal") != "ESTABLISHED" or states.get("project") != "CLOSED":
        raise AcceptanceContractError("business snapshot does not contain completed lifecycle states")
    counts = snapshot.get("counts") or {}
    for name in REQUIRED_POSITIVE_COUNTS:
        value = counts.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AcceptanceContractError(f"business snapshot count must be positive: {name}")
    relations = snapshot.get("relations") or {}
    for name in REQUIRED_RELATIONS:
        if relations.get(name) is not True:
            raise AcceptanceContractError(f"business relationship is not proven: {name}")
    attachment = snapshot.get("attachment") or {}
    if attachment.get("versionCount") != 2 or attachment.get("latestVersion") != 2:
        raise AcceptanceContractError("controlled attachment must contain two versions")
    hashes = attachment.get("sha256ByVersion") or {}
    for version in ("1", "2"):
        digest = hashes.get(version)
        if not isinstance(digest, str) or len(digest) != 64:
            raise AcceptanceContractError(f"attachment SHA-256 is invalid for version {version}")
    if hashes != EXPECTED_INVOICE_HASHES:
        raise AcceptanceContractError("controlled attachment bytes do not match the fixed acceptance evidence")
    evidence = snapshot.get("businessEvidence") or {}
    fixed = {
        "proposalSourceType": "IDEA",
        "proposalSourceSummary": "由智能保障设备现场适配想法形成的原始研究依据",
        "proposalResearchProblem": "验证设备在科研环境中的可用性与稳定性",
        "argumentationConclusion": "建议立项",
        "establishmentConclusion": "同意立项",
        "projectCategory": "GENERAL_RESEARCH",
        "progressSummary": "V1验收-阶段进展记录-20260904",
        "outputTitle": "V1验收-适配研究报告-20260904",
        "closureConclusion": "PASS",
        "expertName": "张老师",
        "expertUnit": "第一研究室",
        "expertExpertise": "科研设备适配",
        "expertGroupName": EXPERT_GROUP_MARKER,
        "equipmentName": EQUIPMENT_MARKER,
        "equipmentLocation": EQUIPMENT_LOCATION,
        "proposalFileName": "v1-proposal-source.txt",
        "projectFileName": "v1-project-record.txt",
        "invoiceFileName": "v1-acceptance-invoice.txt",
        "reimbursementType": "采购报销",
        "reimbursementTotal": "1280.00",
        "invoiceAmount": "1280.00",
        "paymentAmount": "1280.00",
        "standardName": STANDARD_MARKER,
        "templateName": TEMPLATE_MARKER,
        "genericTableName": TABLE_MARKER,
        "genericTableValue": ACCEPTANCE_MARKER,
    }
    for name, value in fixed.items():
        if evidence.get(name) != value:
            raise AcceptanceContractError(f"fixed business evidence does not match: {name}")


def assert_same_snapshot(expected: dict, actual: dict) -> None:
    validate_snapshot(expected)
    validate_snapshot(actual)
    if actual != expected:
        raise AcceptanceContractError("restored business snapshot does not match the pre-backup baseline")


def _services(app):
    names = (
        "proposal_service", "project_service", "resources_service",
        "equipment_resources_service", "expense_service", "file_service",
        "users_repository", "reference_library_service", "generic_tables_service",
    )
    missing = [name for name in names if name not in app.extensions]
    if missing:
        raise AcceptanceContractError(
            "formal runtime services are unavailable: " + ", ".join(missing)
        )
    return {name: app.extensions[name] for name in names}


def _actor_id(services, username: str) -> int:
    account = services["users_repository"].get_by_username(username)
    if not account or account.get("status") != "active":
        raise AcceptanceContractError("active acceptance operator account is unavailable")
    return int(account["id"])


def ensure_operator(services, username: str, name: str, password: str) -> None:
    """Create or reset the disposable acceptance operator via the runtime role."""
    from app.security.auth import BUSINESS_USER, hash_password, validate_password

    username = username.strip()
    name = name.strip()
    if not username or not name:
        raise AcceptanceContractError("acceptance operator username and name are required")
    try:
        validate_password(password)
    except ValueError as error:
        raise AcceptanceContractError(f"acceptance operator password is invalid: {error}") from error

    repository = services["users_repository"]
    with repository.engine.begin() as connection:
        existing = repository.get_by_username(username, connection)
        values = {
            "password": hash_password(password),
            "role": BUSINESS_USER,
            "name": name,
            "status": "active",
            "must_change_password": False,
        }
        if existing is None:
            connection.execute(repository.table.insert().values(
                username=username,
                version=1,
                **values,
            ))
        else:
            connection.execute(
                repository.table.update()
                .where(repository.table.c.id == existing["id"])
                .values(version=repository.table.c.version + 1, **values)
            )


def _read_version(file_service, file_id: str, invoice_id: int, version: int) -> dict:
    opened = file_service.open_version_stream(
        file_id, version, object_type="INVOICE", object_id=str(invoice_id)
    )
    try:
        content = opened["stream"].read()
    finally:
        opened["stream"].close()
    digest = hashlib.sha256(content).hexdigest()
    if digest != opened["sha256"]:
        raise AcceptanceContractError("controlled attachment content hash does not match metadata")
    return {"sha256": digest, "sizeBytes": len(content)}


def build_snapshot(services, identities: dict) -> dict:
    proposals = services["proposal_service"]
    projects = services["project_service"]
    resources = services["resources_service"]
    equipment = services["equipment_resources_service"]
    expenses = services["expense_service"]
    files = services["file_service"]
    references = services["reference_library_service"]
    tables = services["generic_tables_service"]

    proposal = proposals.get(identities["proposalBusinessId"])
    argumentations = proposals.list_argumentations(proposal["businessId"])
    decisions = proposals.list_decisions(proposal["businessId"])
    project = projects.get(identities["projectRegistryId"])
    progress = projects.list_progress(project["id"])
    outputs = projects.list_outputs(project["id"])
    closure = projects.get_closure(project["id"])

    expert_page = resources.list_experts(page=1, page_size=20, keyword="张老师")
    expert_group = resources.get_expert_group(identities["expertGroupId"])
    equipment_row = equipment.get_equipment(identities["equipmentId"])
    equipment_group = equipment.get_equipment_group(identities["equipmentGroupId"])
    project_equipment = equipment.project_equipment(project["businessId"])

    invoice = expenses.get_invoice(identities["invoiceId"])
    payment = expenses.get_payment(identities["paymentId"])
    reimbursement = expenses.get_reimbursement(invoice["reimbursement_id"]) if invoice else None
    linked_files = files.list_for_object(
        object_type="INVOICE", object_id=str(invoice["id"] if invoice else 0)
    )

    expert_members = expert_group.get("members") or []
    equipment_members = equipment_group.get("members") or []
    project_items = project_equipment.get("items") or []
    decision_items = decisions.get("items") or []
    argumentation_items = argumentations.get("items") or []
    invoice_files = (invoice or {}).get("files") or []
    proposal_files = files.list_for_object(
        object_type="PROPOSAL", object_id=proposal["businessId"]
    )
    project_files = files.list_for_object(
        object_type="PROJECT", object_id=project["businessId"]
    )
    standards = [row for row in references.list_standards() if row.get("name") == STANDARD_MARKER]
    template = references.resolve_template_path(f"{TEMPLATE_FOLDER}/{TEMPLATE_MARKER}")
    template_files = files.list_for_object(
        object_type="TEMPLATE", object_id=template["templateId"]
    )
    table = tables.get_by_id_with_current(identities["genericTableId"])
    table_version_id = table.get("current_version_id") if table else None
    table_rows = tables.get_rows(table_version_id) if table_version_id else []

    matching_experts = [item for item in expert_members if item.get("name") == "张老师"]
    matching_equipment = [item for item in equipment_members if item.get("name") == EQUIPMENT_MARKER]
    if len(matching_experts) != 1 or len(matching_equipment) != 1 or len(standards) != 1:
        raise AcceptanceContractError("acceptance resource identities are ambiguous or missing")
    if len(invoice_files) != 1:
        raise AcceptanceContractError("acceptance invoice must have exactly one controlled file")
    actual_file_id = invoice_files[0]["fileId"]
    version_count = files.count_versions(
        actual_file_id, object_type="INVOICE", object_id=str(invoice["id"])
    )
    first = _read_version(files, actual_file_id, int(invoice["id"]), 1)
    second = _read_version(files, actual_file_id, int(invoice["id"]), 2)
    actual_identities = {
        "proposalInternalId": proposal["id"],
        "proposalBusinessId": proposal["businessId"],
        "projectRegistryId": project["id"],
        "projectBusinessId": project["businessId"],
        "expertId": matching_experts[0]["expertId"],
        "expertGroupId": expert_group["groupId"],
        "equipmentId": matching_equipment[0]["equipment_id"],
        "equipmentGroupId": equipment_group["group_id"],
        "reimbursementId": int(invoice["reimbursement_id"]) if invoice else 0,
        "invoiceId": int(invoice["id"]) if invoice else 0,
        "paymentId": int(payment["id"]) if payment else 0,
        "fileId": actual_file_id,
        "proposalFileId": proposal_files[0]["fileId"] if len(proposal_files) == 1 else None,
        "projectFileId": project_files[0]["fileId"] if len(project_files) == 1 else None,
        "standardId": standards[0]["doc_id"],
        "templateId": template["templateId"],
        "genericTableId": table["table_id"] if table else None,
        "genericTableVersionId": table_version_id,
    }
    snapshot = {
        "schemaVersion": 1,
        "marker": ACCEPTANCE_MARKER,
        "identities": actual_identities,
        "states": {"proposal": proposal.get("status"), "project": project.get("status")},
        "counts": {
            "proposals": 1 if proposal.get("title") == ACCEPTANCE_MARKER else 0,
            "argumentations": int(argumentations.get("total") or 0),
            "establishmentDecisions": len([item for item in decision_items if item.get("decision") == "ESTABLISH"]),
            "projects": 1 if project.get("name") == ACCEPTANCE_MARKER else 0,
            "progressRecords": len(progress),
            "outputs": len(outputs),
            "closures": 1 if closure else 0,
            "experts": len([item for item in expert_page.get("items", []) if item.get("expertId") == actual_identities["expertId"]]),
            "expertGroupMembers": len(expert_members),
            "equipment": 1 if equipment_row and equipment_row.get("name") == EQUIPMENT_MARKER else 0,
            "equipmentGroupMembers": len(equipment_members),
            "reimbursements": 1 if reimbursement and reimbursement.get("title") == EXPENSE_MARKER else 0,
            "invoices": 1 if invoice else 0,
            "payments": 1 if payment else 0,
            "controlledFiles": len(linked_files),
            "fileVersions": version_count,
            "proposalFiles": len(proposal_files),
            "projectFiles": len(project_files),
            "standards": len(standards),
            "researchTemplates": 1 if template.get("displayName") == TEMPLATE_MARKER else 0,
            "genericTables": 1 if table and table.get("name") == TABLE_MARKER else 0,
            "genericTableRows": len(table_rows),
        },
        "relations": {
            "proposalToProject": (
                project.get("sourceProposalId") == proposal.get("id")
                and len(decision_items) == 1
                and decision_items[0].get("proposalId") == proposal.get("id")
            ),
            "expertToGroup": (
                expert_group.get("groupId") == actual_identities["expertGroupId"]
                and any(item.get("expertId") == actual_identities["expertId"] for item in expert_members)
            ),
            "equipmentToProjectGroup": any(
                equipment_group.get("group_id") == actual_identities["equipmentGroupId"]
                and equipment_group.get("project_id") == actual_identities["projectBusinessId"]
                and item.get("group_id") == actual_identities["equipmentGroupId"]
                and item.get("equipment_id") == actual_identities["equipmentId"]
                and int(item.get("quantity") or 0) == 2
                and item.get("location") == EQUIPMENT_LOCATION
                for item in project_items
            ),
            "invoiceToReimbursement": bool(invoice) and int(invoice.get("reimbursement_id") or 0) == actual_identities["reimbursementId"],
            "paymentToReimbursement": bool(payment) and int(payment.get("reimbursement_id") or 0) == actual_identities["reimbursementId"],
            "fileToInvoice": (
                len(linked_files) == 1
                and linked_files[0].get("fileId") == actual_file_id
                and invoice_files[0].get("fileId") == actual_file_id
                and int(linked_files[0].get("versionNo") or 0) == 2
            ),
            "proposalToFile": len(proposal_files) == 1 and proposal_files[0].get("fileId") == actual_identities["proposalFileId"],
            "projectToFile": len(project_files) == 1 and project_files[0].get("fileId") == actual_identities["projectFileId"],
            "standardToFile": standards[0].get("fileId") is not None,
            "templateToFile": len(template_files) == 1 and template_files[0].get("fileId") is not None,
            "genericTableToRow": (
                table_version_id == actual_identities["genericTableVersionId"]
                and len(table_rows) == 1
                and table_rows[0].get("row_data", {}).get("project") == ACCEPTANCE_MARKER
            ),
        },
        "attachment": {
            "versionCount": version_count,
            "latestVersion": int(linked_files[0]["versionNo"]) if linked_files else 0,
            "sha256ByVersion": {"1": first["sha256"], "2": second["sha256"]},
            "sizeBytesByVersion": {"1": first["sizeBytes"], "2": second["sizeBytes"]},
        },
        "businessEvidence": {
            "proposalSourceType": proposal.get("sourceType"),
            "proposalSourceSummary": proposal.get("sourceSummary"),
            "proposalResearchProblem": proposal.get("researchProblem"),
            "argumentationConclusion": argumentation_items[0].get("conclusion") if argumentation_items else None,
            "establishmentConclusion": decision_items[0].get("conclusion") if decision_items else None,
            "projectCategory": project.get("category"),
            "progressSummary": progress[0].get("summary") if progress else None,
            "outputTitle": outputs[0].get("title") if outputs else None,
            "closureConclusion": closure.get("conclusion") if closure else None,
            "expertName": matching_experts[0].get("name"),
            "expertUnit": matching_experts[0].get("unit"),
            "expertExpertise": matching_experts[0].get("expertise"),
            "expertGroupName": expert_group.get("groupName"),
            "equipmentName": equipment_row.get("name") if equipment_row else None,
            "equipmentLocation": project_items[0].get("location") if project_items else None,
            "proposalFileName": proposal_files[0].get("originalName") if proposal_files else None,
            "projectFileName": project_files[0].get("originalName") if project_files else None,
            "invoiceFileName": linked_files[0].get("originalName") if linked_files else None,
            "reimbursementType": reimbursement.get("reimbursement_type") if reimbursement else None,
            "reimbursementTotal": f"{float(reimbursement.get('total_amount')):.2f}" if reimbursement else None,
            "invoiceAmount": f"{float(invoice.get('amount')):.2f}" if invoice else None,
            "paymentAmount": f"{float(payment.get('amount')):.2f}" if payment else None,
            "standardName": standards[0].get("name"),
            "templateName": template.get("displayName"),
            "genericTableName": table.get("name") if table else None,
            "genericTableValue": table_rows[0].get("row_data", {}).get("project") if table_rows else None,
        },
    }
    validate_snapshot(snapshot)
    return snapshot


def seed(services, username: str) -> dict:
    proposals = services["proposal_service"]
    projects = services["project_service"]
    resources = services["resources_service"]
    equipment = services["equipment_resources_service"]
    expenses = services["expense_service"]
    files = services["file_service"]
    references = services["reference_library_service"]
    tables = services["generic_tables_service"]
    actor_id = _actor_id(services, username)

    if proposals.list(page=1, page_size=1, keyword=ACCEPTANCE_MARKER)["total"]:
        raise AcceptanceContractError("acceptance business marker already exists; use a fresh run")

    proposal = proposals.create({
        "title": ACCEPTANCE_MARKER,
        "sourceType": "IDEA",
        "sourceSummary": "由智能保障设备现场适配想法形成的原始研究依据",
        "researchProblem": "验证设备在科研环境中的可用性与稳定性",
        "objectives": "形成可复核的适配结论",
        "researchContent": "开展环境适应性验证并记录过程",
        "expectedOutcomes": "适配研究报告",
    }, actor_user_id=actor_id, request_id="acceptance-proposal-create")
    proposal_file = files.upload(
        io.BytesIO(b"V1 proposal source evidence\n"),
        original_name="v1-proposal-source.txt", object_type="PROPOSAL",
        object_id=proposal["businessId"], actor_user_id=actor_id,
        request_id="acceptance-proposal-file",
    )
    argumentation = proposals.add_argumentation(
        proposal["businessId"],
        {"summary": "需求真实，研究边界清晰，具备立项条件", "argumentationDate": "2026-09-04"},
        conclusion="建议立项", basis="原始想法与适配依据完整", expected_version=proposal["version"],
        actor_user_id=actor_id, request_id="acceptance-proposal-argumentation",
    )
    established = projects.establish_from_proposal(
        proposal["businessId"],
        {
            "decision": "ESTABLISH", "decisionDate": "2026-09-04",
            "conclusion": "同意立项", "basis": "论证结论支持进入研究阶段",
            "project": {
                "category": "GENERAL_RESEARCH", "name": ACCEPTANCE_MARKER,
                "leader": "张老师", "plannedEndDate": "2027-09-04",
            },
        },
        idempotency_key="v1-acceptance-establish-20260904",
        expected_version=argumentation["proposal"]["version"], actor_user_id=actor_id,
        request_id="acceptance-project-establish",
    )
    project = established["project"]
    project_file = files.upload(
        io.BytesIO(b"V1 project controlled record\n"),
        original_name="v1-project-record.txt", object_type="PROJECT",
        object_id=project["businessId"], actor_user_id=actor_id,
        request_id="acceptance-project-file",
    )
    active = projects.transition_status(
        project["id"], {"toStatus": "ACTIVE", "reason": "启动现场适配验证", "version": project["version"]},
        actor_user_id=actor_id, request_id="acceptance-project-start",
    )
    progress = projects.add_progress(
        project["id"], {
            "recordedAt": "2026-09-05T09:00:00+08:00", "status": "NORMAL",
            "summary": "V1验收-阶段进展记录-20260904", "riskLevel": "LOW",
            "issues": "", "nextActions": "形成成果并结题", "version": active["version"],
        }, actor_user_id=actor_id, request_id="acceptance-project-progress",
    )
    output = projects.add_output(
        project["id"], {
            "outputType": "REPORT", "title": "V1验收-适配研究报告-20260904",
            "description": "固定验收成果", "formedDate": "2026-09-06",
            "contributors": "张老师、李老师", "version": progress["projectVersion"],
        }, actor_user_id=actor_id, request_id="acceptance-project-output",
    )
    closing = projects.transition_status(
        project["id"], {"toStatus": "CLOSING", "reason": "成果已形成，进入结题", "version": output["projectVersion"]},
        actor_user_id=actor_id, request_id="acceptance-project-closing",
    )
    projects.close_project(
        project["id"], {
            "closedAt": "2026-09-07T09:00:00+08:00", "conclusion": "PASS",
            "summary": "完成研究目标并形成可复核报告", "remainingIssues": "无",
            "version": closing["version"],
        }, actor_user_id=actor_id, request_id="acceptance-project-close",
    )

    expert = resources.create_expert(
        {"name": "张老师", "unit": "第一研究室", "position": "研究员", "expertise": "科研设备适配"},
        uploader="李老师", actor_user_id=actor_id, request_id="acceptance-expert-create",
    )
    expert_group = resources.create_expert_group(
        {"groupName": EXPERT_GROUP_MARKER}, creator="李老师"
    )
    resources.add_expert_group_member(
        expert_group["groupId"], expert["expertId"], selected_by="李老师"
    )

    device = equipment.create_equipment(
        {"name": EQUIPMENT_MARKER, "category": "通用设备", "model": "V1-ACCEPT-01", "mainPurpose": "科研试验保障"},
        actor_user_id=actor_id, request_id="acceptance-equipment-create",
    )
    equipment.create_equipment(
        {"name": "<b data-device-probe>演练设备标签</b>", "category": "通用设备",
         "model": "V1-HTML-PROBE", "mainPurpose": "隔离验收中的纯文本显示验证"},
        actor_user_id=actor_id, request_id="acceptance-equipment-html-probe",
    )
    equipment_link = equipment.link_project_equipment(
        project["businessId"], project["name"], "张老师", device["equipment_id"],
        quantity=2, location=EQUIPMENT_LOCATION,
    )

    reimbursement_id, _ = expenses.create_reimbursement(
        title=EXPENSE_MARKER, approver="张老师", remark="仅用于简单采购/报销登记", reimbursement_type="采购报销"
    )
    invoice_id, stored = expenses.create_invoice_with_upload(
        io.BytesIO(INVOICE_V1), "v1-acceptance-invoice.txt",
        actor_user_id=actor_id, request_id="acceptance-invoice-upload",
        reimbursement_id=reimbursement_id, invoice_no="INV-V1-20260904",
        date="2026-09-04", amount="1280.00", buyer="科研项目组", seller="设备供应方",
        content=EQUIPMENT_MARKER, invoice_type="采购发票",
    )
    files.add_version(
        stored["fileId"], io.BytesIO(INVOICE_V2),
        original_name="v1-acceptance-invoice.txt", object_type="INVOICE", object_id=str(invoice_id),
        expected_version=1, actor_user_id=actor_id, request_id="acceptance-invoice-version",
    )
    payment_id = expenses.create_payment(
        reimbursement_id=reimbursement_id, payment_no="PAY-V1-20260904",
        amount="1280.00", pay_date="2026-09-05", payer="李老师",
    )

    standard = references.upload_standard(
        io.BytesIO(b"V1 research equipment test record standard\n"),
        "v1-research-standard.txt", STANDARD_MARKER, "科研试验规范",
        actor_user_id=actor_id, request_id="acceptance-standard-upload",
    )
    references.create_folder(
        TEMPLATE_FOLDER, None, actor_user_id=actor_id,
        request_id="acceptance-template-folder",
    )
    template = references.upload_template(
        io.BytesIO(b"V1 research project record template\n"),
        "v1-research-template.txt", TEMPLATE_FOLDER, TEMPLATE_MARKER,
        actor_user_id=actor_id, request_id="acceptance-template-upload",
    )
    generic_table_id = tables.create(
        TABLE_MARKER, "仅用于验收保留的通用表格能力", "张老师"
    )
    generic_table = tables.get_by_id_with_current(generic_table_id)
    generic_version_id = generic_table["current_version_id"]
    tables.upsert_column(generic_version_id, "project", "项目", "text")
    tables.upsert_row(
        generic_version_id, "acceptance-row", {"project": ACCEPTANCE_MARKER}
    )

    identities = {
        "proposalInternalId": proposal["id"],
        "proposalBusinessId": proposal["businessId"],
        "projectRegistryId": project["id"],
        "projectBusinessId": project["businessId"],
        "expertId": expert["expertId"],
        "expertGroupId": expert_group["groupId"],
        "equipmentId": device["equipment_id"],
        "equipmentGroupId": equipment_link["group_id"],
        "reimbursementId": reimbursement_id,
        "invoiceId": invoice_id,
        "paymentId": payment_id,
        "fileId": stored["fileId"],
        "proposalFileId": proposal_file["fileId"],
        "projectFileId": project_file["fileId"],
        "standardId": standard["docId"],
        "templateId": template["templateId"],
        "genericTableId": generic_table_id,
        "genericTableVersionId": generic_version_id,
    }
    return build_snapshot(services, identities)


def _write_json(path: Path, payload: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceContractError(f"cannot read acceptance baseline: {error}") from error
    if not isinstance(payload, dict):
        raise AcceptanceContractError("acceptance baseline must be a JSON object")
    return payload


def verify_ui_journey(services, evidence: dict) -> dict:
    """Read the records and physical attachment created only through the browser UI."""
    journey = evidence.get("journey") or {}
    required = ("proposalBusinessId", "projectId", "title", "attachmentName",
                "attachmentSha256", "progressSummary", "outputTitle", "closureSummary")
    research_fields = ("sourceType", "sourceSummary", "researchProblem", "objectives",
                       "researchContent", "expectedOutcomes")
    submitted = journey.get("proposalFields") or {}
    if evidence.get("status") != "PASSED" or journey.get("status") != "PASSED" or any(
        not journey.get(key) for key in required
    ) or any(not submitted.get(key) for key in research_fields):
        raise AcceptanceContractError("browser journey evidence is incomplete")
    proposals = services["proposal_service"]
    projects = services["project_service"]
    proposal = proposals.get(journey["proposalBusinessId"])
    project = projects.get(journey["projectId"])
    arguments = proposals.list_argumentations(proposal["businessId"])["items"]
    decisions = proposals.list_decisions(proposal["businessId"])["items"]
    progress = projects.list_progress(project["id"])
    outputs = projects.list_outputs(project["id"])
    closure = projects.get_closure(project["id"])
    files = services["file_service"].list_for_object(
        object_type="PROPOSAL", object_id=proposal["businessId"]
    )
    checks = {
        "proposal": proposal["title"] == journey["title"] and proposal["status"] == "ESTABLISHED",
        "researchInputs": all(proposal.get(key) == submitted[key] for key in research_fields),
        "project": project["name"] == journey["title"] and project["status"] == "CLOSED",
        "relation": project["sourceProposalId"] == proposal["id"],
        "argumentation": len(arguments) == 1 and arguments[0]["conclusion"] == "建议立项",
        "decision": len(decisions) == 1 and decisions[0]["decision"] == "ESTABLISH",
        "progress": len(progress) == 1 and progress[0]["summary"] == journey["progressSummary"],
        "output": len(outputs) == 1 and outputs[0]["title"] == journey["outputTitle"],
        "closure": bool(closure) and closure["conclusion"] == "PASS"
        and closure["summary"] == journey["closureSummary"],
        "file": len(files) == 1 and files[0]["originalName"] == journey["attachmentName"],
    }
    if not all(checks.values()):
        raise AcceptanceContractError(f"browser journey persistence mismatch: {checks}")
    opened = services["file_service"].open_version_stream(
        files[0]["fileId"], files[0]["versionNo"],
        object_type="PROPOSAL", object_id=proposal["businessId"],
    )
    try:
        digest = hashlib.sha256(opened["stream"].read()).hexdigest()
    finally:
        opened["stream"].close()
    if digest != journey["attachmentSha256"] or digest != opened["sha256"]:
        raise AcceptanceContractError("browser journey attachment hash mismatch")
    return {"status": "PASSED", "checks": checks, "attachmentSha256": digest,
            "proposalBusinessId": proposal["businessId"], "projectId": project["id"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--username", required=True)
    seed_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--expected", type=Path, required=True)
    verify_parser.add_argument("--output", type=Path, required=True)
    operator_parser = subparsers.add_parser("ensure-user")
    operator_parser.add_argument("--username", required=True)
    operator_parser.add_argument("--name", required=True)
    journey_parser = subparsers.add_parser("verify-journey")
    journey_parser.add_argument("--expected", type=Path, required=True)
    journey_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    os.environ["AI_PROVIDER"] = "DISABLED"
    from app import create_app

    app = create_app()
    services = _services(app)
    try:
        if args.command == "ensure-user":
            password = sys.stdin.readline().rstrip("\r\n")
            if not password:
                raise AcceptanceContractError("acceptance operator password was not supplied on stdin")
            ensure_operator(services, args.username, args.name, password)
            password = ""
        elif args.command == "verify-journey":
            snapshot = verify_ui_journey(services, _load_json(args.expected))
            _write_json(args.output, snapshot)
        elif args.command == "seed":
            snapshot = seed(services, args.username)
            _write_json(args.output, snapshot)
        else:
            expected = _load_json(args.expected)
            snapshot = build_snapshot(services, expected.get("identities") or {})
            assert_same_snapshot(expected, snapshot)
            _write_json(args.output, snapshot)
        return 0
    except AcceptanceContractError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    finally:
        engine = app.extensions.get("database_engine")
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
