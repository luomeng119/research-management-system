# -*- coding: utf-8 -*-
"""
方案论证模块 - 路由
"""
import os
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_file
from flask import current_app
import json
import uuid
from datetime import datetime
from werkzeug.utils import secure_filename

# 导入模型
from app.models_argumentation import (
    init_argumentation_db,
    get_template_by_category,
    get_all_templates,
    save_template,
    get_document_by_project,
    get_document_by_id,
    save_document,
    get_document_versions,
    save_document_version,
    get_version_by_num
)

argumentation_bp = Blueprint('argumentation', __name__)

# 板块分类
CATEGORIES = {
    '科研项目': 'research',
    '密码项目': 'crypto', 
    '安全项目': 'security'
}

# 允许的文件扩展名
ALLOWED_EXTENSIONS = {'docx', 'doc'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def init_template_data():
    """初始化示例模板数据"""
    # 检查是否已有模板
    for cat in ['research', 'crypto', 'security']:
        existing = get_template_by_category(cat)
        if existing:
            continue
        
        # 为不同类型创建不同模板
        if cat == 'research':
            template_name = "科研项目论证方案模板"
        elif cat == 'crypto':
            template_name = "密码项目论证方案模板"
        else:
            template_name = "安全项目论证方案模板"
        
        sample_template = {
            "template_id": f"{cat}_v1",
            "name": template_name,
            "category": cat,
            "version": 1,
            "chapters": [
                {"id": "ch1", "title": "一、项目概述", "type": "text",
                 "children": [
                     {"id": "ch1_1", "title": "1.1 项目背景", "type": "text"},
                     {"id": "ch1_2", "title": "1.2 研究目标", "type": "text"}
                 ]},
                {"id": "ch2", "title": "二、需求分析", "type": "text",
                 "children": [
                     {"id": "ch2_1", "title": "2.1 功能需求", "type": "text"},
                     {"id": "ch2_2", "title": "2.2 性能需求", "type": "text"}
                 ]},
                {"id": "ch3", "title": "三、方案设计", "type": "text",
                 "children": [
                     {"id": "ch3_1", "title": "3.1 总体设计", "type": "text"},
                     {"id": "ch3_2", "title": "3.2 详细设计", "type": "text"}
                 ]},
                {"id": "ch4", "title": "四、经费预算", "type": "budget"},
                {"id": "ch5", "title": "五、进度安排", "type": "text"},
                {"id": "ch6", "title": "六、风险分析", "type": "text"},
                {"id": "ch7", "title": "七、结论", "type": "text"}
            ]
        }
        
        save_template(sample_template['template_id'], sample_template['name'], cat, '', json.dumps(sample_template, ensure_ascii=False))
@argumentation_bp.route('/<category>')
def index(category):
    """方案论证首页 - 显示项目列表"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    # 获取该板块的所有项目
    from models import get_db
    with get_db() as conn:
        c = conn.cursor()
    
        table_map = {
            'research': 'projects',
            'crypto': 'crypto_projects', 
            'security': 'security_projects'
        }
    
        table_name = table_map.get(category, 'projects')
    
        try:
            c.execute(f"SELECT * FROM {table_name}")
            projects = c.fetchall()
        except:
            projects = []
    
    
        category_name = {'research': '科研项目', 'crypto': '密码项目', 'security': '安全项目'}.get(category, '科研项目')
    
        # 获取板块模板
        template = get_template_by_category(category)
        if not template:
            # 默认模板
            template = {
                "chapters": [
                    {"id": "ch1", "title": "一、项目概述", "type": "text",
                     "children": [{"id": "ch1_1", "title": "1.1 项目背景", "type": "text"}]},
                    {"id": "ch2", "title": "二、需求分析", "type": "text",
                     "children": [{"id": "ch2_1", "title": "2.1 功能需求", "type": "text"}]},
                    {"id": "ch3", "title": "三、方案设计", "type": "text",
                     "children": [{"id": "ch3_1", "title": "3.1 总体设计", "type": "text"}]},
                    {"id": "ch4", "title": "四、经费预算", "type": "text"},
                    {"id": "ch5", "title": "五、进度安排", "type": "text"},
                    {"id": "ch6", "title": "六、风险分析", "type": "text"},
                    {"id": "ch7", "title": "七、结论", "type": "text"}
                ]
            }
    
        return render_template(f'argumentation/index.html',
                               category=category,
                               category_name=category_name,
                               projects=projects,
                               template=template)


@argumentation_bp.route('/<category>/<project_id>')
def edit(category, project_id):
    """方案论证编辑页面"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    # 获取项目信息
    from models import get_db
    with get_db() as conn:
        c = conn.cursor()
    
        table_map = {
            'research': 'projects',
            'crypto': 'crypto_projects',
            'security': 'security_projects'
        }
    
        table_name = table_map.get(category, 'projects')
    
        try:
            c.execute(f"SELECT * FROM {table_name} WHERE project_id = ?", (project_id,))
            project_row = c.fetchone()
        except:
            project_row = None
    
    
        if not project_row:
            return "项目不存在", 404
    
        # 转换为字典
        project = dict(project_row)
    
        # 获取模板
        template = get_template_by_category(category)
        if template:
            template_data = json.loads(template['chapter_tree'])
        else:
            # 默认模板
            template_data = {
                "chapters": [
                    {"id": "ch1", "title": "一、项目概述", "type": "text",
                     "children": [{"id": "ch1_1", "title": "1.1 项目背景", "type": "text"}]},
                    {"id": "ch2", "title": "二、需求分析", "type": "text",
                     "children": [{"id": "ch2_1", "title": "2.1 功能需求", "type": "text"}]},
                    {"id": "ch3", "title": "三、方案设计", "type": "text",
                     "children": [{"id": "ch3_1", "title": "3.1 总体设计", "type": "text"}]},
                    {"id": "ch4", "title": "四、经费预算", "type": "text"},
                    {"id": "ch5", "title": "五、进度安排", "type": "text"},
                    {"id": "ch6", "title": "六、风险分析", "type": "text"},
                    {"id": "ch7", "title": "七、结论", "type": "text"}
                ]
            }
    
        # 确保 template 有 chapters 属性供模板使用
        if not template:
            template = template_data
        elif isinstance(template, dict) and 'chapters' not in template:
            template['chapters'] = template_data.get('chapters', [])
    
        # 获取已有文档
        doc_row = get_document_by_project(project_id, category)
        document = dict(doc_row) if doc_row else None
    
        # 获取设备列表（用于设备选择）
        from models import get_db as get_equipment_db
        conn = get_equipment_db()
        c = conn.cursor()
        c.execute("SELECT id, equipment_id, name, model, category, form, price, tech_index, tech_status, manufacturer FROM equipment")
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
        equipment_list = [dict(zip(cols, r)) for r in rows]
        # 获取分类列表
        all_categories = sorted(set([e['category'] for e in equipment_list if e['category']]))
    
        # 获取项目的设备选型（project_equipment表暂未建立，设为空）
        project_equipment = []
    
        # 获取版本历史
        versions = []
        if document:
            versions = get_document_versions(document['doc_id'])
    
        category_name = {'research': '科研项目', 'crypto': '密码项目', 'security': '安全项目'}.get(category, '科研项目')
    
        # 项目类型对应的URL前缀
        category_url = {
            'research': '/projects',
            'security': '/security_projects',
            'crypto': '/crypto_projects'
        }.get(category, '/projects')
    
        detail_url = f'{category_url}/detail/{project_id}'
    
        # 提取模板中的内容用于初始化
        template_content = {}
        if template_data and 'chapters' in template_data:
            for ch in template_data.get('chapters', []):
                if 'content' in ch:
                    template_content[ch['id']] = ch['content']
                if 'children' in ch:
                    for sub in ch.get('children', []):
                        if 'content' in sub:
                            template_content[sub['id']] = sub['content']
    
        return render_template(f'argumentation/edit.html',
                               category=category,
                               category_name=category_name,
                               project=dict(project) if project else {},
                               template=template,
                               template_data=template_data,
                               template_content=template_content,
                               document=document,
                               saved_content_str=json.dumps(document.get('content', {})) if document and document.get('content') else '{}',
                               equipment_list=equipment_list,
                               categories=all_categories,

                               project_equipment=[dict(pe) for pe in project_equipment] if project_equipment else [],
                               versions=versions,
                               back_url=detail_url)


@argumentation_bp.route('/save', methods=['POST'])
def save():
    """保存论证文档"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    data = request.json
    project_id = data.get('project_id')
    category = data.get('category')
    content = json.dumps(data.get('content', {}), ensure_ascii=False)
    change_note = data.get('change_note', '')
    
    if not project_id or not category:
        return jsonify({'success': False, 'message': '参数不完整'})
    
    # 获取模板
    template = get_template_by_category(category)
    
    # 生成文档ID
    doc_id = f"{category}_{project_id}"
    
    # 检查是否已有文档，获取当前版本号
    existing = get_document_by_project(project_id, category)
    version_num = 1
    if existing:
        # 获取最新版本号
        versions = get_document_versions(doc_id)
        if versions:
            version_num = versions[0]['version_num'] + 1
    
    # 保存当前版本（先保存内容，不生成Word）
    # 保存到项目文件夹
    project_folder = os.path.join(current_app.config.get('UPLOAD_DIR', 'uploads'), project_id, '论证文档')
    os.makedirs(project_folder, exist_ok=True)
    
    # 同时保存内容JSON到项目文件夹
    content_file = os.path.join(project_folder, f'{doc_id}_content.json')
    with open(content_file, 'w', encoding='utf-8') as f:
        f.write(content)
    
    # 保存文档到数据库
    save_document(doc_id, project_id, category, 
                  template['template_id'] if template else '', 
                  content, content_file)
    
    # 保存版本历史
    version_id = f"{doc_id}_v{version_num}"
    save_document_version(version_id, doc_id, version_num, content, '', 
                         session.get('user'), change_note)
    
    return jsonify({
        'success': True, 
        'message': f'保存成功（版本 {version_num}）',
        'doc_id': doc_id,
        'version': version_num
    })


@argumentation_bp.route('/generate_word', methods=['POST'])
def generate_word():
    """生成Word文档 - 前端组装数据，后端直接生成"""
    try:
        data = request.json
        project_name = data.get('project_name')          # 项目名称（前端传）
        category = data.get('category')              # 类型
        template = data.get('template', [])         # 模板结构
        content = data.get('content', {})            # 内容
        
        if not project_name or not category:
            return jsonify({'success': False, 'message': '参数不完整'})
        
        # 文件名
        word_filename = f"论证方案-{uuid.uuid4().hex}.docx"
        word_dir = os.path.join(current_app.config.get('DATA_DIR', 'data'), 'documents')
        os.makedirs(word_dir, exist_ok=True)
        word_path = os.path.join(word_dir, word_filename)
        
        # 生成Word
        try:
            from docx import Document
            from docx.shared import Pt
            from docx.enum.text import WD_ALIGN_PARAGRAPH
        except ImportError:
            return jsonify({'success': False, 'message': '缺少python-docx库'})
        
        doc = Document()
        title = doc.add_heading(project_name, 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # 添加内容
        import re
        for chapter in template:
            doc.add_heading(chapter.get('title', ''), level=1)
            for sub in chapter.get('children', []):
                sub_id = sub.get('id', '')
                doc.add_heading(sub.get('title', ''), level=2)
                sub_content = content.get(sub_id, '')
                if sub_content:
                    text = re.sub('<[^>]+>', '', sub_content)
                    text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>')
                    if text.strip():
                        doc.add_paragraph(text)
            if not chapter.get('children'):
                ch_content = content.get(chapter.get('id', ''), '')
                if ch_content:
                    text = re.sub('<[^>]+>', '', ch_content)
                    text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>')
                    if text.strip():
                        doc.add_paragraph(text)
        
        doc.save(word_path)
        
        # 返回相对路径
        relative_path = f'data/documents/{word_filename}'
        
        return jsonify({
            'success': True, 
            'message':'生成word成功',
            'word_path': relative_path
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e)})
            

@argumentation_bp.route('/versions/<doc_id>')
def versions(doc_id):
    """查看文档版本历史"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    versions = get_document_versions(doc_id)
    document = get_document_by_id(doc_id)
    
    return render_template('argumentation/versions.html',
                           versions=versions,
                           document=document)


@argumentation_bp.route('/versions/<doc_id>/<int:version_num>')
def view_version(doc_id, version_num):
    """查看指定版本"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    version = get_version_by_num(doc_id, version_num)
    if not version:
        return "版本不存在", 404
    
    document = get_document_by_id(doc_id)
    template = get_template_by_category(document['category']) if document else None
    
    if template:
        template_data = json.loads(template['chapter_tree'])
    else:
        template_data = {'chapters': []}
    
    return render_template('argumentation/edit.html',
                           category=document['category'],
                           project={'project_id': document['project_id'], 'name': '版本查看'},
                           template=template_data,
                           document={'content': version['content']},
                           saved_content_str=version['content'],
                           equipment_list=[],
                           project_equipment=[],
                           versions=[],
                           is_version_view=True,
                           current_version=version_num)


@argumentation_bp.route('/template/upload', methods=['POST'])
def upload_template():
    """上传新模板"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '权限不足'})
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '请选择文件'})
    
    file = request.files['file']
    category = request.form.get('category')
    name = request.form.get('name', '未命名模板')
    
    if file.filename == '':
        return jsonify({'success': False, 'message': '请选择文件'})
    
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': '只支持docx格式'})
    
    if not category:
        return jsonify({'success': False, 'message': '请选择板块'})
    
    # 保存文件
    upload_dir = os.path.join(current_app.config.get('DATA_DIR', 'data'), 'templates')
    os.makedirs(upload_dir, exist_ok=True)
    
    filename = f"{category}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
    file_path = os.path.join(upload_dir, filename)
    file.save(file_path)
    
    # TODO: 解析Word模板生成章节树
    # 这里先用空模板
    chapter_tree = json.dumps({
        "template_id": f"{category}_v1",
        "name": name,
        "category": category,
        "version": 1,
        "chapters": []
    }, ensure_ascii=False)
    
    # 保存模板
    template_id = f"{category}_v1"
    save_template(template_id, name, category, file_path, chapter_tree)
    
    return jsonify({'success': True, 'message': '模板上传成功'})


