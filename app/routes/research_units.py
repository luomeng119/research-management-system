# -*- coding: utf-8 -*-
"""
研制单位字典路由
页面 + RESTful API
"""
from flask import Blueprint, jsonify, request, session
from app.models import ResearchUnitModel

bp = Blueprint('research_units', __name__, url_prefix='/research-units')


def require_admin(f):
    """管理员权限验证"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': '未登录'}), 401
        if session.get('role') != '管理员':
            return jsonify({'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated


# ---------- API 路由 ----------

@bp.route('/api/list', methods=['GET'])
def api_list():
    """获取研制单位列表"""
    model = ResearchUnitModel()
    units = model.get_all()
    return jsonify({'units': units})


@bp.route('/api/match', methods=['GET'])
def api_match():
    """模糊匹配研制单位（供 REQ-009 导入功能调用）"""
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'matched': None, 'exact': False})
    
    model = ResearchUnitModel()
    from app.utils.fuzzy_match import match_research_unit
    result = match_research_unit(name, model)
    return jsonify(result)


@bp.route('/api', methods=['POST'])
@require_admin
def api_create():
    """新增研制单位"""
    data = request.get_json()
    name = data.get('name', '').strip()
    alias = data.get('alias', '').strip()
    
    if not name:
        return jsonify({'error': '研制单位名称不能为空'}), 400
    
    model = ResearchUnitModel()
    result = model.add(name, alias)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result), 201


@bp.route('/api/<unit_id>', methods=['PUT'])
@require_admin
def api_update(unit_id):
    """更新研制单位"""
    data = request.get_json()
    name = data.get('name', '').strip()
    alias = data.get('alias', '').strip()
    
    if not name:
        return jsonify({'error': '研制单位名称不能为空'}), 400
    
    model = ResearchUnitModel()
    result = model.update(unit_id, name, alias)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@bp.route('/api/<unit_id>', methods=['DELETE'])
@require_admin
def api_delete(unit_id):
    """删除研制单位"""
    model = ResearchUnitModel()
    model.delete(unit_id)
    return jsonify({'success': True})
