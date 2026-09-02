# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, send_file, current_app
from datetime import datetime
from app.models import EquipmentModel, EquipmentGroupModel, ProjectModel, SecurityProjectModel, CryptoProjectModel
from app.routes._project_bridge import all_legacy_projects
import openpyxl
from io import BytesIO

bp = Blueprint('equipment_groups', __name__, url_prefix='/equipment/groups')

def get_all_projects():
    """获取所有类型的项目（科研项目、安全保密项目、密码应用项目）"""
    all_projects = []
    project_service = current_app.extensions.get('project_service')
    if project_service is not None:
        labels = {
            'GENERAL_RESEARCH': '科研项目',
            'SECURITY_CONFIDENTIALITY': '安全保密项目',
            'CRYPTO_APPLICATION': '密码应用项目',
        }
        for category, label in labels.items():
            for project in all_legacy_projects(project_service, category):
                all_projects.append({
                    'id': project['project_id'], 'name': project['name'],
                    'leader': project['leader'], 'type': label,
                })
        return all_projects
    
    # 科研项目
    pm = ProjectModel()
    for p in pm.get_all():
        all_projects.append({
            'id': p['project_id'],
            'name': p['name'],
            'leader': p['leader'],
            'type': '科研项目'
        })
    
    # 安全保密项目
    spm = SecurityProjectModel()
    for p in spm.get_all():
        all_projects.append({
            'id': p['project_id'],
            'name': p['name'],
            'leader': p['leader'],
            'type': '安全保密项目'
        })
    
    # 密码应用项目
    cpm = CryptoProjectModel()
    for p in cpm.get_all():
        all_projects.append({
            'id': p['project_id'],
            'name': p['name'],
            'leader': p['leader'],
            'type': '密码应用项目'
        })
    
    return all_projects

@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    project_id = request.args.get('project_id', '').strip()
    model = EquipmentGroupModel()
    groups = model.get_all(project_id if project_id else None)
    return render_template('equipment/groups.html', groups=groups, filter_project_id=project_id)

@bp.route('/new', methods=['GET', 'POST'])
def new():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    # 获取所有类型的项目列表
    all_projects = get_all_projects()
    
    my_projects = all_projects
    
    if request.method == 'POST':
        project_id_raw = request.form.get('project_id', '').strip()
        # 格式: project_id|project_name 或纯 project_id
        if '|' in project_id_raw:
            project_id = project_id_raw.split('|')[0]
        else:
            project_id = project_id_raw if project_id_raw else None
        
        project_name = request.form.get('project_name', '').strip()
        
        if not project_name:
            # 如果关联了项目，使用项目名称作为默认
            if project_id:
                proj = next((item for item in my_projects if item['id'] == project_id), None)
                if proj:
                    project_name = proj['name']
            if not project_name:
                project_name = datetime.now().strftime('%Y-%m-%d项目')
        
        creator = session.get('user')
        model = EquipmentGroupModel()
        result = model.create_group(project_name, creator, project_id)
        
        # 检查是否有错误
        if isinstance(result, dict) and 'error' in result:
            flash(result['error'], 'error')
            return render_template('equipment/group_new.html', my_projects=my_projects)
        
        group_id = result
        flash('设备组创建成功', 'success')
        return redirect(url_for('equipment_groups.edit', group_id=group_id))
    
    return render_template('equipment/group_new.html', my_projects=my_projects)

