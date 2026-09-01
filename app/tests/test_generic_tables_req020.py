"""REQ-020 端到端测试：current 版本概念 + save_snapshot / rollback / 导入限制"""
import sys
import os
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from app.models_generic_tables import GenericTableModel, get_db

BASE = 'http://127.0.0.1:5001'


def login():
    s = requests.Session()
    r = s.post(f'{BASE}/auth/login', data={'username': 'admin', 'password': 'admin123'}, allow_redirects=False)
    assert r.status_code == 302, f'login failed: {r.status_code}'
    return s


def _get_first_table_id(s):
    """从 /tables list 页拿到第一个 GT 开头的 table_id"""
    r = s.get(f'{BASE}/tables')
    import re
    m = re.search(r'GT[0-9a-f]{6,}', r.text)
    return m.group(0) if m else None


def test_req020_data_model():
    """REQ-020 §1: current_version_id 字段 + 4 个新方法存在"""
    m = GenericTableModel()
    # 字段存在
    cols = [r[1] for r in get_db().execute('PRAGMA table_info(generic_tables)').fetchall()]
    assert 'current_version_id' in cols, 'current_version_id 字段不存在'

    # 4 个新方法存在
    for method in ['set_current_version', 'get_by_id_with_current', 'save_snapshot', 'rollback_to']:
        assert hasattr(m, method), f'{method} 方法不存在'

    print('✅ test_req020_data_model')


def test_req020_migrate_old_data():
    """REQ-020 §1: 存量表格有 current_version_id"""
    conn = get_db()
    c = conn.cursor()
    rows = c.execute('SELECT table_id, current_version_id FROM generic_tables').fetchall()
    assert len(rows) > 0, '没有表格'
    for r in rows:
        assert r[1] is not None and r[1] != '', f'表格 {r[0]} 没有 current_version_id'
        # current 指向的版本存在
        v = c.execute('SELECT version_id, is_locked FROM generic_table_versions WHERE version_id = ?', (r[1],)).fetchone()
        assert v is not None, f'表格 {r[0]} current 指向的版本不存在'
    conn.close()
    print(f'✅ test_req020_migrate_old_data ({len(rows)} 个表格全部有 current)')


def test_req020_save_snapshot():
    """REQ-020 §3: save_snapshot 流程"""
    s = login()
    table_id = _get_first_table_id(s)
    assert table_id, '没拿到 table_id'

    # 取 current
    r = s.get(f'{BASE}/api/generic-tables/{table_id}/versions')
    versions = r.json().get('versions', [])
    cur = next((v for v in versions if v['is_locked'] == 0), None)
    assert cur, '没有可编辑的当前版本'

    # 保存快照
    r = s.post(f'{BASE}/api/generic-tables/{table_id}/versions',
               json={'method': 'snapshot', 'source_version_id': cur['version_id'], 'label': 'pytest_test', 'note': 'pytest'})
    assert r.status_code == 200, f'save_snapshot failed: {r.status_code} {r.text}'
    data = r.json()
    assert 'snapshot_id' in data
    assert 'new_current_id' in data
    snapshot_id = data['snapshot_id']
    new_current_id = data['new_current_id']

    # 验证 DB: new_current_id 真的成为了 current
    conn = get_db()
    cur_db = conn.execute('SELECT current_version_id FROM generic_tables WHERE table_id = ?', (table_id,)).fetchone()[0]
    assert cur_db == new_current_id, f'current 切换失败: {cur_db} != {new_current_id}'

    # 验证新 snapshot is_locked=1
    snap = conn.execute('SELECT is_locked FROM generic_table_versions WHERE version_id = ?', (snapshot_id,)).fetchone()
    assert snap[0] == 1, f'snapshot is_locked 应为 1, 实际 {snap[0]}'

    # 验证 new_current is_locked=0
    new = conn.execute('SELECT is_locked FROM generic_table_versions WHERE version_id = ?', (new_current_id,)).fetchone()
    assert new[0] == 0, f'new_current is_locked 应为 0, 实际 {new[0]}'
    conn.close()
    print('✅ test_req020_save_snapshot')


