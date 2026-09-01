# -*- coding: utf-8 -*-
import os
import sqlite3
import hashlib
import secrets
from datetime import datetime
from config import DATA_DIR
import json


def hash_password(password: str) -> str:
    """对密码加盐后 hash"""
    salt = secrets.token_hex(16)
    return salt + hashlib.sha256((salt + password).encode()).hexdigest()


def verify_password(password: str, stored: str) -> bool:
    """验证密码是否匹配"""
    if len(stored) == 96:  # 新格式: 32 hex salt + 64 hex hash
        salt = stored[:32]
        return stored == salt + hashlib.sha256((salt + password).encode()).hexdigest()
    # 旧格式（明文或短长度）：直接比较
    return password == stored

# SQLite 数据库路径
DB_PATH = os.path.join(DATA_DIR, 'research.db')

# 一级目录列表
DIRECTORIES = ['projects', 'security_projects', 'crypto_projects', 'equipment', 'standards', 'templates', 'experts', 'utils', 'expense', 'generic_tables']

# 日志模块映射
LOG_MODULES = {
    'equipment': '设备知识库',
    'standards': '标准法规库',
    'templates': '科研模板'
}

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库表"""
    with get_db() as conn:
        c = conn.cursor()
    
        # users 表
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            name TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'active',
            directory_permissions TEXT DEFAULT '{}'
        )''')
    
        # projects 表
        c.execute('''CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            leader TEXT,
            start_date TEXT,
            planned_end_date TEXT,
            actual_end_date TEXT,
            status TEXT,
            created_at TEXT,
            task_number TEXT
        )''')
    
        # equipment 表
        c.execute('''CREATE TABLE IF NOT EXISTS equipment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            equipment_id TEXT,
            name TEXT NOT NULL,
            model TEXT,
            category TEXT,
            form TEXT,
            price REAL,
            tech_index TEXT,
            tech_status TEXT,
            installation_requirements TEXT,
            manufacturer TEXT,
            equipment_image TEXT,
            related_files TEXT,
            main_purpose TEXT,
            former_name TEXT,
            resource_guarantee TEXT,
            created_at TEXT
        )''')

        # knowledge_subclasses 表（设备知识库子类字典）
        c.execute('''CREATE TABLE IF NOT EXISTS knowledge_subclasses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_category TEXT NOT NULL,
            subclass_name TEXT NOT NULL,
            created_at TEXT,
            UNIQUE(parent_category, subclass_name)
        )''')

        # 如果 equipment 表没有 subclass 字段则添加
        c.execute("PRAGMA table_info(equipment)")
        cols = [r[1] for r in c.fetchall()]
        if 'subclass' not in cols:
            c.execute('ALTER TABLE equipment ADD COLUMN subclass TEXT')

        # standards 表
        c.execute('''CREATE TABLE IF NOT EXISTS standards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT,
            name TEXT NOT NULL,
            category TEXT,
            file_type TEXT,
            uploader TEXT,
            upload_time TEXT,
            file_path TEXT
        )''')
    
        # security_projects 表
        c.execute('''CREATE TABLE IF NOT EXISTS security_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            leader TEXT,
            start_date TEXT,
            planned_end_date TEXT,
            actual_end_date TEXT,
            status TEXT,
            created_at TEXT,
            task_number TEXT
        )''')
    
        # crypto_projects 表
        c.execute('''CREATE TABLE IF NOT EXISTS crypto_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            leader TEXT,
            start_date TEXT,
            planned_end_date TEXT,
            actual_end_date TEXT,
            status TEXT,
            created_at TEXT,
            task_number TEXT
        )''')
    
        # experts 表
        c.execute('''CREATE TABLE IF NOT EXISTS experts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expert_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            unit TEXT,
            position TEXT,
            expertise TEXT,
            bank_card TEXT,
            bank_name TEXT,
            uploader TEXT,
            created_at TEXT,
            updated_at TEXT,
            phone TEXT,
            id_card TEXT
        )''')
    
        # doc_templates 表
        c.execute('''CREATE TABLE IF NOT EXISTS doc_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            file_path TEXT,
            uploader TEXT,
            created_at TEXT
        )''')
    
        # project_documents 表
        c.execute('''CREATE TABLE IF NOT EXISTS project_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT UNIQUE NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            file_path TEXT,
            uploader TEXT,
            created_at TEXT
        )''')
    
        # document_versions 表
        c.execute('''CREATE TABLE IF NOT EXISTS document_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT UNIQUE NOT NULL,
            doc_id TEXT NOT NULL,
            version_number TEXT,
            file_path TEXT,
            changer TEXT,
            changed_at TEXT
        )''')
    
        # expert_groups 表
        c.execute('''CREATE TABLE IF NOT EXISTS expert_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT UNIQUE NOT NULL,
            meeting_name TEXT NOT NULL,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
    
        # expert_group_members 表
        c.execute('''CREATE TABLE IF NOT EXISTS expert_group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            expert_id TEXT NOT NULL,
            selected_by TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES expert_groups(group_id)
        )''')
    
        # equipment_groups 表
        c.execute('''CREATE TABLE IF NOT EXISTS equipment_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT UNIQUE NOT NULL,
            project_name TEXT NOT NULL,
            project_id TEXT,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
    
        # equipment_group_members 表
        c.execute('''CREATE TABLE IF NOT EXISTS equipment_group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            equipment_id TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            selected_by TEXT NOT NULL,
            selected_at TEXT NOT NULL
        )''')

        # host_devices 表（宿主设备主表）
        c.execute('''CREATE TABLE IF NOT EXISTS host_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            model TEXT,
            category TEXT,
            form TEXT,
            created_at TEXT,
            updated_at TEXT
        )''')

        # host_device_categories 表（宿主设备类型表）
        c.execute('''CREATE TABLE IF NOT EXISTS host_device_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT
        )''')

        # device_host_relations 表（宿主设备与密码设备关联中间表）
        c.execute('''CREATE TABLE IF NOT EXISTS device_host_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(device_id, host_id)
        )''')

        # research_units 表（研制单位字典）
        c.execute('''CREATE TABLE IF NOT EXISTS research_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            alias TEXT,
            created_at TEXT,
            updated_at TEXT
        )''')
        conn.commit()


class UserModel:
    def __init__(self):
        init_db()
    
    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT username, password, role, name, created_at, status, directory_permissions FROM users WHERE status != "disabled"')
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]

    def get_by_username(self, username):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT username, password, role, name, created_at, status, directory_permissions FROM users WHERE username = ?', (username,))
            row = c.fetchone()
            return self._row_to_dict(c, row)
    
    def verify(self, username, password):
        user = self.get_by_username(username)
        if user and user['status'] == 'active' and verify_password(password, user['password']):
            return True
        return False
    
    def add(self, username, password, role, name, status='active', directory_permissions=None):
        with get_db() as conn:
            c = conn.cursor()
            if directory_permissions is None:
                # 新用户默认不开放科研/密码/安全项目，只开放通用目录
                directory_permissions = {
                    'projects': 'hidden',
                    'security_projects': 'hidden', 
                    'crypto_projects': 'hidden',
                    'equipment': 'visible',
                    'standards': 'visible',
                    'templates': 'visible',
                    'experts': 'visible'
                }
            c.execute('''INSERT INTO users (username, password, role, name, created_at, status, directory_permissions) 
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (username, hash_password(password), role, name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), status, json.dumps(directory_permissions)))
            conn.commit()
    
    def update(self, username, password=None, role=None, name=None, status=None, directory_permissions=None):
        with get_db() as conn:
            c = conn.cursor()
            updates = []
            params = []
            if password:
                updates.append('password = ?')
                params.append(hash_password(password))
            if role:
                updates.append('role = ?')
                params.append(role)
            if name:
                updates.append('name = ?')
                params.append(name)
            if status:
                updates.append('status = ?')
                params.append(status)
            if directory_permissions is not None:
                updates.append('directory_permissions = ?')
                params.append(json.dumps(directory_permissions))
        
            if updates:
                params.append(username)
                c.execute(f'UPDATE users SET {", ".join(updates)} WHERE username = ?', params)
                conn.commit()
            return True
    
    def update_password(self, username, old_password, new_password):
        user = self.get_by_username(username)
        if not user:
            return False, "用户不存在"
        if not verify_password(old_password, user['password']):
            return False, "原密码错误"
        return self.update(username, password=new_password), "密码修改成功"
    
    def get_directory_permissions(self, username):
        user = self.get_by_username(username)
        if not user:
            return {}
        try:
            stored = json.loads(user['directory_permissions']) if user['directory_permissions'] else {}
            # 缺失的目录默认 visible
            return {d: stored.get(d, 'visible') for d in DIRECTORIES}
        except:
            return {d: 'visible' for d in DIRECTORIES}
    
    def set_directory_permissions(self, username, permissions):
        return self.update(username, directory_permissions=permissions)
    
    def delete(self, username):
        return self.update(username, status='disabled')


class ProjectModel:
    def __init__(self):
        init_db()
    
    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM projects ORDER BY created_at DESC')
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]

    def get_by_id(self, project_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM projects WHERE project_id = ?', (project_id,))
            row = c.fetchone()
            return self._row_to_dict(c, row)

    def get_by_status(self, status):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM projects WHERE status = ?', (status,))
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]
    
    def add(self, project_id, name, leader, start_date, end_date_plan, end_date_actual, status):
        with get_db() as conn:
            c = conn.cursor()
            if not project_id:
                project_id = 'PRJ' + datetime.now().strftime('%Y%m%d%H%M%S')
            created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''INSERT INTO projects (project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')''',
                (project_id, name, leader, start_date, end_date_plan, end_date_actual, status, created_at))
            conn.commit()
            return project_id
    
    def update(self, project_id, **kwargs):
        with get_db() as conn:
            c = conn.cursor()
            cols_map = {
                '项目编号': 'project_id', '项目名称': 'name', '负责人': 'leader',
                '开始日期': 'start_date', '计划结束日期': 'planned_end_date',
                '实际结束日期': 'actual_end_date', '状态': 'status', '任务号': 'task_number'
            }
            updates = []
            params = []
            for k, v in kwargs.items():
                db_col = cols_map.get(k, k)
                updates.append(f'{db_col} = ?')
                params.append(v)
        
            if updates:
                params.append(project_id)
                c.execute(f'UPDATE projects SET {", ".join(updates)} WHERE project_id = ?', params)
                conn.commit()
            return True
    
    def delete(self, project_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM projects WHERE project_id = ?', (project_id,))
            conn.commit()
            return True


class EquipmentModel:
    def __init__(self):
        init_db()
    
    def get_all(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer, equipment_image, related_files, created_at, installation_requirements, main_purpose, former_name, resource_guarantee, subclass FROM equipment')
            rows = c.fetchall()
            return [{
                'equipment_id': r[0], 'name': r[1], 'model': r[2], 'category': r[3], 
                'form': r[4], 'price': r[5], 'tech_index': r[6], 'tech_status': r[7], 
                'manufacturer': r[8], 'equipment_image': r[9], 'related_files': r[10],
                'created_at': r[11], 'installation_requirements': r[12], 'main_purpose': r[13], 
                'former_name': r[14], 'resource_guarantee': r[15], 'subclass': r[16]
            } for r in rows]
    
    def get_by_id(self, equipment_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer, equipment_image, related_files, created_at, installation_requirements, main_purpose, former_name, resource_guarantee, subclass FROM equipment WHERE equipment_id = ?', (equipment_id,))
            row = c.fetchone()
            if not row:
                return None
            return {
                'equipment_id': row[0], 'name': row[1], 'model': row[2], 'category': row[3], 
                'form': row[4], 'price': row[5], 'tech_index': row[6], 'tech_status': row[7], 
                'manufacturer': row[8], 'equipment_image': row[9], 'related_files': row[10],
                'created_at': row[11], 'installation_requirements': row[12], 'main_purpose': row[13], 
                'former_name': row[14], 'resource_guarantee': row[15], 'subclass': row[16]
            }
    
    def get_by_category(self, category):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer, equipment_image, related_files, created_at, installation_requirements, main_purpose, former_name, resource_guarantee, subclass FROM equipment WHERE category = ?', (category,))
            rows = c.fetchall()
            return [{
                'equipment_id': r[0], 'name': r[1], 'model': r[2], 'category': r[3], 
                'form': r[4], 'price': r[5], 'tech_index': r[6], 'tech_status': r[7], 
                'manufacturer': r[8], 'equipment_image': r[9], 'related_files': r[10],
                'created_at': r[11], 'installation_requirements': r[12], 'main_purpose': r[13], 
                'former_name': r[14], 'resource_guarantee': r[15], 'subclass': r[16]
            } for r in rows]
    
    def search(self, category=None, form=None, tech_status=None, keyword=None, subclass=None):
        """综合搜索设备（支持子类筛选）"""
        with get_db() as conn:
            c = conn.cursor()
            conditions = []
            params = []
        
            if category:
                conditions.append('category = ?')
                params.append(category)
            if form:
                conditions.append('form = ?')
                params.append(form)
            if tech_status:
                conditions.append('tech_status = ?')
                params.append(tech_status)
            if keyword:
                conditions.append('(name LIKE ? OR model LIKE ?)')
                params.extend([f'%{keyword}%', f'%{keyword}%'])
            if subclass:
                conditions.append('subclass = ?')
                params.append(subclass)
        
            if conditions:
                sql = 'SELECT equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer, equipment_image, related_files, created_at, installation_requirements, main_purpose, former_name, resource_guarantee, subclass FROM equipment WHERE ' + ' AND '.join(conditions)
            else:
                sql = 'SELECT equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer, equipment_image, related_files, created_at, installation_requirements, main_purpose, former_name, resource_guarantee, subclass FROM equipment'
        
            c.execute(sql, params)
            rows = c.fetchall()
            # 返回字典列表
            return [{
                'equipment_id': r[0], 'name': r[1], 'model': r[2], 'category': r[3], 
                'form': r[4], 'price': r[5], 'tech_index': r[6], 'tech_status': r[7], 
                'manufacturer': r[8], 'equipment_image': r[9], 'related_files': r[10],
                'created_at': r[11], 'installation_requirements': r[12], 'main_purpose': r[13], 
                'former_name': r[14], 'resource_guarantee': r[15], 'subclass': r[16]
            } for r in rows]

    def add(self, name, model, category, form, price, tech_index, tech_status, installation_requirements='', manufacturer='', equipment_image='', related_files='', main_purpose='', former_name='', resource_guarantee='', subclass=''):
        with get_db() as conn:
            c = conn.cursor()
            equipment_id = 'EQP' + datetime.now().strftime('%Y%m%d%H%M%S%f')
            created_at = datetime.now().strftime('%Y-%m-%d %H:%M%S')
            c.execute('''INSERT INTO equipment (equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer, equipment_image, related_files, created_at, main_purpose, former_name, resource_guarantee, installation_requirements, subclass) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer, equipment_image, related_files, created_at, main_purpose, former_name, resource_guarantee, installation_requirements, subclass))
            conn.commit()
            return equipment_id
    
    def update(self, equipment_id, **kwargs):
        with get_db() as conn:
            c = conn.cursor()
            cols_map = {
                '设备编号': 'equipment_id', '设备名称': 'name', '型号': 'model',
                '分类': 'category', '形态': 'form', '单价': 'price',
                '功能技术指标': 'tech_index', '加装要求': 'installation_requirements', 
                '技术状态': 'tech_status', '研制单位': 'manufacturer', 'status': 'tech_status',
                '主要用途': 'main_purpose', '曾用名': 'former_name', '资源保障要求': 'resource_guarantee', '设备子类': 'subclass',
                'equipment_image': 'equipment_image'
            }
            updates = []
            params = []
            for k, v in kwargs.items():
                db_col = cols_map.get(k, k)
                updates.append(f'{db_col} = ?')
                params.append(v)
        
            if updates:
                params.append(equipment_id)
                c.execute(f'UPDATE equipment SET {", ".join(updates)} WHERE equipment_id = ?', params)
                conn.commit()
            return True
    
    def delete(self, equipment_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM equipment WHERE equipment_id = ?', (equipment_id,))
            conn.commit()
            return True


class StandardModel:
    def __init__(self):
        init_db()
    
    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT doc_id, name, category, file_type, uploader, upload_time, file_path FROM standards')
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]

    def get_by_id(self, doc_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT doc_id, name, category, file_type, uploader, upload_time, file_path FROM standards WHERE doc_id = ?', (doc_id,))
            row = c.fetchone()
            return self._row_to_dict(c, row)

    def get_by_category(self, category):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT doc_id, name, category, file_type, uploader, upload_time, file_path FROM standards WHERE category = ?', (category,))
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]
    
    def add(self, name, category, file_type, uploader, file_path):
        with get_db() as conn:
            c = conn.cursor()
            doc_id = 'STD' + datetime.now().strftime('%Y%m%d%H%M%S')
            upload_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''INSERT INTO standards (doc_id, name, category, file_type, uploader, upload_time, file_path) 
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (doc_id, name, category, file_type, uploader, upload_time, file_path))
            conn.commit()
            return doc_id
    
    def delete(self, doc_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM standards WHERE doc_id = ?', (doc_id,))
            conn.commit()
            return True


class OperationLogModel:
    def __init__(self):
        self.file_path = os.path.join(DATA_DIR, 'operation_logs.json')
        self._ensure_file()
    
    def _ensure_file(self):
        if not os.path.exists(self.file_path):
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump([], f)
    
    def _read_logs(self):
        with open(self.file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def _write_logs(self, logs):
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    
    def add(self, module, operation_type, file_name, operator, detail=''):
        logs = self._read_logs()
        
        log_entry = {
            'id': len(logs) + 1,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'module': module,
            'module_name': LOG_MODULES.get(module, module),
            'operation_type': operation_type,
            'file_name': file_name,
            'operator': operator,
            'detail': detail
        }
        
        logs.append(log_entry)
        
        if len(logs) > 100:
            logs = logs[-100:]
        
        self._write_logs(logs)
        return log_entry
    
    def get_by_module(self, module, limit=100):
        logs = self._read_logs()
        module_logs = [log for log in logs if log.get('module') == module]
        return module_logs[-limit:]
    
    def get_all(self, limit=100):
        logs = self._read_logs()
        return logs[-limit:]
    
    def search(self, module=None, operation_type=None, operator=None, file_name=None, start_date=None, end_date=None, limit=100):
        logs = self._read_logs()
        
        results = logs
        
        if module:
            results = [log for log in results if log.get('module') == module]
        if operation_type:
            results = [log for log in results if log.get('operation') == operation_type]
        if operator:
            results = [log for log in results if operator in log.get('operator', '')]
        if file_name:
            results = [log for log in results if file_name in log.get('file_name', '')]
        if start_date:
            results = [log for log in results if log.get('timestamp', '') >= start_date]
        if end_date:
            results = [log for log in results if log.get('timestamp', '') <= end_date + ' 23:59:59']
        
        return results[-limit:]
class SecurityProjectModel:
    def __init__(self):
        pass

    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM security_projects ORDER BY created_at DESC')
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]

    def get_by_id(self, project_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM security_projects WHERE project_id = ?', (project_id,))
            row = c.fetchone()
            return self._row_to_dict(c, row)

    def get_by_status(self, status):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM security_projects WHERE status = ?', (status,))
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]
    
    def add(self, project_id, name, leader, start_date, end_date_plan, end_date_actual, status):
        with get_db() as conn:
            c = conn.cursor()
            if not project_id:
                project_id = 'SP' + datetime.now().strftime('%Y%m%d%H%M%S')
            created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''INSERT INTO security_projects (project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')''',
                (project_id, name, leader, start_date, end_date_plan, end_date_actual, status, created_at))
            conn.commit()
            return project_id
    
    def update(self, project_id, **kwargs):
        with get_db() as conn:
            c = conn.cursor()
            updates = []
            params = []
            for k, v in kwargs.items():
                updates.append(f'{k} = ?')
                params.append(v)
            if updates:
                params.append(project_id)
                c.execute(f'UPDATE security_projects SET {", ".join(updates)} WHERE project_id = ?', params)
                conn.commit()
            return True
    
    def delete(self, project_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM security_projects WHERE project_id = ?', (project_id,))
            conn.commit()
            return True

# 密码应用项目Model
class CryptoProjectModel:
    def __init__(self):
        pass

    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM crypto_projects ORDER BY created_at DESC')
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]

    def get_by_id(self, project_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM crypto_projects WHERE project_id = ?', (project_id,))
            row = c.fetchone()
            return self._row_to_dict(c, row)

    def get_by_status(self, status):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number FROM crypto_projects WHERE status = ?', (status,))
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]
    
    def add(self, project_id, name, leader, start_date, end_date_plan, end_date_actual, status):
        with get_db() as conn:
            c = conn.cursor()
            if not project_id:
                project_id = 'CP' + datetime.now().strftime('%Y%m%d%H%M%S')
            created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''INSERT INTO crypto_projects (project_id, name, leader, start_date, planned_end_date, actual_end_date, status, created_at, task_number) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')''',
                (project_id, name, leader, start_date, end_date_plan, end_date_actual, status, created_at))
            conn.commit()
            return project_id
    
    def update(self, project_id, **kwargs):
        with get_db() as conn:
            c = conn.cursor()
            updates = []
            params = []
            for k, v in kwargs.items():
                updates.append(f'{k} = ?')
                params.append(v)
            if updates:
                params.append(project_id)
                c.execute(f'UPDATE crypto_projects SET {", ".join(updates)} WHERE project_id = ?', params)
                conn.commit()
            return True
    
    def delete(self, project_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM crypto_projects WHERE project_id = ?', (project_id,))
            conn.commit()
            return True
class ExpertModel:
    def __init__(self):
        self._ensure_phone_column()
        self._ensure_id_card_column()
    
    def get_db(self):
        return sqlite3.connect('data/research.db')
    
    def _ensure_phone_column(self):
        """确保 phone 列存在"""
        conn = self.get_db()
        c = conn.cursor()
        try:
            c.execute("SELECT phone FROM experts LIMIT 1")
        except:
            try:
                c.execute("ALTER TABLE experts ADD COLUMN phone TEXT")
                conn.commit()
            except Exception as e:
                pass
        conn.close()
    
    def _ensure_id_card_column(self):
        """确保 id_card 列存在"""
        conn = self.get_db()
        c = conn.cursor()
        try:
            c.execute("SELECT id_card FROM experts LIMIT 1")
        except:
            try:
                c.execute("ALTER TABLE experts ADD COLUMN id_card TEXT")
                conn.commit()
            except Exception as e:
                pass
        conn.close()

    def _to_dict(self, c, row):
        """将行记录转换为字典，不依赖列顺序"""
        if not row:
            return None
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row))

    def get_all(self):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM experts ORDER BY created_at DESC')
        rows = c.fetchall()
        conn.close()
        return [self._to_dict(c, r) for r in rows]
    
    def get_by_id(self, expert_id):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM experts WHERE expert_id = ?', (expert_id,))
        row = c.fetchone()
        result = self._to_dict(c, row)
        conn.close()
        return result
    
    def search(self, keyword):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM experts WHERE name LIKE ? OR unit LIKE ? OR expertise LIKE ? ORDER BY created_at DESC',
            (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'))
        rows = c.fetchall()
        conn.close()
        return [self._to_dict(c, r) for r in rows]
    
    def add(self, name, unit, position, expertise, bank_card='', bank_name='', uploader='', phone='', id_card=''):
        conn = self.get_db()
        c = conn.cursor()
        # 生成专家编号
        c.execute('SELECT COUNT(*) FROM experts')
        count = c.fetchone()[0] + 1
        expert_id = f'EXP{datetime.now().strftime("%Y%m%d")}{count:03d}'
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('''INSERT INTO experts (expert_id, name, unit, position, expertise, bank_card, bank_name, uploader, created_at, updated_at, phone, id_card)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (expert_id, name, unit, position, expertise, bank_card, bank_name, uploader, now, now, phone, id_card))
        conn.commit()
        conn.close()
        return expert_id
    
    def update(self, expert_id, **kwargs):
        conn = self.get_db()
        c = conn.cursor()
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        name = kwargs.get('name', '')
        unit = kwargs.get('unit', '')
        position = kwargs.get('position', '')
        expertise = kwargs.get('expertise', '')
        bank_card = kwargs.get('bank_card', '')
        bank_name = kwargs.get('bank_name', '')
        phone = kwargs.get('phone', '')
        id_card = kwargs.get('id_card', '')
        
        sql = f"""UPDATE experts SET 
            name = ?, unit = ?, position = ?, expertise = ?, 
            bank_card = ?, bank_name = ?, phone = ?, id_card = ?, updated_at = ? 
            WHERE expert_id = ?"""
        
        c.execute(sql, (name, unit, position, expertise, bank_card, bank_name, phone, id_card, now, expert_id))
        conn.commit()
        rows_affected = c.rowcount
        conn.close()
        return rows_affected
    
    def delete(self, expert_id):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('DELETE FROM experts WHERE expert_id = ?', (expert_id,))
        conn.commit()
        conn.close()
        return True

    def generate_expert_id(self):
        """
        生成专家编号，格式 EXP-yyyyMMdd-XXXX
        查当天已存在的最大流水号，+1 返回
        流水号范围 0001~9999
        """
        today = datetime.now().strftime('%Y%m%d')
        prefix = f'EXP{today}'
        conn = self.get_db()
        c = conn.cursor()
        c.execute(
            "SELECT expert_id FROM experts WHERE expert_id LIKE ? ORDER BY expert_id DESC LIMIT 1",
            (prefix + '%',)
        )
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            try:
                last_seq = int(row[0][len(prefix):])
                new_seq = last_seq + 1
            except (ValueError, IndexError):
                new_seq = 1
        else:
            new_seq = 1
        if new_seq > 9999:
            raise ValueError(f'今日 ({today[:4]}-{today[4:6]}-{today[6:]}) 导入已达 9999 条上限，请明日再试')
        return f'{prefix}{new_seq:04d}'

    def insert_batch(self, experts_data, uploader=''):
        """
        批量插入专家记录
        :param experts_data: [{'name': ..., 'unit': ..., '_row_idx': ..., ...}, ...]
        :param uploader: 上传人
        :return: {'success': [...], 'fail': [...]}
        """
        results = {'success': [], 'fail': []}
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = self.get_db()
        c = conn.cursor()
        for row in experts_data:
            name = (row.get('name') or '').strip()
            if not name:
                results['fail'].append({
                    'row_idx': row.get('_row_idx', -1),
                    'name': '',
                    'reason': '姓名为空',
                })
                continue
            try:
                expert_id = None
                # 撞号重试一次（多用户同时导入可能撞号）
                for retry in range(2):
                    try:
                        expert_id = self.generate_expert_id()
                        c.execute('''
                            INSERT INTO experts
                            (expert_id, name, unit, position, expertise,
                             bank_card, bank_name, uploader, created_at, updated_at,
                             phone, id_card)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            expert_id,
                            name,
                            (row.get('unit') or '').strip(),
                            (row.get('position') or '').strip(),
                            (row.get('expertise') or '').strip(),
                            (row.get('bank_card') or '').strip(),
                            (row.get('bank_name') or '').strip(),
                            uploader,
                            now, now,
                            (row.get('phone') or '').strip(),
                            (row.get('id_card') or '').strip(),
                        ))
                        conn.commit()
                        break
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        if retry == 1:
                            raise
                        # 撞号：重试（generate_expert_id 会查新的 max）
                        continue
                results['success'].append({
                    'expert_id': expert_id,
                    'name': name,
                    'unit': (row.get('unit') or '').strip(),
                })
            except Exception as e:
                conn.rollback()
                results['fail'].append({
                    'row_idx': row.get('_row_idx', -1),
                    'name': name,
                    'reason': str(e)[:200],
                })
        conn.close()
        return results

# ============ 专家组模型 ============
class ExpertGroupModel:
    def __init__(self):
        pass
    
    def get_db(self):
        return sqlite3.connect('data/research.db')
    
    def create_tables(self):
        """创建专家组相关表"""
        conn = self.get_db()
        c = conn.cursor()
        # 专家组表
        c.execute('''CREATE TABLE IF NOT EXISTS expert_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT UNIQUE NOT NULL,
            meeting_name TEXT NOT NULL,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        # 组成员表
        c.execute('''CREATE TABLE IF NOT EXISTS expert_group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            expert_id TEXT NOT NULL,
            selected_by TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES expert_groups(group_id)
        )''')
        conn.commit()
        conn.close()
    
    def create_group(self, meeting_name, creator):
        """创建专家组"""
        conn = self.get_db()
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('SELECT COUNT(*) FROM expert_groups')
        count = c.fetchone()[0] + 1
        group_id = f'EG{datetime.now().strftime("%Y%m%d")}{count:03d}'
        c.execute('''INSERT INTO expert_groups (group_id, meeting_name, creator, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)''',
            (group_id, meeting_name, creator, now, now))
        conn.commit()
        conn.close()
        return group_id
    
    def add_member(self, group_id, expert_id, selected_by):
        """添加成员"""
        conn = self.get_db()
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('''INSERT INTO expert_group_members (group_id, expert_id, selected_by, selected_at)
            VALUES (?, ?, ?, ?)''',
            (group_id, expert_id, selected_by, now))
        conn.commit()
        conn.close()
        return True
    
    def remove_member(self, group_id, expert_id):
        """移除成员"""
        conn = self.get_db()
        c = conn.cursor()
        c.execute('DELETE FROM expert_group_members WHERE group_id = ? AND expert_id = ?',
            (group_id, expert_id))
        conn.commit()
        conn.close()
        return True
    
    def get_all(self):
        """获取所有专家组"""
        conn = self.get_db()
        c = conn.cursor()
        c.execute('''SELECT g.group_id, g.meeting_name, g.creator, g.created_at, 
            (SELECT COUNT(*) FROM expert_group_members WHERE group_id = g.group_id) as member_count
            FROM expert_groups g ORDER BY g.created_at DESC''')
        rows = c.fetchall()
        conn.close()
        return [{'group_id': r[0], 'meeting_name': r[1], 'creator': r[2], 'created_at': r[3], 'member_count': r[4]} for r in rows]
    
    def get_by_id(self, group_id):
        """获取专家组详情"""
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT group_id, meeting_name, creator, created_at, updated_at FROM expert_groups WHERE group_id = ?', (group_id,))
        group = c.fetchone()
        if not group:
            return None
        c.execute('''SELECT m.expert_id, e.name, e.unit, e.position, e.expertise, e.phone, e.bank_card, e.bank_name, e.id_card, m.selected_by, m.selected_at
            FROM expert_group_members m 
            JOIN experts e ON m.expert_id = e.expert_id 
            WHERE m.group_id = ?''', (group_id,))
        members = c.fetchall()
        conn.close()
        return {
            'group_id': group[0], 'meeting_name': group[1], 'creator': group[2], 'created_at': group[3],
            'members': [{'expert_id': m[0], 'name': m[1], 'unit': m[2], 'position': m[3], 'expertise': m[4], 'phone': m[5], 'bank_card': m[6], 'bank_name': m[7], 'id_card': m[8], 'selected_by': m[9], 'selected_at': m[10]} for m in members]
        }
    
    def delete_group(self, group_id):
        """删除专家组"""
        conn = self.get_db()
        c = conn.cursor()
        c.execute('DELETE FROM expert_group_members WHERE group_id = ?', (group_id,))
        c.execute('DELETE FROM expert_groups WHERE group_id = ?', (group_id,))
        conn.commit()
        conn.close()
        return True

# ============ 设备组模型 ============
class EquipmentGroupModel:
    def __init__(self):
        pass
    
    def get_db(self):
        return sqlite3.connect('data/research.db')
    
    def create_tables(self):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS equipment_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT UNIQUE NOT NULL,
            project_name TEXT NOT NULL,
            project_id TEXT,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS equipment_group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            equipment_id TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            selected_by TEXT NOT NULL,
            selected_at TEXT NOT NULL
        )''')
        conn.commit()
        conn.close()
    
    def migrate_project_id(self):
        """迁移：为 equipment_groups 表添加 project_id 字段"""
        conn = self.get_db()
        c = conn.cursor()
        try:
            c.execute('ALTER TABLE equipment_groups ADD COLUMN project_id TEXT')
        except sqlite3.OperationalError:
            pass  # 字段已存在
        conn.commit()
        conn.close()
    
    def create_group(self, project_name, creator, project_id=None):
        """创建设备组，project_id 为可选关联项目ID"""
        conn = self.get_db()
        c = conn.cursor()
        
        # 检查项目是否已关联设备组
        if project_id:
            c.execute('SELECT group_id, project_name FROM equipment_groups WHERE project_id = ?', (project_id,))
            existing = c.fetchone()
            if existing:
                return {'error': '该项目已关联设备组', 'existing_group': existing[0], 'existing_name': existing[1]}
        
        # 检查设备组名称是否重复
        c.execute('SELECT group_id FROM equipment_groups WHERE project_name = ?', (project_name,))
        existing_name = c.fetchone()
        if existing_name:
            return {'error': '设备组名称已存在', 'existing_group': existing_name[0]}
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('SELECT COUNT(*) FROM equipment_groups')
        count = c.fetchone()[0] + 1
        group_id = f'FG{datetime.now().strftime("%Y%m%d")}{count:03d}'
        c.execute('''INSERT INTO equipment_groups (group_id, project_name, project_id, creator, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)''',
            (group_id, project_name, project_id, creator, now, now))
        conn.commit()
        conn.close()
        return group_id
    
    def get_or_create_by_project(self, project_id, project_name, creator):
        """根据项目ID获取或创建设备组"""
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT group_id FROM equipment_groups WHERE project_id = ?', (project_id,))
        row = c.fetchone()
        if row:
            return row[0]
        # 不存在则创建
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('SELECT COUNT(*) FROM equipment_groups')
        count = c.fetchone()[0] + 1
        group_id = f'FG{datetime.now().strftime("%Y%m%d")}{count:03d}'
        c.execute('''INSERT INTO equipment_groups (group_id, project_name, project_id, creator, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)''',
            (group_id, project_name, project_id, creator, now, now))
        conn.commit()
        conn.close()
        return group_id
    
    def add_member(self, group_id, equipment_id, quantity, selected_by, location=''):
        conn = self.get_db()
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('''INSERT INTO equipment_group_members (group_id, equipment_id, quantity, selected_by, selected_at, location)
            VALUES (?, ?, ?, ?, ?, ?)''',
            (group_id, equipment_id, quantity, selected_by, now, location))
        conn.commit()
        conn.close()
        return True

    def update_member(self, group_id, equipment_id, quantity=None, location=None):
        """更新设备组成员的数量和使用位置"""
        conn = self.get_db()
        c = conn.cursor()
        updates = []
        params = []
        if quantity is not None:
            updates.append('quantity = ?')
            params.append(quantity)
        if location is not None:
            updates.append('location = ?')
            params.append(location)
        if updates:
            params.extend([group_id, equipment_id])
            c.execute(f'UPDATE equipment_group_members SET {", ".join(updates)} WHERE group_id = ? AND equipment_id = ?', params)
            conn.commit()
        conn.close()
        return True

    def remove_member(self, group_id, equipment_id):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('DELETE FROM equipment_group_members WHERE group_id = ? AND equipment_id = ?', (group_id, equipment_id))
        conn.commit()
        conn.close()
        return True
    
    def get_all(self, project_id=None):
        conn = self.get_db()
        c = conn.cursor()
        if project_id:
            c.execute('''SELECT g.group_id, g.project_name, g.project_id, g.creator, g.created_at, 
                (SELECT COUNT(*) FROM equipment_group_members WHERE group_id = g.group_id) as member_count
                FROM equipment_groups g WHERE g.project_id = ? ORDER BY g.created_at DESC''', (project_id,))
        else:
            c.execute('''SELECT g.group_id, g.project_name, g.project_id, g.creator, g.created_at, 
                (SELECT COUNT(*) FROM equipment_group_members WHERE group_id = g.group_id) as member_count
                FROM equipment_groups g ORDER BY g.created_at DESC''')
        rows = c.fetchall()
        conn.close()
        return [{'group_id': r[0], 'project_name': r[1], 'project_id': r[2], 'creator': r[3], 'created_at': r[4], 'member_count': r[5]} for r in rows]
    
    def get_by_id(self, group_id):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT group_id, project_name, project_id, creator, created_at, updated_at FROM equipment_groups WHERE group_id = ?', (group_id,))
        group = c.fetchone()
        if not group:
            return None
        # 设备表实际列顺序: equipment_id,name,model,category,form,price,tech_index,tech_status,
        # manufacturer,equipment_image,related_files,created_at,installation_requirements,
        # main_purpose,former_name,resource_guarantee
        # members表列顺序: id,group_id,equipment_id,quantity,selected_by,selected_at,location
        c.execute('''SELECT m.id, m.group_id, m.equipment_id,
            e.name, e.model, e.category, e.form, e.price, e.tech_index, e.tech_status,
            e.manufacturer, e.equipment_image, e.related_files, e.main_purpose, e.former_name,
            e.resource_guarantee, e.installation_requirements,
            m.quantity, m.selected_by, m.selected_at, m.location
            FROM equipment_group_members m JOIN equipment e ON m.equipment_id = e.equipment_id WHERE m.group_id = ?''',
            (group_id,))
        members = c.fetchall()
        conn.close()
        return {
            'group_id': group[0], 'project_name': group[1], 'project_id': group[2],
            'creator': group[3], 'created_at': group[4], 'updated_at': group[5],
            'members': [{
                'id': m[0], 'group_id': m[1], 'equipment_id': m[2],
                'name': m[3], 'model': m[4], 'category': m[5], 'form': m[6],
                'price': m[7], 'tech_index': m[8], 'tech_status': m[9],
                'manufacturer': m[10], 'equipment_image': m[11], 'related_files': m[12],
                'main_purpose': m[13], 'former_name': m[14], 'resource_guarantee': m[15],
                'installation_requirements': m[16], 'quantity': m[17],
                'selected_by': m[18], 'selected_at': m[19], 'location': m[20]
            } for m in members]}
    
    def update_group(self, group_id, project_name=None, project_id=None):
        """更新设备组信息"""
        conn = self.get_db()
        c = conn.cursor()
        updates = []
        params = []
        if project_name is not None:
            updates.append('project_name = ?')
            params.append(project_name)
        if project_id is not None:
            updates.append('project_id = ?')
            params.append(project_id)
        if updates:
            updates.append('updated_at = ?')
            params.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            params.append(group_id)
            c.execute(f'UPDATE equipment_groups SET {", ".join(updates)} WHERE group_id = ?', params)
            conn.commit()
        conn.close()
        return True
    
    def delete_group(self, group_id):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('DELETE FROM equipment_group_members WHERE group_id = ?', (group_id,))
        c.execute('DELETE FROM equipment_groups WHERE group_id = ?', (group_id,))
        conn.commit()
        conn.close()
        return True


# ============ LLM 模型配置 ============
class LLMModel:
    """LLM 模型配置管理"""
    def __init__(self):
        pass

    def get_db(self):
        return sqlite3.connect(DB_PATH)

    def create_table(self):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS llm_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            model_type TEXT NOT NULL DEFAULT 'corrector',
            file_path TEXT,
            n_ctx INTEGER DEFAULT 512,
            n_threads INTEGER DEFAULT 4,
            temperature REAL DEFAULT 0.3,
            max_tokens INTEGER DEFAULT 512,
            prompt_template TEXT,
            is_active INTEGER DEFAULT 0,
            description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        conn.commit()
        conn.close()

    def get_all(self):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM llm_models ORDER BY is_active DESC, id ASC')
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
        conn.close()
        return [dict(zip(cols, r)) for r in rows]

    def get_active(self):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM llm_models WHERE is_active = 1 LIMIT 1')
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
        conn.close()
        return dict(zip(cols, row))

    def get_by_id(self, id):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM llm_models WHERE id = ?', (id,))
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
        conn.close()
        return dict(zip(cols, row))

    def create(self, name, model_type='corrector', file_path='', n_ctx=512, n_threads=4,
               temperature=0.3, max_tokens=512, prompt_template='', is_active=0, description=''):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = self.get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO llm_models
            (name, model_type, file_path, n_ctx, n_threads, temperature, max_tokens,
             prompt_template, is_active, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (name, model_type, file_path, n_ctx, n_threads, temperature, max_tokens,
             prompt_template, is_active, description, now, now))
        conn.commit()
        conn.close()
        return True

    def update(self, id, **kwargs):
        allowed = ['name', 'model_type', 'file_path', 'n_ctx', 'n_threads', 'temperature',
                   'max_tokens', 'prompt_template', 'is_active', 'description']
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f'{k} = ?')
                params.append(v)
        if not updates:
            return False
        updates.append('updated_at = ?')
        params.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        params.append(id)
        conn = self.get_db()
        c = conn.cursor()
        c.execute(f'UPDATE llm_models SET {", ".join(updates)} WHERE id = ?', params)
        conn.commit()
        conn.close()
        return True

    def delete(self, id):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('DELETE FROM llm_models WHERE id = ?', (id,))
        conn.commit()
        conn.close()
        return True


# ============ 推理服务器状态 ============
class InferenceServerStatus:
    """推理服务器运行状态"""
    def __init__(self):
        pass

    def get_db(self):
        return sqlite3.connect(DB_PATH)

    def create_table(self):
        conn = self.get_db()
        c = conn.cursor()
        sql = (
            "CREATE TABLE IF NOT EXISTS inference_server_status ("
            "  id INTEGER PRIMARY KEY CHECK (id = 1),"
            "  server_status TEXT DEFAULT 'stopped',"
            "  model_loaded TEXT DEFAULT '',"
            "  model_load_time REAL DEFAULT 0,"
            "  total_requests INTEGER DEFAULT 0,"
            "  avg_latency_ms REAL DEFAULT 0,"
            "  last_request_time TEXT DEFAULT '',"
            "  memory_usage_mb REAL DEFAULT 0,"
            "  cpu_usage_percent REAL DEFAULT 0,"
            "  error_message TEXT DEFAULT '',"
            "  updated_at TEXT NOT NULL"
            ")"
        )
        c.execute(sql)
        c.execute(
            "INSERT OR IGNORE INTO inference_server_status (id, updated_at) VALUES (1, ?)",
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)
        )
        conn.commit()
        conn.close()

    def get(self):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM inference_server_status WHERE id = 1')
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
        conn.close()
        return dict(zip(cols, row))

    def update(self, **kwargs):
        allowed = ['server_status', 'model_loaded', 'model_load_time', 'total_requests',
                   'avg_latency_ms', 'last_request_time', 'memory_usage_mb', 'cpu_usage_percent',
                   'error_message']
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f'{k} = ?')
                params.append(v)
        if not updates:
            return False
        updates.append('updated_at = ?')
        params.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        params.append(1)
        conn = self.get_db()
        c = conn.cursor()
        c.execute(f'UPDATE inference_server_status SET {chr(44).join(updates)} WHERE id = 1', params)
        conn.commit()
        conn.close()
        return True


# =============================================================================
# REQ-008 宿主设备相关 Model
# =============================================================================

class HostDeviceModel:
    """宿主设备 CRUD"""
    def __init__(self):
        init_db()

    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self, category=None, form=None, keyword=None, page=1, per_page=20):
        with get_db() as conn:
            c = conn.cursor()
            conditions = []
            params = []
            if category:
                conditions.append('category = ?')
                params.append(category)
            if form:
                conditions.append('form = ?')
                params.append(form)
            if keyword:
                conditions.append('(name LIKE ? OR model LIKE ?)')
                params.extend([f'%{keyword}%', f'%{keyword}%'])
            # [REQ-008-fix] 删除半成品代码：subclass 变量从未在函数签名声明，
            # 任何调用都会 NameError → 500 错误。host_devices 表也没有 subclass 列。
            sql = 'SELECT host_id, name, model, category, form, created_at, updated_at FROM host_devices'
            if conditions:
                sql += ' WHERE ' + ' AND '.join(conditions)
            sql += ' ORDER BY created_at DESC'
            c.execute(sql, params)
            rows = c.fetchall()
            all_data = [self._row_to_dict(c, r) for r in rows]
            total = len(all_data)
            total_pages = (total + per_page - 1) // per_page if total > 0 else 1
            start = (page - 1) * per_page
            end = start + per_page
            return all_data[start:end], total

    def get_by_id(self, host_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT host_id, name, model, category, form, created_at, updated_at FROM host_devices WHERE host_id = ?', (host_id,))
            row = c.fetchone()
            return self._row_to_dict(c, row)

    def add(self, name, model, category, form):
        with get_db() as conn:
            c = conn.cursor()
            host_id = 'HD' + datetime.now().strftime('%Y%m%d%H%M%S%f')
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:19].replace('.', '')
            c.execute('''INSERT INTO host_devices (host_id, name, model, category, form, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (host_id, name, model, category, form, now, now))
            conn.commit()
            return host_id

    def update(self, host_id, **kwargs):
        with get_db() as conn:
            c = conn.cursor()
            updates = []
            params = []
            for k, v in kwargs.items():
                if k in ('name', 'model', 'category', 'form'):
                    updates.append(f'{k} = ?')
                    params.append(v)
            if updates:
                updates.append('updated_at = ?')
                params.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                params.append(host_id)
                c.execute(f'UPDATE host_devices SET {", ".join(updates)} WHERE host_id = ?', params)
                conn.commit()
            return True

    def delete(self, host_id):
        with get_db() as conn:
            c = conn.cursor()
            # 先删除关联关系
            c.execute('DELETE FROM device_host_relations WHERE host_id = ?', (host_id,))
            c.execute('DELETE FROM host_devices WHERE host_id = ?', (host_id,))
            conn.commit()
            return True


class HostDeviceCategoryModel:
    """宿主设备类型管理"""
    def __init__(self):
        init_db()

    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id, name, created_at FROM host_device_categories ORDER BY created_at ASC')
            rows = c.fetchall()
            return [self._row_to_dict(c, r) for r in rows]

    def get_by_name(self, name):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id, name, created_at FROM host_device_categories WHERE name = ?', (name,))
            row = c.fetchone()
            return self._row_to_dict(c, row)

    def add(self, name):
        with get_db() as conn:
            c = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('INSERT INTO host_device_categories (name, created_at) VALUES (?, ?)', (name, now))
            conn.commit()
            return c.lastrowid

    def rename(self, old_name, new_name):
        """合并：将 old_name 类型改为 new_name，同时更新 host_devices 表"""
        with get_db() as conn:
            c = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('UPDATE host_devices SET category = ?, updated_at = ? WHERE category = ?', (new_name, now, old_name))
            c.execute('DELETE FROM host_device_categories WHERE name = ?', (old_name,))
            conn.commit()
            return True

    def delete(self, name):
        """删除类型，如有关联设备则返回 False"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM host_devices WHERE category = ?', (name,))
            count = c.fetchone()[0]
            if count > 0:
                return False
            c.execute('DELETE FROM host_device_categories WHERE name = ?', (name,))
            conn.commit()
            return True

    def init_defaults(self):
        """初始化默认三种类型"""
        defaults = ['通信电台', '计算存储', '数据链']
        for name in defaults:
            existing = self.get_by_name(name)
            if not existing:
                self.add(name)


