import re

import pytest

from app.tests.test_legacy_modules import expert_engine, equipment_service, equipment_routes


@pytest.mark.parametrize('operation,label', [('更新设备','更新设备'), ('导入文件','导入文件'), ('PREVIEW','预览文件')])
def test_existing_log_operations_are_filterable_and_keep_other_filters(equipment_routes, equipment_service, monkeypatch, operation, label):
    calls = []
    def logs(module, **filters):
        calls.append((module, filters))
        return [dict(timestamp='2026-09-08 16:59', operator='张老师', operation_type=operation,
                     file_name='演练设备', detail='')]
    monkeypatch.setattr(equipment_service, 'list_logs', logs)
    response = equipment_routes.get('/equipment/logs/equipment', query_string={
        'operation_type':operation, 'operator':'张老师', 'file_name':'演练设备',
        'start_date':'2026-09-08','end_date':'2026-09-09'})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    form = re.search(r'<form\b[^>]*method="get"[^>]*>(.*?)</form>', html, re.S).group(1)
    assert 'name="operation_type"' in form
    assert re.search(rf'<option value="{operation}"\s+selected>{label}</option>', form)
    for value in ['张老师','演练设备','2026-09-08','2026-09-09']:
        assert f'value="{value}"' in form
    assert label in html and '<td>-</td>' in html
    assert calls == [('equipment', dict(operation=operation,operator='张老师',file_name='演练设备',start_date='2026-09-08',end_date='2026-09-09'))]
    if operation == 'PREVIEW':
        assert 'title="PREVIEW">预览文件</span>' in html
