# -*- coding: utf-8 -*-
"""
报销助手数据库模块
负责 expense.db 的初始化和迁移
"""
import os
import sqlite3
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'expense.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化/迁移数据库"""
    with get_db() as conn:
        c = conn.cursor()

        # 报销项表
        c.execute('''CREATE TABLE IF NOT EXISTS expense_reimbursement (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reimbursement_no TEXT UNIQUE,
            title TEXT,
            total_amount REAL DEFAULT 0,
            status TEXT DEFAULT '草稿',
            remark TEXT,
            approver TEXT,
            created_at TEXT,
            updated_at TEXT,
            confirmed_at TEXT
        )''')

        # 发票表
        c.execute('''CREATE TABLE IF NOT EXISTS expense_invoice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reimbursement_id INTEGER,
            invoice_no TEXT,
            date TEXT,
            amount REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            price_ex_tax REAL DEFAULT 0,
            buyer TEXT,
            seller TEXT,
            content TEXT,
            invoice_type TEXT,
            tax_rate TEXT,
            ocr_text TEXT,
            file_path TEXT,
            confidence TEXT DEFAULT '高',
            status TEXT DEFAULT '未匹配',
            matched_payment_ids TEXT DEFAULT '[]',
            created_at TEXT,
            FOREIGN KEY (reimbursement_id) REFERENCES expense_reimbursement(id)
        )''')

        # 发票商品明细行表
        c.execute('''CREATE TABLE IF NOT EXISTS expense_invoice_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER,
            seq INTEGER DEFAULT 0,
            name TEXT,
            spec TEXT,
            unit TEXT,
            quantity REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            tax_rate TEXT,
            tax_amount REAL DEFAULT 0,
            FOREIGN KEY (invoice_id) REFERENCES expense_invoice(id)
        )''')

        # 支付记录表
        c.execute('''CREATE TABLE IF NOT EXISTS expense_payment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reimbursement_id INTEGER,
            payment_no TEXT,
            amount REAL DEFAULT 0,
            pay_date TEXT,
            ocr_text TEXT,
            file_path TEXT,
            status TEXT DEFAULT '未匹配',
            matched_invoice_ids TEXT DEFAULT '[]',
            created_at TEXT,
            FOREIGN KEY (reimbursement_id) REFERENCES expense_reimbursement(id)
        )''')

        # 索引（加速常用查询）
        c.execute('CREATE INDEX IF NOT EXISTS idx_invoice_reimbursement ON expense_invoice(reimbursement_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_invoice_status ON expense_invoice(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_invoice_date ON expense_invoice(date)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_payment_reimbursement ON expense_payment(reimbursement_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_payment_status ON expense_payment(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_reimbursement_status ON expense_reimbursement(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_reimbursement_created ON expense_reimbursement(created_at)')

        conn.commit()

        # ===== 增量迁移：新增字段 =====

        # 1. expense_reimbursement.reimbursement_type
        c.execute("PRAGMA table_info(expense_reimbursement)")
        cols = [row[1] for row in c.fetchall()]
        if 'reimbursement_type' not in cols:
            c.execute("ALTER TABLE expense_reimbursement ADD COLUMN reimbursement_type TEXT DEFAULT '采购报销'")

        # 2. expense_invoice.spec
        c.execute("PRAGMA table_info(expense_invoice)")
        cols = [row[1] for row in c.fetchall()]
        if 'spec' not in cols:
            c.execute("ALTER TABLE expense_invoice ADD COLUMN spec TEXT DEFAULT ''")

        # 3. expense_payment.payer
        c.execute("PRAGMA table_info(expense_payment)")
        cols = [row[1] for row in c.fetchall()]
        if 'payer' not in cols:
            c.execute("ALTER TABLE expense_payment ADD COLUMN payer TEXT DEFAULT ''")

        # 4. expense_reimbursement.is_paid
        c.execute("PRAGMA table_info(expense_reimbursement)")
        cols = [row[1] for row in c.fetchall()]
        if 'is_paid' not in cols:
            c.execute("ALTER TABLE expense_reimbursement ADD COLUMN is_paid INTEGER DEFAULT 0")

        # 5. expense_reimbursement.documents（单据 JSON 数组）
        c.execute("PRAGMA table_info(expense_reimbursement)")
        cols = [row[1] for row in c.fetchall()]
        if 'documents' not in cols:
            c.execute("ALTER TABLE expense_reimbursement ADD COLUMN documents TEXT DEFAULT '[]'")

        # --- 发票表结构迁移 ---
        c.execute("PRAGMA table_info(expense_invoice)")
        invoice_cols = [row[1] for row in c.fetchall()]
        new_cols = {
            'train_no': "ALTER TABLE expense_invoice ADD COLUMN train_no TEXT DEFAULT ''",
            'departure_station': "ALTER TABLE expense_invoice ADD COLUMN departure_station TEXT DEFAULT ''",
            'arrival_station': "ALTER TABLE expense_invoice ADD COLUMN arrival_station TEXT DEFAULT ''",
            'departure_date': "ALTER TABLE expense_invoice ADD COLUMN departure_date TEXT DEFAULT ''",
            'seat_type': "ALTER TABLE expense_invoice ADD COLUMN seat_type TEXT DEFAULT ''",
            'passenger_name': "ALTER TABLE expense_invoice ADD COLUMN passenger_name TEXT DEFAULT ''",
            'id_card_no': "ALTER TABLE expense_invoice ADD COLUMN id_card_no TEXT DEFAULT ''",
            'flight_no': "ALTER TABLE expense_invoice ADD COLUMN flight_no TEXT DEFAULT ''",
            'departure_airport': "ALTER TABLE expense_invoice ADD COLUMN departure_airport TEXT DEFAULT ''",
            'arrival_airport': "ALTER TABLE expense_invoice ADD COLUMN arrival_airport TEXT DEFAULT ''",
            'departure_time': "ALTER TABLE expense_invoice ADD COLUMN departure_time TEXT DEFAULT ''",
            'departure_city': "ALTER TABLE expense_invoice ADD COLUMN departure_city TEXT DEFAULT ''",
            'arrival_city': "ALTER TABLE expense_invoice ADD COLUMN arrival_city TEXT DEFAULT ''",
        }
        for col_name, alter_sql in new_cols.items():
            if col_name not in invoice_cols:
                c.execute(alter_sql)

        conn.commit()

