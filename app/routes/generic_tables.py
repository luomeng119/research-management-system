# -*- coding: utf-8 -*-
"""
通用表格管理路由
页面 + RESTful API
"""
import os
from pathlib import Path
import re
import tempfile
import uuid
from flask import Blueprint, render_template, request, jsonify, session, send_file, current_app
import sqlalchemy as sa
from app.models_generic_tables import GenericTableModel
from app.services.generic_tables import GenericTablesError, MAX_FILE_BYTES
from werkzeug.wsgi import ClosingIterator

bp = Blueprint('generic_tables', __name__, url_prefix='/tables')
bp2 = Blueprint('generic_tables_api', __name__, url_prefix='/api/generic-tables')

ALLOWED_EXTENSIONS = {'xlsx'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@bp2.errorhandler(GenericTablesError)
def handle_generic_tables_error(error):
    return jsonify({'error': error.message, 'code': error.code}), error.status_code


@bp.errorhandler(GenericTablesError)
def handle_generic_tables_page_error(error):
    return error.message, error.status_code


def require_login(f):
    """简易登录验证装饰器"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': '未登录'}), 401
        return f(*args, **kwargs)
    return decorated


# ---------- 页面路由 ----------

def _creator_names(versions):
    """Resolve only creators displayed on this page, without altering stored versions."""
    usernames = {version.get('creator') for version in versions if version.get('creator')}
    repository = current_app.extensions.get('users_repository')
    if not usernames or repository is None:
        return {}
    users = repository.table
    statement = sa.select(users.c.username, users.c.name).where(
        users.c.username.in_(sorted(usernames))
    )
    with repository.engine.connect() as connection:
        return {row['username']: row['name'] or row['username']
                for row in connection.execute(statement).mappings()}

@bp.route('')
def list_page():
    """表格列表页"""
    if 'user' not in session:
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))
    model = GenericTableModel()
    tables = model.get_all()
    return render_template('generic_tables/list.html', tables=tables)


@bp.route('/<table_id>')
def detail_page(table_id):
    """表格详情页（REQ-020: 默认打开 current_version）"""
    if 'user' not in session:
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))
    # REQ-020: 兼容老 URL 参数 ?version=<id>（切到只读历史快照）
    requested_vid = request.args.get('version') or request.args.get('vid')
    if requested_vid:
        return version_readonly_page(table_id, requested_vid)

    model = GenericTableModel()
    table = model.get_by_id_with_current(table_id)
    if not table:
        return "表格不存在", 404
    current_vid = table.get('current_version_id')
    # GET 必须保持只读；完整性异常由迁移/管理流程修复。
    if not current_vid:
        return "表格当前版本数据异常", 409
    versions = model.get_versions(table_id)
    version = model.get_version_by_id(current_vid)
    if not version:
        return "当前版本不存在", 404
    columns = model.get_columns(current_vid)
    page = max(1, request.args.get('page', 1, type=int))
    rows, row_total = model.get_rows_page(current_vid, page=page, page_size=version.get('page_size', 20))
    try:
        stats = model.get_column_stats(current_vid) if version_id_helper_has_stats() else {}
    except GenericTablesError as error:
        if error.code != 'RESULT_TOO_LARGE':
            raise
        stats = {}
    # 构建列名映射
    cols_map = {col['col_key']: col['col_name'] for col in columns}
    # REQ-020: 过滤出历史快照（用于侧边栏）
    snapshots = [v for v in versions if v['version_id'] != current_vid]
    return render_template('generic_tables/detail.html',
                           table=table, versions=versions,
                           current_version=version, columns=columns,
                           rows=rows, stats=stats, cols_map=cols_map,
                           snapshots=snapshots, is_current=True,
                           creator_names=_creator_names([version, *versions]),
                           row_total=row_total, current_page=page)

def version_id_helper_has_stats():
    """REQ-020: 占位函数，避免重复计算 stats"""
    return True

@bp.route('/<table_id>/version/<version_id>/')
def version_readonly_page(table_id, version_id):
    """REQ-020: 历史快照只读详情页"""
    if 'user' not in session:
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))
    model = GenericTableModel()
    table = model.get_by_id_with_current(table_id)
    version = model.get_version_by_id(version_id)
    if not table or not version:
        return "版本不存在", 404
    if version['table_id'] != table_id:
        return "版本不属于该表格", 400
    is_current = (version_id == table.get('current_version_id'))
    versions = model.get_versions(table_id)
    columns = model.get_columns(version_id)
    page = max(1, request.args.get('page', 1, type=int))
    rows, row_total = model.get_rows_page(version_id, page=page, page_size=version.get('page_size', 20))
    try:
        stats = model.get_column_stats(version_id)
    except GenericTablesError as error:
        if error.code != 'RESULT_TOO_LARGE':
            raise
        stats = {}
    cols_map = {col['col_key']: col['col_name'] for col in columns}
    snapshots = [v for v in versions if v['version_id'] != table.get('current_version_id')]
    return render_template('generic_tables/detail.html',
                           table=table, versions=versions,
                           current_version=version, columns=columns,
                           rows=rows, stats=stats, cols_map=cols_map,
                           snapshots=snapshots, is_current=is_current,
                           creator_names=_creator_names([version, *versions]),
                           row_total=row_total, current_page=page)


@bp.route('/<table_id>/compare')
def compare_page(table_id):
    """版本对比页"""
    if 'user' not in session:
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))
    model = GenericTableModel()
    table = model.get_by_id(table_id)
    if not table:
        return "表格不存在", 404
    versions = model.get_versions(table_id)
    vid_a = request.args.get('a')
    vid_b = request.args.get('b')
    if vid_a and vid_b:
        diff = model.compare_versions(vid_a, vid_b)
    else:
        diff = None
    return render_template('generic_tables/compare.html',
                           table=table, versions=versions,
                           vid_a=vid_a, vid_b=vid_b, diff=diff)


# ---------- API 路由 ----------

# 表格管理
@bp2.route('', methods=['GET'])
@require_login
def api_list():
    model = GenericTableModel()
    tables = model.get_all()
    return jsonify({'tables': tables})

@bp2.route('', methods=['POST'])
@require_login
def api_create():
    data = request.get_json() or {}
    model = GenericTableModel()
    table_id = model.create(
        name=data.get('name', ''),
        description=data.get('description', ''),
        creator=session.get('user', 'unknown')
    )
    return jsonify({'table_id': table_id})

@bp2.route('/<table_id>', methods=['GET'])
@require_login
def api_get(table_id):
    model = GenericTableModel()
    table = model.get_by_id(table_id)
    if not table:
        return jsonify({'error': '表格不存在'}), 404
    return jsonify(table)

@bp2.route('/<table_id>', methods=['PUT'])
@require_login
def api_update(table_id):
    data = request.get_json() or {}
    model = GenericTableModel()
    model.update(table_id, name=data.get('name'), description=data.get('description'))
    return jsonify({'success': True})

@bp2.route('/<table_id>', methods=['DELETE'])
@require_login
def api_delete(table_id):
    model = GenericTableModel()
    model.delete(table_id)
    return jsonify({'success': True})

# 版本管理
@bp2.route('/<table_id>/versions', methods=['GET'])
@require_login
def api_versions(table_id):
    model = GenericTableModel()
    versions = model.get_versions(table_id)
    table = model.get_by_id(table_id)
    current_id = table.get('current_version_id') if table else None
    for version in versions:
        version['is_current'] = version['version_id'] == current_id
    return jsonify({'versions': versions})

@bp2.route('/<table_id>/versions', methods=['POST'])
@require_login
def api_create_version(table_id):
    """
    REQ-020: 创建版本
    method='snapshot': 调 save_snapshot（自动建 snapshot + 新 working 副本 + 更新 current_version_id）
    method='import':   保留旧 URL 错误合约，引导到原子导入接口
    """
    data = request.get_json() or {}
    method = data.get('method', 'snapshot')
    model = GenericTableModel()

    if method == 'snapshot':
        # REQ-020: 新流程
        # source_version_id 优先用请求里的，否则用 current
        source_id = data.get('source_version_id')
        if not source_id:
            table = model.get_by_id_with_current(table_id)
            if not table or not table.get('current_version_id'):
                return jsonify({'error': '没有可用的源版本'}), 400
            source_id = table['current_version_id']
        # 校验源版本存在
        source_version = model.get_version_by_id(source_id)
        if not source_version or source_version['table_id'] != table_id:
            return jsonify({'error': '源版本不存在或不属于该表格'}), 404
        try:
            snapshot_id, new_current_id = model.save_snapshot(
                table_id=table_id, source_version_id=source_id,
                label=data.get('label', '快照'),
                note=data.get('note', ''),
                creator=session.get('user', 'unknown')
            )
            return jsonify({
                'version_id': snapshot_id,  # 兼容老接口
                'snapshot_id': snapshot_id,
                'new_current_id': new_current_id
            })
        except GenericTablesError:
            raise
        except Exception:
            return jsonify({'error': '保存快照失败', 'code': 'SNAPSHOT_FAILED'}), 500
    elif method == 'import':
        # 保留旧 URL 和明确错误形状，但不再允许“先切版本、后上传”。
        return jsonify({
            'error': '请通过原子 Excel 导入接口创建导入版本',
            'code': 'ATOMIC_IMPORT_REQUIRED',
        }), 409
    elif method == 'create':
        # 保留兼容
        version_id = model.create_version(
            table_id=table_id,
            method='create',
            source_version_id=None,
            creator=session.get('user', 'unknown'),
            label=data.get('label', ''),
            note=data.get('note', '')
        )
        return jsonify({'version_id': version_id})
    else:
        return jsonify({'error': f'不支持的 method: {method}'}), 400


@bp2.route('/<table_id>/rollback', methods=['POST'])
@require_login
def api_rollback(table_id):
    """REQ-020: 从历史快照回滚"""
    data = request.get_json() or {}
    source_id = data.get('source_version_id')
    if not source_id:
        return jsonify({'error': '缺少 source_version_id'}), 400
    model = GenericTableModel()
    source_version = model.get_version_by_id(source_id)
    if not source_version or source_version['table_id'] != table_id:
        return jsonify({'error': '源版本不存在或不属于该表格'}), 404
    try:
        new_current_id = model.rollback_to(
            table_id=table_id, source_version_id=source_id,
            creator=session.get('user', 'unknown')
        )
        return jsonify({'new_current_id': new_current_id, 'version_id': new_current_id})
    except GenericTablesError:
        raise
    except Exception:
        return jsonify({'error': '回滚失败', 'code': 'ROLLBACK_FAILED'}), 500


@bp2.route('/<table_id>/versions/import', methods=['POST'])
@require_login
def api_import_new_version(table_id):
    """列表页原子导入：文件解析成功后才创建并切换新 current。"""
    file = request.files.get('file')
    if not file or not allowed_file(file.filename):
        return jsonify({'error': '仅支持 .xlsx 文件', 'code': 'INVALID_EXCEL_TYPE'}), 400
    source_id = request.form.get('source_version_id', '')
    if not source_id:
        return jsonify({'error': '缺少源版本', 'code': 'SOURCE_VERSION_REQUIRED'}), 400
    handle, filepath = tempfile.mkstemp(suffix='.xlsx')
    os.close(handle)
    try:
        file.save(filepath)
        model = GenericTableModel()
        version_id, count = model.import_as_new_version(
            table_id, source_id, filepath, session.get('user', 'unknown'),
            label=request.form.get('label', '导入版'), note=request.form.get('note', ''),
        )
        return jsonify({'version_id': version_id, 'new_current_id': version_id, 'imported': count})
    finally:
        Path(filepath).unlink(missing_ok=True)

@bp2.route('/versions/<version_id>', methods=['GET'])
@require_login
def api_version_get(version_id):
    model = GenericTableModel()
    version = model.get_version_by_id(version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404
    return jsonify(version)

@bp2.route('/versions/<version_id>', methods=['DELETE'])
@require_login
def api_version_delete(version_id):
    model = GenericTableModel()
    model.delete_version(version_id)
    return jsonify({'success': True})

# 列管理
@bp2.route('/versions/<version_id>/columns', methods=['GET'])
@require_login
def api_columns(version_id):
    model = GenericTableModel()
    cols = model.get_columns(version_id)
    return jsonify({'columns': cols})

@bp2.route('/versions/<version_id>/columns', methods=['POST'])
@require_login
def api_column_create(version_id):
    data = request.get_json() or {}
    model = GenericTableModel()
    model.upsert_column(
        version_id=version_id,
        col_key=data.get('col_key', ''),
        col_name=data.get('col_name', ''),
        col_type=data.get('col_type', 'text'),
        col_index=data.get('col_index'),
        col_width=data.get('col_width', 120),
        col_align=data.get('col_align', 'left'),
        col_summary=data.get('col_summary', ''),
        col_options=data.get('col_options')
    )
    return jsonify({'success': True})

@bp2.route('/versions/<version_id>/columns/<col_key>', methods=['PUT'])
@require_login
def api_column_update(version_id, col_key):
    data = request.get_json() or {}
    model = GenericTableModel()
    model.upsert_column(
        version_id=version_id,
        col_key=col_key,
        col_name=data.get('col_name', ''),
        col_type=data.get('col_type', 'text'),
        col_width=data.get('col_width', 120),
        col_align=data.get('col_align', 'left'),
        col_summary=data.get('col_summary', ''),
        col_options=data.get('col_options')
    )
    return jsonify({'success': True})

@bp2.route('/versions/<version_id>/columns/<col_key>', methods=['DELETE'])
@require_login
def api_column_delete(version_id, col_key):
    model = GenericTableModel()
    model.delete_column(version_id, col_key)
    return jsonify({'success': True})

@bp2.route('/versions/<version_id>/columns/reorder', methods=['PUT'])
@require_login
def api_columns_reorder(version_id):
    data = request.get_json() or {}
    model = GenericTableModel()
    model.reorder_columns(version_id, data.get('col_keys', []))
    return jsonify({'success': True})

# REQ-016 增强接口
@bp2.route('/versions/<version_id>/rows/<row_key>/color', methods=['PUT'])
@require_login
def api_row_color(version_id, row_key):
    data = request.get_json() or {}
    row_color = data.get('row_color', '')
    if row_color not in ('', 'yellow', 'green', 'red'):
        return jsonify({'error': '颜色值无效（仅支持空/黄/绿/红）'}), 400
    model = GenericTableModel()
    if not model.update_row_color(version_id, row_key, row_color):
        return jsonify({'error': '行不存在'}), 404
    return jsonify({'ok': True, 'row_color': row_color})

@bp2.route('/versions/<version_id>/columns/<col_key>/width', methods=['PUT'])
@require_login
def api_column_width(version_id, col_key):
    data = request.get_json() or {}
    col_width = data.get('col_width', 120)
    try:
        col_width = int(col_width)
    except (TypeError, ValueError):
        return jsonify({'error': '宽度必须是整数'}), 400
    if not (10 <= col_width <= 800):
        return jsonify({'error': '宽度需在 10-800 之间'}), 400
    model = GenericTableModel()
    if not model.update_column_width(version_id, col_key, col_width):
        return jsonify({'error': '列不存在'}), 404
    return jsonify({'ok': True, 'col_width': col_width})

@bp2.route('/versions/<version_id>/page-size', methods=['PUT'])
@require_login
def api_version_page_size(version_id):
    data = request.get_json() or {}
    page_size = data.get('page_size', 20)
    if page_size not in (-1, 10, 20, 50, 100):
        return jsonify({'error': '分页数无效（仅支持 -1/10/20/50/100）'}), 400
    model = GenericTableModel()
    if not model.update_page_size(version_id, page_size):
        return jsonify({'error': '版本不存在'}), 404
    return jsonify({'ok': True, 'page_size': page_size})

# 行数据
@bp2.route('/versions/<version_id>/rows', methods=['GET'])
@require_login
def api_rows(version_id):
    keyword = request.args.get('q', '')
    model = GenericTableModel()
    version = model.get_version_by_id(version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = int(request.args.get('page_size', version.get('page_size', 20)))
    except (TypeError, ValueError):
        return jsonify({'error': '分页参数无效'}), 400
    rows, total = model.get_rows_page(version_id, keyword=keyword if keyword else None, page=page, page_size=page_size)
    stats, stats_error = {}, None
    try:
        stats = model.get_column_stats(version_id)
    except GenericTablesError as error:
        if error.code != 'RESULT_TOO_LARGE':
            raise
        stats_error = {'code': error.code, 'message': error.message}
    return jsonify({'rows': rows, 'stats': stats, 'stats_error': stats_error,
                    'total': total, 'page': page, 'page_size': page_size})

@bp2.route('/versions/<version_id>/rows', methods=['POST'])
@require_login
def api_row_create(version_id):
    data = request.get_json() or {}
    row_key = data.get('row_key') or ('GTR' + uuid.uuid4().hex[:12])
    model = GenericTableModel()
    model.upsert_row(version_id, row_key, data.get('row_data', {}))
    return jsonify({'row_key': row_key})

@bp2.route('/versions/<version_id>/rows/<row_key>', methods=['PUT'])
@require_login
def api_row_update(version_id, row_key):
    data = request.get_json() or {}
    model = GenericTableModel()
    model.upsert_row(version_id, row_key, data.get('row_data', {}))
    return jsonify({'success': True})

@bp2.route('/versions/<version_id>/rows/<row_key>', methods=['DELETE'])
@require_login
def api_row_delete(version_id, row_key):
    model = GenericTableModel()
    model.delete_row(version_id, row_key)
    return jsonify({'success': True})

@bp2.route('/versions/<version_id>/rows/import', methods=['POST'])
@require_login
def api_rows_import(version_id):
    """REQ-020: Excel 文件导入，校验目标版本是 current（is_locked=0）"""
    if 'file' not in request.files:
        return jsonify({'error': '未上传文件'}), 400
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': '仅支持 .xlsx 文件'}), 400
    model = GenericTableModel()
    # REQ-020: 校验版本存在 + is_locked=0（只能在 current 导入）
    version = model.get_version_by_id(version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404
    if version.get('is_locked', 0) == 1:
        return jsonify({'error': '只能在当前编辑版本导入数据'}), 403
    handle, filepath = tempfile.mkstemp(suffix='.xlsx')
    os.close(handle)
    try:
        file.save(filepath)
        if Path(filepath).stat().st_size > MAX_FILE_BYTES:
            return jsonify({'error': '导入文件过大'}), 413
        count = model.import_rows_from_excel(version_id, filepath,
                                             session.get('user', 'unknown'),
                                             request.form.get('mode', 'replace'))
    finally:
        Path(filepath).unlink(missing_ok=True)
    return jsonify({'imported': count})


@bp2.route('/versions/<version_id>/export', methods=['GET'])
@require_login
def api_export(version_id):
    """导出指定版本为Excel文件"""
    model = GenericTableModel()
    version = model.get_version_by_id(version_id)
    if not version:
        return jsonify({'error': '版本不存在'}), 404
    filepath = model.export_to_excel(version_id)
    try:
        table = model.get_by_id(version['table_id'])
        raw_name = f"{table['name'] if table else '导出'}_{version.get('version_label', version.get('version_number', ''))}"
        safe_name = re.sub(r'[\x00-\x1f\x7f/\\]+', '_', str(raw_name)).strip(' ._')[:120] or '导出'
        filename = f"{safe_name}.xlsx"
        response = send_file(filepath, as_attachment=True, download_name=filename, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        # Hand cleanup ownership to the response only after every preceding
        # operation has succeeded.
        response.response = ClosingIterator(response.response, [lambda: Path(filepath).unlink(missing_ok=True)])
    except Exception:
        Path(filepath).unlink(missing_ok=True)
        raise
    return response


# 统计与对比
@bp2.route('/versions/<version_id>/stats', methods=['GET'])
@require_login
def api_stats(version_id):
    model = GenericTableModel()
    stats = model.get_column_stats(version_id)
    return jsonify({'stats': stats})

@bp2.route('/versions/compare', methods=['GET'])
@require_login
def api_compare():
    vid_a = request.args.get('a')
    vid_b = request.args.get('b')
    if not vid_a or not vid_b:
        return jsonify({'error': '缺少版本参数'}), 400
    model = GenericTableModel()
    diff = model.compare_versions(vid_a, vid_b)
    v_a = model.get_version_by_id(vid_a)
    v_b = model.get_version_by_id(vid_b)
    return jsonify({
        'base_version': v_a,
        'compare_version': v_b,
        'diff': diff
    })
