# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, send_file, current_app, Response, stream_with_context
from app.models import EquipmentModel, OperationLogModel, DeviceHostRelationModel, HostDeviceModel, ResearchUnitModel, KnowledgeSubclassModel, get_db
import os
import re
import csv
import io
import json
import threading
import time
import uuid
from datetime import datetime

from app.utils import import_progress as ip

bp = Blueprint('equipment', __name__, url_prefix='/equipment')


def _safe_filename(filename):
    """净化文件名,防止路径穿越"""
    filename = os.path.basename(filename)  # 去掉路径,只保留文件名
    filename = re.sub(r'[^\w\s.-]', '_', filename)  # 只保留字母数字下划线短横线点和空格
    filename = filename.strip('. ')  # 去掉首尾点和空格
    return filename or 'unnamed_file'


def get_subclasses_json():
    """获取所有子类，按分类分组，用于前端联动下拉"""
    sc_model = KnowledgeSubclassModel()
    all_subclasses = sc_model.get_all()
    result = {}
    for sc in all_subclasses:
        cat = sc.get('parent_category', '')
        if cat not in result:
            result[cat] = []
        result[cat].append({'subclass_name': sc.get('subclass_name', '')})
    return result


@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    equipment_model = EquipmentModel()
    category_filter = request.args.get('category', '')
    form_filter = request.args.get('form', '')
    tech_status_filter = request.args.get('tech_status', '')
    keyword = request.args.get('keyword', '')
    subclass_filter = request.args.get('subclass', '')
    page = int(request.args.get('page', 1))
    per_page = 20

    # 使用搜索方法
    if category_filter or form_filter or tech_status_filter or keyword or subclass_filter:
        all_equipment = equipment_model.search(
            category=category_filter if category_filter else None,
            form=form_filter if form_filter else None,
            tech_status=tech_status_filter if tech_status_filter else None,
            keyword=keyword if keyword else None,
            subclass=subclass_filter if subclass_filter else None
        )
    else:
        all_equipment = equipment_model.get_all()

    # 分页
    total = len(all_equipment)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    start = (page - 1) * per_page
    end = start + per_page
    equipment = all_equipment[start:end]

    # 统计
    stats = {
        'total': len(equipment),
        'security': len([e for e in equipment if e.get('category') == '安全设备']),
        'crypto': len([e for e in equipment if e.get('category') == '密码设备']),
        'general': len([e for e in equipment if e.get('category') == '通用设备']),
        'total_value': sum([float(e.get('price') or 0) for e in equipment])
    }

    category_groups = {'安全设备': [], '密码设备': [], '通用设备': []}
    for e in equipment:
        cat = e.get('category') or '通用设备'
        if cat in category_groups:
            category_groups[cat].append(e)

    sc_model = KnowledgeSubclassModel()
    all_subclasses = sc_model.get_all()
    subclasses_json = {}
    for sc in all_subclasses:
        cat = sc.get('parent_category', '')
        if cat not in subclasses_json:
            subclasses_json[cat] = []
        subclasses_json[cat].append({'subclass_name': sc.get('subclass_name', '')})

    # REQ-015: ?fragment=1 时返回纯表格片段（AJAX 用）
    if request.args.get('fragment') == '1':
        return render_template('equipment/_table_fragment.html',
                             equipment=equipment,
                             category_filter=category_filter,
                             form_filter=form_filter,
                             tech_status_filter=tech_status_filter,
                             keyword=keyword,
                             page=page,
                             total_pages=total_pages,
                             total=total,
                             per_page=per_page,
                             subclass_filter=subclass_filter)

    return render_template('equipment/index.html',
                         equipment=equipment,
                         category_groups=category_groups,
                         category_filter=category_filter,
                         form_filter=form_filter,
                         tech_status_filter=tech_status_filter,
                         keyword=keyword,
                         stats=stats,
                         page=page,
                         total_pages=total_pages,
                         total=total,
                         per_page=per_page,
                         subclass_filter=subclass_filter,
                         subclasses_json=subclasses_json)

@bp.route('/stats')
def stats():
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    equipment_model = EquipmentModel()
    equipment = equipment_model.get_all()

    # 按分类统计
    by_category = {
        '安全设备': {'count': 0, 'value': 0},
        '密码设备': {'count': 0, 'value': 0},
        '通用设备': {'count': 0, 'value': 0}
    }

    # 按技术状态统计
    by_status = {}

    for e in equipment:
        cat = e.get('category') or '通用设备'
        status = e.get('tech_status') or '未知'
        value = float(e.get('price') or 0)

        if cat in by_category:
            by_category[cat]['count'] += 1
            by_category[cat]['value'] += value

        if status not in by_status:
            by_status[status] = 0
        by_status[status] += 1

    total_value = sum([by_category[k]['value'] for k in by_category])

    return render_template('equipment/stats.html',
                         by_category=by_category,
                         by_status=by_status,
                         total_value=total_value,
                         total=len(equipment))

