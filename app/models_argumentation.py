# -*- coding: utf-8 -*-
"""
方案论证模块 - 数据库模型
"""
import sqlite3
import os
from datetime import datetime
from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, 'research.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_argumentation_db():
    """初始化方案论证相关表"""
    with get_db() as conn:
        c = conn.cursor()
    
        # 先删除旧表（schema 可能不一致），再重建
        c.execute('DROP TABLE IF EXISTS doc_templates')
        c.execute('DROP TABLE IF EXISTS project_documents')
        c.execute('DROP TABLE IF EXISTS document_versions')
    
        # 论证模板表
        c.execute('''CREATE TABLE IF NOT EXISTS doc_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            file_path TEXT,
            chapter_tree TEXT,
            version INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )''')
    
        # 项目论证文档表
        c.execute('''CREATE TABLE IF NOT EXISTS project_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT UNIQUE NOT NULL,
            project_id TEXT NOT NULL,
            category TEXT NOT NULL,
            template_id TEXT,
            content TEXT,
            word_file_path TEXT,
            status TEXT DEFAULT 'draft',
            created_at TEXT,
            updated_at TEXT
        )''')
    
        # 文档版本历史表
        c.execute('''CREATE TABLE IF NOT EXISTS document_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT UNIQUE NOT NULL,
            doc_id TEXT NOT NULL,
            version_num INTEGER,
            content TEXT,
            word_file_path TEXT,
            editor TEXT,
            change_note TEXT,
            created_at TEXT
        )''')
    
        conn.commit()


# ==================== 模板管理 ====================

def get_template_by_category(category):
    """获取指定板块的模板"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM doc_templates WHERE category = ? ORDER BY version DESC LIMIT 1", (category,))
        row = c.fetchone()
        return dict(row) if row else None

def get_all_templates():
    """获取所有模板"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM doc_templates ORDER BY category, version DESC")
        rows = c.fetchall()
        return [dict(r) for r in rows]

def save_template(template_id, name, category, file_path, chapter_tree):
    """保存模板"""
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
        # 检查是否存在
        c.execute("SELECT id, version FROM doc_templates WHERE template_id = ?", (template_id,))
        existing = c.fetchone()
    
        if existing:
            new_version = existing['version'] + 1
            c.execute('''UPDATE doc_templates 
                SET name=?, file_path=?, chapter_tree=?, version=?, updated_at=?
                WHERE template_id=?''',
                (name, file_path, chapter_tree, new_version, now, template_id))
        else:
            c.execute('''INSERT INTO doc_templates (template_id, name, category, file_path, chapter_tree, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (template_id, name, category, file_path, chapter_tree, now, now))
    
        conn.commit()


# ==================== 文档管理 ====================

def get_document_by_project(project_id, category):
    """获取项目的论证文档"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM project_documents WHERE project_id = ? AND category = ?", 
                  (project_id, category))
        row = c.fetchone()
        return dict(row) if row else None

def get_document_by_id(doc_id):
    """根据ID获取文档"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM project_documents WHERE doc_id = ?", (doc_id,))
        row = c.fetchone()
        return dict(row) if row else None

def save_document(doc_id, project_id, category, template_id, content, word_file_path):
    """保存或更新文档，返回doc_id"""
    import uuid
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
        if doc_id == 0 or not doc_id:
            # 新建文档，生成doc_id
            doc_id = str(uuid.uuid4())[:8]
            c.execute('''INSERT INTO project_documents 
                (doc_id, project_id, category, template_id, content, word_file_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (doc_id, project_id, category, template_id, content, word_file_path, now, now))
        else:
            c.execute("SELECT id FROM project_documents WHERE doc_id = ?", (doc_id,))
            existing = c.fetchone()
            if existing:
                c.execute('''UPDATE project_documents 
                    SET template_id=?, content=?, word_file_path=?, updated_at=?
                    WHERE doc_id=?''',
                    (template_id, content, word_file_path, now, doc_id))
            else:
                doc_id = str(uuid.uuid4())[:8]
                c.execute('''INSERT INTO project_documents 
                    (doc_id, project_id, category, template_id, content, word_file_path, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (doc_id, project_id, category, template_id, content, word_file_path, now, now))
    
        conn.commit()
        return doc_id

def get_document_versions(doc_id):
    """获取文档的所有版本"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM document_versions WHERE doc_id = ? ORDER BY version_num DESC", (doc_id,))
        rows = c.fetchall()
        return [dict(r) for r in rows]

def save_document_version(version_id, doc_id, version_num, content, word_file_path, editor, change_note):
    """保存文档版本"""
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
        c.execute('''INSERT INTO document_versions 
            (version_id, doc_id, version_num, content, word_file_path, editor, change_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (version_id, doc_id, version_num, content, word_file_path, editor, change_note, now))
    
        conn.commit()

def get_version_by_num(doc_id, version_num):
    """获取指定版本的文档"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM document_versions WHERE doc_id = ? AND version_num = ?", 
                  (doc_id, version_num))
        row = c.fetchone()
        return dict(row) if row else None
