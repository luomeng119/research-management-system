from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from app.services.projects import ProjectServiceError
from app.services.proposals import ProposalServiceError


LOGGER = logging.getLogger(__name__)

PROPOSAL_STATUSES = {
    "DRAFT": ("草稿", "继续填写"),
    "ARGUMENTATION": ("论证中", "记录论证"),
}
PROJECT_STATUSES = {
    "ACTIVE": ("执行中", "记录进展"),
    "CLOSING": ("结题中", "继续结题"),
}


def _list(service, *, status=None, page_size=5, **filters):
    if service is None:
        return None
    try:
        return service.list(
            page=1, page_size=page_size, status=status, **filters
        )
    except (ProposalServiceError, ProjectServiceError, SQLAlchemyError) as error:
        LOGGER.warning("dashboard list unavailable: %s", type(error).__name__)
        return None


def _proposal_item(item):
    label, action = PROPOSAL_STATUSES[item["status"]]
    business_id = item["businessId"]
    target = "edit" if item["status"] == "DRAFT" else ""
    return {
        "kind": "提案", "businessId": business_id, "name": item["title"],
        "status": item["status"], "statusLabel": label, "action": action,
        "updatedAt": item.get("updatedAt") or "",
        "url": f"/proposals/{business_id}/{target}".rstrip("/"),
    }


def _project_item(item):
    label, action = PROJECT_STATUSES[item["status"]]
    return {
        "kind": "项目", "businessId": item["businessId"], "name": item["name"],
        "status": item["status"], "statusLabel": label, "action": action,
        "updatedAt": item.get("updatedAt") or "",
        "url": f"/projects/{item['id']}/overview",
    }


def _newest(items, limit=5):
    return sorted(items, key=lambda item: item.get("updatedAt") or "", reverse=True)[:limit]


def build_dashboard_context(proposal_service, project_service):
    proposal_lists = {
        status: _list(proposal_service, status=status, page_size=5)
        for status in PROPOSAL_STATUSES
    }
    project_lists = {
        status: _list(
            project_service, status=status, page_size=5,
            category="GENERAL_RESEARCH",
        )
        for status in PROJECT_STATUSES
    }
    latest_proposals = _list(proposal_service, page_size=5)
    latest_projects = _list(
        project_service, page_size=5, category="GENERAL_RESEARCH"
    )

    work_items = []
    for status, result in proposal_lists.items():
        if result:
            work_items.extend(
                _proposal_item(item) for item in result["items"]
                if item.get("status") == status
            )
    for status, result in project_lists.items():
        if result:
            work_items.extend(
                _project_item(item) for item in result["items"]
                if item.get("status") == status
            )

    recent = []
    if latest_proposals:
        recent.extend(
            _proposal_item(item) for item in latest_proposals["items"]
            if item.get("status") in PROPOSAL_STATUSES
        )
    if latest_projects:
        recent.extend(
            _project_item(item) for item in latest_projects["items"]
            if item.get("status") in PROJECT_STATUSES
        )

    return {
        "summary": {
            "draftProposals": (
                proposal_lists["DRAFT"]["total"]
                if proposal_lists["DRAFT"] is not None else None
            ),
            "argumentationProposals": (
                proposal_lists["ARGUMENTATION"]["total"]
                if proposal_lists["ARGUMENTATION"] is not None else None
            ),
            "activeProjects": (
                project_lists["ACTIVE"]["total"]
                if project_lists["ACTIVE"] is not None else None
            ),
            "closingProjects": (
                project_lists["CLOSING"]["total"]
                if project_lists["CLOSING"] is not None else None
            ),
        },
        "workItems": _newest(work_items),
        "recentUpdates": _newest(recent),
        "unavailable": not any([
            latest_proposals, latest_projects, *proposal_lists.values(),
            *project_lists.values(),
        ]),
    }
