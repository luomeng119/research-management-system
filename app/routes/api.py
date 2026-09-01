# -*- coding: utf-8 -*-
from flask import Blueprint, jsonify, session, current_app, request
from app.models import ProjectModel, EquipmentModel, StandardModel, UserModel, DIRECTORIES, OperationLogModel, SecurityProjectModel, CryptoProjectModel, ExpertModel
from config import VERSION
import os

bp = Blueprint('api', __name__, url_prefix='/api')

def get_user_directories():
    """获取当前用户可见的目录列表"""
    if 'user' not in session:
        return DIRECTORIES  # 未登录返回全部
    
    username = session.get('user')
    role = session.get('role')
    
    # 管理员可见全部目录
    if role == '管理员':
        return DIRECTORIES
    
    # 普通用户获取权限设置
    user_model = UserModel()
    perms = user_model.get_directory_permissions(username)
    
    # 返回可见的目录
    return [d for d, v in perms.items() if v == 'visible']

@bp.route('/tree')
def tree():
    """获取目录树结构（根据用户权限过滤）"""
    project_model = ProjectModel()
    equipment_model = EquipmentModel()
    standard_model = StandardModel()
    
    projects = project_model.get_all()
    equipment = equipment_model.get_all()
    standards = standard_model.get_all()
    
    # 获取用户可见的目录
    visible_dirs = get_user_directories()
    
    # 获取当前用户信息
    current_user = session.get('user')
    user_role = session.get('role')
    
    # 项目级数据权限：普通用户只能看到自己负责的项目
    if user_role != '管理员' and current_user:
        projects = [p for p in projects if p['leader'] == current_user]
    
    # 按状态分组项目
    status_groups = {}
    for p in projects:
        status = p['status'] if p['status'] else '未知'
        if status not in status_groups:
            status_groups[status] = []
        # 保存整个项目元组以便后续使用
        status_groups[status].append(p)
    
    # 构建树结构
    tree_data = []
    
    # 科研项目
    if 'projects' in visible_dirs:
        # 按固定顺序排序：任务下达、通过院内评审、通过机关评审、结题上报
        status_order = {'任务下达': 0, '通过院内评审': 1, '通过机关评审': 2, '结题上报': 3, '已结题': 4}
        sorted_status = sorted(status_groups.items(), key=lambda x: status_order.get(x[0], 99))
        
        tree_data.append({
            'id': 'projects',
            'label': '科研项目',
            'icon': 'bi-journal-text text-primary',
            'url': '/projects',
            'children': [
            ] + [
                {
                    'id': f'project-status-{status}',
                    'label': f'{status} ({len(items)})',
                    'icon': 'bi-folder2-open text-warning',
                    'url': f'/projects?status={status}',
                    'children': [
                        {
                            'id': f'project-{p["project_id"]}-info',
                            'label': p['name'],
                            'icon': 'bi-folder2 text-primary',
                            'url': f'/projects/detail/{p["project_id"]}',
                            'children': [
                                {
                                    'id': f'{p["project_id"]}-info',
                                    'label': '项目信息',
                                    'icon': 'bi-info-circle text-info',
                                    'url': f'/projects/detail/{p["project_id"]}?view=info',
                                    'children': []
                                },
                                {
                                    'id': f'{p["project_id"]}-equipment',
                                    'label': '设备选型',
                                    'icon': 'bi-cpu text-warning',
                                    'url': f'/projects/detail/{p["project_id"]}?view=equipment',
                                    'children': []
                                },
                                {
                                    'id': f'{p["project_id"]}-documents',
                                    'label': '项目文档',
                                    'icon': 'bi-file-earmark text-success',
                                    'url': f'/projects/detail/{p["project_id"]}?view=documents',
                                    'children': []
                                },
                                {
                                    'id': f'{p["project_id"]}-argumentation',
                                    'label': '方案论证',
                                    'icon': 'bi-file-earmark-text text-primary',
                                    'url': f'/argumentation/research/{p["project_id"]}',
                                    'children': []
                                }
                            ]
                        }
                        for p in items
                    ]
                }
                for status, items in sorted_status
            ]

        })
    
    # 安全保密项目
    if 'security_projects' in visible_dirs:
        security_model = SecurityProjectModel()
        security_projects = security_model.get_all()
        security_status_groups = {}
        for p in security_projects:
            status = p['status'] if p['status'] else '未知'
            if status not in security_status_groups:
                security_status_groups[status] = []
            security_status_groups[status].append(p)
        
        status_order = {'任务下达': 0, '通过院内评审': 1, '通过机关评审': 2, '结题上报': 3, '已结题': 4}
        sorted_status = sorted(security_status_groups.items(), key=lambda x: status_order.get(x[0], 99))
        
        tree_data.append({
            'id': 'security_projects',
            'label': '安全保密项目',
            'icon': 'bi-shield-lock text-danger',
            'url': '/security_projects',
            'children': [
                {
                    'id': f'security-status-{status}',
                    'label': f'{status} ({len(items)})',
                    'icon': 'bi-folder2-open text-warning',
                    'url': f'/security_projects?status={status}',
                    'children': [
                        {
                            'id': f'security-{p["project_id"]}',
                            'label': p['name'],
                            'icon': 'bi-shield-lock text-danger',
                            'url': f'/security_projects/detail/{p["project_id"]}',
                            'children': []
                        }
                        for p in items[:5]
                    ]
                }
                for status, items in sorted_status
            ]
        })
    
    # 密码应用项目
    if 'crypto_projects' in visible_dirs:
        crypto_model = CryptoProjectModel()
        crypto_projects = crypto_model.get_all()
        crypto_status_groups = {}
        for p in crypto_projects:
            status = p['status'] if p['status'] else '未知'
            if status not in crypto_status_groups:
                crypto_status_groups[status] = []
            crypto_status_groups[status].append(p)
        
        status_order = {'任务下达': 0, '通过院内评审': 1, '通过机关评审': 2, '结题上报': 3, '已结题': 4}
        sorted_status = sorted(crypto_status_groups.items(), key=lambda x: status_order.get(x[0], 99))
        
        tree_data.append({
            'id': 'crypto_projects',
            'label': '密码应用项目',
            'icon': 'bi-key text-warning',
            'url': '/crypto_projects',
            'children': [
                {
                    'id': f'crypto-status-{status}',
                    'label': f'{status} ({len(items)})',
                    'icon': 'bi-folder2-open text-warning',
                    'url': f'/crypto_projects?status={status}',
                    'children': [
                        {
                            'id': f'crypto-{p["project_id"]}',
                            'label': p['name'],
                            'icon': 'bi-key text-warning',
                            'url': f'/crypto_projects/detail/{p["project_id"]}',
                            'children': []
                        }
                        for p in items[:5]
                    ]
                }
                for status, items in sorted_status
            ]
        })
    
    # 设备知识库
    if 'equipment' in visible_dirs:
        tree_data.append({
            'id': 'equipment',
            'label': '设备知识库',
            'icon': 'bi-cpu text-info',
            'url': '/equipment',
            'children': [
                {
                    'id': 'equipment-category-安全设备',
                    'label': '安全设备',
                    'icon': 'bi-shield-check text-success',
                    'url': '/equipment?category=安全设备',
                    'children': []
                },
                {
                    'id': 'equipment-category-密码设备',
                    'label': '密码设备',
                    'icon': 'bi-key text-danger',
                    'url': '/equipment?category=密码设备',
                    'children': []
                },
                {
                    'id': 'equipment-category-通用设备',
                    'label': '通用设备',
                    'icon': 'bi-hdd-stack text-secondary',
                    'url': '/equipment?category=通用设备',
                    'children': []
                },
                {
                    'id': 'host-devices',
                    'label': '宿主设备',
                    'icon': 'bi-pc-horizontal text-primary',
                    'url': '/equipment/hosts',
                    'children': []
                },
                {
                    'id': 'equipment-logs',
                    'label': '操作日志',
                    'icon': 'bi-clock-history text-secondary',
                    'url': '/equipment/logs/equipment',
                    'children': []
                }
            ]
        })
    

    
    # 标准法规库
    if 'standards' in visible_dirs:
        tree_data.append({
            'id': 'standards',
            'label': '标准法规库',
            'icon': 'bi-book text-success',
            'url': '/standards/',
            'children': [
                {
                    'id': 'standards-category-国军标',
                    'label': '国军标',
                    'icon': 'bi-award text-danger',
                    'url': '/standards?category=国军标',
                    'children': []
                },
                {
                    'id': 'standards-category-国家标准',
                    'label': '国家标准',
                    'icon': 'bi-geo-alt text-primary',
                    'url': '/standards?category=国家标准',
                    'children': []
                },
                {
                    'id': 'standards-category-行业标准',
                    'label': '行业标准',
                    'icon': 'bi-building text-info',
                    'url': '/standards?category=行业标准',
                    'children': []
                },
                {
                    'id': 'standards-logs',
                    'label': '操作日志',
                    'icon': 'bi-clock-history text-secondary',
                    'url': '/standards/logs/standards',
                    'children': []
                }
            ]
        })
    
    # 科研模板 - 动态加载子目录
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    template_dir = os.path.join(BASE_DIR, 'templates')
    template_children = []
    for cat in ['财务模板', '会务模板', '公文模板', '方案模板', '其他模板']:
        cat_path = os.path.join(template_dir, cat)
        children = []
        if os.path.exists(cat_path):
            try:
                for sub in os.listdir(cat_path):
                    sub_path = os.path.join(cat_path, sub)
                    if os.path.isdir(sub_path) and not sub.startswith('.'):
                        children.append({'id': '', 'label': sub, 'icon': 'bi-folder2 text-warning', 'url': '', 'children': []})
            except: pass
        template_children.append({'id': f'template-{cat}', 'label': cat, 'icon': 'bi-folder text-warning', 'url': f'/templates?cat={cat}', 'children': children})
    template_children.append({'id': 'template-专家信息库', 'label': '专家信息库', 'icon': 'bi-folder text-secondary', 'url': '/experts', 'children': []})
    template_children.append({'id': 'templates-logs', 'label': '操作日志', 'icon': 'bi-clock-history text-secondary', 'url': '/templates/logs/templates', 'children': []})
    
    tree_data.append({
        'id': 'templates',
        'label': '科研模板',
        'icon': 'bi-file-earmark-ruled',
        'url': '/templates/',
        'children': template_children
    })

    # 辅助工具（utils）- 包含报销助手等
    if 'utils' in visible_dirs:
        expense_children = []
        if 'expense' in visible_dirs:
            expense_children = [
                {
                    'id': 'expense-assistant',
                    'label': '报销助手',
                    'icon': 'bi-receipt text-warning',
                    'url': '/expense',
                    'children': [
                        {'id': 'expense-upload', 'label': '上传发票', 'icon': 'bi-upload text-info', 'url': '/expense', 'children': []},
                        {'id': 'expense-records', 'label': '报销记录', 'icon': 'bi-list-check text-success', 'url': '/expense', 'children': []},
                        {'id': 'expense-fill', 'label': '生成审批单', 'icon': 'bi-file-earmark-plus text-primary', 'url': '/expense', 'children': []},
                    ]
                },
            ]
        tree_data.append({
            'id': 'utils',
            'label': '辅助工具',
            'icon': 'bi-tools text-secondary',
            'url': '/utils',
            'children': [
                *expense_children,
                {'id': 'utils-doc', 'label': '文档校正', 'icon': 'bi-file-earmark-check text-info', 'url': '/utils/document_correction', 'children': []},
            ]
        })

    # 通用表格管理
    if 'generic_tables' in visible_dirs:
        from app.models_generic_tables import GenericTableModel
        gt_model = GenericTableModel()
        tables = gt_model.get_all() or []
        tree_data.append({
            'id': 'generic_tables',
            'label': '通用表格管理',
            'icon': 'bi-table text-primary',
            'url': '/tables',
            'children': [
                {
                    'id': f'gt-table-{t["table_id"]}',
                    'label': t['name'],
                    'icon': 'bi bi-table text-info',
                    'url': f'/tables/{t["table_id"]}',
                    'children': []
                }
                for t in tables[:10]  # 最多显示10个
            ]
        })

    return jsonify({
        'code': 0,
        'msg': 'success',
        'data': tree_data,
        'version': VERSION
    })

