# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, current_app, send_file
from app.models import ProjectModel
from app.routes._project_bridge import (
    legacy_page, safe_project_documents_path, safe_project_path, upload_project_file,
    merge_project_files, controlled_project_download, upload_project_folder,
)
from app.security.auth import current_identity
from app.services.projects import ProjectServiceError
from app.services.resources import ResourceServiceError
import os
import re
import zipfile
from datetime import datetime

bp = Blueprint('projects', __name__, url_prefix='/projects')


def _equipment_service():
    service = current_app.extensions.get('equipment_resources_service')
    if service is None:
        raise RuntimeError('equipment resources service is not configured')
    return service

FOLDER_TYPES = ['任务输入文件', '研究成果文件', '院内审查文件', '机关审查文件', '成果上报文件']


def _get_project(project_id):
    service = current_app.extensions.get('project_service')
    if service is not None:
        try:
            return service.get_legacy(category='GENERAL_RESEARCH', business_id=project_id)
        except ProjectServiceError:
            return None
    return ProjectModel().get_by_id(project_id)


def _safe_join(*parts):
    """安全拼接路径，防止路径穿越"""
    return safe_project_path(parts[0], parts[1], *parts[2:])


def _safe_upload_path(base_dir, filename):
    """净化上传文件名，防止路径穿越"""
    # 去掉所有路径成分，只保留文件名
    filename = os.path.basename(filename.replace('\\', '/'))
    # 去掉危险字符
    filename = re.sub(r'[^\w\s.-]', '_', filename)
    filename = filename.strip('. ')
    if not filename:
        filename = 'unnamed'
    return os.path.join(base_dir, filename)

def build_folder_tree(project_dir, base_path='', level=0):
    result = []
    if not base_path:
        # 先添加FOLDER_TYPES定义的5个固定文件夹
        for folder in FOLDER_TYPES:
            folder_path = os.path.join(project_dir, folder)
            if not os.path.exists(folder_path):
                os.makedirs(folder_path, exist_ok=True)
            files = []
            if os.path.exists(folder_path):
                for f in os.listdir(folder_path):
                    # 跳过隐藏目录（如.history）
                    if f.startswith('.'):
                        continue
                    f_path = os.path.join(folder_path, f)
                    if os.path.isfile(f_path):
                        # 检查是否有历史版本
                        history_dir = os.path.join(folder_path, '.history')
                        has_history = 'false'
                        current_version = 'v1'
                        if os.path.exists(history_dir):
                            name, ext = os.path.splitext(f)
                            version_file = os.path.join(history_dir, f"{name}_versions.json")
                            if os.path.exists(version_file):
                                import json
                                with open(version_file, 'r', encoding='utf-8') as vf:
                                    version_data = json.load(vf)
                                    if f in version_data and len(version_data[f]) > 0:
                                        has_history = 'true'
                                        current_version = f"v{version_data[f][-1]['version'] + 1}"
                        
                        files.append({
                            'name': f, 
                            'size': os.path.getsize(f_path), 
                            'modified': datetime.fromtimestamp(os.path.getmtime(f_path)).strftime('%Y-%m-%d %H:%M'), 
                            'path': os.path.join(folder, f).replace('\\', '/'),
                            'has_history': has_history,
                            'current_version': current_version
                        })
            node = {'name': folder, 'path': folder, 'level': level, 'fileCount': len(files), 'files': files, 'children': []}
            if os.path.exists(folder_path):
                subdirs = [d for d in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, d))]
                for subdir in subdirs:
                    node['children'].extend(build_folder_tree(project_dir, os.path.join(folder, subdir), level + 1))
            result.append(node)
        
        # 再添加用户在一级目录创建的文件夹
        if os.path.exists(project_dir):
            for item in os.listdir(project_dir):
                item_path = os.path.join(project_dir, item)
                # 只添加文件夹，且不在FOLDER_TYPES中的
                if os.path.isdir(item_path) and item not in FOLDER_TYPES:
                    files = []
                    for f in os.listdir(item_path):
                        f_path = os.path.join(item_path, f)
                        if os.path.isfile(f_path):
                            files.append({'name': f, 'size': os.path.getsize(f_path), 'modified': datetime.fromtimestamp(os.path.getmtime(f_path)).strftime('%Y-%m-%d %H:%M'), 'path': os.path.join(item, f).replace('\\', '/')})
                    node = {'name': item, 'path': item, 'level': level, 'fileCount': len(files), 'files': files, 'children': []}
                    # 递归添加子文件夹
                    subdirs = [d for d in os.listdir(item_path) if os.path.isdir(os.path.join(item_path, d))]
                    for subdir in subdirs:
                        node['children'].extend(build_folder_tree(project_dir, os.path.join(item, subdir), level + 1))
                    result.append(node)
    else:
        folder_path = os.path.join(project_dir, base_path)
        if not os.path.exists(folder_path):
            return []
        folder_name = os.path.basename(base_path)
        files = []
        for f in os.listdir(folder_path):
            f_path = os.path.join(folder_path, f)
            if os.path.isfile(f_path):
                files.append({'name': f, 'size': os.path.getsize(f_path), 'modified': datetime.fromtimestamp(os.path.getmtime(f_path)).strftime('%Y-%m-%d %H:%M'), 'path': os.path.join(base_path, f).replace('\\', '/')})
        node = {'name': folder_name, 'path': base_path, 'level': level, 'fileCount': len(files), 'files': files, 'children': []}
        subdirs = [d for d in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, d))]
        for subdir in subdirs:
            node['children'].extend(build_folder_tree(project_dir, os.path.join(base_path, subdir), level + 1))
        result.append(node)
    return result