class DeviceHostRelationModel:
    """宿主设备与密码设备关联关系"""
    def __init__(self):
        init_db()

    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def add_relation(self, device_id, host_id, quantity=1):
        """添加关联，同一对设备-宿主只会有一条记录（UNIQUE约束）"""
        with get_db() as conn:
            c = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            try:
                c.execute('''INSERT INTO device_host_relations (device_id, host_id, quantity, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)''',
                    (device_id, host_id, quantity, now, now))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # 已存在，更新数量
                c.execute('UPDATE device_host_relations SET quantity = ?, updated_at = ? WHERE device_id = ? AND host_id = ?',
                    (quantity, now, device_id, host_id))
                conn.commit()
                return True

    def update_quantity(self, device_id, host_id, quantity):
        with get_db() as conn:
            c = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('UPDATE device_host_relations SET quantity = ?, updated_at = ? WHERE device_id = ? AND host_id = ?',
                (quantity, now, device_id, host_id))
            conn.commit()
            return True

    def remove_relation(self, device_id, host_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM device_host_relations WHERE device_id = ? AND host_id = ?', (device_id, host_id))
            conn.commit()
            return True

    def get_devices_by_host(self, host_id):
        """获取某宿主设备关联的全部密码设备（含数量）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''SELECT r.device_id, r.quantity, r.created_at, r.updated_at,
                e.name, e.model, e.category, e.tech_status
                FROM device_host_relations r
                JOIN equipment e ON r.device_id = e.equipment_id
                WHERE r.host_id = ?''', (host_id,))
            rows = c.fetchall()
            return [{
                'device_id': r[0], 'quantity': r[1], 'created_at': r[2], 'updated_at': r[3],
                'name': r[4], 'model': r[5], 'category': r[6], 'tech_status': r[7]
            } for r in rows]

    def get_hosts_by_device(self, device_id):
        """获取某密码设备关联的全部宿主设备（无数量概念）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''SELECT r.host_id, r.created_at,
                h.name, h.model, h.category, h.form
                FROM device_host_relations r
                JOIN host_devices h ON r.host_id = h.host_id
                WHERE r.device_id = ?''', (device_id,))
            rows = c.fetchall()
            return [{
                'host_id': r[0], 'created_at': r[1],
                'name': r[2], 'model': r[3], 'category': r[4], 'form': r[5]
            } for r in rows]

    def delete_by_host(self, host_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM device_host_relations WHERE host_id = ?', (host_id,))
            conn.commit()

    def delete_by_device(self, device_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM device_host_relations WHERE device_id = ?', (device_id,))
            conn.commit()

    def get_device_count_by_host(self, host_id):
        """获取某宿主设备关联的密码设备数量"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM device_host_relations WHERE host_id = ?', (host_id,))
            return c.fetchone()[0]


class ResearchUnitModel:
    """研制单位字典模型"""
    
    def __init__(self):
        pass
    
    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None
    
    def init_defaults(self):
        """初始化默认研制单位数据（如果为空）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM research_units')
            if c.fetchone()[0] == 0:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                defaults = [
                    ('RU2025052301', '解放军信息工程大学', '信息工程大学/解放军信息工程大学/陆装部'),
                    ('RU2025052302', '中国科学院信息工程研究所', '中科院信工所/信息安全共性技术研究中心'),
                    ('RU2025052303', '中国电子科技集团公司第三十研究所', '三十所/中国电科30所'),
                    ('RU2025052304', '中国电子科技集团公司第三十二研究所', '三十二所/中国电科32所'),
                    ('RU2025052305', '国家信息技术安全研究中心', '信息中心/安全中心'),
                    ('RU2025052306', '中国科学院软件研究所', '中科院软件所'),
                    ('RU2025052307', '华中科技大学', '华科大/HUST'),
                ]
                c.executemany(
                    'INSERT INTO research_units (unit_id, name, alias, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                    [(u[0], u[1], u[2], now, now) for u in defaults]
                )
                conn.commit()
    
    def get_all(self):
        """获取所有研制单位，按名称排序"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT unit_id, name, alias, created_at, updated_at FROM research_units ORDER BY name')
            return [self._row_to_dict(c, r) for r in c.fetchall()]
    
    def get_by_id(self, unit_id):
        """根据 unit_id 获取研制单位"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT unit_id, name, alias, created_at, updated_at FROM research_units WHERE unit_id = ?', (unit_id,))
            return self._row_to_dict(c, c.fetchone())
    
    def get_by_name(self, name):
        """根据名称精确查找"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT unit_id, name, alias, created_at, updated_at FROM research_units WHERE name = ?', (name,))
            return self._row_to_dict(c, c.fetchone())
    
    def name_exists(self, name, exclude_id=None):
        """检查名称是否已存在（排除指定 unit_id）"""
        with get_db() as conn:
            c = conn.cursor()
            if exclude_id:
                c.execute('SELECT unit_id FROM research_units WHERE name = ? AND unit_id != ?', (name, exclude_id))
            else:
                c.execute('SELECT unit_id FROM research_units WHERE name = ?', (name,))
            return c.fetchone() is not None
    
    def add(self, name, alias='', creator='系统'):
        """新增研制单位"""
        if self.name_exists(name):
            return {'error': '研制单位名称已存在'}
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM research_units")
            count = c.fetchone()[0] + 1
            unit_id = f'RU{datetime.now().strftime("%Y%m%d")}{count:02d}'
            
            c.execute('''INSERT INTO research_units (unit_id, name, alias, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)''',
                (unit_id, name, alias, now, now))
            conn.commit()
            return {'unit_id': unit_id, 'name': name, 'alias': alias}
    
    def update(self, unit_id, name, alias=''):
        """更新研制单位"""
        if self.name_exists(name, exclude_id=unit_id):
            return {'error': '研制单位名称已存在'}
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with get_db() as conn:
            c = conn.cursor()
            c.execute('UPDATE research_units SET name = ?, alias = ?, updated_at = ? WHERE unit_id = ?',
                (name, alias, now, unit_id))
            conn.commit()
            return {'unit_id': unit_id, 'name': name, 'alias': alias}
    
    def delete(self, unit_id):
        """删除研制单位"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM research_units WHERE unit_id = ?', (unit_id,))
            conn.commit()
            return True


class KnowledgeSubclassModel:
    """设备知识库子类字典模型"""

    def __init__(self):
        pass

    def _row_to_dict(self, c, row):
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row)) if row else None

    def init_defaults(self):
        """初始化默认子类数据（如果为空）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM knowledge_subclasses')
            if c.fetchone()[0] == 0:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                defaults = [
                    # 密码设备
                    ('密码设备', '密码机', '2026-01-01 00:00:00'),
                    ('密码设备', '安全芯片', '2026-01-01 00:00:00'),
                    ('密码设备', '加密卡', '2026-01-01 00:00:00'),
                    # 安全设备
                    ('安全设备', '防火墙', '2026-01-01 00:00:00'),
                    ('安全设备', '入侵检测', '2026-01-01 00:00:00'),
                    ('安全设备', '漏洞扫描', '2026-01-01 00:00:00'),
                    # 通用设备
                    ('通用设备', '服务器', '2026-01-01 00:00:00'),
                    ('通用设备', '存储设备', '2026-01-01 00:00:00'),
                ]
                c.executemany(
                    'INSERT INTO knowledge_subclasses (parent_category, subclass_name, created_at) VALUES (?, ?, ?)',
                    defaults
                )
                conn.commit()

    def get_all(self):
        """获取所有子类"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id, parent_category, subclass_name, created_at FROM knowledge_subclasses ORDER BY parent_category, subclass_name')
            return [self._row_to_dict(c, r) for r in c.fetchall()]

    def get_by_category(self, parent_category):
        """根据父分类获取子类列表"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id, parent_category, subclass_name, created_at FROM knowledge_subclasses WHERE parent_category = ? ORDER BY subclass_name', (parent_category,))
            return [self._row_to_dict(c, r) for r in c.fetchall()]

    def get_subclasses_by_categories(self, categories):
        """根据多个父分类获取子类列表"""
        with get_db() as conn:
            c = conn.cursor()
            placeholders = ','.join(['?'] * len(categories))
            c.execute(f'SELECT id, parent_category, subclass_name, created_at FROM knowledge_subclasses WHERE parent_category IN ({placeholders}) ORDER BY parent_category, subclass_name', categories)
            return [self._row_to_dict(c, r) for r in c.fetchall()]

    def name_exists(self, parent_category, subclass_name):
        """检查子类是否已存在"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id FROM knowledge_subclasses WHERE parent_category = ? AND subclass_name = ?', (parent_category, subclass_name))
            return c.fetchone() is not None

    def add(self, parent_category, subclass_name):
        """新增子类"""
        if self.name_exists(parent_category, subclass_name):
            return {'error': '该子类已存在'}
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with get_db() as conn:
            c = conn.cursor()
            c.execute('INSERT INTO knowledge_subclasses (parent_category, subclass_name, created_at) VALUES (?, ?, ?)',
                (parent_category, subclass_name, now))
            conn.commit()
            return {'parent_category': parent_category, 'subclass_name': subclass_name}

    def delete(self, id):
        """删除子类"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM knowledge_subclasses WHERE id = ?', (id,))
            conn.commit()
            return True
