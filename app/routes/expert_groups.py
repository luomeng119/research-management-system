# -*- coding: utf-8 -*-
from flask import Blueprint, current_app, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from datetime import datetime
import openpyxl
from io import BytesIO

bp = Blueprint('expert_groups', __name__, url_prefix='/experts/groups')


def _xlsx_cell(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.isoformat()
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def _resources_service():
    service = current_app.extensions.get('resources_service')
    if service is None:
        raise RuntimeError('专家库服务未就绪')
    return service


def _actor():
    return {
        'user_id': int(session.get('user_id') or 0),
        'name': session.get('name') or session.get('user') or '',
        'request_id': getattr(request, 'request_id', 'legacy-expert-group-request'),
    }


def _legacy_expert(item):
    return {
        'id': item.get('id'), 'expert_id': item.get('expertId'),
        'name': item.get('name'), 'unit': item.get('unit'),
        'position': item.get('position'), 'expertise': item.get('expertise'),
        'phone': item.get('phone'), 'id_card': item.get('idCard'),
        'bank_card': item.get('bankCard'), 'bank_name': item.get('bankName'),
        'selected_by': item.get('selectedBy'),
        'selected_at': item.get('selectedAt'),
    }


def _legacy_group(item):
    return {
        'group_id': item.get('groupId'),
        'meeting_name': item.get('groupName'),
        'creator': item.get('creator'),
        'created_at': item.get('createdAt'),
        'member_count': item.get('memberCount', len(item.get('members', []))),
        'members': [_legacy_expert(member) for member in item.get('members', [])],
    }

@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    groups = [_legacy_group(item) for item in _resources_service().list_expert_groups(
        page=max(1, int(request.args.get('page', 1) or 1)), page_size=100
    )['items']]
    return render_template('experts/groups.html', groups=groups)

@bp.route('/new', methods=['GET', 'POST'])
def new():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    if request.method == 'POST':
        meeting_name = request.form.get('group_name', request.form.get('meeting_name', '')).strip()
        if not meeting_name:
            meeting_name = datetime.now().strftime('%Y-%m-%d专家组')
        
        creator = session.get('user')
        group_id = _resources_service().create_expert_group(
            {'groupName': meeting_name}, creator=creator
        )['groupId']
        flash('专家组创建成功', 'success')
        return redirect(url_for('expert_groups.edit', group_id=group_id))
    
    return render_template('experts/group_new.html')

@bp.route('/edit/<group_id>', methods=['GET', 'POST'])
def edit(group_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    service = _resources_service()
    try:
        group = _legacy_group(service.get_expert_group(group_id))
    except Exception:
        group = None
    if not group:
        flash('专家组不存在', 'error')
        return redirect(url_for('expert_groups.index'))
    
    # API: 实时搜索
    if request.args.get('api') == 'search':
        search = request.args.get('q', '').strip()
        filter_unit = request.args.get('unit', '').strip()
        filter_position = request.args.get('position', '').strip()
        filter_expertise = request.args.get('expertise', '').strip()
        
        available_experts = [_legacy_expert(item) for item in service.list_available_experts(
            group_id, keyword=search, unit=filter_unit,
            position=filter_position, expertise=filter_expertise, limit=20,
        )]
        return jsonify({'items': available_experts[:20]})
    
    # 获取筛选参数
    search = request.args.get('search', '').strip()
    filter_unit = request.args.get('unit', '').strip()
    filter_position = request.args.get('position', '').strip()
    filter_expertise = request.args.get('expertise', '').strip()
    
    available_experts = [_legacy_expert(item) for item in service.list_available_experts(
        group_id, keyword=search, unit=filter_unit,
        position=filter_position, expertise=filter_expertise, limit=20,
    )]
    facets = service.expert_facets()
    units, positions, expertises = (
        facets['units'], facets['positions'], facets['expertises']
    )
    
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
                    service.add_expert_group_member(
                        group_id, eid, selected_by=session.get('user')
                    )
                flash(f'成功添加 {len(expert_ids)} 位专家', 'success')
            elif expert_id:
                service.add_expert_group_member(
                    group_id, expert_id, selected_by=session.get('user')
                )
                flash('添加成功', 'success')
        elif action == 'remove' and expert_id:
            service.remove_expert_group_member(group_id, expert_id)
            flash('移除成功', 'success')
        
        return redirect(url_for('expert_groups.edit', group_id=group_id))
    
    return render_template('experts/group_edit.html', group=group, available_experts=available_experts, units=units, positions=positions, expertises=expertises)

@bp.route('/delete/<group_id>', methods=['POST'])
def delete(group_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    _resources_service().delete_expert_group(group_id)
    return jsonify({'success': True, 'message': '删除成功'})

@bp.route('/export/<group_id>')
def export(group_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    actor = _actor()
    try:
        group = _legacy_group(_resources_service().export_expert_group_sensitive(
            group_id, actor_user_id=actor['user_id'],
            request_id=actor['request_id'],
        ))
    except Exception:
        group = None
    if not group:
        flash('专家组不存在', 'error')
        return redirect(url_for('expert_groups.index'))
    
    # 导出Excel
    output = BytesIO()
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = '专家组'
    
    ws.append(['专家组名称', _xlsx_cell(group['meeting_name'])])
    ws.append(['创建人', _xlsx_cell(group['creator'])])
    ws.append(['创建时间', _xlsx_cell(group['created_at'])])
    ws.append([])
    ws.append(['序号', '姓名', '单位', '职务', '专业领域', '手机', '银行卡号', '开户行', '挑选人', '挑选时间'])
    
    for i, m in enumerate(group['members'], 1):
        ws.append([_xlsx_cell(value) for value in [i, m['name'], m['unit'], m['position'], m['expertise'], m['phone'] or '', m['bank_card'] or '', m['bank_name'] or '', m['selected_by'], m['selected_at']]])
    
    workbook.save(output)
    output.seek(0)
    return send_file(output, download_name=f'{group["meeting_name"]}_专家组.xlsx', as_attachment=True)