@bp.route('/add', methods=['GET', 'POST'])
def add():
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        model = request.form.get('model', '').strip()
        category = request.form.get('category', '').strip()
        form = request.form.get('form', '').strip()
        price = request.form.get('price', '').strip()
        tech_index = request.form.get('tech_index', '').strip()
        installation_requirements = request.form.get('installation_requirements', '').strip()
        tech_status = request.form.get('tech_status', '').strip()
        manufacturer = request.form.get('manufacturer', '').strip()
        main_purpose = request.form.get('main_purpose', '').strip()
        former_name = request.form.get('former_name', '').strip()
        resource_guarantee = request.form.get('resource_guarantee', '').strip()

        equipment_model = EquipmentModel()
        subclass = request.form.get('subclass', '').strip()
        equipment_id = equipment_model.add(name, model, category, form, price, tech_index, tech_status, installation_requirements, manufacturer, main_purpose=main_purpose, former_name=former_name, resource_guarantee=resource_guarantee, subclass=subclass)

        # 记录操作日志
        log_model = OperationLogModel()
        log_model.add(module='equipment', operation_type='添加设备', file_name=name, operator=session.get('user', '未知'))

        # 处理图片上传 - 支持多图
        image_files = request.files.getlist('equipment_images')
        image_paths = []
        if image_files and any(f.filename for f in image_files):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_dir = os.path.dirname(base_dir)
            img_dir = os.path.join(base_dir, 'uploads', 'equipment_images')
            os.makedirs(img_dir, exist_ok=True)
            for idx, image_file in enumerate(image_files):
                if image_file.filename:
                    ext = image_file.filename.rsplit('.', 1)[-1].lower() if '.' in image_file.filename else ''
                    if ext in ['jpg', 'jpeg', 'png', 'gif']:
                        img_name = f"{equipment_id}_{idx}.{ext}"
                        img_path = os.path.join(img_dir, img_name)
                        image_file.save(img_path)
                        image_paths.append(f"/uploads/equipment_images/{img_name}")
                        log_model.add(module='equipment', operation_type='上传图片', file_name=image_file.filename, operator=session.get('user', '未知'))
            if image_paths:
                equipment_model.update(equipment_id, equipment_image=';'.join(image_paths))

        flash('设备添加成功', 'success')
        return redirect(url_for('equipment.index'))

    return render_template('equipment/add.html', subclasses_json=get_subclasses_json())