@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    status_filter = request.args.get('status', '')
    search_keyword = request.args.get('search', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = 50
    partial = request.args.get('partial') == '1'
    service = current_app.extensions.get('project_service')
    if service is not None:
        result, status_groups = legacy_page(
            service, category='GENERAL_RESEARCH', page=page, page_size=per_page,
            status=status_filter, keyword=search_keyword,
        )
        projects_page = result['items']
        total = result['total']
        total_pages = (total + per_page - 1) // per_page if total else 1
        template = 'projects/partial_table.html' if partial else 'projects/index.html'
        return render_template(template,
            projects=projects_page, status_groups=status_groups,
            status_filter=status_filter, search_keyword=search_keyword,
            visible_directories=['projects', 'equipment', 'standards'],
            project_type='projects', project_type_display='科研项目',
            page=page, total_pages=total_pages, total=total)

    all_projects = ProjectModel().get_all()

    if status_filter:
        projects = [p for p in all_projects if p['status'] == status_filter]
    else:
        projects = all_projects
    if search_keyword:
        projects = [p for p in projects if search_keyword.lower() in str(p['project_id']).lower() or search_keyword.lower() in str(p['name']).lower() or search_keyword.lower() in str(p['leader']).lower()]

    total = len(projects)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    start = (page - 1) * per_page
    end = start + per_page
    projects_page = projects[start:end]

    status_groups = {}
    for p in projects_page:
        status = p['status'] if p['status'] else '未知'
        if status not in status_groups:
            status_groups[status] = []
        status_groups[status].append(p)

    if partial:
        return render_template('projects/partial_table.html',
            projects=projects_page, status_groups=status_groups,
            status_filter=status_filter, search_keyword=search_keyword,
            visible_directories=['projects', 'equipment', 'standards'],
            project_type='projects', project_type_display='科研项目',
            page=page, total_pages=total_pages, total=total)

    return render_template('projects/index.html',
        projects=projects_page, status_groups=status_groups,
        status_filter=status_filter, search_keyword=search_keyword,
        visible_directories=['projects', 'equipment', 'standards'],
        project_type='projects', project_type_display='科研项目',
        page=page, total_pages=total_pages, total=total)

@bp.route('/add', methods=['GET', 'POST'])
def add():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    if request.method == 'POST':
        project_id = request.form.get('project_id', '').strip()
        name = request.form.get('name', '').strip()
        user_name = session.get('name') or session.get('user')
        leader = request.form.get('leader', '').strip() or user_name
        today = datetime.now().strftime('%Y-%m-%d')
        start_date = request.form.get('start_date', '') or today
        end_date_plan = request.form.get('planned_end_date', '') or request.form.get('end_date_plan', '')
        end_date_actual = request.form.get('end_date_actual', '')
        status = request.form.get('status', '任务下达')
        if not name:
            flash('项目名称不能为空', 'error')
            return redirect(url_for('projects.add'))
        service = current_app.extensions.get('project_service')
        if service is not None:
            try:
                created = service.create_standalone('GENERAL_RESEARCH', {
                    'projectId': project_id, 'name': name, 'leader': leader,
                    'startDate': start_date, 'plannedEndDate': end_date_plan,
                    'actualEndDate': end_date_actual, 'status': status,
                    'taskNumber': request.form.get('task_number', ''),
                }, actor_user_id=current_identity().user_id)
                project_id = created['businessId']
            except ProjectServiceError as error:
                flash(error.message, 'error')
                return render_template('projects/add.html', today=today, default_leader=leader), error.status_code
        else:
            project_id = ProjectModel().add(project_id, name, leader, start_date, end_date_plan, end_date_actual, status)
        project_dir = os.path.join(current_app.config['UPLOAD_DIR'], project_id)
        try:
            os.makedirs(project_dir, exist_ok=True)
            for folder in FOLDER_TYPES:
                os.makedirs(os.path.join(project_dir, folder), exist_ok=True)
        except OSError:
            flash('项目已创建，但文件目录暂时无法初始化，不影响项目业务数据', 'warning')
        else:
            flash('项目创建成功', 'success')
        return redirect(url_for('projects.index'))
    today = datetime.now().strftime('%Y-%m-%d')
    current_user = session.get('name') or session.get('user')
    return render_template('projects/add.html', today=today, default_leader=current_user,
                           return_url=url_for('projects.index'))

@bp.route('/detail/<project_id>')
def detail(project_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    project = _get_project(project_id)
    if not project:
        flash('项目不存在', 'error')
        return redirect(url_for('projects.index'))
    
    try:
        project_dir = safe_project_path(current_app.config['UPLOAD_DIR'], project_id)
    except ValueError:
        return jsonify({'success': False, 'message': '非法路径'}), 400
    folder_tree = merge_project_files(build_folder_tree(project_dir), project_id)
    resources = _equipment_service().project_equipment(project_id)
    project_groups = resources['groups']
    project_equipment = resources['items']
    available_equipment = resources['available']
    return render_template('projects/detail.html', project=project, category='research', folder_tree=folder_tree, folder_types=FOLDER_TYPES, project_equipment=project_equipment, available_equipment=available_equipment, available_equipment_total=resources['available_total'], project_groups=project_groups)

@bp.route('/upload/<project_id>', methods=['POST'])
def upload(project_id):
    return upload_project_file(project_id, "GENERAL_RESEARCH")

@bp.route('/upload_folder/<project_id>', methods=['POST'])
def upload_folder(project_id):
    return upload_project_folder(project_id, "GENERAL_RESEARCH")

@bp.route('/create_folder/<project_id>', methods=['POST'])
def create_folder(project_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    folder_name = request.form.get('folder_name', '').strip()
    parent_folder = request.form.get('parent_folder', '')
    if not folder_name:
        return jsonify({'success': False, 'message': '请输入文件夹名称'})
    try:
        project_dir = safe_project_path(current_app.config['UPLOAD_DIR'], project_id)
        new_path = safe_project_path(
            current_app.config['UPLOAD_DIR'], project_id, parent_folder, folder_name
        )
    except ValueError:
        return jsonify({'success': False, 'message': '非法路径'}), 400
    os.makedirs(project_dir, exist_ok=True)
    
    if os.path.exists(new_path):
        return jsonify({'success': False, 'message': '文件夹已存在'})
    os.makedirs(new_path, exist_ok=True)
    return jsonify({'success': True, 'message': '创建成功'})

@bp.route('/download/<project_id>/<path:filepath>')
def download_file(project_id, filepath):
    controlled = controlled_project_download(project_id, "GENERAL_RESEARCH", filepath)
    if controlled is not None:
        return controlled
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    try:
        full_path = _safe_join(current_app.config['UPLOAD_DIR'], project_id, filepath)
    except ValueError:
        flash('非法路径', 'error')
        return redirect(url_for('projects.detail', project_id=project_id))
    if not os.path.exists(full_path):
        flash('文件不存在', 'error')
        return redirect(url_for('projects.detail', project_id=project_id))
    return send_file(full_path, as_attachment=True)

@bp.route('/delete_folder/<project_id>', methods=['POST'])
def delete_folder(project_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    folderpath = request.form.get('folder_path', '').strip()
    if not folderpath:
        return jsonify({'success': False, 'message': '文件夹路径不能为空'})
    try:
        full_path = safe_project_path(current_app.config['UPLOAD_DIR'], project_id, folderpath)
    except ValueError:
        return jsonify({'success': False, 'message': '非法路径'}), 400
    if not os.path.exists(full_path):
        return jsonify({'success': False, 'message': '文件夹不存在'})
    try:
        import shutil
        shutil.rmtree(full_path)
        return jsonify({'success': True, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@bp.route('/delete_file/<project_id>', methods=['POST'])
def delete_file(project_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    filepath = request.form.get('file_path', '').strip()
    if not filepath:
        return jsonify({'success': False, 'message': '文件路径不能为空'})
    try:
        full_path = safe_project_path(current_app.config['UPLOAD_DIR'], project_id, filepath)
    except ValueError:
        return jsonify({'success': False, 'message': '非法路径'}), 400
    if not os.path.exists(full_path):
        return jsonify({'success': False, 'message': '文件不存在'})
    try:
        os.remove(full_path)
        return jsonify({'success': True, 'message': '删除成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@bp.route('/rename_file/<project_id>', methods=['POST'])
def rename_file(project_id):
    """文件名重命名"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    filepath = request.form.get('file_path', '').strip()
    new_name = request.form.get('new_name', '').strip()
    if not filepath or not new_name:
        return jsonify({'success': False, 'message': '文件路径和新文件名不能为空'})
    try:
        full_path = safe_project_path(current_app.config['UPLOAD_DIR'], project_id, filepath)
    except ValueError:
        return jsonify({'success': False, 'message': '非法路径'}), 400
    if not os.path.exists(full_path):
        return jsonify({'success': False, 'message': '文件不存在'})
    # 获取新路径
    dir_path = os.path.dirname(filepath)
    new_filepath = os.path.join(dir_path, new_name) if dir_path else new_name
    try:
        new_full_path = safe_project_path(
            current_app.config['UPLOAD_DIR'], project_id, new_filepath
        )
    except ValueError:
        return jsonify({'success': False, 'message': '非法路径'}), 400
    if os.path.exists(new_full_path):
        return jsonify({'success': False, 'message': '文件名已存在'})
    try:
        os.rename(full_path, new_full_path)
        return jsonify({'success': True, 'message': '重命名成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@bp.route('/archive/<project_id>', methods=['POST'])
def archive_project(project_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    project = _get_project(project_id)
    if not project:
        flash('项目不存在', 'error')
        return redirect(url_for('projects.index'))
    
    # 支持两种方式：1. form表单的folders数组 2. JSON的paths数组
    import json
    selected_folders = request.form.getlist('folders')
    if not selected_folders:
        # 尝试从JSON body中获取
        try:
            data = request.get_json(silent=True)
            if data and 'paths' in data:
                selected_folders = data['paths']
        except:
            pass
    
    try:
        project_dir = safe_project_path(current_app.config['UPLOAD_DIR'], project_id)
    except ValueError:
        flash('非法项目路径', 'error')
        return redirect(url_for('projects.index'))
    if not os.path.exists(project_dir):
        flash('项目文件夹不存在', 'error')
        return redirect(url_for('projects.detail', project_id=project_id))
    safe_project_name = re.sub(r'[^\w\s.-]', '_', project['name']).strip('. ') or '项目'
    zip_filename = f"{safe_project_name}_{project_id}.zip"
    zip_path = os.path.join(current_app.config['UPLOAD_DIR'], zip_filename)
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if selected_folders:
                for folder in selected_folders:
                    try:
                        folder_path = safe_project_path(
                            current_app.config['UPLOAD_DIR'], project_id, folder
                        )
                    except ValueError:
                        continue
                    if os.path.exists(folder_path):
                        for root, dirs, files in os.walk(folder_path):
                            for f in files:
                                fp = os.path.join(root, f)
                                arcname = os.path.relpath(fp, project_dir)
                                zf.write(fp, arcname)
            else:
                for root, dirs, files in os.walk(project_dir):
                    for f in files:
                        fp = os.path.join(root, f)
                        arcname = os.path.relpath(fp, project_dir)
                        zf.write(fp, arcname)
        return send_file(zip_path, as_attachment=True, download_name=zip_filename)
    except Exception as e:
        flash(f'归档失败: {e}', 'error')
        return redirect(url_for('projects.detail', project_id=project_id))

@bp.route('/link_equipment/<project_id>', methods=['POST'])
def link_equipment(project_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    leader = session.get('user')
    project = _get_project(project_id)
    if not project:
        return jsonify({'success': False, 'message': '项目不存在'}), 404
    equipment_id = request.form.get('equipment_id', '').strip()
    quantity = request.form.get('quantity', '1').strip()
    location = request.form.get('location', '').strip()
    if not equipment_id:
        return jsonify({'success': False, 'message': '请选择设备'})

    # 获取或创建项目关联的设备组
    project_name = project['name'] if project else project_id
    try:
        _equipment_service().link_project_equipment(
            project_id, project_name, leader, equipment_id,
            quantity=quantity, location=location,
        )
        return jsonify({'success': True, 'message': '关联成功'})
    except ResourceServiceError as error:
        return jsonify({'success': False, 'message': error.message}), error.status_code

@bp.route('/unlink_equipment/<project_id>/<group_id>/<equipment_id>', methods=['POST'])
def unlink_equipment(project_id, group_id, equipment_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    project = _get_project(project_id)
    if not project:
        return jsonify({'success': False, 'message': '项目不存在'}), 404
    try:
        _equipment_service().unlink_project_equipment(project_id, group_id, equipment_id)
        return jsonify({'success': True, 'message': '取消关联成功'})
    except ResourceServiceError as error:
        return jsonify({'success': False, 'message': error.message}), error.status_code

@bp.route('/get_equipment_requirements/<project_id>')
def get_equipment_requirements(project_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    project = _get_project(project_id)
    if not project:
        return jsonify({'success': False, 'message': '项目不存在'})
    
    # 从设备组获取设备
    result = _equipment_service().project_equipment(project_id)['items']
    return jsonify({'success': True, 'equipment': result})

@bp.route('/export_equipment/<project_id>')
def export_equipment(project_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    project = _get_project(project_id)
    if not project:
        flash('项目不存在', 'error')
        return redirect(url_for('projects.index'))
    
    # 获取项目设备（从设备组获取）
    project_equipment = _equipment_service().project_equipment(project_id)['items']
    
    # 创建Excel
    import io
    from openpyxl import Workbook
    
    wb = Workbook()
    ws = wb.active
    ws.title = '设备选型'
    
    # 表头
    ws.append(['设备名称', '型号', '分类', '数量', '使用位置'])
    
    # 数据
    for item in project_equipment:
        ws.append([
            item.get('name', ''), item.get('model', ''), item.get('category', ''),
            item.get('quantity', 1), item.get('location', '')
        ])
    
    # 保存到内存
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    from flask import make_response
    
    # 使用ASCII文件名避免编码问题
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    response.headers['Content-Disposition'] = 'attachment; filename="equipment_export.xlsx"'
    return response

@bp.route('/update_equipment/<project_id>/<group_id>/<equipment_id>', methods=['POST'])
def update_equipment(project_id, group_id, equipment_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    project = _get_project(project_id)
    if not project:
        return jsonify({'success': False, 'message': '项目不存在'}), 404
    quantity = request.form.get('quantity', '').strip()
    location = request.form.get('location', '').strip()
    try:
        _equipment_service().update_project_equipment(
            project_id, group_id, equipment_id,
            quantity=quantity or 1, location=location,
        )
        return jsonify({'success': True, 'message': '更新成功'})
    except ResourceServiceError as error:
        return jsonify({'success': False, 'message': error.message}), error.status_code

@bp.route('/update_field/<project_id>', methods=['POST'])
def update_project_field(project_id):
    """更新项目单个字段"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    
    field = request.form.get('field', '').strip()
    value = request.form.get('value', '').strip()
    
    # 字段映射
    field_map = {
        '实际结束日期': 'actual_end_date',
        '状态': 'status',
        '任务号': 'task_number'
    }
    
    if field not in field_map:
        return jsonify({'success': False, 'message': '无效字段'})

    service = current_app.extensions.get('project_service')
    if service is not None:
        try:
            service.update_legacy_field(
                category='GENERAL_RESEARCH', business_id=project_id,
                field=field_map[field], value=value,
                actor_user_id=current_identity().user_id,
                request_id=getattr(request, 'request_id', 'legacy-project-update'),
            )
            return jsonify({'success': True, 'message': '更新成功'})
        except ProjectServiceError as error:
            return jsonify({'success': False, 'message': error.message}), error.status_code
    
    if field_map[field] == 'status':
        return jsonify({'success': False, 'message': '项目状态服务暂不可用，未执行修改'}), 503

    project = ProjectModel().get_by_id(project_id)
    if not project:
        return jsonify({'success': False, 'message': '项目不存在'})
    
    ProjectModel().update(project_id, **{field_map[field]: value})
    return jsonify({'success': True, 'message': '更新成功'})

@bp.route('/batch_export', methods=['POST'])
def batch_export():
    """批量导出项目"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    import json
    project_ids = json.loads(request.form.get('project_ids', '[]'))
    
    if not project_ids:
        return jsonify({'success': False, 'message': '请选择要导出的项目'})
    
    # 创建ZIP文件（使用内存）
    import zipfile
    import io
    
    # 获取文档目录（项目文件存放在uploads目录）
    docs_base = current_app.config['UPLOAD_DIR']
    
    # 用于跟踪同名文件，保留最新版本
    file_tracker = {}  # {relative_path: (full_path, modified_time)}
    
    for project_id in project_ids:
        project = _get_project(project_id)
        if not project:
            continue
        
        # 项目文件夹命名：文件编号+名称
        folder_name = f"{project['project_id']}_{project['name']}"
        # 清理文件夹名中的非法字符
        folder_name = ''.join(c for c in folder_name if c not in ['\\', '/', ':', '*', '?', '"', '<', '>', '|'])

        project_dir = safe_project_path(docs_base, project['project_id'])
        
        if os.path.exists(project_dir):
            # 遍历项目文件，保留层级结构
            for root, dirs, files in os.walk(project_dir):
                for filename in files:
                    src_path = os.path.join(root, filename)
                    
                    # 计算相对路径（保留层级结构）
                    rel_path = os.path.relpath(src_path, project_dir)
                    dest_rel_path = os.path.join(folder_name, rel_path)
                    
                    # 获取文件修改时间
                    mtime = os.path.getmtime(src_path)
                    
                    # 同名文件只保留最新版本
                    if dest_rel_path in file_tracker:
                        existing_path, existing_mtime = file_tracker[dest_rel_path]
                        if mtime > existing_mtime:
                            file_tracker[dest_rel_path] = (src_path, mtime)
                    else:
                        file_tracker[dest_rel_path] = (src_path, mtime)
    
    # 在内存中创建ZIP文件
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dest_rel_path, (src_path, mtime) in file_tracker.items():
            zf.write(src_path, dest_rel_path)
    
    zip_buffer.seek(0)
    
    # 返回ZIP文件
    return send_file(zip_buffer, as_attachment=True, download_name='projects_export.zip', mimetype='application/zip')

@bp.route('/delete/<project_id>', methods=['POST'])
def delete(project_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    service = current_app.extensions.get('project_service')
    if service is not None:
        try:
            service.delete_legacy_standalone(category='GENERAL_RESEARCH', business_id=project_id)
            return jsonify({'success': True, 'message': '删除成功'})
        except ProjectServiceError as error:
            return jsonify({'success': False, 'message': error.message}), error.status_code
    project = ProjectModel().get_by_id(project_id)
    if not project:
        return jsonify({'success': False, 'message': '项目不存在'})
    ProjectModel().delete(project_id)
    return jsonify({'success': True, 'message': '删除成功'})

@bp.route('/documents/<project_id>')
def project_documents(project_id):
    """项目文档页面"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    
    project = _get_project(project_id)
    if not project:
        flash('项目不存在', 'error')
        return redirect(url_for('projects.index'))
    
    # 获取项目文档目录
    docs_dir = safe_project_documents_path(current_app.config['UPLOAD_DIR'], project_id)
    documents = []
    
    if os.path.exists(docs_dir):
        for f in os.listdir(docs_dir):
            f_path = os.path.join(docs_dir, f)
            if os.path.isfile(f_path):
                ext = f.rsplit('.', 1)[-1].lower() if '.' in f else ''
                documents.append({
                    'name': f,
                    'path': f'/uploads/projects/{project_id}/{f}',
                    'type': 'pdf' if ext == 'pdf' else 'doc' if ext in ['doc', 'docx'] else 'xls' if ext in ['xls', 'xlsx'] else 'other',
                    'size': os.path.getsize(f_path),
                    'upload_time': datetime.fromtimestamp(os.path.getmtime(f_path)).strftime('%Y-%m-%d %H:%M')
                })
    
    return render_template('projects/documents.html', 
                         project=project,
                         documents=documents,
                         document_blueprint='projects')

@bp.route('/upload_doc/<project_id>', methods=['POST'])
def upload_project_doc(project_id):
    """上传项目文档"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    if not _get_project(project_id):
        return jsonify({'success': False, 'message': '项目不存在'}), 404
    
    if 'document' not in request.files:
        return jsonify({'success': False, 'message': '请选择文件'})
    
    file = request.files['document']
    if file.filename == '':
        return jsonify({'success': False, 'message': '请选择文件'})
    
    docs_dir = safe_project_documents_path(current_app.config['UPLOAD_DIR'], project_id)
    os.makedirs(docs_dir, exist_ok=True)
    
    filename = os.path.basename(file.filename.replace('\\', '/'))
    filename = re.sub(r'[^\w\s.-]', '_', filename).strip('. ')
    if not filename:
        filename = 'unnamed'
    file_path = os.path.join(docs_dir, filename)
    file.save(file_path)
    
    flash('文档上传成功', 'success')
    return redirect(url_for('projects.project_documents', project_id=project_id))

@bp.route('/delete_doc/<project_id>/<filename>', methods=['POST'])
def delete_project_doc(project_id, filename):
    """删除项目文档"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    if not _get_project(project_id):
        return jsonify({'success': False, 'message': '项目不存在'}), 404
    
    safe_name = re.sub(r'[^\w\s.-]', '_', filename).strip('. ') or 'unnamed'
    file_path = safe_project_documents_path(
        current_app.config['UPLOAD_DIR'], project_id, safe_name
    )
    
    if os.path.exists(file_path):
        os.remove(file_path)
        return jsonify({'success': True, 'message': '删除成功'})
    else:
        return jsonify({'success': False, 'message': '文件不存在'})

@bp.route('/download_doc/<project_id>/<filename>')
def download_project_doc(project_id, filename):
    """下载项目文档"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    if not _get_project(project_id):
        flash('项目不存在', 'error')
        return redirect(url_for('projects.index'))
    try:
        file_path = safe_project_documents_path(
            current_app.config['UPLOAD_DIR'], project_id,
            re.sub(r'[^\w\s.-]', '_', filename).strip('. ') or 'unnamed',
        )
    except ValueError:
        flash('非法路径', 'error')
        return redirect(url_for('projects.project_documents', project_id=project_id))
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        flash('文件不存在', 'error')
        return redirect(url_for('projects.project_documents', project_id=project_id))