def _row_to_dict(c, row):
    if not row:
        return None
    cols = [d[0] for d in c.description]
    return dict(zip(cols, row))

def get_all_reimbursements(status=None, keyword=None, limit=200):
    with get_db() as conn:
        c = conn.cursor()
        conditions = []
        params = []
        if status:
            conditions.append('r.status=?')
            params.append(status)
        if keyword:
            conditions.append('(r.reimbursement_no LIKE ? OR r.title LIKE ?)')
            params.extend([f'%{keyword}%', f'%{keyword}%'])
        where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        sql = f'''SELECT r.*,
            COALESCE(r.is_paid, 0) AS is_paid,
            (
                SELECT MAX(dt) FROM (
                    SELECT MAX(i.date) AS dt FROM expense_invoice i WHERE i.reimbursement_id = r.id AND i.date IS NOT NULL AND i.date != ''
                    UNION ALL
                    SELECT MAX(p.pay_date) AS dt FROM expense_payment p WHERE p.reimbursement_id = r.id AND p.pay_date IS NOT NULL AND p.pay_date != ''
                )
            ) AS payment_time,
            (SELECT COUNT(*) FROM expense_invoice WHERE reimbursement_id=r.id) AS invoice_count,
            (SELECT COUNT(*) FROM expense_payment WHERE reimbursement_id=r.id) AS payment_count
            FROM expense_reimbursement r
            {where_clause}
            ORDER BY r.created_at DESC LIMIT ?'''
        params.append(limit)
        c.execute(sql, params)
        return [_row_to_dict(c, row) for row in c.fetchall()]

def get_reimbursement_by_id(rid):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM expense_reimbursement WHERE id=?', (rid,))
        row = c.fetchone()
        if not row:
            return None
        item = _row_to_dict(c, row)
        return item

def create_reimbursement(title='', approver='', remark='', reimbursement_type='采购报销'):
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 生成编号（使用 MAX+1 代替 COUNT+1，避免并发竞态）
        today = datetime.now().strftime('%Y%m%d')
        prefix = f'REI{today}'
        c.execute("SELECT reimbursement_no FROM expense_reimbursement WHERE reimbursement_no LIKE ? ORDER BY reimbursement_no DESC LIMIT 1", (f'{prefix}%',))
        row = c.fetchone()
        if row:
            # 提取序号部分并+1
            last_no = row[0]
            try:
                seq = int(last_no[len(prefix):]) + 1
            except ValueError:
                seq = 1
        else:
            seq = 1
        reimbursement_no = f'{prefix}{seq:03d}'
        c.execute('''INSERT INTO expense_reimbursement
            (reimbursement_no, title, total_amount, status, remark, approver, reimbursement_type, created_at, updated_at)
            VALUES (?, ?, 0, '草稿', ?, ?, ?, ?, ?)''',
            (reimbursement_no, title, remark, approver, reimbursement_type, now, now))
        rid = c.lastrowid
        conn.commit()
        return rid, reimbursement_no