@bp.route('/detail/<equipment_id>')
def detail(equipment_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    equipment_model = EquipmentModel()
    equipment = equipment_model.get_by_id(equipment_id)

    if not equipment:
        flash('设备不存在', 'error')
        return redirect(url_for('equipment.index'))

    rel_model = DeviceHostRelationModel()
    related_hosts = rel_model.get_hosts_by_device(equipment_id)

    return render_template('equipment/detail.html', equipment=equipment, related_hosts=related_hosts)

@bp.route('/edit/<equipment_id>', methods=['GET', 'POST'])
def edit(equipment_id):
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    equipment_model = EquipmentModel()
    equipment = equipment_model.get_by_id(equipment_id)

    if not equipment:
        flash('设备不存在', 'error')
        return redirect(url_for('equipment.index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        model = request.form.get('model', '').strip()
        category = request.form.get('category', '').strip()
        form = request.form.get('form', '').strip()
        price = request.form.get('price', '').strip()
        tech_index = request.form.get('tech_index', '').strip()
        installation_requirements = request.form.get('installation_requirements', '').strip()
        tech_status = request.form.get('tech_status', '').strip()
        manufacturer = request.form.get('manufacturer', '').strip()
        main_purpose = request.form.get('main_purpose', '').strip()
        former_name = request.form.get('former_name', '').strip()
        resource_guarantee = request.form.get('resource_guarantee', '').strip()
        subclass = request.form.get('subclass', '').strip()

        equipment_model.update(equipment_id,
            name=name,
            model=model,
            category=category,
            form=form,
            price=price,
            tech_index=tech_index,
            installation_requirements=installation_requirements,
            tech_status=tech_status,
            manufacturer=manufacturer,
            main_purpose=main_purpose,
            former_name=former_name,
            resource_guarantee=resource_guarantee,
            subclass=subclass)

        # 处理图片上传
        # 处理图片上传 - 支持多图
        image_files = request.files.getlist('equipment_images')
        if image_files and any(f.filename for f in image_files):
            # 使用绝对路径确保目录创建正确
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_dir = os.path.dirname(base_dir)  # 再往上一级到项目根目录
            img_dir = os.path.join(base_dir, 'uploads', 'equipment_images')
            os.makedirs(img_dir, exist_ok=True)
            log_model = OperationLogModel()  # 初始化日志模型
            image_paths = []
            for idx, image_file in enumerate(image_files):
                if image_file.filename:
                    ext = image_file.filename.rsplit('.', 1)[-1].lower() if '.' in image_file.filename else ''
                    if ext in ['jpg', 'jpeg', 'png', 'gif']:
                        img_name = f"{equipment_id}_{idx}.{ext}"
                        img_path = os.path.join(img_dir, img_name)
                        image_file.save(img_path)
                        image_paths.append(f"/uploads/equipment_images/{img_name}")
                        log_model.add(module='equipment', operation_type='上传图片', file_name=image_file.filename, operator=session.get('user', '未知'))
            if image_paths:
                equipment_model.update(equipment_id, equipment_image=';'.join(image_paths))

        flash('设备更新成功', 'success')
        return redirect(url_for('equipment.detail', equipment_id=equipment_id))

    return render_template('equipment/edit.html', equipment=equipment, subclasses_json=get_subclasses_json())

@bp.route('/delete/<equipment_id>', methods=['POST'])
def delete(equipment_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    equipment_model = EquipmentModel()
    equipment = equipment_model.get_by_id(equipment_id)
    equipment_name = equipment['name'] if equipment else equipment_id
    equipment_model.delete(equipment_id)

    # 记录操作日志
    log_model = OperationLogModel()
    log_model.add(module='equipment', operation_type='删除设备', file_name=equipment_name, operator=session.get('user', '未知'))

    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/upload_file/<equipment_id>', methods=['POST'])
def upload_file(equipment_id):
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    if 'related_file' not in request.files:
        return jsonify({'success': False, 'message': '请选择文件'})

    file = request.files['related_file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '请选择文件'})

    equipment_model = EquipmentModel()
    equipment = equipment_model.get_by_id(equipment_id)
    if not equipment:
        return jsonify({'success': False, 'message': '设备不存在'})

    # 保存文件 - 使用绝对路径
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base_dir = os.path.dirname(base_dir)  # 再往上一级到项目根目录
    file_dir = os.path.join(base_dir, 'uploads', 'equipment_files', equipment_id)
    os.makedirs(file_dir, exist_ok=True)
    safe_name = _safe_filename(file.filename)
    file_path = os.path.join(file_dir, safe_name)
    file.save(file_path)

    # 更新关联
    existing = equipment['related_files'] or ''
    new_file = f"/uploads/equipment_files/{equipment_id}/{safe_name}"
    updated = new_file if not existing else existing + ',' + new_file
    equipment_model.update(equipment_id, related_files=updated)

    # 记录操作日志
    log_model = OperationLogModel()
    log_model.add(module='equipment', operation_type='上传文件', file_name=safe_name, operator=session.get('user', '未知'))

    return jsonify({'success': True, 'message': '上传成功'})

@bp.route('/export')
def export():
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    from io import BytesIO
    import openpyxl

    equipment_model = EquipmentModel()
    rel_model = DeviceHostRelationModel()
    all_equipment = equipment_model.get_all()

    output = BytesIO()
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = '设备知识库'

    # 表头（增强：关联宿主设备ID/名称列）
    ws.append([
        '设备编号', '设备名称', '曾用名', '型号', '分类', '设备子类', '形态',
        '单价', '主要用途', '资源保障要求', '功能技术指标',
        '技术状态', '研制单位', '创建时间',
        '关联宿主设备ID', '关联宿主设备名称'
    ])

    for e in all_equipment:
        host_rels = rel_model.get_hosts_by_device(e['equipment_id'])
        host_ids = '、'.join([h['host_id'] for h in host_rels]) if host_rels else ''
        host_names = '、'.join([h['name'] for h in host_rels]) if host_rels else ''
        ws.append([
            e.get('equipment_id'), e.get('name'), e.get('former_name'), e.get('model'),
            e.get('category'), e.get('subclass'), e.get('form'), e.get('price'), e.get('main_purpose'),
            e.get('resource_guarantee'), e.get('tech_index'), e.get('tech_status'),
            e.get('manufacturer'), e.get('created_at'),
            host_ids, host_names
        ])

    # 自动列宽
    for col in ws.columns:
        max_len = 0
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    workbook.save(output)
    output.seek(0)
    return send_file(output,
                     download_name='设备知识库.xlsx',
                     as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ---------- 三步导入流程 ----------
from app.utils.fuzzy_match import match_host_device, match_research_unit, match_subclass


@bp.route('/import', methods=['GET'])
def import_page():
    """步骤1：导入上传页"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('equipment/import_step1.html')


@bp.route('/import/preview', methods=['POST'])
def import_preview():
    """
    步骤1提交：解析文件 + 列映射 + 执行模糊匹配
    支持 preview_only=1 模式：只返回预览数据
    """
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    from io import BytesIO
    import openpyxl
    import json

    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '请上传文件'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '请选择文件'})

    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({'success': False, 'message': '请上传 xlsx 文件'})

    preview_only = request.form.get('preview_only', '') == '1'

    try:
        stream = BytesIO(file.read())
        workbook = openpyxl.load_workbook(stream, data_only=True)
        ws = workbook.active

        all_rows = []
        for row in ws.iter_rows(values_only=True):
            all_rows.append(list(row))

        if len(all_rows) < 2:
            return jsonify({'success': False, 'message': '文件数据少于2行'})

        header = [str(h).strip() if h is not None else '' for h in all_rows[0]]
        data_rows = all_rows[1:]
        preview_rows = data_rows[:10]

        if preview_only:
            return jsonify({
                'success': True,
                'headers': header,
                'preview_rows': [[str(c) if c is not None else '' for c in row] for row in preview_rows],
            })

        # 完整解析模式
        try:
            column_mapping = json.loads(request.form.get('column_mapping_json', '{}'))
        except Exception:
            return jsonify({'success': False, 'message': '列映射参数错误'})

        # [REQ-012] 读取用户选定的批量分类（必填）
        default_category = request.form.get('default_category', '').strip()
        valid_categories = ('安全设备', '密码设备', '通用设备')
        if not default_category:
            return jsonify({'success': False, 'message': '请先选择批量分类'}), 400
        if default_category not in valid_categories:
            return jsonify({'success': False, 'message': f'无效的分类：{default_category}（仅支持 安全设备/密码设备/通用设备）'}), 400

        equipment_model = EquipmentModel()
        rel_model = DeviceHostRelationModel()
        related_col_idx = column_mapping.get('related_host_devices')

        processed = []
        exact_count = 0
        fuzzy_count = 0
        unmatched_count = 0
        duplicate_count = 0
        new_category_count = 0

        # 固定分类（不允许动态新增）
        valid_categories = {'安全设备', '密码设备', '通用设备', '其他设备'}

        for row_idx, row in enumerate(data_rows):
            if not row or not any(v for v in row if v is not None):
                continue

            name = _eq_get_row_value(row, column_mapping, 'name')
            model = _eq_get_row_value(row, column_mapping, 'model')
            # [REQ-012] category 不再从 xlsx 读取，统一使用用户在步骤1选定的批量分类
            category = default_category
            form = _eq_get_row_value(row, column_mapping, 'form')
            price_str = _eq_get_row_value(row, column_mapping, 'price')
            tech_index = _eq_get_row_value(row, column_mapping, 'tech_index')
            tech_status = _eq_get_row_value(row, column_mapping, 'tech_status')
            manufacturer = _eq_get_row_value(row, column_mapping, 'manufacturer')
            main_purpose = _eq_get_row_value(row, column_mapping, 'main_purpose')
            former_name = _eq_get_row_value(row, column_mapping, 'former_name')
            resource_guarantee = _eq_get_row_value(row, column_mapping, 'resource_guarantee')
            installation_requirements = _eq_get_row_value(row, column_mapping, 'installation_requirements')
            equipment_id_raw = _eq_get_row_value(row, column_mapping, 'equipment_id')
            subclass = _eq_get_row_value(row, column_mapping, 'subclass')

            if not name:
                continue

            # 重复检测：按 name + model 组合
            duplicate_status = None
            existing_eq = None
            if equipment_id_raw:
                existing_eq = equipment_model.get_by_id(str(equipment_id_raw))
            else:
                # 按 name+model 查找
                all_eq = equipment_model.get_all()
                for eq in all_eq:
                    if eq.get('name') == name and (not model or eq.get('model') == model):
                        existing_eq = eq
                        break

            if existing_eq:
                duplicate_status = 'exists'

            # 类型归一
            is_new_category = False
            if category and category not in valid_categories:
                is_new_category = True
                new_category_count += 1

            # 关联宿主设备匹配
            related_result = {'original': '', 'devices': []}
            if related_col_idx is not None and related_col_idx < len(row):
                related_text = str(row[related_col_idx] or '').strip()
                related_result = match_host_device(related_text)

                for dev in related_result['devices']:
                    if dev['status'] == 'exact':
                        exact_count += 1
                    elif dev['status'] == 'fuzzy':
                        fuzzy_count += 1
                    else:
                        unmatched_count += 1

            # 研制单位模糊匹配
            manufacturer_result = {'original': manufacturer, 'units': []}
            ru_model = ResearchUnitModel()
            if manufacturer:
                # 按 、 ， ； 分隔
                parts = re.split(r'[、，；]', manufacturer)
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    mru = match_research_unit(part, ru_model)
                    unit_entry = {
                        'original': part,
                        'matched': mru['matched'],
                        'exact': mru['exact'],
                        'method': mru['method'],
                        'candidates': mru.get('candidates', []),
                    }
                    manufacturer_result['units'].append(unit_entry)
                    if mru['exact']:
                        exact_count += 1
                    elif mru['matched']:
                        fuzzy_count += 1
                    else:
                        unmatched_count += 1

            # 设备子类模糊匹配
            subclass_result = {'original': subclass, 'matched': None, 'exact': False, 'method': 'none'}
            sc_model = KnowledgeSubclassModel()
            if subclass:
                ms = match_subclass(subclass, sc_model, category)
                subclass_result = {
                    'original': subclass,
                    'matched': ms['matched'],
                    'exact': ms['exact'],
                    'method': ms['method'],
                    'candidates': ms.get('candidates', []),
                }
                if ms['exact']:
                    exact_count += 1
                elif ms['matched']:
                    fuzzy_count += 1

            # 价格字段校验
            price_error = ''
            if price_str:
                try:
                    float(price_str)
                except Exception:
                    price_error = '价格格式错误'

            processed.append({
                'row_idx': row_idx,
                'equipment_id_raw': equipment_id_raw,
                'name': name,
                'model': model,
                'category': category,
                'form': form,
                'price': price_str,
                'tech_index': tech_index,
                'tech_status': tech_status,
                'manufacturer': manufacturer,
                'main_purpose': main_purpose,
                'former_name': former_name,
                'resource_guarantee': resource_guarantee,
                'installation_requirements': installation_requirements,
                'is_new_category': is_new_category,
                'price_error': price_error,
                'duplicate_status': duplicate_status,
                'existing_equipment': dict(existing_eq) if existing_eq else None,
                'related_result': related_result,
                'manufacturer_result': manufacturer_result,
                'subclass_result': subclass_result,
            })

            if duplicate_status == 'exists':
                duplicate_count += 1

        statistics = {
            'total': len(processed),
            'exact': exact_count,
            'fuzzy': fuzzy_count,
            'unmatched': unmatched_count,
            'duplicate': duplicate_count,
            'new_category': new_category_count,
        }

        session['equipment_import_preview'] = {
            'filename': file.filename,
            'column_mapping': column_mapping,
            'processed': processed,
            'statistics': statistics,
        }

        return jsonify({'success': True, 'redirect': url_for('equipment.import_preview_page')})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'解析失败: {str(e)}'})


def _eq_get_row_value(row, column_mapping, field):
    col_idx = column_mapping.get(field)
    if col_idx is None or col_idx >= len(row):
        return ''
    val = row[col_idx]
    return str(val).strip() if val is not None else ''


@bp.route('/import/save_row', methods=['POST'])
def import_save_row():
    """[REQ-013] AJAX: 保存单行修改到 session（用户翻页前自动同步）

    请求: form 字段 row_idx=N, field=name|model|category|..., value=xxx
    效果: 更新 session['equipment_import_preview']['processed'][idx][field] = value
    """
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'}), 401
    preview_data = session.get('equipment_import_preview')
    if not preview_data:
        return jsonify({'success': False, 'message': '会话过期'}), 400

    try:
        row_idx = int(request.form.get('row_idx', -1))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'row_idx 无效'}), 400

    field = request.form.get('field', '').strip()
    value = request.form.get('value', '').strip()
    # 允许的字段
    allowed_fields = {'name', 'model', 'category', 'form', 'price', 'tech_index',
                      'tech_status', 'manufacturer', 'main_purpose', 'former_name',
                      'resource_guarantee', 'installation_requirements',
                      'subclass', 'subclass_action', 'dup_action'}
    if field not in allowed_fields:
        return jsonify({'success': False, 'message': f'不允许的字段: {field}'}), 400

    # 找到对应行并更新
    target = None
    for r in preview_data['processed']:
        if r.get('row_idx') == row_idx:
            target = r
            break
    if not target:
        return jsonify({'success': False, 'message': f'行 {row_idx} 不存在'}), 404

    # price 特殊处理
    if field == 'price':
        try:
            target['price'] = float(value) if value else None
        except ValueError:
            target['price_error'] = '价格格式错误'
    elif field == 'subclass_action':
        target.setdefault('_user_actions', {})[field] = value
    elif field == 'dup_action':
        target.setdefault('_user_actions', {})[field] = value
    else:
        target[field] = value

    # session 写回
    session['equipment_import_preview'] = preview_data
    session.modified = True
    return jsonify({'success': True, 'row_idx': row_idx, 'field': field, 'value': value})


@bp.route('/import/preview', methods=['GET'])
def import_preview_page():
    """步骤2：匹配预览页面（支持分页 + 标签筛选）

    [REQ-013-fix] 修复 1000+ 条数据时 HTML 8.2MB 渲染超时问题：
    - 服务端按 page/filter 切片返回数据
    - 每页 100 行，避免一次渲染所有行
    - 翻页/筛选通过 GET URL 参数实现（无 JS 状态）
    """
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    preview_data = session.get('equipment_import_preview')
    if not preview_data:
        flash('请先上传文件', 'error')
        return redirect(url_for('equipment.import_page'))

    # 读取分页/筛选参数
    try:
        page = int(request.args.get('p', 1))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = int(request.args.get('per_page', 100))
    except (ValueError, TypeError):
        per_page = 100
    if per_page not in (50, 100, 200):
        per_page = 100
    if page < 1:
        page = 1

    filter_type = request.args.get('filter', 'all')
    if filter_type not in ('all', 'exact', 'fuzzy', 'unmatched', 'duplicate', 'newcat'):
        filter_type = 'all'

    all_processed = preview_data['processed']

    # 标签筛选
    if filter_type == 'all':
        filtered_rows = all_processed
    elif filter_type == 'duplicate':
        filtered_rows = [r for r in all_processed if r.get('duplicate_status') == 'exists']
    elif filter_type == 'newcat':
        filtered_rows = [r for r in all_processed if r.get('is_new_category')]
    else:
        # exact / fuzzy / unmatched (基于 subclass_result.method 或 related_result)
        filtered_rows = []
        for r in all_processed:
            # 优先看 subclass_result.method
            sr = r.get('subclass_result') or {}
            if sr.get('method') == filter_type:
                filtered_rows.append(r)
                continue
            # 再看 related_result.devices 中的状态
            for d in (r.get('related_result') or {}).get('devices', []):
                if d.get('status') == filter_type:
                    filtered_rows.append(r)
                    break

    # 分页切片
    total_rows = len(filtered_rows)
    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = filtered_rows[start:end]

    return render_template('equipment/import_step2.html',
                           filename=preview_data['filename'],
                           processed=page_rows,
                           statistics=preview_data['statistics'],
                           page=page,
                           per_page=per_page,
                           total_pages=total_pages,
                           total_rows=total_rows,
                           filter_type=filter_type)


@bp.route('/import/commit', methods=['POST'])
def import_commit():
    """步骤2确认提交：异步执行导入

    [REQ-014] 性能优化 + 实时进度反馈
    - 异步后台线程跑导入（避免阻塞 HTTP）
    - 整批事务原子（成功全提交 / 失败全回滚）
    - SSE 实时推送进度
    - 单价默认 0（不是 NULL）
    """
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录', 'redirect': url_for('auth.login')}), 401
    preview_data = session.get('equipment_import_preview')
    if not preview_data:
        return jsonify({'success': False, 'message': '会话过期，请重新上传'}), 400

    # [REQ-014-fix] 改用 JSON body（form 受 Flask max_form_memory_size 限制，大数据量 413）
    form_data = request.get_json(silent=True) or {}

    total = len(preview_data.get('processed', []))

    # 创建进度任务
    task_id = ip.create_task(total=total, user=session.get('user', ''))

    # 关键：路由内立即 pop session，避免重复提交
    session.pop('equipment_import_preview', None)
    # 防止 Flask session 大小超限（文件系统 session 不怕大，但保险起见）

    # 启动后台线程跑导入
    t = threading.Thread(
        target=_do_import_thread,
        args=(task_id, preview_data, form_data),
        daemon=True,
        name=f'import-{task_id[:8]}',
    )
    t.start()

    return jsonify({
        'success': True,
        'task_id': task_id,
        'total': total,
        'sse_url': f'/equipment/import/progress/{task_id}',
    })


def _do_import_thread(task_id: str, preview_data: dict, form_data: dict):
    """后台线程：实际执行导入（事务原子 + 进度更新 + 可取消）

    [REQ-014] 关键改进：
    - 整批 1 个 connection + 1 个事务（旧版 1000 行 = 3000 个连接/事务）
    - 失败时 ROLLBACK 全部回滚（旧版半成品）
    - 每条 update_progress，每 10 行检查 cancel_requested
    """
    processed = preview_data.get('processed', [])
    total = len(processed)

    imported_new = 0
    imported_overwrite = 0
    skipped = 0
    relations_created = 0
    skipped_relations = []

    # 写操作日志用
    operation_log = OperationLogModel()
    # [Bug fix 2026-06-25] 后台线程不能读 flask session（RuntimeError）
    # 从 progress 字典里读 user（路由里 ip.create_task 时已传入）
    _p = ip.get_progress(task_id)
    user = _p.get('user', 'unknown') if _p else 'unknown'

    conn = None
    i = 0
    current_name = ''
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('BEGIN IMMEDIATE')  # 写锁，避免并发冲突

        for i, row_data in enumerate(processed):
            # 检查取消
            if i > 0 and i % 10 == 0 and ip.is_cancel_requested(task_id):
                conn.rollback()
                conn.close()
                ip.mark_cancelled(task_id, saved_count=i)
                return

            row_idx = row_data['row_idx']
            current_name = ''

            # 解析单行 form
            name = form_data.get(f'name_{row_idx}', row_data.get('name', '')).strip()
            if not name:
                skipped += 1
                ip.update_progress(task_id, i + 1, '<跳过（无名称）>')
                continue

            model = form_data.get(f'model_{row_idx}', row_data.get('model', '')).strip()
            category = form_data.get(f'category_{row_idx}', row_data.get('category', '')).strip()
            if not category or category not in {'安全设备', '密码设备', '通用设备', '其他设备'}:
                category = '其他设备'
            form_val = form_data.get(f'form_{row_idx}', row_data.get('form', '')).strip()
            price_str = form_data.get(f'price_{row_idx}', row_data.get('price', '') or '').strip()
            # [REQ-014] 单价默认 0（不是 None / NULL）
            price = 0.0
            if price_str:
                try:
                    price = float(price_str)
                except (ValueError, TypeError):
                    price = 0.0
            tech_index = form_data.get(f'tech_index_{row_idx}', row_data.get('tech_index', '')).strip()
            tech_status = form_data.get(f'tech_status_{row_idx}', row_data.get('tech_status', '')).strip() or '货架产品'
            manufacturer = form_data.get(f'manufacturer_{row_idx}', row_data.get('manufacturer', '')).strip()
            main_purpose = form_data.get(f'main_purpose_{row_idx}', row_data.get('main_purpose', '')).strip()
            former_name = form_data.get(f'former_name_{row_idx}', row_data.get('former_name', '')).strip()
            resource_guarantee = form_data.get(f'resource_guarantee_{row_idx}', row_data.get('resource_guarantee', '')).strip()
            installation_requirements = form_data.get(f'installation_requirements_{row_idx}', row_data.get('installation_requirements', '')).strip()
            subclass = form_data.get(f'subclass_{row_idx}', '').strip()
            subclass_action = form_data.get(f'subclass_action_{row_idx}', '').strip()
            dup_action = form_data.get(f'dup_action_{row_idx}', '')

            # 处理新增子类确认
            if subclass_action == 'confirm' and subclass:
                sc_model = KnowledgeSubclassModel()
                # 用同一个 conn 而不是新开连接（事务原子性关键）
                try:
                    # 复制 KnowledgeSubclassModel.add 的逻辑但用现有 conn
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    c.execute('SELECT 1 FROM knowledge_subclasses WHERE parent_category = ? AND subclass_name = ? LIMIT 1',
                              (category, subclass))
                    if not c.fetchone():
                        c.execute('INSERT INTO knowledge_subclasses (parent_category, subclass_name, created_at) VALUES (?, ?, ?)',
                                  (category, subclass, now))
                except Exception:
                    pass  # 已存在则忽略

            current_name = name
            saved_eq_id = None

            # 插设备
            if row_data.get('duplicate_status') == 'exists':
                if dup_action == 'skip':
                    skipped += 1
                elif dup_action == 'overwrite':
                    eq_id = row_data['existing_equipment']['equipment_id']
                    # 直接 UPDATE，不用 model.update（避免新连接）
                    c.execute('''UPDATE equipment SET name=?, model=?, category=?, form=?, price=?,
                                  tech_index=?, tech_status=?, manufacturer=?, main_purpose=?,
                                  former_name=?, resource_guarantee=?, installation_requirements=?, subclass=?
                                  WHERE equipment_id=?''',
                              (name, model, category, form_val, price, tech_index, tech_status,
                               manufacturer, main_purpose, former_name, resource_guarantee,
                               installation_requirements, subclass, eq_id))
                    saved_eq_id = eq_id
                    imported_overwrite += 1
                else:  # 'new' 或空都视为新建
                    saved_eq_id = _insert_equipment(c, name, model, category, form_val, price,
                                                     tech_index, tech_status, manufacturer, main_purpose,
                                                     former_name, resource_guarantee, installation_requirements, subclass)
                    imported_new += 1
            else:
                saved_eq_id = _insert_equipment(c, name, model, category, form_val, price,
                                                 tech_index, tech_status, manufacturer, main_purpose,
                                                 former_name, resource_guarantee, installation_requirements, subclass)
                imported_new += 1

            # 关联关系
            related_result = row_data.get('related_result', {})
            for dev_idx, dev_match in enumerate(related_result.get('devices', [])):
                selected_id = form_data.get(f'related_select_{row_idx}_{dev_idx}', '').strip()
                if selected_id == '_skip_':
                    skipped_relations.append({'name': name, 'device': dev_match.get('name', '')})
                    continue
                if not selected_id:
                    if dev_match.get('selected'):
                        selected_id = dev_match['selected'].get('host_id')
                    elif dev_match.get('exact'):
                        selected_id = dev_match['exact'][0].get('host_id')
                    elif dev_match.get('fuzzy'):
                        selected_id = dev_match['fuzzy'][0].get('host_id')

                if selected_id and saved_eq_id:
                    try:
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        c.execute('''INSERT INTO device_host_relations (device_id, host_id, quantity, created_at, updated_at)
                                     VALUES (?, ?, 1, ?, ?)''',
                                  (saved_eq_id, selected_id, now, now))
                        relations_created += 1
                    except Exception:
                        # UNIQUE 冲突 → 跳过
                        pass

            # 推进度
            ip.update_progress(task_id, i + 1, current_name)

        # 全部成功 → 提交事务
        conn.commit()
        conn.close()

        # 写操作日志
        try:
            operation_log.add('equipment', 'batch_import',
                              f'导入 {total} 条 (新增 {imported_new}, 覆盖 {imported_overwrite}, 跳过 {skipped}, 关联 {relations_created})',
                              user, f'task_id={task_id}')
        except Exception:
            pass

        ip.mark_done(task_id, saved_new=imported_new, saved_overwrite=imported_overwrite,
                     skipped=skipped, relations_created=relations_created,
                     skipped_relations=skipped_relations)

        # 5 分钟后清理进度
        def _delayed_cleanup():
            time.sleep(300)
            ip.cleanup_task(task_id)
        threading.Thread(target=_delayed_cleanup, daemon=True).start()

    except Exception as e:
        # 失败 → 回滚
        if conn:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        failed_row = {'row_idx': i, 'name': current_name}
        ip.mark_failed(task_id, error=f'{type(e).__name__}: {e}', failed_row=failed_row)


def _insert_equipment(c, name, model, category, form, price, tech_index, tech_status,
                       manufacturer, main_purpose, former_name, resource_guarantee,
                       installation_requirements, subclass):
    """直接用传入 cursor 插入设备，返回 equipment_id（不 commit）"""
    equipment_id = 'EQP' + datetime.now().strftime('%Y%m%d%H%M%S%f')
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''INSERT INTO equipment
                  (equipment_id, name, model, category, form, price, tech_index, tech_status,
                   manufacturer, equipment_image, related_files, created_at,
                   main_purpose, former_name, resource_guarantee, installation_requirements, subclass)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (equipment_id, name, model, category, form, price, tech_index, tech_status,
               manufacturer, '', '', created_at, main_purpose, former_name,
               resource_guarantee, installation_requirements, subclass))
    return equipment_id


@bp.route('/import/progress/<task_id>')
def import_progress_stream(task_id):
    """SSE 实时推送导入进度

    客户端：new EventSource('/equipment/import/progress/<task_id>')
    事件格式：data: {json}\n\n
    """
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'}), 401

    def generate():
        # 最多推送 5 分钟（防止泄漏）
        max_iterations = 5 * 60 / 0.2  # 1500
        seen_status = None
        for _ in range(int(max_iterations)):
            p = ip.get_progress(task_id)
            if p is None:
                # 任务不存在或已清理
                yield f"data: {json.dumps({'status': 'not_found', 'error': '任务不存在或已清理'})}\n\n"
                break
            yield f"data: {json.dumps(p, ensure_ascii=False)}\n\n"
            if p['status'] in ('done', 'failed', 'cancelled'):
                seen_status = p['status']
                break
            time.sleep(0.2)
        # SSE 标准做法：done 后多发一个 comment 让客户端知道 stream 结束
        if seen_status:
            yield f"event: end\ndata: {json.dumps({'status': seen_status})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route('/import/cancel/<task_id>', methods=['POST'])
def import_cancel_task(task_id):
    """请求取消导入任务

    异步：标记 cancel_requested=True，后台线程每 10 行检查一次
    """
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'}), 401

    if ip.request_cancel(task_id):
        return jsonify({'success': True, 'message': '已发送取消请求'})
    else:
        p = ip.get_progress(task_id)
        if p is None:
            return jsonify({'success': False, 'message': '任务不存在'}), 404
        return jsonify({'success': False, 'message': f"任务状态 {p['status']}，无法取消"})


@bp.route('/import/done')
def import_done():
    """步骤3：导入结果页"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    import json
    new_count = int(request.args.get('new', 0))
    overwrite_count = int(request.args.get('overwrite', 0))
    skip_count = int(request.args.get('skip', 0))
    relations_count = int(request.args.get('relations', 0))
    try:
        skipped_relations = json.loads(request.args.get('skipped_relations', '[]'))
    except Exception:
        skipped_relations = []
    return render_template('equipment/import_done.html',
                           new_count=new_count,
                           overwrite_count=overwrite_count,
                           skip_count=skip_count,
                           relations_count=relations_count,
                           skipped_relations=skipped_relations)


# 旧的 CSV 导入保留（兼容）
@bp.route('/import/csv', methods=['POST'])
def import_csv():
    """旧的 CSV 导入（兼容现有调用）"""
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登录'})
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '请选择文件'})
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '请选择文件'})
    if not file.filename.endswith('.csv'):
        return jsonify({'success': False, 'message': '请上传CSV文件'})
    try:
        content = file.read().decode('utf-8')
        reader = csv.DictReader(io.StringIO(content))
        equipment_model = EquipmentModel()
        count = 0
        for row in reader:
            equipment_model.add(
                name=row.get('设备名称', ''),
                model=row.get('型号', ''),
                category=row.get('分类', '通用设备'),
                form=row.get('形态', ''),
                price=row.get('单价', ''),
                tech_index=row.get('功能技术指标', ''),
                status=row.get('技术状态', '货架产品'),
                manufacturer=row.get('研制单位', ''),
                main_purpose=row.get('主要用途', ''),
                former_name=row.get('曾用名', ''),
                resource_guarantee=row.get('资源保障要求', '')
            )
            count += 1
        return jsonify({'success': True, 'message': f'成功导入 {count} 条记录'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'导入失败: {str(e)}'})

@bp.route('/logs/<module_name>')
def logs(module_name):
    """操作日志页面"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    # 获取筛选参数
    operator = request.args.get('operator')
    operation_type = request.args.get('operation_type')
    file_name = request.args.get('file_name')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    log_model = OperationLogModel()

    logs = log_model.search(
        module=module_name,
        operation_type=operation_type,
        operator=operator,
        file_name=file_name,
        start_date=start_date,
        end_date=end_date,
        limit=100)

    module_names = {
        'equipment': '设备知识库',
        'standards': '标准法规库',
        'templates': '科研模板'
    }

    return render_template('equipment/logs.html',
                          logs=logs,
                          module_name=module_name,
                          module_title=module_names.get(module_name, '日志'))


# ---------- 密码设备关联宿主设备 API ----------

@bp.route('/api/hosts/<device_id>', methods=['GET'])
def api_hosts_by_device(device_id):
    """获取密码设备关联的宿主设备列表"""
    if 'user' not in session:
        return jsonify({'success': False})
    rel_model = DeviceHostRelationModel()
    hosts = rel_model.get_hosts_by_device(device_id)
    return jsonify({'success': True, 'data': hosts})


@bp.route('/api/hosts', methods=['POST'])
def api_add_host_relation():
    """从密码设备侧添加宿主设备关联（无数量概念）"""
    if 'user' not in session:
        return jsonify({'success': False})
    data = request.get_json() or {}
    device_id = data.get('device_id')
    host_id = data.get('host_id')
    if not device_id or not host_id:
        return jsonify({'success': False, 'message': '缺少参数'})
    rel_model = DeviceHostRelationModel()
    rel_model.add_relation(device_id, host_id, quantity=1)
    return jsonify({'success': True})


@bp.route('/api/hosts/<device_id>/<host_id>', methods=['DELETE'])
def api_remove_host_relation(device_id, host_id):
    """从密码设备侧移除宿主设备关联"""
    if 'user' not in session:
        return jsonify({'success': False})
    rel_model = DeviceHostRelationModel()
    rel_model.remove_relation(device_id, host_id)
    return jsonify({'success': True})
