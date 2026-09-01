# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from datetime import datetime
from app.models import ExpertModel, ExpertGroupModel
import openpyxl
from io import BytesIO

bp = Blueprint('expert_groups', __name__, url_prefix='/experts/groups')

@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    model = ExpertGroupModel()
    groups = model.get_all()
    return render_template('experts/groups.html', groups=groups)

@bp.route('/new', methods=['GET', 'POST'])
def new():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    if request.method == 'POST':
        meeting_name = request.form.get('meeting_name', '').strip()
        if not meeting_name:
            meeting_name = datetime.now().strftime('%Y-%m-%d会议')
        
        creator = session.get('user')
        model = ExpertGroupModel()
        group_id = model.create_group(meeting_name, creator)
        flash('专家组创建成功', 'success')
        return redirect(url_for('expert_groups.edit', group_id=group_id))
    
    return render_template('experts/group_new.html')

@bp.route('/edit/<group_id>', methods=['GET', 'POST'])
def edit(group_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    group_model = ExpertGroupModel()
    expert_model = ExpertModel()
    
    group = group_model.get_by_id(group_id)
    if not group:
        flash('专家组不存在', 'error')
        return redirect(url_for('expert_groups.index'))
    
    # API: 实时搜索
    if request.args.get('api') == 'search':
        search = request.args.get('q', '').strip()
        filter_unit = request.args.get('unit', '').strip()
        filter_position = request.args.get('position', '').strip()
        filter_expertise = request.args.get('expertise', '').strip()
        
        added_ids = [m['expert_id'] for m in group['members']]
        all_experts = expert_model.get_all()
        available_experts = [e for e in all_experts if e['expert_id'] not in added_ids]
        
        if search:
            available_experts = [e for e in available_experts 
                if search.lower() in (e.get('name') or '').lower() 
                or search.lower() in (e.get('unit') or '').lower()
                or search.lower() in (e.get('expertise') or '').lower()]
        if filter_unit:
            available_experts = [e for e in available_experts if e.get('unit') == filter_unit]
        if filter_position:
            available_experts = [e for e in available_experts if e.get('position') == filter_position]
        if filter_expertise:
            available_experts = [e for e in available_experts if e.get('expertise') == filter_expertise]
        
        return jsonify({'items': available_experts[:20]})
    
    # 获取筛选参数
    search = request.args.get('search', '').strip()
    filter_unit = request.args.get('unit', '').strip()
    filter_position = request.args.get('position', '').strip()
    filter_expertise = request.args.get('expertise', '').strip()
    
    # 获取未添加的专家
    added_ids = [m['expert_id'] for m in group['members']]
    all_experts = expert_model.get_all()
    available_experts = [e for e in all_experts if e['expert_id'] not in added_ids]
    
    # 获取唯一筛选选项
    units = sorted(list(set(e.get('unit') for e in all_experts if e.get('unit'))))
    positions = sorted(list(set(e.get('position') for e in all_experts if e.get('position'))))
    expertises = sorted(list(set(e.get('expertise') for e in all_experts if e.get('expertise'))))
    
    # 筛选
    if search:
        available_experts = [e for e in available_experts 
            if search.lower() in (e.get('name') or '').lower() 
            or search.lower() in (e.get('unit') or '').lower()
            or search.lower() in (e.get('expertise') or '').lower()]
    if filter_unit:
        available_experts = [e for e in available_experts if e.get('unit') == filter_unit]
    if filter_position:
        available_experts = [e for e in available_experts if e.get('position') == filter_position]
    if filter_expertise:
        available_experts = [e for e in available_experts if e.get('expertise') == filter_expertise]
    
    # 处理POST添加
    if request.method == 'POST':
        action = request.form.get('action')
        expert_id = request.form.get('expert_id')
        expert_ids = request.form.getlist('expert_ids')
        
        if action == 'add':
            if expert_ids:
                for eid in expert_ids:
                    group_model.add_member(group_id, eid, session.get('user'))
                flash(f'成功添加 {len(expert_ids)} 位专家', 'success')
            elif expert_id:
                group_model.add_member(group_id, expert_id, session.get('user'))
                flash('添加成功', 'success')
        elif action == 'remove' and expert_id:
            group_model.remove_member(group_id, expert_id)
            flash('移除成功', 'success')
        
        return redirect(url_for('expert_groups.edit', group_id=group_id))
    
    return render_template('experts/group_edit.html', group=group, available_experts=available_experts, units=units, positions=positions, expertises=expertises)

@bp.route('/delete/<group_id>', methods=['POST'])
def delete(group_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    role = session.get('role')
    if role != '管理员':
        return jsonify({'success': False, 'message': '无权限'})
    
    model = ExpertGroupModel()
    model.delete_group(group_id)
    return jsonify({'success': True, 'message': '删除成功'})

@bp.route('/export/<group_id>')
def export(group_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    model = ExpertGroupModel()
    group = model.get_by_id(group_id)
    if not group:
        flash('专家组不存在', 'error')
        return redirect(url_for('expert_groups.index'))
    
    # 导出Excel
    output = BytesIO()
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = '专家组'
    
    ws.append(['会议名称', group['meeting_name']])
    ws.append(['创建人', group['creator']])
    ws.append(['创建时间', group['created_at']])
    ws.append([])
    ws.append(['序号', '姓名', '单位', '职务', '专业领域', '手机', '银行卡号', '开户行', '挑选人', '挑选时间'])
    
    for i, m in enumerate(group['members'], 1):
        ws.append([i, m['name'], m['unit'], m['position'], m['expertise'], m['phone'] or '', m['bank_card'] or '', m['bank_name'] or '', m['selected_by'], m['selected_at']])
    
    workbook.save(output)
    output.seek(0)
    return send_file(output, download_name=f'{group["meeting_name"]}_专家组.xlsx', as_attachment=True)
