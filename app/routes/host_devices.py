# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from app.models import HostDeviceModel, HostDeviceCategoryModel, DeviceHostRelationModel, EquipmentModel

bp = Blueprint('host_devices', __name__, url_prefix='/equipment/hosts')


def _check_login():
    if 'user' not in session:
        return False
    return True


@bp.route('/')
def index():
    if not _check_login():
        return redirect(url_for('auth.login'))

    host_model = HostDeviceModel()
    category_model = HostDeviceCategoryModel()
    rel_model = DeviceHostRelationModel()

    category_filter = request.args.get('category', '')
    form_filter = request.args.get('form', '')
    keyword = request.args.get('keyword', '')
    page = int(request.args.get('page', 1))
    per_page = 20

    all_hosts, total = host_model.get_all(
        category=category_filter if category_filter else None,
        form=form_filter if form_filter else None,
        keyword=keyword if keyword else None,
        page=page, per_page=per_page
    )

    # 补充关联设备数量和摘要
    for h in all_hosts:
        count = rel_model.get_device_count_by_host(h['host_id'])
        h['_device_count'] = count
        # 获取关联设备名称摘要
        devices = rel_model.get_devices_by_host(h['host_id'])
        if not devices:
            h['_device_summary'] = '无'
        elif len(devices) == 1:
            h['_device_summary'] = devices[0]['name']
        elif len(devices) == 2:
            h['_device_summary'] = f"{devices[0]['name']}、{devices[1]['name']}"
        else:
            h['_device_summary'] = f"{devices[0]['name']}、{devices[1]['name']}等{len(devices)}台"

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    return render_template('host_devices/index.html',
                         hosts=all_hosts,
                         categories=category_model.get_all(),
                         category_filter=category_filter,
                         form_filter=form_filter,
                         keyword=keyword,
                         page=page,
                         total_pages=total_pages,
                         total=total)


@bp.route('/add', methods=['GET', 'POST'])
def add():
    if not _check_login():
        return redirect(url_for('auth.login'))

    category_model = HostDeviceCategoryModel()
    rel_model = DeviceHostRelationModel()
    equipment_model = EquipmentModel()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        model = request.form.get('model', '').strip()
        category = request.form.get('category', '').strip()
        form = request.form.get('form', '').strip()

        if not name:
            flash('名称不能为空', 'error')
            return render_template('host_devices/add.html',
                                 categories=category_model.get_all(),
                                 selected_relations=[])

        # 处理新增类型（需要先处理，因为要用最终类型名称保存宿主设备）
        new_category = request.form.get('new_category', '').strip()
        if new_category:
            existing = category_model.get_by_name(new_category)
            if not existing:
                category_model.add(new_category)
            category = new_category

        host_model = HostDeviceModel()
        host_id = host_model.add(name, model, category, form)

        # 保存关联关系
        device_ids = request.form.getlist('device_id')
        quantities = request.form.getlist('quantity')
        for device_id, qty in zip(device_ids, quantities):
            if device_id:
                rel_model.add_relation(device_id, host_id, max(1, int(qty or 1)))

        flash('宿主设备添加成功', 'success')
        return redirect(url_for('host_devices.index'))

    return render_template('host_devices/add.html',
                         categories=category_model.get_all(),
                         selected_relations=[])


