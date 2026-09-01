# -*- coding: utf-8 -*-
"""
方案论证模块 - 模板管理路由
"""
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from flask import current_app
import json
import uuid
from datetime import datetime

# 导入模型
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from models_argumentation import (
    get_template_by_category,
    get_all_templates,
    save_template,
    init_argumentation_db
)

template_bp = Blueprint('template', __name__, url_prefix='/template')


@template_bp.route('/manage')
def manage():
    """模板管理页面"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    templates = get_all_templates()
    
    # 解析 chapter_tree JSON
    for t in templates:
        if t.get('chapter_tree'):
            try:
                t['chapters_json'] = json.loads(t['chapter_tree'])
            except:
                t['chapters_json'] = {'chapters': []}
        else:
            t['chapters_json'] = {'chapters': []}
    
    # 三类模板
    categories = [
        {'id': 'research', 'name': '科研项目'},
        {'id': 'security', 'name': '安全保密项目'},
        {'id': 'crypto', 'name': '密码应用项目'}
    ]
    
    return render_template('template/manage.html', 
                       templates=templates,
                       categories=categories)


@template_bp.route('/edit/<category>')
def edit(category):
    """编辑指定分类的模板"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    template = get_template_by_category(category)
    
    if template and template.get('chapter_tree'):
        try:
            template_data = json.loads(template['chapter_tree'])
        except:
            template_data = {'chapters': []}
    else:
        template_data = {
            'template_id': f'{category}_v1',
            'name': '',
            'category': category,
            'version': 1,
            'chapters': []
        }
    
    category_names = {
        'research': '科研项目',
        'security': '安全保密项目',
        'crypto': '密码应用项目'
    }
    
    return render_template('template/edit.html',
                        category=category,
                        category_name=category_names.get(category, category),
                        template_data=template_data)


@template_bp.route('/api/save', methods=['POST'])
def api_save():
    """保存模板API"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '请先登录'}), 401
    
    data = request.json
    category = data.get('category')
    template_data = data.get('template_data')
    
    if not category or not template_data:
        return jsonify({'success': False, 'message': '参数不完整'})
    
    template_id = f'{category}_v1'
    chapter_tree = json.dumps(template_data, ensure_ascii=False)
    
    save_template(template_id, template_data.get('name', ''), category, '', chapter_tree)
    
    return jsonify({'success': True, 'message': '模板保存成功'})


@template_bp.route('/api/get/<category>')
def api_get(category):
    """获取模板API"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '请先登录'})
    
    template = get_template_by_category(category)
    
    if template and template.get('chapter_tree'):
        try:
            template_data = json.loads(template['chapter_tree'])
        except:
            template_data = {'chapters': []}
    else:
        template_data = {'chapters': []}
    
    return jsonify({'success': True, 'template': template_data})