def test_req020_rollback():
    """REQ-020 §3: rollback 流程"""
    s = login()
    table_id = _get_first_table_id(s)
    assert table_id, '没拿到 table_id'

    # 拿一个历史快照
    r = s.get(f'{BASE}/api/generic-tables/{table_id}/versions')
    versions = r.json().get('versions', [])
    snapshot = next((v for v in versions if v['is_locked'] == 1), None)
    assert snapshot, '没有历史快照可回滚'

    # 回滚
    r = s.post(f'{BASE}/api/generic-tables/{table_id}/rollback',
               json={'source_version_id': snapshot['version_id']})
    assert r.status_code == 200, f'rollback failed: {r.status_code} {r.text}'
    data = r.json()
    assert 'new_current_id' in data

    # 验证 new_current is_locked=0
    conn = get_db()
    new = conn.execute('SELECT is_locked FROM generic_table_versions WHERE version_id = ?', (data['new_current_id'],)).fetchone()
    assert new[0] == 0
    conn.close()
    print('✅ test_req020_rollback')


def test_req020_import_locked_rejected():
    """REQ-020 §3: 导入到 is_locked=1 版本应返回 403"""
    s = login()
    table_id = _get_first_table_id(s)
    assert table_id, '没拿到 table_id'

    # 拿一个 locked 版本
    r = s.get(f'{BASE}/api/generic-tables/{table_id}/versions')
    versions = r.json().get('versions', [])
    locked = next((v for v in versions if v['is_locked'] == 1), None)
    assert locked, '没有 locked 版本可测'

    # 导入
    r = s.post(f'{BASE}/api/generic-tables/versions/{locked["version_id"]}/rows/import',
               files={'file': ('test.xlsx', b'fake', 'application/vnd.openxmlformats')})
    assert r.status_code == 403, f'期望 403, 实际 {r.status_code}: {r.text}'
    assert '当前编辑版本' in r.json().get('error', ''), f'错误消息不对: {r.json()}'
    print('✅ test_req020_import_locked_rejected')


def test_req020_detail_page_current():
    """REQ-020 §3: detail 页用 current_version，is_current=True"""
    s = login()
    table_id = _get_first_table_id(s)
    assert table_id, '没拿到 table_id'

    r = s.get(f'{BASE}/tables/{table_id}')
    assert r.status_code == 200
    assert 'is_current' in r.text or '当前编辑' in r.text, 'detail 页应包含 current 编辑标识'
    print('✅ test_req020_detail_page_current')


def test_req020_version_readonly_page():
    """REQ-020 §3: version 只读页用 is_current=False"""
    s = login()
    table_id = _get_first_table_id(s)
    assert table_id, '没拿到 table_id'

    # 拿一个历史快照
    r = s.get(f'{BASE}/api/generic-tables/{table_id}/versions')
    versions = r.json().get('versions', [])
    snapshot = next((v for v in versions if v['is_locked'] == 1), None)
    if not snapshot:
        print('⚠️ test_req020_version_readonly_page SKIP: 无历史快照')
        return

    r = s.get(f'{BASE}/tables/{table_id}/version/{snapshot["version_id"]}/')
    assert r.status_code == 200
    assert '只读快照' in r.text, 'version 只读页应包含"只读快照"标识'
    # 不应包含"添加行"按钮（onclick 形式，不含 JS 函数定义）
    assert 'onclick="addRow()"' not in r.text, '只读页不该有"添加行"按钮（onclick）'
    # 不应包含"导入Excel"按钮
    assert 'onchange="importExcelNew(this)"' not in r.text, '只读页不该有"导入Excel"按钮'
    # 不应包含"保存快照"按钮（onclick 形式）
    assert 'onclick="openSaveSnapshotModal()"' not in r.text, '只读页不该有"保存快照"按钮（onclick）'
    # 不应包含"快速保存"按钮
    assert 'onclick="quickSaveSnapshot()"' not in r.text, '只读页不该有"快速保存"按钮'
    print('✅ test_req020_version_readonly_page')


def test_req020_list_page():
    """REQ-020 §3: list 页显示 current 版本信息"""
    s = login()
    r = s.get(f'{BASE}/tables')
    assert r.status_code == 200
    # 应包含"当前版本"标识
    assert '当前版本' in r.text, 'list 页应包含"当前版本"标识'
    print('✅ test_req020_list_page')


if __name__ == '__main__':
    test_req020_data_model()
    test_req020_migrate_old_data()
    test_req020_save_snapshot()
    test_req020_rollback()
    test_req020_import_locked_rejected()
    test_req020_detail_page_current()
    test_req020_version_readonly_page()
    test_req020_list_page()
    print()
    print('=== REQ-020 全部测试通过 ===')