@bp.route('/edit/<host_id>', methods=['GET', 'POST'])
def edit(host_id):
    if not _check_login():
        return redirect(url_for('auth.login'))

    host_model = HostDeviceModel()
    category_model = HostDeviceCategoryModel()
    rel_model = DeviceHostRelationModel()

    host = host_model.get_by_id(host_id)
    if not host:
        flash('宿主设备不存在', 'error')
        return redirect(url_for('host_devices.index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        model = request.form.get('model', '').strip()
        category = request.form.get('category', '').strip()
        form = request.form.get('form', '').strip()

        if not name:
            flash('名称不能为空', 'error')
            return render_template('host_devices/edit.html',
                                 host=host,
                                 categories=category_model.get_all(),
                                 selected_relations=rel_model.get_devices_by_host(host_id))

        host_model.update(host_id, name=name, model=model, category=category, form=form)

        # 重建关联关系：先删后加
        rel_model.delete_by_host(host_id)
        device_ids = request.form.getlist('device_id')
        quantities = request.form.getlist('quantity')
        for device_id, qty in zip(device_ids, quantities):
            if device_id:
                rel_model.add_relation(device_id, host_id, max(1, int(qty or 1)))

        # 处理新增类型
        new_category = request.form.get('new_category', '').strip()
        if new_category and not category_model.get_by_name(new_category):
            category_model.add(new_category)

        flash('宿主设备更新成功', 'success')
        return redirect(url_for('host_devices.detail', host_id=host_id))

    selected_relations = rel_model.get_devices_by_host(host_id)
    return render_template('host_devices/edit.html',
                         host=host,
                         categories=category_model.get_all(),
                         selected_relations=selected_relations)


@bp.route('/detail/<host_id>')
def detail(host_id):
    if not _check_login():
        return redirect(url_for('auth.login'))

    host_model = HostDeviceModel()
    rel_model = DeviceHostRelationModel()

    host = host_model.get_by_id(host_id)
    if not host:
        flash('宿主设备不存在', 'error')
        return redirect(url_for('host_devices.index'))

    related_devices = rel_model.get_devices_by_host(host_id)
    return render_template('host_devices/detail.html',
                         host=host,
                         related_devices=related_devices)


@bp.route('/delete/<host_id>', methods=['POST'])
def delete(host_id):
    if not _check_login():
        return redirect(url_for('auth.login'))
    host_model = HostDeviceModel()
    host_model.delete(host_id)
    flash('宿主设备已删除', 'success')
    return redirect(url_for('host_devices.index'))


@bp.route('/categories')
def categories():
    if not _check_login():
        return redirect(url_for('auth.login'))
    category_model = HostDeviceCategoryModel()
    host_model = HostDeviceModel()
    all_categories = category_model.get_all()

    # 统计每个类型的设备数量
    for cat in all_categories:
        hosts, _ = host_model.get_all(category=cat['name'], per_page=1000)
        cat['_device_count'] = len(hosts)

    return render_template('host_devices/categories.html',
                         categories=all_categories)


@bp.route('/categories/add', methods=['POST'])
def categories_add():
    if not _check_login():
        return jsonify({'success': False, 'message': '未登录'})
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'message': '类型名称不能为空'})
    category_model = HostDeviceCategoryModel()
    existing = category_model.get_by_name(name)
    if existing:
        return jsonify({'success': False, 'message': '类型已存在'})
    category_model.add(name)
    return jsonify({'success': True})


@bp.route('/categories/merge', methods=['POST'])
def categories_merge():
    if not _check_login():
        return jsonify({'success': False, 'message': '未登录'})
    old_name = request.form.get('old_name', '').strip()
    new_name = request.form.get('new_name', '').strip()
    if not old_name or not new_name:
        return jsonify({'success': False, 'message': '参数不完整'})
    if old_name == new_name:
        return jsonify({'success': False, 'message': '不能合并到自身'})
    category_model = HostDeviceCategoryModel()
    category_model.rename(old_name, new_name)
    return jsonify({'success': True})


@bp.route('/categories/delete', methods=['POST'])
def categories_delete():
    if not _check_login():
        return jsonify({'success': False, 'message': '未登录'})
    name = request.form.get('name', '').strip()
    category_model = HostDeviceCategoryModel()
    result = category_model.delete(name)
    if not result:
        return jsonify({'success': False, 'message': '该类型有关联设备，无法删除'})
    return jsonify({'success': True})


# ---------- API ----------

@bp.route('/api/relations/<host_id>')
def api_relations(host_id):
    """获取宿主设备关联的密码设备列表（含数量）"""
    if not _check_login():
        return jsonify({'success': False})
    rel_model = DeviceHostRelationModel()
    devices = rel_model.get_devices_by_host(host_id)
    return jsonify({'success': True, 'data': devices})


