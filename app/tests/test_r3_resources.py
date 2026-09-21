from datetime import datetime, timezone
from io import BytesIO
import re
import subprocess

import openpyxl
import pytest

from app.tests.test_legacy_modules import expert_engine, equipment_service, equipment_routes


@pytest.mark.parametrize("kind", ["equipment", "host", "group"])
def test_exports_accept_timezone_aware_dates(kind, equipment_routes, equipment_service, monkeypatch):
    now = datetime(2026, 9, 9, 8, 30, tzinfo=timezone.utc)
    equipment = equipment_service.create_equipment({"name": "导出设备", "price": 3})
    if kind == "equipment":
        row = equipment_service.export_equipment()[0]
        row["created_at"] = now
        monkeypatch.setattr(equipment_service, "export_equipment", lambda: [row])
        url, cells = "/equipment/export", {"N2": now.isoformat(sep=" ")}
    elif kind == "host":
        equipment_service.create_host_device({"name": "导出宿主"})
        row = equipment_service.export_host_devices()[0]
        row.update(created_at=now, updated_at=now)
        monkeypatch.setattr(equipment_service, "export_host_devices", lambda: [row])
        url, cells = "/equipment/hosts/export", {"I2": now.isoformat(sep=" "), "J2": now.isoformat(sep=" ")}
    else:
        group = equipment_service.create_equipment_group("导出课题", "测试员")
        equipment_service.add_group_member(group["group_id"], equipment["equipment_id"], quantity=3)
        row = equipment_service.get_equipment_group(group["group_id"])
        row["created_at"] = now
        row["members"][0]["selected_at"] = now
        monkeypatch.setattr(equipment_service, "get_equipment_group", lambda group_id: row)
        url = "/equipment/groups/export/" + group["group_id"]
        cells = {"B3": now.isoformat(sep=" "), "P6": now.isoformat(sep=" "), "N6": 3}
    response = equipment_routes.get(url)
    assert response.status_code == 200
    sheet = openpyxl.load_workbook(BytesIO(response.data)).active
    for cell, expected in cells.items():
        assert sheet[cell].value == expected


def test_group_count_sums_quantities_and_keeps_empty_groups(equipment_service):
    empty = equipment_service.create_equipment_group("空组", "测试员")
    group = equipment_service.create_equipment_group("有设备组", "测试员")
    for name, quantity in [("设备甲", 3), ("设备乙", 2)]:
        equipment = equipment_service.create_equipment({"name": name})
        equipment_service.add_group_member(group["group_id"], equipment["equipment_id"], quantity=quantity)
    result = equipment_service.list_equipment_groups()
    counts = {row["group_id"]: row["member_count"] for row in result["items"]}
    assert result["total"] == 2
    assert counts == {empty["group_id"]: 0, group["group_id"]: 5}


def test_group_detail_distinguishes_kinds_and_units(equipment_routes, equipment_service):
    group = equipment_service.create_equipment_group("数量课题", "测试员")
    equipment = equipment_service.create_equipment({"name": "数量设备"})
    url = "/equipment/groups/edit/" + group["group_id"]
    assert "已选择设备 (0种，共0台)" in equipment_routes.get(url).get_data(as_text=True)
    equipment_service.add_group_member(group["group_id"], equipment["equipment_id"], quantity=2)
    assert "已选择设备 (1种，共2台)" in equipment_routes.get(url).get_data(as_text=True)


def test_shelf_product_edit_preserves_selected_status(equipment_routes, equipment_service):
    equipment = equipment_service.create_equipment({"name": "货架设备", "tech_status": "货架产品"})
    response = equipment_routes.get("/equipment/edit/" + equipment["equipment_id"])
    assert response.status_code == 200
    assert re.search(r'<option value="货架产品"\s+selected>', response.get_data(as_text=True))
    saved = equipment_routes.post("/equipment/edit/" + equipment["equipment_id"], data={
        "name": "货架设备改名", "tech_status": "货架产品",
    })
    assert saved.status_code == 302
    assert equipment_service.get_equipment(equipment["equipment_id"])["tech_status"] == "货架产品"


def test_host_modal_uses_registered_search_route(equipment_routes, equipment_service):
    host = equipment_service.create_host_device({"name": "检索宿主"})
    equipment_service.create_equipment({"name": "检索目标", "category": "通用设备"})
    html = equipment_routes.get("/equipment/hosts/detail/" + host["host_id"]).get_data(as_text=True)
    route = re.search(r'let url = `([^?]+)\?keyword=', html).group(1)
    response = equipment_routes.get(route + "?keyword=检索目标&category=通用设备&page=1")
    assert response.status_code == 200
    assert response.get_json()["data"][0]["name"] == "检索目标"


def test_host_pagination_keeps_quoted_search_values(equipment_routes, equipment_service):
    host = equipment_service.create_host_device({"name": "分页宿主"})
    html = equipment_routes.get("/equipment/hosts/detail/" + host["host_id"]).get_data(as_text=True)
    function = html.split("function renderDeviceList(", 1)[1].split("function confirmRelation()", 1)[0]
    script = r"""
const assert = require('node:assert/strict');
let calls = [];
const pagination = {
    set innerHTML(html) {
        this.buttons = [...html.matchAll(/<button\b([^>]*)>/g)].map(([, attrs]) => {
            const inline = attrs.match(/onclick="([^"]*)"/);
            return {
                dataset: { page: attrs.match(/data-page="([^"]*)"/)?.[1] },
                click: inline ? () => eval(inline[1]) : null,
                addEventListener(event, listener) { this.click = listener; }
            };
        });
    },
    querySelectorAll() { return this.buttons; }
};
const document = {
    getElementById(id) { return id === 'modal-pagination' ? pagination : {}; },
    querySelectorAll() { return []; }
};
function loadDeviceList(...args) { calls.push(args); }
""" + "function renderDeviceList(" + function + r"""
const keyword = "O'Brien";
const category = "通用'设备";
renderDeviceList([], 41, 2, 3, keyword, category);
assert.equal(pagination.buttons.length, 2);
pagination.buttons.forEach(button => button.click());
assert.deepEqual(calls, [[keyword, category, 1], [keyword, category, 3]]);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_host_modal_search_button_uses_current_filters(equipment_routes, equipment_service):
    host = equipment_service.create_host_device({"name": "筛选宿主"})
    html = equipment_routes.get("/equipment/hosts/detail/" + host["host_id"]).get_data(as_text=True)
    handler = re.search(r'onclick="(modalSearch\(\))"', html).group(1)
    function = re.search(r'function modalSearch\(\) \{.*?\n\}', html, re.S)
    assert function is not None, "宿主弹窗搜索按钮调用的函数未定义"
    script = r"""
const assert = require('node:assert/strict');
const document = { getElementById(id) {
    return { value: id === 'modal-keyword' ? "O'Brien" : '密码设备' };
}};
let calls = [];
function loadDeviceList(...args) { calls.push(args); }
""" + function.group(0) + "\n" + handler + r""";
assert.deepEqual(calls, [["O'Brien", '密码设备', 1]]);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
