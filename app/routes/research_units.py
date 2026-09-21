# -*- coding: utf-8 -*-
"""
研制单位字典路由
页面 + RESTful API
"""
from flask import Blueprint, current_app, jsonify, request
from app.services.resources import ResourceServiceError
from app.security.auth import business_required

bp = Blueprint('research_units', __name__, url_prefix='/research-units')


def _service():
    service = current_app.extensions.get('equipment_resources_service')
    if service is None:
        raise RuntimeError('equipment resources service is not configured')
    return service


# ---------- API 路由 ----------

@bp.route('/api/list', methods=['GET'])
def api_list():
    """获取研制单位列表"""
    units = _service().list_research_units()
    return jsonify({'units': units})


@bp.route('/api/match', methods=['GET'])
def api_match():
    """模糊匹配研制单位（供 REQ-009 导入功能调用）"""
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'matched': None, 'exact': False})
    
    from app.utils.fuzzy_match import match_research_unit
    units = _service().list_research_units()
    result = match_research_unit(name, _UnitMatcher(units))
    return jsonify(result)


@bp.route('/api', methods=['POST'])
@business_required
def api_create():
    """新增研制单位"""
    data = request.get_json()
    name = data.get('name', '').strip()
    alias = data.get('alias', '').strip()
    
    if not name:
        return jsonify({'error': '研制单位名称不能为空'}), 400
    
    try:
        return jsonify(_service().create_research_unit(name, alias)), 201
    except ResourceServiceError as error:
        return jsonify({'error': error.message}), error.status_code


@bp.route('/api/<unit_id>', methods=['PUT'])
@business_required
def api_update(unit_id):
    """更新研制单位"""
    data = request.get_json()
    name = data.get('name', '').strip()
    alias = data.get('alias', '').strip()
    
    if not name:
        return jsonify({'error': '研制单位名称不能为空'}), 400
    
    try:
        return jsonify(_service().update_research_unit(unit_id, name, alias))
    except ResourceServiceError as error:
        return jsonify({'error': error.message}), error.status_code


@bp.route('/api/<unit_id>', methods=['DELETE'])
@business_required
def api_delete(unit_id):
    """删除研制单位"""
    try:
        _service().delete_research_unit(unit_id)
        return jsonify({'success': True})
    except ResourceServiceError as error:
        return jsonify({'error': error.message}), error.status_code


class _UnitMatcher:
    """Compatibility view for the existing deterministic fuzzy matcher."""

    def __init__(self, units):
        self.units = units

    def get_all(self):
        return self.units