@bp.route('/api/relations', methods=['POST'])
def api_relations_add():
    """批量添加关联"""
    if not _check_login():
        return jsonify({'success': False})
    data = request.get_json() or {}
    host_id = data.get('host_id')
    relations = data.get('relations', [])  # [{device_id, quantity}, ...]
    if not host_id:
        return jsonify({'success': False, 'message': 'host_id required'})
    rel_model = DeviceHostRelationModel()
    for r in relations:
        rel_model.add_relation(r['device_id'], host_id, r.get('quantity', 1))
    return jsonify({'success': True})


@bp.route('/api/relations/<device_id>/<host_id>', methods=['PATCH'])
def api_relation_patch(device_id, host_id):
    """修改关联数量"""
    if not _check_login():
        return jsonify({'success': False})
    data = request.get_json() or {}
    quantity = data.get('quantity', 1)
    rel_model = DeviceHostRelationModel()
    rel_model.update_quantity(device_id, host_id, quantity)
    return jsonify({'success': True})


@bp.route('/api/relations/<device_id>/<host_id>', methods=['DELETE'])
def api_relation_delete(device_id, host_id):
    """移除关联"""
    if not _check_login():
        return jsonify({'success': False})
    rel_model = DeviceHostRelationModel()
    rel_model.remove_relation(device_id, host_id)
    return jsonify({'success': True})


@bp.route('/api/search')
def api_search():
    """搜索宿主设备（用于关联选择弹框中过滤已选）"""
    if not _check_login():
        return jsonify({'success': False})
    keyword = request.args.get('keyword', '')
    page = int(request.args.get('page', 1))
    per_page = 20
    host_model = HostDeviceModel()
    hosts, total = host_model.get_all(keyword=keyword, page=page, per_page=per_page)
    return jsonify({'success': True, 'data': hosts, 'total': total, 'page': page})


# ---------- 导入导出 ----------