@bp.route('/tree_children')
def tree_children():
    """动态加载目录树的子节点"""
    parent_id = request.args.get('parent_id', '')
    
    children = []
    
    if parent_id.startswith('equipment-category-'):
        # 设备分类下的子目录（暂无)
        category = parent_id.replace('equipment-category-', '')
    
    elif parent_id.startswith('standards-category-'):
        # 标准分类下的子目录（暂无）
        category = parent_id.replace('standards-category-', '')
    
    elif parent_id.startswith('template-'):
        # 模板分类下的子目录（从实际目录加载）
        category = parent_id.replace('template-', '')
        template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates', category)
        if os.path.exists(template_dir):
            for item in os.listdir(template_dir):
                item_path = os.path.join(template_dir, item)
                if os.path.isdir(item_path) and not item.startswith('.'):
                    children.append({
                        'id': f'{parent_id}-{item}',
                        'label': item,
                        'icon': 'bi-folder2 text-warning',
                        'url': f'/templates?cat={category}&sub={item}',
                        'children': []
                    })
    
    elif parent_id.startswith('template-') and '&sub=' in parent_id:
        # 子分类下的更深层级
        parts = parent_id.replace('template-', '').split('&sub=')
        if len(parts) == 2:
            category, sub = parts
            template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates', category, sub)
            if os.path.exists(template_dir):
                for item in os.listdir(template_dir):
                    item_path = os.path.join(template_dir, item)
                    if os.path.isdir(item_path) and not item.startswith('.'):
                        children.append({
                            'id': f'{parent_id}-{item}',
                            'label': item,
                            'icon': 'bi-folder2 text-warning',
                            'url': f'/templates?cat={category}&sub={sub}/{item}',
                            'children': []
                        })
    
    return jsonify({'code': 0, 'data': children})

