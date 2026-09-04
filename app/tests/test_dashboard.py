from app import create_app
from app.dashboard import build_dashboard_context
from app.services.proposals import ProposalServiceError


class ListingService:
    def __init__(self, by_status, latest):
        self.by_status = by_status
        self.latest = latest
        self.calls = []

    def list(self, **values):
        self.calls.append(values)
        status = values.get("status")
        if status is None:
            return {"items": self.latest[: values["page_size"]], "total": len(self.latest)}
        items = self.by_status.get(status, [])
        return {"items": items[: values["page_size"]], "total": len(items)}


def test_dashboard_uses_bounded_real_service_results():
    proposals = ListingService(
        {
            "DRAFT": [{
                "businessId": "TP-001", "title": "设备适配想法",
                "status": "DRAFT", "updatedAt": "2026-09-04T10:00:00+08:00",
            }],
            "ARGUMENTATION": [{
                "businessId": "TP-002", "title": "复杂环境研究",
                "status": "ARGUMENTATION", "updatedAt": "2026-09-04T09:00:00+08:00",
            }],
        },
        [{
            "businessId": "TP-001", "title": "设备适配想法",
            "status": "DRAFT", "updatedAt": "2026-09-04T10:00:00+08:00",
        }],
    )
    projects = ListingService(
        {
            "ACTIVE": [{
                "id": "registry-1", "businessId": "KY-001", "name": "保障研究",
                "status": "ACTIVE", "updatedAt": "2026-09-04T11:00:00+08:00",
            }],
            "CLOSING": [],
        },
        [{
            "id": "registry-1", "businessId": "KY-001", "name": "保障研究",
            "status": "ACTIVE", "updatedAt": "2026-09-04T11:00:00+08:00",
        }],
    )

    result = build_dashboard_context(proposals, projects)

    assert result["summary"] == {
        "draftProposals": 1, "argumentationProposals": 1,
        "activeProjects": 1, "closingProjects": 0,
    }
    assert [item["businessId"] for item in result["workItems"]] == [
        "KY-001", "TP-001", "TP-002",
    ]
    assert result["workItems"][0]["url"] == "/projects/registry-1/overview"
    assert result["recentUpdates"][0]["businessId"] == "KY-001"
    assert result["unavailable"] is False
    assert all(call["page"] == 1 for call in proposals.calls + projects.calls)
    assert all(call["page_size"] <= 5 for call in proposals.calls + projects.calls)
    assert all(
        call.get("category") == "GENERAL_RESEARCH" for call in projects.calls
    )


def test_dashboard_degrades_to_manual_links_when_services_are_unavailable():
    class Unavailable:
        def list(self, **_values):
            raise ProposalServiceError("UNAVAILABLE", "database unavailable", 503)

    result = build_dashboard_context(Unavailable(), None)

    assert result["summary"] == {
        "draftProposals": None, "argumentationProposals": None,
        "activeProjects": None, "closingProjects": None,
    }
    assert result["workItems"] == []
    assert result["recentUpdates"] == []
    assert result["unavailable"] is True


def test_dashboard_does_not_hide_programming_errors():
    class Broken:
        def list(self, **_values):
            raise RuntimeError("unexpected bug")

    try:
        build_dashboard_context(Broken(), None)
    except RuntimeError as error:
        assert str(error) == "unexpected bug"
    else:
        raise AssertionError("unexpected programming errors must propagate")


def test_dashboard_marks_only_failed_summary_as_unavailable():
    class Partial:
        def list(self, **values):
            if values.get("status") == "DRAFT":
                raise ProposalServiceError("UNAVAILABLE", "temporary", 503)
            return {"items": [], "total": 0}

    result = build_dashboard_context(Partial(), None)

    assert result["summary"]["draftProposals"] is None
    assert result["summary"]["argumentationProposals"] == 0
    assert result["unavailable"] is False


def test_dashboard_route_renders_real_counts_and_names(tmp_path):
    proposals = ListingService({
        "DRAFT": [{
            "businessId": "TP-009", "title": "现场保障想法",
            "status": "DRAFT", "updatedAt": "2026-09-04T10:00:00+08:00",
        }],
        "ARGUMENTATION": [],
    }, [])
    projects = ListingService({"ACTIVE": [], "CLOSING": []}, [])
    app = create_app({
        "TESTING": True, "SECRET_KEY": "dashboard-test-secret",
        "DATA_DIR": str(tmp_path / "data"),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "DOCUMENTS_DIR": str(tmp_path / "documents"),
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "SECURITY_AUTH_ENABLED": False, "CSRF_ENABLED": False,
        "PROPOSAL_SERVICE": proposals, "PROJECT_SERVICE": projects,
        "LOG_FILE": None,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=7, user="zhang", name="张老师", role="BUSINESS_USER")

    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "现场保障想法" in body
    assert "TP-009" in body
    assert "待继续提案</span><strong>1</strong>" in body
    assert "张老师" in body