@bp.route('/edit/<group_id>', methods=['GET', 'POST'])
def edit(group_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    group_model = EquipmentGroupModel()
    equipment_model = EquipmentModel()
    
    group = group_model.get_by_id(group_id)
    if not group:
        flash('设备组不存在', 'error')
        return redirect(url_for('equipment_groups.index'))
    
    # API: 实时搜索
    if request.args.get('api') == 'search':
        search = request.args.get('q', '').strip()
        filter_category = request.args.get('category', '').strip()
        filter_form = request.args.get('form', '').strip()
        
        added_ids = [m['equipment_id'] for m in group['members']]
        all_equipment = equipment_model.get_all()
        available_equipment = [e for e in all_equipment if e['equipment_id'] not in added_ids]
        
        if search:
            available_equipment = [e for e in available_equipment 
                if search.lower() in (e.get('name') or '').lower() 
                or search.lower() in (e.get('model') or '').lower()]
        if filter_category:
            available_equipment = [e for e in available_equipment if e.get('category') == filter_category]
        if filter_form:
            available_equipment = [e for e in available_equipment if e.get('form') == filter_form]
        
        return jsonify({'items': available_equipment[:20]})
    
    search = request.args.get('search', '').strip()
    filter_category = request.args.get('category', '').strip()
    filter_form = request.args.get('form', '').strip()
    
    added_ids = [m['equipment_id'] for m in group['members']]
    all_equipment = equipment_model.get_all()
    available_equipment = [e for e in all_equipment if e['equipment_id'] not in added_ids]
    
    # 获取唯一筛选选项
    categories = sorted(list(set(e.get('category') for e in all_equipment if e.get('category'))))
    forms = sorted(list(set(e.get('form') for e in all_equipment if e.get('form'))))
    
    if search:
        available_equipment = [e for e in available_equipment 
            if search.lower() in (e.get('name') or '').lower() 
            or search.lower() in (e.get('model') or '').lower()]
    if filter_category:
        available_equipment = [e for e in available_equipment if e.get('category') == filter_category]
    if filter_form:
        available_equipment = [e for e in available_equipment if e.get('form') == filter_form]
    
    if request.method == 'POST':
        action = request.form.get('action')
        equipment_id = request.form.get('equipment_id')
        equipment_ids = request.form.getlist('equipment_ids')
        quantity = int(request.form.get('quantity', 1))
        
        if action == 'add':
            if equipment_ids:
                for eid in equipment_ids:
                    group_model.add_member(group_id, eid, quantity, session.get('user'))
                flash(f'成功添加 {len(equipment_ids)} 台设备', 'success')
            elif equipment_id:
                group_model.add_member(group_id, equipment_id, quantity, session.get('user'))
                flash('添加成功', 'success')
        elif action == 'remove' and equipment_id:
            group_model.remove_member(group_id, equipment_id)
            flash('移除成功', 'success')
        
        return redirect(url_for('equipment_groups.edit', group_id=group_id))
    
    return render_template('equipment/group_edit.html', group=group, available_equipment=available_equipment, categories=categories, forms=forms)

@bp.route('/delete/<group_id>', methods=['POST'])
def delete(group_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    model = EquipmentGroupModel()
    model.delete_group(group_id)
    return jsonify({'success': True, 'message': '删除成功'})

@bp.route('/export/<group_id>')
def export(group_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    model = EquipmentGroupModel()
    group = model.get_by_id(group_id)
    if not group:
        flash('设备组不存在', 'error')
        return redirect(url_for('equipment_groups.index'))
    
    output = BytesIO()
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = '设备组'
    
    ws.append(['项目名称', group['project_name']])
    ws.append(['创建人', group['creator']])
    ws.append(['创建时间', group['created_at']])
    ws.append([])
    ws.append(['序号', '设备名称', '型号', '分类', '形态', '单价', '功能技术指标', '技术状态', '研制单位', '主要用途', '曾用名', '资源保障', '加装要求', '数量', '挑选人', '挑选时间'])
    
    for i, m in enumerate(group['members'], 1):
        ws.append([i, m['name'], m['model'], m['category'], m['form'], m['price'], m.get('tech_index') or '', m.get('tech_status') or '', m.get('manufacturer') or '', m.get('main_purpose') or '', m.get('former_name') or '', m.get('resource_guarantee') or '', m.get('installation_requirements') or '', m['quantity'], m['selected_by'], m['selected_at']])
    
    workbook.save(output)
    output.seek(0)
    return send_file(output, download_name=f'{group["project_name"]}_设备组.xlsx', as_attachment=True)