@bp.route('/version')
def version():
    return jsonify({
        'code': 0,
        'version': VERSION
    })

@bp.route('/logs')
def get_logs():
    """获取操作日志"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    
    module = request.args.get('module')
    operator = request.args.get('operator')
    file_name = request.args.get('file_name')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    log_model = OperationLogModel()
    logs = log_model.search(
        module=module,
        operator=operator,
        file_name=file_name,
        start_date=start_date,
        end_date=end_date,
        limit=100
    )
    
    return jsonify({'code': 0, 'data': logs})

@bp.route('/logs/<module_name>')
def get_module_logs(module_name):
    """获取指定模块的日志"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    
    log_model = OperationLogModel()
    logs = log_model.get_by_module(module_name, limit=100)
    
    return jsonify({'code': 0, 'data': logs})


# ============ LLM / AI 功能接口 ============

@bp.route('/llm/status')
def llm_status():
    """查询模型加载状态"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    try:
        from app.llm import get_pool
        pool = get_pool()
        return jsonify({'code': 0, 'data': pool.status()})
    except Exception as e:
        return jsonify({'code': 2, 'msg': str(e)})


@bp.route('/llm/reload', methods=['POST'])
def llm_reload():
    """重新加载指定模型"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    name = request.json.get('name') if request.is_json else None
    if name not in ('corrector', 'qwen'):
        return jsonify({'code': 1, 'msg': '无效模型名称'})
    try:
        from app.llm import get_pool
        pool = get_pool()
        pool.release(name)
        pool.get_model(name)
        return jsonify({'code': 0, 'msg': f'{name} 已重新加载'})
    except Exception as e:
        return jsonify({'code': 2, 'msg': str(e)})


