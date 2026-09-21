from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import openpyxl

from app.tests.test_legacy_modules import expert_engine, equipment_service, equipment_routes


def test_group_list_display_preserves_source_and_unknown_creator(equipment_routes, equipment_service, monkeypatch):
    created = datetime(2026, 9, 9, 8, 30, 45, 123456, tzinfo=timezone.utc)
    items = [dict(group_id="fixture-1", project_name="显示测试", project_id=None,
                  member_count=0, creator="qa_fixture", created_at=created),
             dict(group_id="fixture-2", project_name="旧创建人", project_id=None,
                  member_count=0, creator="历史记录人", created_at=None)]
    monkeypatch.setattr(equipment_service, "list_equipment_groups", lambda **kwargs: {"items": items, "total": 2})
    monkeypatch.setitem(equipment_routes.application.extensions, "users_repository", SimpleNamespace(
        list_accounts=lambda: [{"username": "qa_fixture", "name": "张老师"}]))
    response = equipment_routes.get("/equipment/groups/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "<td>张老师</td>" in html
    assert "<td>历史记录人</td>" in html
    assert ">2026-09-09 08:30</time>" in html
    assert 'title="2026-09-09 08:30:45.123456+00:00"' in html
    assert items[0]["creator"] == "qa_fixture"
    assert items[0]["created_at"] is created


def test_group_list_without_account_repository_keeps_creator(equipment_routes, equipment_service, monkeypatch):
    monkeypatch.delitem(equipment_routes.application.extensions, "users_repository", raising=False)
    monkeypatch.setattr(equipment_service, "list_equipment_groups", lambda **kwargs: {"items": [
        dict(group_id="fixture", project_name="显示测试", project_id=None, member_count=0,
             creator="旧系统用户", created_at="2026-09-09 08:30:45")], "total": 1})
    response = equipment_routes.get("/equipment/groups/")
    assert response.status_code == 200
    assert "<td>旧系统用户</td>" in response.get_data(as_text=True)


def test_group_export_still_preserves_raw_creator_microseconds_and_timezone(equipment_routes, equipment_service, monkeypatch):
    created = datetime(2026, 9, 9, 8, 30, 45, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(equipment_service, "get_equipment_group", lambda group_id: dict(
        project_name="导出测试", creator="qa_fixture", created_at=created, members=[]))
    response = equipment_routes.get("/equipment/groups/export/fixture")
    assert response.status_code == 200
    sheet = openpyxl.load_workbook(BytesIO(response.data)).active
    assert sheet["B2"].value == "qa_fixture"
    assert sheet["B3"].value == "2026-09-09 08:30:45.123456+00:00"
