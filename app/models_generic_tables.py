# -*- coding: utf-8 -*-
"""
通用表格管理模块 - 数据模型
支持多版本、动态列、Excel导入导出
"""
import os
import sqlite3
import json
import uuid
from datetime import datetime
from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, 'generic_tables.db')

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库表"""
    # REQ-016 安全加固：启动时自动检测/修复索引损坏
    # 防止 race condition 写入导致 sqlite_autoindex 索引条目数错位
    with get_db() as conn:
        c = conn.cursor()
        results = c.execute('PRAGMA integrity_check;').fetchall()
        if results and results[0][0] != 'ok':
            print(f'[WARN] DB 索引损坏: {results[0][0]}，自动 REINDEX 修复中...')
            try:
                c.execute('REINDEX;')
                conn.commit()
                # 再次校验
                results2 = c.execute('PRAGMA integrity_check;').fetchall()
                if results2 and results2[0][0] == 'ok':
                    print('[OK] REINDEX 修复成功')
                else:
                    print(f'[ERROR] REINDEX 修复失败: {results2}')
            except Exception as e:
                print(f'[ERROR] REINDEX 失败: {e}')
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS generic_tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            current_version_id TEXT  -- REQ-020: 指向当前编辑版本（is_locked=0）
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS generic_table_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT UNIQUE NOT NULL,
            table_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            version_label TEXT,
            create_method TEXT NOT NULL,
            source_version_id TEXT,
            row_count INTEGER DEFAULT 0,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            note TEXT,
            is_locked INTEGER DEFAULT 0,  -- REQ-019: 快照版本锁定标志（1=锁定行数据，0=可编辑）
            FOREIGN KEY (table_id) REFERENCES generic_tables(table_id)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS generic_table_columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL,
            col_key TEXT NOT NULL,
            col_name TEXT NOT NULL,
            col_type TEXT NOT NULL,
            col_index INTEGER NOT NULL,
            col_width INTEGER DEFAULT 120,
            col_align TEXT DEFAULT 'left',
            col_summary TEXT DEFAULT '',
            col_options TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (version_id) REFERENCES generic_table_versions(version_id),
            UNIQUE(version_id, col_key)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS generic_table_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL,
            row_key TEXT NOT NULL,
            row_index INTEGER NOT NULL,
            row_data TEXT NOT NULL,
            row_color TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (version_id) REFERENCES generic_table_versions(version_id),
            UNIQUE(version_id, row_key)
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_gv_table_id ON generic_table_versions(table_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_gc_version_id ON generic_table_columns(version_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_gd_version_id ON generic_table_data(version_id)')
        # 兼容老库：行表补 row_color 字段、版本表补 page_size 字段、is_locked 字段、表格补 current_version_id 字段（try/except 幂等）
        for ddl in [
            "ALTER TABLE generic_table_data ADD COLUMN row_color TEXT DEFAULT ''",
            "ALTER TABLE generic_table_versions ADD COLUMN page_size INTEGER DEFAULT 20",
            "ALTER TABLE generic_table_versions ADD COLUMN is_locked INTEGER DEFAULT 0",  # REQ-019
            "ALTER TABLE generic_tables ADD COLUMN current_version_id TEXT",  # REQ-020
        ]:
            try:
                c.execute(ddl)
            except Exception:
                pass  # 字段已存在
        # REQ-019: 存量快照回填 is_locked=1（method='snapshot' 的老快照视为锁定）
        try:
            c.execute("UPDATE generic_table_versions SET is_locked = 1 "
                      "WHERE create_method = 'snapshot' AND (is_locked = 0 OR is_locked IS NULL)")
        except Exception:
            pass
        # REQ-020: 存量表格回填 current_version_id
        # 规则 1：取最大版本号且 is_locked=0 的版本
        try:
            c.execute('''
                UPDATE generic_tables
                SET current_version_id = (
                    SELECT version_id FROM generic_table_versions
                    WHERE table_id = generic_tables.table_id
                      AND is_locked = 0
                    ORDER BY version_number DESC
                    LIMIT 1
                )
                WHERE current_version_id IS NULL OR current_version_id = ''
            ''')
        except Exception:
            pass
        # 规则 2（兜底）：所有版本都 is_locked=1 → 取最大版本号
        try:
            c.execute('''
                UPDATE generic_tables
                SET current_version_id = (
                    SELECT version_id FROM generic_table_versions
                    WHERE table_id = generic_tables.table_id
                    ORDER BY version_number DESC
                    LIMIT 1
                )
                WHERE current_version_id IS NULL OR current_version_id = ''
            ''')
        except Exception:
            pass
        # 规则 3（兜底）：没有版本（理论上不会发生）→ 创建 v1
        try:
            c.execute('''
                INSERT INTO generic_table_versions
                (version_id, table_id, version_number, version_label, create_method, row_count, creator, created_at, is_locked)
                SELECT 'GTV-MIGRATE-' || table_id, table_id, 1, 'v1', 'create', 0, 'migrate', datetime('now'), 0
                FROM generic_tables
                WHERE NOT EXISTS (
                    SELECT 1 FROM generic_table_versions
                    WHERE generic_table_versions.table_id = generic_tables.table_id
                )
            ''')
            c.execute('''
                UPDATE generic_tables
                SET current_version_id = 'GTV-MIGRATE-' || table_id
                WHERE current_version_id IS NULL OR current_version_id = ''
            ''')
        except Exception:
            pass
        # 规则 4（兜底）：current 指向的版本不存在（异常情况）→ 重新指向最大版本号
        try:
            c.execute('''
                UPDATE generic_tables
                SET current_version_id = (
                    SELECT version_id FROM generic_table_versions
                    WHERE table_id = generic_tables.table_id
                    ORDER BY version_number DESC
                    LIMIT 1
                )
                WHERE current_version_id NOT IN (SELECT version_id FROM generic_table_versions)
            ''')
        except Exception:
            pass
        conn.commit()

def _row_to_dict(c, row):
    cols = [d[0] for d in c.description]
    return dict(zip(cols, row)) if row else None

def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def _new_id(prefix):
    return prefix + datetime.now().strftime('%Y%m%d%H%M%S') + str(uuid.uuid4())[:4]


class GenericTableModel:
    def __init__(self):
        init_db()

    # ---------- 表格级CRUD ----------

    def get_all(self):
        """REQ-020: 返回所有表格（带最新版本 + current 版本信息）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''
                SELECT gt.*,
                       (SELECT COUNT(*) FROM generic_table_versions gtv WHERE gtv.table_id = gt.table_id) as version_count,
                       (SELECT gtv.version_number FROM generic_table_versions gtv
                        WHERE gtv.table_id = gt.table_id ORDER BY gtv.version_number DESC LIMIT 1) as latest_version_number,
                       (SELECT gtv.version_id FROM generic_table_versions gtv
                        WHERE gtv.table_id = gt.table_id ORDER BY gtv.version_number DESC LIMIT 1) as latest_version_id,
                       gtv.version_number as cur_version_number,
                       gtv.version_label as cur_version_label,
                       gtv.creator as cur_creator,
                       gtv.created_at as cur_created_at
                FROM generic_tables gt
                LEFT JOIN generic_table_versions gtv ON gtv.version_id = gt.current_version_id
                ORDER BY gt.updated_at DESC
            ''')
            return [_row_to_dict(c, r) for r in c.fetchall()]

    def get_by_id(self, table_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM generic_tables WHERE table_id = ?', (table_id,))
            row = c.fetchone()
            return _row_to_dict(c, row) if row else None

    def create(self, name, description, creator):
        """创建新表格，自动创建v1版本空结构（REQ-020: v1 即为 current）"""
        table_id = _new_id('GT')
        now = _now()
        with get_db() as conn:
            c = conn.cursor()
            # 自动创建 v1 空版本
            version_id = _new_id('GTV')
            c.execute('''INSERT INTO generic_table_versions
                (version_id, table_id, version_number, version_label, create_method, row_count, creator, created_at, is_locked)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (version_id, table_id, 1, 'v1', 'create', 0, creator, now, 0))
            # REQ-020: 表格直接指向 v1 作为 current
            c.execute('''INSERT INTO generic_tables (table_id, name, description, creator, created_at, updated_at, current_version_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (table_id, name, description or '', creator, now, now, version_id))
            conn.commit()
        return table_id

    def update(self, table_id, **kwargs):
        with get_db() as conn:
            c = conn.cursor()
            updates = []
            params = []
            for k, v in kwargs.items():
                if k in ('name', 'description'):
                    updates.append(f'{k} = ?')
                    params.append(v)
            if updates:
                updates.append('updated_at = ?')
                params.append(_now())
                params.append(table_id)
                c.execute(f'UPDATE generic_tables SET {", ".join(updates)} WHERE table_id = ?', params)
                conn.commit()
        return True

    def delete(self, table_id):
        """删除表格（含所有版本）"""
        with get_db() as conn:
            c = conn.cursor()
            # 先查所有版本
            c.execute('SELECT version_id FROM generic_table_versions WHERE table_id = ?', (table_id,))
            version_ids = [r['version_id'] for r in c.fetchall()]
            # 删行数据
            for vid in version_ids:
                c.execute('DELETE FROM generic_table_data WHERE version_id = ?', (vid,))
                c.execute('DELETE FROM generic_table_columns WHERE version_id = ?', (vid,))
            # 删版本
            c.execute('DELETE FROM generic_table_versions WHERE table_id = ?', (table_id,))
            # 删表格
            c.execute('DELETE FROM generic_tables WHERE table_id = ?', (table_id,))
            conn.commit()
        return True

    # ---------- 版本管理 ----------

    def get_versions(self, table_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM generic_table_versions
                WHERE table_id = ? ORDER BY version_number DESC''',
                (table_id,))
            return [_row_to_dict(c, r) for r in c.fetchall()]

    def get_version_by_id(self, version_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM generic_table_versions WHERE version_id = ?', (version_id,))
            row = c.fetchone()
            return _row_to_dict(c, row) if row else None

    def create_version(self, table_id, method, source_version_id, creator, label, note='', _conn=None):
        """
        创建新版本
        method='snapshot': 完整复制源版本的列结构和数据（REQ-020: is_locked=1 锁定为历史快照）
        method='import':   REQ-020: 内部用，复制源版本列+数据为新 working 副本（is_locked=0，调用 save_snapshot/rollback_to 时 source 必须传）
        method='rollback': REQ-020: 内部用，复制源版本为新 current（is_locked=0，同 import 行为）
        method='create':   REQ-020: 保留兼容，is_locked=0，source 不传
        _conn: REQ-020 新增，外部传入数据库连接（save_snapshot/rollback_to 用，事务原子性）
        """
        def _op(c):
            # 查当前最大版本号
            c.execute('SELECT MAX(version_number) as max_v FROM generic_table_versions WHERE table_id = ?', (table_id,))
            row = c.fetchone()
            new_version_number = (row['max_v'] or 0) + 1
            now = _now()
            new_version_id = _new_id('GTV')

            # REQ-020: 哪些 method 需要从 source 复制列+数据
            # snapshot: 复制（历史快照）
            # import/rollback: 复制（save_snapshot 和 rollback_to 内部用，复制 source 为新 working 副本）
            # create: 不复制（无 source）
            need_copy_source = method in ('snapshot', 'import', 'rollback') and source_version_id

            if need_copy_source:
                # 复制列定义
                c.execute('SELECT * FROM generic_table_columns WHERE version_id = ?', (source_version_id,))
                cols = [_row_to_dict(c, r) for r in c.fetchall()]
                for col in cols:
                    c.execute('''INSERT INTO generic_table_columns
                        (version_id, col_key, col_name, col_type, col_index, col_width, col_align, col_summary, col_options, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (new_version_id, col['col_key'], col['col_name'], col['col_type'],
                         col['col_index'], col['col_width'], col['col_align'], col['col_summary'],
                         col['col_options'], now))
                # 复制行数据
                c.execute('SELECT * FROM generic_table_data WHERE version_id = ?', (source_version_id,))
                rows = [_row_to_dict(c, r) for r in c.fetchall()]
                for row in rows:
                    c.execute('''INSERT INTO generic_table_data
                        (version_id, row_key, row_index, row_data, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)''',
                        (new_version_id, row['row_key'], row['row_index'], row['row_data'], now, now))
                row_count = len(rows)
            else:
                row_count = 0

            # REQ-020: is_locked 规则
            # snapshot → 1（历史快照，只读）
            # create/import/rollback → 0（可编辑 working 副本）
            is_locked = 1 if method == 'snapshot' else 0

            c.execute('''INSERT INTO generic_table_versions
                (version_id, table_id, version_number, version_label, create_method, source_version_id, row_count, creator, created_at, note, is_locked)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (new_version_id, table_id, new_version_number,
                 label or f'v{new_version_number}_{datetime.now().strftime("%Y%m%d_%H%M")}',
                 method, source_version_id if need_copy_source else None,
                 row_count, creator, now, note or '',
                 is_locked))
            # 更新表格更新时间
            c.execute('UPDATE generic_tables SET updated_at = ? WHERE table_id = ?', (now, table_id))
            return new_version_id

        if _conn is not None:
            return _op(_conn.cursor())
        else:
            with get_db() as conn:
                c = conn.cursor()
                new_id = _op(c)
                conn.commit()
            return new_id

    def delete_version(self, version_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM generic_table_data WHERE version_id = ?', (version_id,))
            c.execute('DELETE FROM generic_table_columns WHERE version_id = ?', (version_id,))
            c.execute('DELETE FROM generic_table_versions WHERE version_id = ?', (version_id,))
            conn.commit()
        return True

    # ---------- REQ-020: current 版本管理 ----------

    def get_by_id_with_current(self, table_id):
        """获取表格 + 当前版本信息（LEFT JOIN 一次性返回）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''
                SELECT gt.*,
                       gtv.version_number as cur_version_number,
                       gtv.version_label as cur_version_label,
                       gtv.create_method as cur_create_method,
                       gtv.row_count as cur_row_count,
                       gtv.is_locked as cur_is_locked,
                       gtv.creator as cur_creator,
                       gtv.created_at as cur_created_at
                FROM generic_tables gt
                LEFT JOIN generic_table_versions gtv ON gtv.version_id = gt.current_version_id
                WHERE gt.table_id = ?
            ''', (table_id,))
            row = c.fetchone()
            return _row_to_dict(c, row) if row else None

    def set_current_version(self, table_id, version_id):
        """设置表格的当前编辑版本（不校验 version 是否存在，调用方保证）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('UPDATE generic_tables SET current_version_id = ?, updated_at = ? WHERE table_id = ?',
                      (version_id, _now(), table_id))
            conn.commit()
        return True

    def save_snapshot(self, table_id, source_version_id, label, note, creator):
        """
        REQ-020: 保存快照流程
        1. 复制 source 为 snapshot (is_locked=1)
        2. 复制 source 为新 working 副本 (is_locked=0)
        3. 更新 current_version_id 指向新 working 副本
        返回 (snapshot_version_id, new_current_version_id)
        """
        with get_db() as conn:
            c = conn.cursor()
            # 1. 复制 source 为 snapshot
            snapshot_id = self.create_version(
                table_id=table_id, method='snapshot', source_version_id=source_version_id,
                creator=creator, label=label, note=note, _conn=conn  # 复用同一连接
            )
            # 2. 复制 source 为新 working 副本
            new_current_id = self.create_version(
                table_id=table_id, method='import', source_version_id=source_version_id,
                creator=creator, label=None, note='save_snapshot_new_working', _conn=conn
            )
            # 3. 更新 current_version_id
            c.execute('UPDATE generic_tables SET current_version_id = ? WHERE table_id = ?',
                      (new_current_id, table_id))
            conn.commit()
        return snapshot_id, new_current_id

    def rollback_to(self, table_id, source_version_id, creator):
        """
        REQ-020: 从历史快照回滚
        复制 source 为新 working 副本 (method='rollback', is_locked=0) + 更新 current_version_id
        """
        with get_db() as conn:
            c = conn.cursor()
            # 创建新 working 副本
            new_current_id = self.create_version(
                table_id=table_id, method='rollback', source_version_id=source_version_id,
                creator=creator, label=None, note=f'rollback_from_{source_version_id[:20]}', _conn=conn
            )
            # 更新 current_version_id
            c.execute('UPDATE generic_tables SET current_version_id = ? WHERE table_id = ?',
                      (new_current_id, table_id))
            conn.commit()
        return new_current_id

    # ---------- 列管理 ----------

    def get_columns(self, version_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM generic_table_columns WHERE version_id = ? ORDER BY col_index', (version_id,))
            rows = [_row_to_dict(c, r) for r in c.fetchall()]
            for r in rows:
                if r['col_options']:
                    r['col_options'] = json.loads(r['col_options'])
            return rows

    def upsert_column(self, version_id, col_key, col_name, col_type,
                      col_index=None, col_width=120, col_align='left',
                      col_summary='', col_options=None, _conn=None):
        """新增或更新列定义；传入 _conn 则复用该连接，不自己开关"""
        now = _now()
        if _conn is not None:
            c = _conn.cursor()
            c.execute('SELECT id FROM generic_table_columns WHERE version_id = ? AND col_key = ?',
                      (version_id, col_key))
            exists = c.fetchone()
            if exists:
                c.execute('''UPDATE generic_table_columns
                    SET col_name=?, col_type=?, col_width=?, col_align=?, col_summary=?, col_options=?
                    WHERE version_id=? AND col_key=?''',
                    (col_name, col_type, col_width, col_align, col_summary,
                     json.dumps(col_options) if col_options else None,
                     version_id, col_key))
            else:
                if col_index is None:
                    c.execute('SELECT MAX(col_index) as max_i FROM generic_table_columns WHERE version_id = ?', (version_id,))
                    row = c.fetchone()
                    # 修复：row['max_i'] 可能是 0，不能用 `or -1`（0 是 falsy）
                    max_i = row['max_i'] if row['max_i'] is not None else -1
                    col_index = max_i + 1
                c.execute('''INSERT INTO generic_table_columns
                    (version_id, col_key, col_name, col_type, col_index, col_width, col_align, col_summary, col_options, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (version_id, col_key, col_name, col_type, col_index, col_width, col_align,
                     col_summary, json.dumps(col_options) if col_options else None, now))
            return
        # 无外部连接：自己管连接
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id FROM generic_table_columns WHERE version_id = ? AND col_key = ?',
                      (version_id, col_key))
            exists = c.fetchone()
            if exists:
                c.execute('''UPDATE generic_table_columns
                    SET col_name=?, col_type=?, col_width=?, col_align=?, col_summary=?, col_options=?
                    WHERE version_id=? AND col_key=?''',
                    (col_name, col_type, col_width, col_align, col_summary,
                     json.dumps(col_options) if col_options else None,
                     version_id, col_key))
            else:
                if col_index is None:
                    c.execute('SELECT MAX(col_index) as max_i FROM generic_table_columns WHERE version_id = ?', (version_id,))
                    row = c.fetchone()
                    # 修复：row['max_i'] 可能是 0，不能用 `or -1`（0 是 falsy）
                    max_i = row['max_i'] if row['max_i'] is not None else -1
                    col_index = max_i + 1
                c.execute('''INSERT INTO generic_table_columns
                    (version_id, col_key, col_name, col_type, col_index, col_width, col_align, col_summary, col_options, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (version_id, col_key, col_name, col_type, col_index, col_width, col_align,
                     col_summary, json.dumps(col_options) if col_options else None, now))
            conn.commit()
        return True

    def delete_column(self, version_id, col_key):
        """删除列定义，并从所有行数据中移除该列"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM generic_table_columns WHERE version_id = ? AND col_key = ?', (version_id, col_key))
            # 从行数据中移除该列
            c.execute('SELECT row_key, row_data FROM generic_table_data WHERE version_id = ?', (version_id,))
            for row in c.fetchall():
                data = json.loads(row['row_data'])
                if col_key in data:
                    del data[col_key]
                    c.execute('UPDATE generic_table_data SET row_data=? WHERE row_key=? AND version_id=?',
                              (json.dumps(data, ensure_ascii=False), row['row_key'], version_id))
            conn.commit()
        return True

    def reorder_columns(self, version_id, col_keys):
        """批量重排列顺序"""
        with get_db() as conn:
            c = conn.cursor()
            for idx, ck in enumerate(col_keys):
                c.execute('UPDATE generic_table_columns SET col_index=? WHERE version_id=? AND col_key=?',
                          (idx, version_id, ck))
            conn.commit()
        return True

    # ---------- 行数据 ----------

    def get_rows(self, version_id, keyword=None):
        """返回行数据列表，可选快速搜索"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM generic_table_data WHERE version_id = ? ORDER BY row_index', (version_id,))
            rows = [_row_to_dict(c, r) for r in c.fetchall()]
            result = []
            for r in rows:
                r['row_data'] = json.loads(r['row_data'])
                if keyword:
                    text = ' '.join(str(v) for v in r['row_data'].values())
                    if keyword.lower() not in text.lower():
                        continue
                result.append(r)
            return result

    def upsert_row(self, version_id, row_key, row_data_dict):
        """插入或更新一行"""
        now = _now()
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id, row_index FROM generic_table_data WHERE version_id = ? AND row_key = ?',
                      (version_id, row_key))
            existing = c.fetchone()
            if existing:
                row_index = existing['row_index']
                c.execute('UPDATE generic_table_data SET row_data=?, updated_at=? WHERE row_key=? AND version_id=?',
                          (json.dumps(row_data_dict, ensure_ascii=False), now, row_key, version_id))
            else:
                # 取当前最大 row_index
                c.execute('SELECT MAX(row_index) as max_i FROM generic_table_data WHERE version_id = ?', (version_id,))
                row = c.fetchone()
                row_index = (row['max_i'] or -1) + 1
                c.execute('''INSERT INTO generic_table_data
                    (version_id, row_key, row_index, row_data, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (version_id, row_key, row_index, json.dumps(row_data_dict, ensure_ascii=False), now, now))
            # 更新版本行数
            c.execute('SELECT COUNT(*) as cnt FROM generic_table_data WHERE version_id = ?', (version_id,))
            cnt = c.fetchone()['cnt']
            c.execute('UPDATE generic_table_versions SET row_count=? WHERE version_id = ?', (cnt, version_id))
            conn.commit()
        return True

    def delete_row(self, version_id, row_key):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM generic_table_data WHERE version_id = ? AND row_key = ?', (version_id, row_key))
            # 更新版本行数
            c.execute('SELECT COUNT(*) as cnt FROM generic_table_data WHERE version_id = ?', (version_id,))
            cnt = c.fetchone()['cnt']
            c.execute('UPDATE generic_table_versions SET row_count=? WHERE version_id = ?', (cnt, version_id))
            conn.commit()
        return True

    def import_rows_from_excel(self, version_id, file_path, creator, mode='replace'):
        """从Excel文件批量导入数据到指定版本
        mode: 'replace' 清空旧数据后导入；'append' 保留旧数据追加新行
        支持合并单元格：
          - 水平合并（跨列）：表头行跳过非首列，数据行自动填充
          - 垂直合并（跨行）：数据行自动向下填充合并值
        """
        import openpyxl
        now = _now()
        wb = openpyxl.load_workbook(file_path, data_only=True)
        ws = wb.active

        # ---------- 预处理：填充合并单元格 ----------
        merged_ranges = list(ws.merged_cells.ranges)

        # fill_map: (row_1based, col_1based) -> 填充值
        fill_map = {}
        for mr in merged_ranges:
            top_val = ws.cell(mr.min_row, mr.min_col).value
            for r in range(mr.min_row, mr.max_row + 1):
                for c in range(mr.min_col, mr.max_col + 1):
                    fill_map[(r, c)] = top_val

        # 水平合并（同一行跨多列）的非首列 → 表头不创建列
        horiz_header_skip = set()  # 0-based col indices to skip in header
        for mr in merged_ranges:
            if mr.min_row == mr.max_row:  # 水平合并
                for c in range(mr.min_col + 1, mr.max_col + 1):
                    horiz_header_skip.add(c - 1)  # 转为 0-based

        # 构建行数据
        raw_rows = []
        for row_cells in ws.iter_rows(min_row=1):
            row_vals = [fill_map.get((cell.row, cell.column), cell.value)
                        for cell in row_cells]
            raw_rows.append(row_vals)

        if len(raw_rows) < 2:
            return 0

        header = [str(h) if h is not None else '' for h in raw_rows[0]]
        # 生成 col_key（英文字段名），跳过水平合并的非首列
        col_key_map = {}
        col_name_map = {}  # col_key -> col_name
        with get_db() as conn:
            c = conn.cursor()
            if mode == 'replace':
                c.execute('DELETE FROM generic_table_data WHERE version_id = ?', (version_id,))
            for idx, col_name in enumerate(header):
                if idx in horiz_header_skip or not col_name.strip():
                    continue
                col_key = self._name_to_key(col_name)
                col_key_map[idx] = col_key
                col_name_map[idx] = col_name
                self.upsert_column(version_id, col_key, col_name, 'text',
                                   col_index=idx, col_width=120, _conn=conn)
            # 导入数据行
            c.execute('SELECT MAX(row_index) as max_i FROM generic_table_data WHERE version_id = ?', (version_id,))
            base_idx_raw = c.fetchone()['max_i']
            # 修复：row_index 可能是 0，不能用 `or -1`（0 是 falsy）
            base_idx = (base_idx_raw if base_idx_raw is not None else -1) + 1
            row_count = 0
            for raw_row in raw_rows[1:]:
                row_dict = {}
                for idx, val in enumerate(raw_row):
                    if idx in col_key_map:
                        row_dict[col_key_map[idx]] = val if val is not None else ''
                if row_dict:
                    row_key = _new_id('GTR')
                    c.execute('''INSERT INTO generic_table_data
                        (version_id, row_key, row_index, row_data, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)''',
                        (version_id, row_key, base_idx + row_count,
                         json.dumps(row_dict, ensure_ascii=False), now, now))
                    row_count += 1
            if mode == 'replace':
                c.execute('UPDATE generic_table_versions SET row_count=? WHERE version_id = ?', (row_count, version_id))
            else:
                c.execute('SELECT COUNT(*) as cnt FROM generic_table_data WHERE version_id = ?', (version_id,))
                cnt = c.fetchone()['cnt']
                c.execute('UPDATE generic_table_versions SET row_count=? WHERE version_id = ?', (cnt, version_id))
            c.execute('''UPDATE generic_tables SET updated_at=? WHERE table_id=
                        (SELECT table_id FROM generic_table_versions WHERE version_id=?)''',
                      (now, version_id))
            conn.commit()
        return row_count

    def _name_to_key(self, name):
        """将中文列名转换为英文字段名"""
        import re
        s = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]', '_', str(name))
        s = re.sub(r'_+', '_', s).strip('_')
        if s and s[0].isdigit():
            s = 'c_' + s
        if not s:
            s = 'col'
        return s[:30]

    # ---------- 统计与对比 ----------

    def get_column_stats(self, version_id):
        """计算每列的汇总统计"""
        rows = self.get_rows(version_id)
        cols = self.get_columns(version_id)
        stats = {}
        for col in cols:
            ck = col['col_key']
            values = []
            for row in rows:
                v = row['row_data'].get(ck, '')
                if col['col_type'] == 'number' and v not in ('', None):
                    try:
                        values.append(float(v))
                    except (ValueError, TypeError):
                        pass
            # 数字列自动计算 sum（无论是否有 col_summary 配置）
            if col['col_type'] == 'number' and values:
                if col.get('col_summary') in ('sum', 'count', 'avg'):
                    summary_type = col['col_summary']
                else:
                    summary_type = 'sum'  # 数字列默认显示合计
                if summary_type == 'count':
                    stats[ck] = {'type': 'count', 'value': len(values)}
                elif summary_type == 'sum':
                    stats[ck] = {'type': 'sum', 'value': round(sum(values), 2)}
                elif summary_type == 'avg':
                    stats[ck] = {'type': 'avg', 'value': round(sum(values)/len(values), 2)}
        return stats

    def compare_versions(self, version_id_a, version_id_b):
        """对比两个版本，返回差异"""
        cols_a = {c['col_key']: c for c in self.get_columns(version_id_a)}
        cols_b = {c['col_key']: c for c in self.get_columns(version_id_b)}
        rows_a = {r['row_key']: r for r in self.get_rows(version_id_a)}
        rows_b = {r['row_key']: r for r in self.get_rows(version_id_b)}

        all_keys = set(cols_a.keys()) | set(cols_b.keys())
        col_diff = {}
        for ck in all_keys:
            if ck not in cols_a:
                col_diff[ck] = 'added'
            elif ck not in cols_b:
                col_diff[ck] = 'deleted'
            else:
                a = cols_a[ck]
                b = cols_b[ck]
                if (a['col_name'] != b['col_name'] or
                    a['col_type'] != b['col_type'] or
                    a['col_summary'] != b['col_summary']):
                    col_diff[ck] = 'modified'
                else:
                    col_diff[ck] = 'unchanged'

        all_row_keys = set(rows_a.keys()) | set(rows_b.keys())
        row_diff = []
        for rk in sorted(all_row_keys):
            if rk in rows_a and rk in rows_b:
                if rows_a[rk]['row_data'] == rows_b[rk]['row_data']:
                    status = 'unchanged'
                else:
                    status = 'modified'
                row_diff.append({
                    'row_key': rk,
                    'status': status,
                    'old': rows_a[rk]['row_data'],
                    'new': rows_b[rk]['row_data']
                })
            elif rk in rows_a:
                row_diff.append({'row_key': rk, 'status': 'deleted', 'old': rows_a[rk]['row_data'], 'new': None})
            else:
                row_diff.append({'row_key': rk, 'status': 'added', 'old': None, 'new': rows_b[rk]['row_data']})

        return {
            'col_diff': col_diff,
            'row_diff': row_diff,
            'cols_a': list(cols_a.keys()),
            'cols_b': list(cols_b.keys())
        }

    # ---------- REQ-016 增强：行染色 / 列宽 / 分页数 ----------
    # 注：reorder_columns 已存在（line 340），不在此重复

    def update_row_color(self, version_id, row_key, row_color):
        """更新单行颜色（row_color 必为 ''/yellow/green/red 之一）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id FROM generic_table_data WHERE version_id=? AND row_key=?',
                      (version_id, row_key))
            if not c.fetchone():
                return False
            c.execute('''UPDATE generic_table_data
                SET row_color=?, updated_at=?
                WHERE version_id=? AND row_key=?''',
                (row_color, _now(), version_id, row_key))
            conn.commit()
            return True

    def update_column_width(self, version_id, col_key, col_width):
        """更新单列宽度（60-800）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''UPDATE generic_table_columns
                SET col_width=?
                WHERE version_id=? AND col_key=?''',
                (col_width, version_id, col_key))
            conn.commit()
            return c.rowcount > 0

    def update_page_size(self, version_id, page_size):
        """更新版本的每页条数（-1/10/20/50/100）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''UPDATE generic_table_versions
                SET page_size=?
                WHERE version_id=?''',
                (page_size, version_id))
            conn.commit()
            return c.rowcount > 0

    def export_to_excel(self, version_id):
        """将指定版本的数据导出为Excel文件，返回临时文件路径"""
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        import tempfile, os

        columns = self.get_columns(version_id)
        rows = self.get_rows(version_id)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "数据导出"

        # 标题行样式
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="4472C4")
        header_align = Alignment(horizontal="center", vertical="center")
        thin_border = Border(
            left=Side(style='thin'), right=Side(style='thin'),
            top=Side(style='thin'), bottom=Side(style='thin')
        )

        # 写表头
        headers = [col.get('col_name', col.get('col_key', '')) for col in columns]
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

        # 写数据行
        for row_idx, row in enumerate(rows, 2):
            row_data = row.get('row_data', {})
            for col_idx, col in enumerate(columns, 1):
                col_key = col.get('col_key', '')
                val = row_data.get(col_key, '')
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")

        # 自动列宽
        for col_idx, col in enumerate(columns, 1):
            max_len = len(str(col.get('col_name', '')))
            for row in rows:
                val = str(row.get('row_data', {}).get(col.get('col_key', ''), '') or '')
                max_len = max(max_len, len(val))
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = min(max_len + 2, 50)

        # 保存到临时文件
        fd, path = tempfile.mkstemp(suffix='.xlsx')
        os.close(fd)
        wb.save(path)
        return path