@argumentation_bp.route('/template/edit', methods=['POST'])
def edit_template():
    """编辑模板章节结构"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '请先登录'})
    
    data = request.json
    category = data.get('category')
    chapter_tree = data.get('chapter_tree')
    
    if not category or not chapter_tree:
        return jsonify({'success': False, 'message': '参数不完整'})
    
    template_id = f"{category}_v1"
    category_name = {'research': '科研项目论证方案模板', 'crypto': '密码项目论证方案模板', 'security': '安全项目论证方案模板'}.get(category, '论证方案模板')
    
    existing = get_template_by_category(category)
    
    if existing:
        # 更新
        save_template(template_id, existing['name'], category, existing['file_path'], chapter_tree)
    else:
        # 创建新模板
        save_template(template_id, category_name, category, '', chapter_tree)
    
    return jsonify({'success': True, 'message': '模板保存成功'})


def init_argumentation_routes(app):
    """初始化路由"""
    app.register_blueprint(argumentation_bp, url_prefix='/argumentation')
    init_argumentation_db()
    init_template_data()


@argumentation_bp.route('/download')
def download_word():
    """下载Word文档"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    filepath = request.args.get('path', '')
    if not filepath:
        return "缺少文件路径", 400
    
    # Only accept the legacy reference emitted by generate_word, never an OS path.
    prefix = 'data/documents/'
    if not filepath.startswith(prefix):
        return "非法文档引用", 400
    filename = filepath[len(prefix):]
    if (not filename or any(part in filename for part in ('/', '\\', '\x00', '%'))
            or Path(filename).suffix.lower() not in {'.docx', '.doc'}):
        return "非法文档引用", 400
    data_root = Path(current_app.config['DATA_DIR']).resolve()
    document_root = data_root / 'documents'
    candidate = document_root / filename
    try:
        if (document_root.is_symlink() or candidate.is_symlink()
                or candidate.resolve().parent != document_root
                or not candidate.is_file()):
            return "文件不存在", 404
    except (OSError, ValueError):
        return "文件不存在", 404
    return send_file(candidate, as_attachment=True)