def update_reimbursement(rid, **kwargs):
    with get_db() as conn:
        c = conn.cursor()
        allowed = ['title', 'total_amount', 'status', 'remark', 'approver', 'confirmed_at', 'reimbursement_type', 'is_paid']
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f'{k}=?')
                params.append(v)
        if updates:
            updates.append('updated_at=?')
            params.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            params.append(rid)
            c.execute(f'UPDATE expense_reimbursement SET {", ".join(updates)} WHERE id=?', params)
            conn.commit()

def toggle_reimbursement_paid(rid):
    """切换 is_paid 状态，0→1 或 1→0，返回切换后的值"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT is_paid FROM expense_reimbursement WHERE id=?', (rid,))
        row = c.fetchone()
        if not row:
            return None
        new_val = 1 if (row[0] in (None, 0)) else 0
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('UPDATE expense_reimbursement SET is_paid=?, updated_at=? WHERE id=?', (new_val, now, rid))
        conn.commit()
        return new_val

def delete_reimbursement(rid):
    """删除报销项，将关联的发票/支付记录撤回到待整理区（原子操作）"""
    conn = get_db()
    try:
        # 显式开启事务，确保全部成功或全部回滚
        conn.execute('BEGIN IMMEDIATE')
        c = conn.cursor()
        # 将关联的发票撤回到待整理区（解除关联，恢复状态）
        c.execute(
            "UPDATE expense_invoice SET reimbursement_id=NULL, status='未匹配' WHERE reimbursement_id=?",
            (rid,)
        )
        # 将关联的支付记录撤回到待整理区
        c.execute(
            "UPDATE expense_payment SET reimbursement_id=NULL, status='未匹配' WHERE reimbursement_id=?",
            (rid,)
        )
        # 删除报销项
        c.execute('DELETE FROM expense_reimbursement WHERE id=?', (rid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def add_invoice(record_id=None, reimbursement_id=None, **fields):
    """添加发票记录（原子操作）"""
    conn = get_db()
    try:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        invoice_no = fields.get('invoice_no', '')
        date = fields.get('date', '')
        amount = fields.get('amount', 0)
        tax_amount = fields.get('tax_amount', 0)
        price_ex_tax = fields.get('price_ex_tax', 0)
        buyer = fields.get('buyer', '')
        seller = fields.get('seller', '')
        content = fields.get('content', '')
        spec = fields.get('spec', '')
        invoice_type = fields.get('invoice_type', '')
        tax_rate = fields.get('tax_rate', '')
        ocr_text = fields.get('ocr_text', '')
        file_path = fields.get('file_path', '')
        confidence = fields.get('confidence', '高')
        items = fields.get('items', [])
        # 火车票/机票字段
        train_no = fields.get('train_no', '')
        departure_station = fields.get('departure_station', '')
        arrival_station = fields.get('arrival_station', '')
        departure_date = fields.get('departure_date', '')
        seat_type = fields.get('seat_type', '')
        passenger_name = fields.get('passenger_name', '')
        id_card_no = fields.get('id_card_no', '')
        flight_no = fields.get('flight_no', '')
        departure_airport = fields.get('departure_airport', '')
        arrival_airport = fields.get('arrival_airport', '')
        departure_time = fields.get('departure_time', '')
        departure_city = fields.get('departure_city', '')
        arrival_city = fields.get('arrival_city', '')

        c.execute('''INSERT INTO expense_invoice
            (reimbursement_id, invoice_no, date, amount, tax_amount, price_ex_tax, buyer, seller,
             content, invoice_type, tax_rate, ocr_text, file_path, confidence, status, matched_payment_ids, created_at,
             spec, train_no, departure_station, arrival_station, departure_date, seat_type, passenger_name, id_card_no,
             flight_no, departure_airport, arrival_airport, departure_time, departure_city, arrival_city, match_group_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '未匹配', '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (reimbursement_id, invoice_no, date, amount, tax_amount, price_ex_tax, buyer, seller,
             content, invoice_type, tax_rate, ocr_text, file_path, confidence, now,
             spec, train_no, departure_station, arrival_station, departure_date, seat_type, passenger_name, id_card_no,
             flight_no, departure_airport, arrival_airport, departure_time, departure_city, arrival_city, None))
        invoice_id = c.lastrowid

        # 插入商品明细行
        for seq, item in enumerate(items):
            c.execute('''INSERT INTO expense_invoice_item
                (invoice_id, seq, name, spec, unit, quantity, unit_price, amount, tax_rate, tax_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (invoice_id, seq, item.get('name',''), item.get('spec',''), item.get('unit',''),
                 item.get('quantity',0), item.get('unit_price',0), item.get('amount',0),
                 item.get('tax_rate',''), item.get('tax_amount',0)))

        conn.commit()
        return invoice_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_invoices(reimbursement_id=None, status=None):
    with get_db() as conn:
        c = conn.cursor()
        sql = 'SELECT * FROM expense_invoice WHERE 1=1'
        params = []
        if reimbursement_id is not None:
            sql += ' AND reimbursement_id=?'
            params.append(reimbursement_id)
        if status:
            sql += ' AND status=?'
            params.append(status)
        sql += ' ORDER BY created_at DESC'
        c.execute(sql, params)
        rows = c.fetchall()
        result = []
        for r in rows:
            item = _row_to_dict(c, r)
            if item:
                # 获取明细行
                c2 = conn.cursor()
                c2.execute('SELECT * FROM expense_invoice_item WHERE invoice_id=? ORDER BY seq', (item['id'],))
                item['items'] = [_row_to_dict(c2, row) for row in c2.fetchall()]
                result.append(item)
        return result

def get_invoice_by_id(iid):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM expense_invoice WHERE id=?', (iid,))
        row = c.fetchone()
        if not row:
            return None
        item = _row_to_dict(c, row)
        c.execute('SELECT * FROM expense_invoice_item WHERE invoice_id=? ORDER BY seq', (iid,))
        item['items'] = [_row_to_dict(c, r) for r in c.fetchall()]
        return item

def update_invoice(iid, **fields):
    with get_db() as conn:
        c = conn.cursor()
        allowed = ['invoice_no', 'date', 'amount', 'tax_amount', 'price_ex_tax', 'buyer', 'seller',
                   'content', 'spec', 'invoice_type', 'tax_rate', 'confidence', 'status', 'reimbursement_id', 'matched_payment_ids',
                   'train_no', 'departure_station', 'arrival_station', 'departure_date', 'seat_type',
                   'passenger_name', 'id_card_no', 'flight_no', 'departure_airport', 'arrival_airport', 'departure_time',
                   'departure_city', 'arrival_city']
        updates = []
        params = []
        for k, v in fields.items():
            if k in allowed:
                updates.append(f'{k}=?')
                params.append(v)
        if updates:
            params.append(iid)
            c.execute(f'UPDATE expense_invoice SET {", ".join(updates)} WHERE id=?', params)
            conn.commit()

def delete_invoice(iid):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM expense_invoice_item WHERE invoice_id=?', (iid,))
        c.execute('DELETE FROM expense_invoice WHERE id=?', (iid,))
        conn.commit()

def add_payment(reimbursement_id=None, **fields):
    """添加支付记录"""
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        payment_no = fields.get('payment_no', '')
        amount = fields.get('amount', 0)
        pay_date = fields.get('pay_date', '')
        payer = fields.get('payer', '')
        ocr_text = fields.get('ocr_text', '')
        file_path = fields.get('file_path', '')

        # 生成支付记录编号
        today = datetime.now().strftime('%Y%m%d')
        c.execute("SELECT COUNT(*) FROM expense_payment WHERE payment_no LIKE ?", (f'PAY{today}%',))
        seq = c.fetchone()[0] + 1
        if not payment_no:
            payment_no = f'PAY{today}{seq:03d}'

        c.execute('''INSERT INTO expense_payment
            (reimbursement_id, payment_no, amount, pay_date, payer, ocr_text, file_path, status, matched_invoice_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '未匹配', '[]', ?)''',
            (reimbursement_id, payment_no, amount, pay_date, payer, ocr_text, file_path, now))
        pid = c.lastrowid
        conn.commit()
        return pid

def get_payments(reimbursement_id=None, status=None):
    with get_db() as conn:
        c = conn.cursor()
        sql = 'SELECT * FROM expense_payment WHERE 1=1'
        params = []
        if reimbursement_id is not None:
            sql += ' AND reimbursement_id=?'
            params.append(reimbursement_id)
        if status:
            sql += ' AND status=?'
            params.append(status)
        sql += ' ORDER BY created_at DESC'
        c.execute(sql, params)
        rows = c.fetchall()
        return [_row_to_dict(c, r) if hasattr(c, 'description') else dict(zip([d[0] for d in c.description], r)) for r in rows]

def get_payment_by_id(pid):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM expense_payment WHERE id=?', (pid,))
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row))

def update_payment(pid, **fields):
    with get_db() as conn:
        c = conn.cursor()
        allowed = ['payment_no', 'amount', 'pay_date', 'status', 'reimbursement_id', 'matched_invoice_ids', 'payer']
        updates = []
        params = []
        for k, v in fields.items():
            if k in allowed:
                updates.append(f'{k}=?')
                params.append(v)
        if updates:
            params.append(pid)
            c.execute(f'UPDATE expense_payment SET {", ".join(updates)} WHERE id=?', params)
            conn.commit()

def delete_payment(pid):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM expense_payment WHERE id=?', (pid,))
        conn.commit()

def get_unmatched_invoices():
    return get_invoices(status='未匹配')

def get_unmatched_payments():
    return get_payments(status='未匹配')

def recalculate_reimbursement_total(rid):
    """重新计算报销项总金额"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT SUM(amount) FROM expense_invoice WHERE reimbursement_id=?', (rid,))
        total = c.fetchone()[0] or 0
        c.execute('UPDATE expense_reimbursement SET total_amount=? WHERE id=?', (total, rid))
        conn.commit()
        return total