@bp.route('/correct', methods=['POST'])
def correct_text():
    """文本纠错"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    if not request.is_json:
        return jsonify({'code': 1, 'msg': '需要 JSON 请求'})
    text = request.json.get('text', '')
    if not text.strip():
        return jsonify({'code': 1, 'msg': '文本不能为空'})
    try:
        from app.llm import get_pool
        from app.llm.corrector import Corrector
        pool = get_pool()
        corrector = Corrector(pool)
        result = corrector.correct(text)
        return jsonify({'code': 0, 'data': result})
    except Exception as e:
        return jsonify({'code': 2, 'msg': f'纠错失败: {e}'})


@bp.route('/summarize', methods=['POST'])
def summarize_text():
    """文档摘要"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    if not request.is_json:
        return jsonify({'code': 1, 'msg': '需要 JSON 请求'})
    text = request.json.get('text', '')
    max_length = request.json.get('max_length', 100)
    if not text.strip():
        return jsonify({'code': 1, 'msg': '内容不能为空'})
    try:
        from app.llm import get_pool
        from app.llm.qwen import QwenHelper
        pool = get_pool()
        qwen = QwenHelper(pool)
        result = qwen.summarize(text, max_length=max_length)
        return jsonify({'code': 0, 'data': result})
    except Exception as e:
        return jsonify({'code': 2, 'msg': f'摘要生成失败: {e}'})