@bp.route('/export')
def export():
    """导出全部宿主设备为 Excel"""
    if not _check_login():
        return redirect(url_for('auth.login'))

    from io import BytesIO
    import openpyxl

    host_model = HostDeviceModel()
    rel_model = DeviceHostRelationModel()

    all_hosts, _ = host_model.get_all(per_page=10000)

    output = BytesIO()
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = '宿主设备'

    # 表头（增强：增加关联设备ID列）
    ws.append(['宿主设备编号', '名称', '型号', '类型', '形态', '关联设备数',
               '关联设备ID', '关联设备名称', '创建时间', '更新时间'])

    for h in all_hosts:
        device_count = rel_model.get_device_count_by_host(h['host_id'])
        devices = rel_model.get_devices_by_host(h['host_id'])
        device_ids = '、'.join([d['device_id'] for d in devices]) if devices else ''
        device_names = '、'.join([d['name'] for d in devices]) if devices else ''
        ws.append([
            h['host_id'],
            h['name'],
            h['model'] or '',
            h['category'] or '',
            h['form'] or '',
            device_count,
            device_ids,
            device_names,
            h['created_at'] or '',
            h['updated_at'] or '',
        ])

    # 自动列宽
    for col in ws.columns:
        max_len = 0
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except:
                pass
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    workbook.save(output)
    output.seek(0)
    from flask import send_file
    return send_file(output, download_name='宿主设备库.xlsx',
                     as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route('/import', methods=['GET'])
def import_page():
    """步骤1：导入上传页"""
    if not _check_login():
        return redirect(url_for('auth.login'))
    return render_template('host_devices/import_step1.html')


@bp.route('/import/preview', methods=['POST'])
def import_preview():
    """
    步骤1提交：解析文件 + 列映射 + 执行模糊匹配
    返回 JSON，前端收到后跳转到预览页

    支持两种模式：
    - preview_only=1: 只返回表头+前几行数据（用于列映射页面预览）
    - 正常模式: 完整解析+模糊匹配，存入 session，重定向到预览页
    """
    if not _check_login():
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

        # 读取所有行
        all_rows = []
        for row in ws.iter_rows(values_only=True):
            all_rows.append(list(row))

        if len(all_rows) < 2:
            return jsonify({'success': False, 'message': '文件数据少于2行'})

        header = [str(h).strip() if h is not None else '' for h in all_rows[0]]
        data_rows = all_rows[1:]
        preview_rows = data_rows[:10]

        # preview_only 模式：只返回预览数据
        if preview_only:
            return jsonify({
                'success': True,
                'headers': header,
                'preview_rows': [[str(c) if c is not None else '' for c in row] for row in preview_rows],
            })

        # 正常模式：完整解析 + 模糊匹配
        try:
            column_mapping = json.loads(request.form.get('column_mapping_json', '{}'))
        except Exception:
            return jsonify({'success': False, 'message': '列映射参数错误'})

        from app.utils.fuzzy_match import match_equipment
        host_model = HostDeviceModel()
        category_model = HostDeviceCategoryModel()

        related_col_idx = column_mapping.get('related_equipment')

        processed = []
        exact_count = 0
        fuzzy_count = 0
        unmatched_count = 0
        duplicate_count = 0

        for row_idx, row in enumerate(data_rows):
            if not row or not any(v for v in row if v is not None):
                continue

            name = _get_row_value(row, column_mapping, 'name')
            model = _get_row_value(row, column_mapping, 'model')
            category = _get_row_value(row, column_mapping, 'category')
            form = _get_row_value(row, column_mapping, 'form')
            host_id_raw = _get_row_value(row, column_mapping, 'host_id')

            if not name:
                continue

            # 重复检测
            duplicate_status = None
            existing_host = None
            if host_id_raw and str(host_id_raw).startswith('HD'):
                existing_host = host_model.get_by_id(str(host_id_raw))
                if existing_host:
                    duplicate_status = 'exists'

            # 关联设备匹配
            related_result = {'original': '', 'devices': []}
            if related_col_idx is not None and related_col_idx < len(row):
                related_text = str(row[related_col_idx] or '').strip()
                related_result = match_equipment(related_text)

                for dev in related_result['devices']:
                    if dev['status'] == 'exact':
                        exact_count += 1
                    elif dev['status'] == 'fuzzy':
                        fuzzy_count += 1
                    else:
                        unmatched_count += 1

            # 新增类型检测
            is_new_category = False
            if category:
                existing_cats = [c['name'] for c in category_model.get_all()]
                if category not in existing_cats:
                    is_new_category = True

            processed.append({
                'row_idx': row_idx,
                'host_id_raw': host_id_raw,
                'name': name,
                'model': model,
                'category': category,
                'form': form,
                'is_new_category': is_new_category,
                'duplicate_status': duplicate_status,
                'existing_host': dict(existing_host) if existing_host else None,
                'related_result': related_result,
            })

            if duplicate_status == 'exists':
                duplicate_count += 1

        statistics = {
            'total': len(processed),
            'exact': exact_count,
            'fuzzy': fuzzy_count,
            'unmatched': unmatched_count,
            'duplicate': duplicate_count,
        }

        # 存入 session
        session['host_import_preview'] = {
            'filename': file.filename,
            'column_mapping': column_mapping,
            'processed': processed,
            'statistics': statistics,
        }

        return jsonify({'success': True, 'redirect': url_for('host_devices.import_preview_page')})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'解析失败: {str(e)}'})


def _get_row_value(row, column_mapping, field):
    """根据列映射获取某行某字段的值"""
    col_idx = column_mapping.get(field)
    if col_idx is None or col_idx >= len(row):
        return ''
    val = row[col_idx]
    return str(val).strip() if val is not None else ''


@bp.route('/import/preview', methods=['GET'])
def import_preview_page():
    """步骤2：匹配预览页面"""
    if not _check_login():
        return redirect(url_for('auth.login'))

    preview_data = session.get('host_import_preview')
    if not preview_data:
        flash('请先上传文件', 'error')
        return redirect(url_for('host_devices.import_page'))

    return render_template('host_devices/import_step2.html',
                           filename=preview_data['filename'],
                           processed=preview_data['processed'],
                           statistics=preview_data['statistics'])


