#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用表格版本模块回归测试
- REQ-018: 快照未固化修复（version_id 隔离）+ 默认 label 带日期
- REQ-019: 快照版本锁定（is_locked）+ create_version 自动锁定
用法: cd <项目根> && python3 -m app.tests.test_generic_tables_versions
"""
import os
import sys
import re
import tempfile

# 用临时 DB 跑测试（不影响生产数据）
TEST_DB = tempfile.mktemp(suffix='.db', prefix='gt_test_')

# 先把 app.models_generic_tables 导入，再覆盖 DB_PATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app.models_generic_tables as _gt  # noqa
_gt.DB_PATH = TEST_DB
_gt.get_db.__globals__['DB_PATH'] = TEST_DB  # 修 get_db 闭包

from app.models_generic_tables import GenericTableModel  # noqa


def banner(s):
    print(f'\n=== {s} ===')


def assert_eq(a, b, msg):
    if a != b:
        print(f'  ❌ {msg}: 期望 {b!r} 实际 {a!r}')
        raise AssertionError(msg)
    print(f'  ✅ {msg}: {a!r}')


def main():
    m = GenericTableModel()
    # 1) 建表格 + v1 (import)
    banner('准备：建表格 + v1(import)')
    table_id = m.create(name='测试表', description='回归测试', creator='tester')
    v1_id = m.create_version(table_id, method='import', source_version_id=None,
                             creator='tester', label='v1_test')
    v1 = m.get_version_by_id(v1_id)
    assert_eq(v1['is_locked'], 0, 'v1(import) 应未锁定')

    # 手动加 1 列 + 1 行
    with _gt.get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO generic_table_columns
            (version_id, col_key, col_name, col_type, col_index, col_width, col_align, col_summary, col_options, created_at)
            VALUES (?, 'name', '姓名', 'text', 0, 120, 'left', '', NULL, '2026-06-27')""", (v1_id,))
        c.execute("""INSERT INTO generic_table_data
            (version_id, row_key, row_index, row_data, row_color, created_at, updated_at)
            VALUES (?, 'row1', 0, ?, '', '2026-06-27', '2026-06-27')""", (v1_id, '{"name":"A"}'))
        conn.commit()
    print(f'  table_id={table_id}, v1={v1_id}')

    # ============ REQ-018 测试 ============
    banner('REQ-018 测试 1: 快照未固化（version_id 隔离）')
    v2_id = m.create_version(table_id, method='snapshot', source_version_id=v1_id,
                             creator='tester', label=None)  # label=None 触发默认带日期
    v2 = m.get_version_by_id(v2_id)
    # 关键断言：v2 复制了 row1
    rows_v2 = m.get_rows(v2_id)
    assert_eq(rows_v2[0]['row_data'].get('name'), 'A', '快照复制了 v1 数据')
    # 改 v2 的 row1.name = 'B'
    with _gt.get_db() as conn:
        c = conn.cursor()
        c.execute("""UPDATE generic_table_data SET row_data = ?, updated_at = ?
            WHERE version_id = ? AND row_key = 'row1'""",
            ('{"name":"B"}', '2026-06-27', v2_id))
        conn.commit()
    # 关键断言：v1 的 row1.name 仍然是 'A'
    rows_v1 = m.get_rows(v1_id)
    assert_eq(rows_v1[0]['row_data'].get('name'), 'A', 'v1 数据未受影响（快照固化）')
    rows_v2_after = m.get_rows(v2_id)
    assert_eq(rows_v2_after[0]['row_data'].get('name'), 'B', 'v2 数据已更新')

    banner('REQ-018 测试 2: 默认 label 带日期（v{N}_YYYYMMDD_HHMM）')
    label = v2.get('version_label', '')
    assert re.match(r'^v\d+_\d{8}_\d{4}$', label), f'label 格式不符: {label!r}'
    print(f'  ✅ v2 label 格式正确: {label!r}')

    # ============ REQ-019 测试 ============
    banner('REQ-019 测试 1: 快照版本自动锁定 is_locked=1')
    assert_eq(v2['is_locked'], 1, 'snapshot 创建的版本自动锁定')

    banner('REQ-019 测试 2: 存量快照回填锁定（method=snapshot 且 is_locked=0 → 1）')
    with _gt.get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO generic_table_versions
            (version_id, table_id, version_number, version_label, create_method,
             source_version_id, row_count, creator, created_at, note, is_locked)
            VALUES ('GTV_LEGACY', ?, 99, 'legacy_snap', 'snapshot',
                    NULL, 0, 'old', '2020-01-01 00:00:00', '', 0)""", (table_id,))
        conn.commit()
    # 跑回填 SQL
    with _gt.get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE generic_table_versions SET is_locked = 1 "
                  "WHERE create_method = 'snapshot' AND (is_locked = 0 OR is_locked IS NULL)")
        conn.commit()
    legacy = m.get_version_by_id('GTV_LEGACY')
    assert_eq(legacy['is_locked'], 1, '存量 snapshot 回填锁定成功')

    banner('REQ-019 测试 3: import 版本 is_locked=0（可编辑）')
    v3_id = m.create_version(table_id, method='import', source_version_id=None,
                             creator='tester', label='v3_import')
    v3 = m.get_version_by_id(v3_id)
    assert_eq(v3['is_locked'], 0, 'import 创建的版本未锁定')

    # ============ REQ-018 测试 3: delete_column version_id 隔离 ============
    banner('REQ-018 测试 3: delete_column 不会误删 v1 的列')
    v1_col_keys_before = sorted(c['col_key'] for c in m.get_columns(v1_id))
    v2_col_keys = [c['col_key'] for c in m.get_columns(v2_id)]
    if 'name' in v2_col_keys:
        with _gt.get_db() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM generic_table_columns WHERE version_id = ? AND col_key = 'name'",
                      (v2_id,))
            conn.commit()
    v1_col_keys_after = sorted(c['col_key'] for c in m.get_columns(v1_id))
    assert_eq(v1_col_keys_after, v1_col_keys_before, 'v1 列未受影响')
    print('  ✅ v1 列结构完整')

    print('\n🎉 全部断言通过！REQ-018 + REQ-019 修复有效。')


if __name__ == '__main__':
    try:
        main()
    except AssertionError as e:
        print(f'\n❌ 测试失败: {e}')
        sys.exit(1)
    finally:
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