@bp.route('/ai-search', methods=['POST'])
def ai_search():
    """AI 意图检索"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    if not request.is_json:
        return jsonify({'code': 1, 'msg': '需要 JSON 请求'})
    query = request.json.get('query', '')
    records = request.json.get('records', [])
    if not query.strip():
        return jsonify({'code': 0, 'data': records})  # 空查询返回全部
    if not isinstance(records, list):
        return jsonify({'code': 1, 'msg': 'records 必须是数组'})
    try:
        from app.llm import get_pool
        from app.llm.qwen import QwenHelper
        pool = get_pool()
        qwen = QwenHelper(pool)
        filtered = qwen.filter_relevant(query, records)
        return jsonify({'code': 0, 'data': filtered, 'total': len(records), 'filtered': len(filtered)})
    except Exception as e:
        return jsonify({'code': 2, 'msg': f'检索失败: {e}'})


@bp.route('/equipment/hosts/search', methods=['GET'])
def equipment_hosts_search():
    """关联选择弹框：搜索所有设备（安全设备/密码设备/通用设备/其他设备）"""
    if 'user' not in session:
        return jsonify({'code': 1, 'msg': '未登录'})
    keyword = request.args.get('keyword', '')
    category = request.args.get('category', '')
    page = int(request.args.get('page', 1))
    per_page = 20

    equipment_model = EquipmentModel()
    all_equipment = equipment_model.search(category=category if category else None, keyword=keyword if keyword else None)

    total = len(all_equipment)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    start = (page - 1) * per_page
    end = start + per_page
    data = [{
        'equipment_id': e['equipment_id'],
        'name': e['name'],
        'model': e['model'],
        'category': e['category'],
        'tech_status': e.get('tech_status', '')
    } for e in all_equipment[start:end]]

    return jsonify({'code': 0, 'data': data, 'total': total, 'page': page, 'total_pages': total_pages})


# ---------- 模糊匹配 API ----------
from app.utils.fuzzy_match import fuzzy_search_equipment, fuzzy_search_host_device


@bp.route('/fuzzy-match/equipment')
def fuzzy_match_equipment():
    """模糊搜索设备（供导入预览页下拉框实时搜索）"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    keyword = request.args.get('q', '') or request.args.get('keyword', '')
    if len(keyword) < 1:
        return jsonify({'success': True, 'data': []})
    results = fuzzy_search_equipment(keyword, limit=20)
    return jsonify({'success': True, 'data': results})


@bp.route('/fuzzy-match/host-device')
def fuzzy_match_host_device():
    """模糊搜索宿主设备（供导入预览页下拉框实时搜索）"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    keyword = request.args.get('q', '') or request.args.get('keyword', '')
    if len(keyword) < 1:
        return jsonify({'success': True, 'data': []})
    results = fuzzy_search_host_device(keyword, limit=20)
    return jsonify({'success': True, 'data': results})
