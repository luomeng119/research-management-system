import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "acceptance_business.py"
ACCEPTANCE = ROOT / "scripts" / "acceptance.ps1"


def _load_helper():
    spec = importlib.util.spec_from_file_location("acceptance_business", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_business_helper_uses_formal_services_for_complete_v1_chain():
    source = HELPER.read_text(encoding="utf-8")
    required_calls = (
        ".create(",
        ".add_argumentation(",
        ".establish_from_proposal(",
        ".transition_status(",
        ".add_progress(",
        ".add_output(",
        ".close_project(",
        ".create_expert(",
        ".create_expert_group(",
        ".add_expert_group_member(",
        ".create_equipment(",
        ".link_project_equipment(",
        ".create_reimbursement(",
        ".create_invoice_with_upload(",
        ".create_payment(",
        ".add_version(",
    )
    for call in required_calls:
        assert call in source
    assert "create_app()" in source
    assert "sqlite" not in source.casefold()
    assert "INSERT INTO" not in source.upper()


def test_snapshot_contract_rejects_zero_counts_and_detects_restore_drift(tmp_path):
    helper = _load_helper()
    snapshot = {
        "schemaVersion": 1,
        "marker": helper.ACCEPTANCE_MARKER,
        "identities": {
            "proposalInternalId": "PROP-UUID",
            "proposalBusinessId": "TP-1",
            "projectRegistryId": "11111111-1111-1111-1111-111111111111",
            "projectBusinessId": "KY-1",
            "expertId": "EXP-1",
            "expertGroupId": "EG-1",
            "equipmentId": "EQP-1",
            "equipmentGroupId": "FG-1",
            "reimbursementId": 1,
            "invoiceId": 2,
            "paymentId": 3,
            "fileId": "22222222-2222-2222-2222-222222222222",
            "proposalFileId": "33333333-3333-3333-3333-333333333333",
            "projectFileId": "44444444-4444-4444-4444-444444444444",
            "standardId": "STD-1",
            "templateId": "TPL-1",
            "genericTableId": "GT-1",
            "genericTableVersionId": "GTV-1",
        },
        "states": {"proposal": "ESTABLISHED", "project": "CLOSED"},
        "counts": {name: 1 for name in helper.REQUIRED_POSITIVE_COUNTS},
        "relations": {name: True for name in helper.REQUIRED_RELATIONS},
        "attachment": {
            "versionCount": 2,
            "latestVersion": 2,
            "sha256ByVersion": helper.EXPECTED_INVOICE_HASHES,
        },
        "businessEvidence": {
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
            "expertGroupName": helper.EXPERT_GROUP_MARKER,
            "equipmentName": helper.EQUIPMENT_MARKER,
            "equipmentLocation": helper.EQUIPMENT_LOCATION,
            "proposalFileName": "v1-proposal-source.txt",
            "projectFileName": "v1-project-record.txt",
            "invoiceFileName": "v1-acceptance-invoice.txt",
            "reimbursementType": "采购报销",
            "reimbursementTotal": "1280.00",
            "invoiceAmount": "1280.00",
            "paymentAmount": "1280.00",
            "standardName": helper.STANDARD_MARKER,
            "templateName": helper.TEMPLATE_MARKER,
            "genericTableName": helper.TABLE_MARKER,
            "genericTableValue": helper.ACCEPTANCE_MARKER,
        },
    }
    helper.validate_snapshot(snapshot)

    invalid = json.loads(json.dumps(snapshot))
    invalid["counts"][helper.REQUIRED_POSITIVE_COUNTS[0]] = 0
    with pytest.raises(helper.AcceptanceContractError, match="positive"):
        helper.validate_snapshot(invalid)

    drift = json.loads(json.dumps(snapshot))
    drift["identities"]["genericTableVersionId"] = "GTV-2"
    with pytest.raises(helper.AcceptanceContractError, match="does not match"):
        helper.assert_same_snapshot(snapshot, drift)


def test_acceptance_seeds_and_verifies_business_snapshot_around_backup_restore():
    source = ACCEPTANCE.read_text(encoding="utf-8")
    install = source.index('Invoke-AcceptanceStep "01-install"')
    browser = source.index('Invoke-AcceptanceStep "04-core-e2e"')
    backup = source.index('Invoke-AcceptanceStep "05-backup"')
    stop = source.index('Invoke-AcceptanceStep "06-stop"')
    consistency = source.index('Invoke-AcceptanceStep "10-consistency"')
    outbound = source.index('Invoke-AcceptanceStep "11-outbound-scan"')

    assert "acceptance_business.py" in source
    assert '"seed"' in source[install:browser]
    assert '"verify"' in source[backup:stop]
    assert '"verify"' in source[consistency:outbound]
    assert "business-baseline.json" in source
    assert "business-before-backup.json" in source
    assert "business-after-restore.json" in source


def test_playwright_reads_real_business_markers_from_each_v1_surface():
    source = ACCEPTANCE.read_text(encoding="utf-8")
    e2e = source[
        source.index('Invoke-AcceptanceStep "04-core-e2e"'):
        source.index('Invoke-AcceptanceStep "05-backup"')
    ]
    assert "Acceptance User" not in source
    assert '$env:ACCEPTANCE_USERNAME = "zhanglaoshi"' in source
    for path in ("/projects", "/experts", "/equipment", "/expense/records", "/utils/"):
        assert path in e2e
    for marker in (
        "V1验收-智能保障设备适配研究-20260904",
        "张老师",
        "V1验收-科研保障设备-20260904",
        "V1验收-设备采购登记-20260904",
        "文档校对",
    ):
        assert marker in e2e
    assert "waitFor" in e2e
    consistency = source[
        source.index('Invoke-AcceptanceStep "10-consistency"'):
        source.index('Invoke-AcceptanceStep "11-outbound-scan"')
    ]
    assert 'ACCEPTANCE_PHASE = "after-restore"' in consistency
    assert "Invoke-NodeWithSecretFromStdin" in consistency
    assert "sourceProposalId !== ids.proposalInternalId" in e2e
    assert "file.fileId === ids.projectFileId" in e2e
    assert "Number(file.versionNo) === 1" in e2e
    assert "v1-project-record.txt" in e2e
    assert "const selectedMemberRow" in e2e
    assert "selectedMemberRow.getByText('张老师'" in e2e
    assert "selectedMemberRow.getByText('第一研究室'" in e2e
    assert "selectedMemberRow.getByText('科研设备适配'" in e2e
    assert consistency.index("finally { $RuntimeUrl = $null; $FlaskSecret = $null }") < consistency.index(
        'ACCEPTANCE_PHASE = "after-restore"'
    )


def _fake_services(helper):
    proposal = {
        "id": "PROP-UUID", "businessId": "TP-ACTUAL", "title": helper.ACCEPTANCE_MARKER,
        "sourceType": "IDEA", "sourceSummary": "由智能保障设备现场适配想法形成的原始研究依据",
        "researchProblem": "验证设备在科研环境中的可用性与稳定性",
        "status": "ESTABLISHED", "version": 3,
    }
    project = {
        "id": "REG-ACTUAL", "businessId": "KY-ACTUAL", "name": helper.ACCEPTANCE_MARKER,
        "sourceProposalId": "PROP-UUID", "category": "GENERAL_RESEARCH",
        "status": "CLOSED", "version": 6,
    }
    expert = {"expertId": "EXP-ACTUAL", "name": "张老师", "unit": "第一研究室", "expertise": "科研设备适配"}
    device = {"equipment_id": "EQP-ACTUAL", "name": helper.EQUIPMENT_MARKER}
    invoice_file = {"fileId": "FILE-ACTUAL", "versionNo": 2, "originalName": "v1-acceptance-invoice.txt", "status": "ACTIVE"}

    proposals = Mock()
    proposals.list.return_value = {"total": 0}
    proposals.create.return_value = {**proposal, "status": "DRAFT", "version": 1}
    proposals.add_argumentation.return_value = {"proposal": {**proposal, "status": "ARGUMENTATION", "version": 2}}
    proposals.get.return_value = proposal
    proposals.list_argumentations.return_value = {
        "items": [{"facts": {"summary": "ok"}, "conclusion": "建议立项"}], "total": 1
    }
    proposals.list_decisions.return_value = {
        "items": [{"decision": "ESTABLISH", "proposalId": "PROP-UUID", "conclusion": "同意立项"}],
        "total": 1,
    }

    projects = Mock()
    projects.establish_from_proposal.return_value = {"project": {**project, "status": "PENDING", "version": 1}}
    projects.transition_status.side_effect = [
        {**project, "status": "ACTIVE", "version": 2},
        {**project, "status": "CLOSING", "version": 5},
    ]
    projects.add_progress.return_value = {"projectVersion": 3}
    projects.add_output.return_value = {"projectVersion": 4}
    projects.close_project.return_value = {"projectStatus": "CLOSED", "projectVersion": 6}
    projects.get.return_value = project
    projects.list_progress.return_value = [{"summary": "V1验收-阶段进展记录-20260904"}]
    projects.list_outputs.return_value = [{"title": "V1验收-适配研究报告-20260904"}]
    projects.get_closure.return_value = {"conclusion": "PASS"}

    resources = Mock()
    resources.create_expert.return_value = expert
    resources.create_expert_group.return_value = {"groupId": "EG-ACTUAL", "groupName": helper.EXPERT_GROUP_MARKER}
    resources.list_experts.return_value = {"items": [expert], "total": 1}
    resources.get_expert_group.return_value = {
        "groupId": "EG-ACTUAL", "groupName": helper.EXPERT_GROUP_MARKER, "members": [expert]
    }

    equipment = Mock()
    equipment.create_equipment.return_value = device
    equipment.link_project_equipment.return_value = {"group_id": "FG-ACTUAL", "equipment_id": "EQP-ACTUAL"}
    equipment.get_equipment.return_value = device
    equipment.get_equipment_group.return_value = {
        "group_id": "FG-ACTUAL", "project_id": "KY-ACTUAL", "members": [device]
    }
    equipment.project_equipment.return_value = {
        "items": [{**device, "group_id": "FG-ACTUAL", "quantity": 2, "location": helper.EQUIPMENT_LOCATION}]
    }

    expenses = Mock()
    expenses.create_reimbursement.return_value = (41, "REI-ACTUAL")
    expenses.create_invoice_with_upload.return_value = (42, {"fileId": "FILE-ACTUAL"})
    expenses.create_payment.return_value = 43
    expenses.get_invoice.return_value = {"id": 42, "reimbursement_id": 41, "amount": 1280.0, "files": [invoice_file]}
    expenses.get_payment.return_value = {"id": 43, "reimbursement_id": 41, "amount": 1280.0}
    expenses.get_reimbursement.return_value = {
        "id": 41, "title": helper.EXPENSE_MARKER, "reimbursement_type": "采购报销", "total_amount": 1280.0
    }

    files = Mock()
    files.upload.side_effect = [{"fileId": "PFILE-ACTUAL"}, {"fileId": "JFILE-ACTUAL"}]
    files.list_for_object.side_effect = lambda *, object_type, object_id: {
        "INVOICE": [invoice_file],
        "PROPOSAL": [{"fileId": "PFILE-ACTUAL", "versionNo": 1, "originalName": "v1-proposal-source.txt"}],
        "PROJECT": [{"fileId": "JFILE-ACTUAL", "versionNo": 1, "originalName": "v1-project-record.txt"}],
        "TEMPLATE": [{"fileId": "TFILE-ACTUAL", "versionNo": 1}],
    }[object_type]
    files.open_version_stream.side_effect = lambda _file, version, **_kwargs: {
        "stream": io.BytesIO(helper.INVOICE_V1 if version == 1 else helper.INVOICE_V2),
        "sha256": helper.EXPECTED_INVOICE_HASHES[str(version)],
    }
    files.count_versions.return_value = 2

    references = Mock()
    references.upload_standard.return_value = {"docId": "STD-ACTUAL"}
    references.upload_template.return_value = {"templateId": "TPL-ACTUAL"}
    references.list_standards.return_value = [{
        "doc_id": "STD-ACTUAL", "name": helper.STANDARD_MARKER, "fileId": "SFILE-ACTUAL"
    }]
    references.resolve_template_path.return_value = {
        "templateId": "TPL-ACTUAL", "displayName": helper.TEMPLATE_MARKER
    }

    tables = Mock()
    tables.create.return_value = "GT-ACTUAL"
    tables.get_by_id_with_current.return_value = {
        "table_id": "GT-ACTUAL", "name": helper.TABLE_MARKER, "current_version_id": "GTV-ACTUAL"
    }
    tables.get_rows.return_value = [{"row_data": {"project": helper.ACCEPTANCE_MARKER}}]

    users = Mock()
    users.get_by_username.return_value = {"id": 7, "status": "active"}
    return {
        "proposal_service": proposals, "project_service": projects,
        "resources_service": resources, "equipment_resources_service": equipment,
        "expense_service": expenses, "file_service": files,
        "reference_library_service": references, "generic_tables_service": tables,
        "users_repository": users,
    }


def test_seed_builds_actual_snapshot_and_drift_fails_closed():
    helper = _load_helper()
    services = _fake_services(helper)
    snapshot = helper.seed(services, "zhanglaoshi")
    helper.validate_snapshot(snapshot)
    assert snapshot["identities"]["fileId"] == "FILE-ACTUAL"
    assert snapshot["attachment"]["versionCount"] == 2
    services["file_service"].count_versions.assert_called_with(
        "FILE-ACTUAL", object_type="INVOICE", object_id="42"
    )
    assert snapshot["counts"]["genericTableRows"] == 1

    selectors = dict(snapshot["identities"])
    selectors["fileId"] = "CALLER-ECHO-MUST-NOT-WIN"
    rebuilt = helper.build_snapshot(services, selectors)
    assert rebuilt["identities"]["fileId"] == "FILE-ACTUAL"

    services["project_service"].get.return_value = {
        **services["project_service"].get.return_value, "status": "ACTIVE"
    }
    with pytest.raises(helper.AcceptanceContractError, match="completed lifecycle"):
        helper.build_snapshot(services, snapshot["identities"])
