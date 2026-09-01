# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, current_app
from app.models import StandardModel, OperationLogModel
import os
import re
from datetime import datetime

bp = Blueprint('standards', __name__, url_prefix='/standards')


def _safe_filename(filename):
    filename = os.path.basename(filename.replace('\\', '/'))
    filename = re.sub(r'[^\w\s.-]', '_', filename).strip('. ')
    return filename or 'unnamed'

@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    standard_model = StandardModel()
    category_filter = request.args.get('category', '')
    
    if category_filter:
        standards = standard_model.get_by_category(category_filter)
    else:
        standards = standard_model.get_all()
    
    category_groups = {}
    for s in standards:
        category = s['category'] if s.get('category') else '未分类'
        if category not in category_groups:
            category_groups[category] = []
        category_groups[category].append(s)
    
    return render_template('standards/index.html', standards=standards, category_groups=category_groups, category_filter=category_filter)

@bp.route('/upload', methods=['POST'])
def upload():
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    name = request.form.get('name', '').strip()
    category = request.form.get('category', '').strip()
    
    if not name or not category:
        return jsonify({'success': False, 'message': '请填写完整信息'})
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '请选择文件'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '请选择文件'})
    
    standards_dir = os.path.join(current_app.config['UPLOAD_DIR'], 'standards')
    os.makedirs(standards_dir, exist_ok=True)
    
    filename = _safe_filename(file.filename)
    file_path = os.path.join(standards_dir, filename)
    file.save(file_path)
    
    standard_model = StandardModel()
    file_type = filename.split('.')[-1].upper()
    standard_model.add(name, category, file_type, session.get('user'), file_path)
    
    # 记录操作日志
    log_model = OperationLogModel()
    log_model.add(module='standards', operation_type='上传文件', file_name=filename, operator=session.get('user', '未知'))
    
    return jsonify({'success': True, 'message': '上传成功'})

@bp.route('/download/<doc_id>')
def download(doc_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    standard_model = StandardModel()
    standard = standard_model.get_by_id(doc_id)
    if not standard:
        return jsonify({'success': False, 'message': '文件不存在'}), 404
    from flask import send_file
    file_path = standard.get('file_path', '')
    if not os.path.exists(file_path):
        return jsonify({'success': False, 'message': '文件不存在'}), 404
    filename = standard.get('name', doc_id)
    return send_file(file_path, as_attachment=True, download_name=filename)

@bp.route('/delete/<doc_id>', methods=['POST'])
def delete(doc_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    role = session.get('role')
    if role != '管理员':
        return jsonify({'success': False, 'message': '仅管理员可删除'})
    
    standard_model = StandardModel()
    standard = standard_model.get_by_id(doc_id)
    standard_name = standard['name'] if standard else doc_id
    standard_model.delete(doc_id)
    
    # 记录操作日志
    log_model = OperationLogModel()
    log_model.add(module='standards', operation_type='删除文件', file_name=standard_name, operator=session.get('user', '未知'))
    
    return jsonify({'success': True, 'message': '删除成功'})

@bp.route('/logs/<module_name>')
def logs(module_name):
    """操作日志页面"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    # 获取筛选参数
    operator = request.args.get('operator')
    file_name = request.args.get('file_name')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    log_model = OperationLogModel()
    logs = log_model.search(
        module=module_name,
        operator=operator,
        file_name=file_name,
        start_date=start_date,
        end_date=end_date,
        limit=100
    )
    
    # 获取标准列表
    standard_model = StandardModel()
    standards = standard_model.get_all()
    
    # 按分类分组
    category_groups = {}
    for s in standards:
        category = s['category'] if s.get('category') else '未分类'
        if category not in category_groups:
            category_groups[category] = []
        category_groups[category].append(s)
    
    module_names = {
        'equipment': '设备知识库',
        'standards': '标准法规库',
        'templates': '科研模板'
    }
    
    return render_template('standards/logs.html', 
                          logs=logs, 
                          standards=standards,
                          category_groups=category_groups,
                          module_name=module_name,
                          module_title=module_names.get(module_name, '日志'))