# ============================================================
# 单据模板管理（读文件系统，不查数据库）
# ============================================================

def get_document_templates():
    """返回所有模板定义（扫描 app/document_templates/ 目录）"""
    import os
    tpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'document_templates')
    templates = []
    if not os.path.isdir(tpl_dir):
        return templates
    for doc_type in os.listdir(tpl_dir):
        tpl_path = os.path.join(tpl_dir, doc_type)
        if not os.path.isdir(tpl_path):
            continue
        meta_path = os.path.join(tpl_path, 'template.json')
        if os.path.exists(meta_path):
            try:
                import json as _json
                meta = _json.loads(open(meta_path, encoding='utf-8').read())
                templates.append(meta)
            except Exception:
                pass
    return templates


def get_document_template(doc_type):
    """返回指定类型的模板定义"""
    tpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'document_templates', doc_type)
    meta_path = os.path.join(tpl_dir, 'template.json')
    if not os.path.exists(meta_path):
        return None
    import json as _json
    return _json.loads(open(meta_path, encoding='utf-8').read())


# ============================================================
# 单据 CRUD（操作 expense_reimbursement.documents JSON）
# ============================================================

def get_reimbursement_documents(rid):
    """获取某报销项的所有单据"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT documents FROM expense_reimbursement WHERE id=?', (rid,))
        row = c.fetchone()
        if not row:
            return []
        docs = row[0]
        if not docs:
            return []
        import json as _json
        try:
            return _json.loads(docs)
        except Exception:
            return []


def add_document_to_reimbursement(rid, doc):
    """添加一个单据到报销项，返回更新后的 documents 列表"""
    docs = get_reimbursement_documents(rid)
    import uuid as _uuid
    import json as _json
    doc['id'] = doc.get('id') or _uuid.uuid4().hex
    doc['filled_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    docs.append(doc)
    docs_json = _json.dumps(docs, ensure_ascii=False)
    with get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE expense_reimbursement SET documents=? WHERE id=?', (docs_json, rid))
        conn.commit()
    return docs


def update_document_in_reimbursement(rid, doc_id, updates):
    """更新报销项中指定单据的字段，返回更新后的 documents 列表"""
    docs = get_reimbursement_documents(rid)
    import json as _json
    for doc in docs:
        if doc.get('id') == doc_id:
            doc['fields'].update(updates.get('fields', {}))
            doc['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            break
    docs_json = _json.dumps(docs, ensure_ascii=False)
    with get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE expense_reimbursement SET documents=? WHERE id=?', (docs_json, rid))
        conn.commit()
    return docs


def delete_document_from_reimbursement(rid, doc_id):
    """从报销项删除指定单据"""
    docs = get_reimbursement_documents(rid)
    import json as _json
    docs = [d for d in docs if d.get('id') != doc_id]
    docs_json = _json.dumps(docs, ensure_ascii=False)
    with get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE expense_reimbursement SET documents=? WHERE id=?', (docs_json, rid))
        conn.commit()
    return docs