@bp.route('/import/commit', methods=['POST'])
def import_commit():
    """步骤2确认提交：执行导入"""
    if not _check_login():
        return redirect(url_for('auth.login'))

    preview_data = session.get('host_import_preview')
    if not preview_data:
        return jsonify({'success': False, 'message': '会话过期，请重新上传'})

    host_model = HostDeviceModel()
    category_model = HostDeviceCategoryModel()
    rel_model = DeviceHostRelationModel()

    imported_new = 0
    imported_overwrite = 0
    skipped = 0
    relations_created = 0
    skipped_relations = []

    from app.utils.fuzzy_match import split_device_names
    import json

    # 遍历每行，按用户修正执行写入
    for row_data in preview_data['processed']:
        row_idx = row_data['row_idx']

        # 读取用户修正后的值
        name = request.form.get(f'name_{row_idx}', row_data['name']).strip()
        model = request.form.get(f'model_{row_idx}', row_data['model']).strip()
        category = request.form.get(f'category_{row_idx}', row_data['category']).strip()
        form = request.form.get(f'form_{row_idx}', row_data['form']).strip()
        host_id_raw = request.form.get(f'host_id_{row_idx}', row_data['host_id_raw']).strip()

        # 重复处理选择
        dup_action = request.form.get(f'dup_action_{row_idx}', '')

        if not name:
            skipped += 1
            continue

        # 处理新增类型
        if category:
            existing_cats = [c['name'] for c in category_model.get_all()]
            if category not in existing_cats:
                category_model.add(category)

        # 写入 host_devices
        saved_host_id = None
        if row_data['duplicate_status'] == 'exists':
            if dup_action == 'skip':
                skipped += 1
            elif dup_action == 'overwrite':
                host_model.update(row_data['existing_host']['host_id'],
                                  name=name, model=model, category=category, form=form)
                saved_host_id = row_data['existing_host']['host_id']
                imported_overwrite += 1
            elif dup_action == 'new':
                saved_host_id = host_model.add(name, model, category, form)
                imported_new += 1
            else:
                # 默认新建
                saved_host_id = host_model.add(name, model, category, form)
                imported_new += 1
        else:
            saved_host_id = host_model.add(name, model, category, form)
            imported_new += 1

        # 处理关联关系
        related_result = row_data['related_result']
        for dev_idx, dev_match in enumerate(related_result.get('devices', [])):
            # 用户选择的设备ID
            selected_id = request.form.get(f'related_select_{row_idx}_{dev_idx}', '').strip()
            if selected_id == '_skip_':
                # 用户跳过该关联
                skipped_relations.append({
                    'name': row_data['name'],
                    'device': dev_match.get('name', '')
                })
                continue
            if not selected_id:
                # 未选择，尝试用自动匹配的
                if dev_match.get('selected'):
                    selected_id = dev_match['selected'].get('equipment_id')
                elif dev_match.get('exact'):
                    selected_id = dev_match['exact'][0].get('equipment_id')
                elif dev_match.get('fuzzy'):
                    selected_id = dev_match['fuzzy'][0].get('equipment_id')

            if selected_id and saved_host_id:
                try:
                    rel_model.add_relation(selected_id, saved_host_id, 1)
                    relations_created += 1
                except Exception:
                    pass

    # 清除 session
    session.pop('host_import_preview', None)

    # 跳转到结果页
    return redirect(url_for('host_devices.import_done',
                            new=imported_new,
                            overwrite=imported_overwrite,
                            skip=skipped,
                            relations=relations_created,
                            skipped_relations=json.dumps(skipped_relations)))


@bp.route('/import/done')
def import_done():
    """步骤3：导入结果页"""
    if not _check_login():
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

    return render_template('host_devices/import_done.html',
                           new_count=new_count,
                           overwrite_count=overwrite_count,
                           skip_count=skip_count,
                           relations_count=relations_count,
                           skipped_relations=skipped_relations)
