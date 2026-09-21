# -*- coding: utf-8 -*-
"""报销单据模板路由：模板管理、单据填充及汇总打印。"""
import os
import io
import uuid as _uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime
from flask import Blueprint, current_app, render_template, request, jsonify, session, send_file, redirect, url_for
from werkzeug.exceptions import HTTPException

from app.expense_db import (
    get_reimbursement_by_id,
    get_reimbursement_documents,
    add_document_to_reimbursement,
    update_document_in_reimbursement,
    delete_document_from_reimbursement,
    get_document_templates,
    get_document_template,
    get_reimbursement_children,
)
from app.document_engine import DocumentFiller, _cn_number


def _fill_chuchai_template(tmpl_docx_bytes, context):
    """
    填充出差完整报销单据模板.docx（合并三合一模板）。
    直接操作 XML，确保复杂合并单元格替换正确。
    context: _build_report_context() 返回的字典。
    返回填充后的 docx 字节串。
    """
    import zipfile
    from lxml import etree

    NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

    def q(tag): return f'{{{NS}}}{tag}'

    # 解析 docx
    doc_zip = zipfile.ZipFile(io.BytesIO(tmpl_docx_bytes))
    doc_xml = doc_zip.read('word/document.xml')
    doc_tree = etree.fromstring(doc_xml)

    tables = doc_tree.findall(f'.//{q("tbl")}')
    if not tables:
        raise ValueError("模板中没有表格")

    def get_cell_text(cell):
        """获取单元格所有文本"""
        return ''.join(t.text or '' for t in cell.findall(f'.//{q("t")}'))

    def set_cell_text(cell, text):
        """设置单元格文本（清空所有run，创建新文本）"""
        # 移除所有现有的 t 元素
        for t in cell.findall(f'.//{q("t")}'):
            if t.getparent() is not None:
                t.getparent().remove(t)
        # 在第一个 paragraph 的第一个 run 中设置文本
        paras = cell.findall(f'{q("p")}')
        if not paras:
            return
        para = paras[0]
        # 找所有 r 元素
        runs = para.findall(f'{q("r")}')
        if runs:
            # 保留第一个run的样式
            first_run = runs[0]
            # 移除其余runs
            for r in runs[1:]:
                r.getparent().remove(r)
            # 清除第一个run的文本
            for t in first_run.findall(f'{q("t")}'):
                if t.getparent() is not None:
                    t.getparent().remove(t)
            # 创建新文本
            t_elem = etree.SubElement(first_run, q('t'))
            t_elem.text = text
        else:
            # 创建一个新 run
            r = etree.SubElement(para, q('r'))
            t_elem = etree.SubElement(r, q('t'))
            t_elem.text = text

    def replace_cell_text_full(cell, old_text, new_text):
        """
        在单元格中替换精确文本。
        处理文本跨多个 <w:t> 元素的合并单元格情况：
        将段落完整文本中的 old_text 替换为 new_text，
        然后重写所有受影响 run 的 <w:t> 元素。
        """
        changed = False
        for para in cell.findall(f'{q("p")}'):
            # 拼接段落所有 <w:t> 文本
            full_text = ''.join(t.text or '' for t in para.findall(f'.//{q("t")}'))
            if old_text not in full_text:
                continue
            new_full_text = full_text.replace(old_text, new_text, 1)
            # 保留段落属性(pPr)，清除所有 runs，重建一个包含完整新文本的 run
            # 清除所有 runs
            for r in list(para.findall(f'{q("r")}')):
                para.remove(r)
            # 重建一个 run，包含完整的文本
            new_r = etree.Element(q('r'))
            new_t = etree.SubElement(new_r, q('t'))
            new_t.text = new_full_text
            new_t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            para.append(new_r)
            changed = True
        return changed

    # ---- 提取数据 ----
    invoices = context.get('invoices', [])
    reimb = context.get('reimb', {})
    total = context.get('total_invoice', 0)
    arrival = context.get('arrival', '') or '宁波'
    earliest = context.get('earliest_date', '')  # 2026-04-16
    latest = context.get('latest_date', '')

    # ---- Table 2 (index=2): 因公出差审批单 ----
    if len(tables) >= 3:
        tbl2 = tables[2]
        for row in tbl2.findall(q('tr')):
            cells = row.findall(q('tc'))
            row_texts = [get_cell_text(c) for c in cells]
            row_str = ' '.join(row_texts)

            if '到达单位' in row_str or '到达地点' in row_str:
                # 到达地点在第2个单元格（index 1）
                if len(cells) >= 2:
                    set_cell_text(cells[1], arrival)
            elif '起止时间' in row_str:
                date_range = earliest
                if earliest and latest and earliest != latest:
                    date_range = f'{earliest} 至 {latest}'
                if len(cells) >= 2:
                    set_cell_text(cells[1], date_range)
            elif '出差事由' in row_str:
                title = reimb.get('title', '') or '出差报销'
                if len(cells) >= 2:
                    set_cell_text(cells[1], title)

    # ---- Table 0 (index=0): 差旅费报销凭证 ----
    if len(tables) >= 1:
        tbl0 = tables[0]
        for row in tbl0.findall(q('tr')):
            cells = row.findall(q('tc'))
            if not cells:
                continue
            row_texts = [get_cell_text(c) for c in cells]
            row_str = ' '.join(row_texts)

            # 机票行：精确替换 5710.0 → 总额，4 → 张数
            if '5710.0' in row_str and '飞' in row_str:
                # 找到所有含 5710.0 的单元格并替换（合并单元格可能跨多列）
                for ci, cell in enumerate(cells):
                    cell_text = get_cell_text(cell)
                    if '5710.0' in cell_text:
                        replace_cell_text_full(cell, '5710.0', f'{total:.2f}')
                # 张数：找到含"4"的单元格替换为实际张数
                for ci, cell in enumerate(cells):
                    cell_text = get_cell_text(cell)
                    if cell_text == '4':
                        replace_cell_text_full(cell, '4', str(len(invoices)))
                        break
            # 订退改签行：清零
            elif '250.0' in row_str:
                replace_cell_text_full(cells[min(5, len(cells)-1)], '250.0', '')
                # 清除张数 "2"
                for ci, cell in enumerate(cells):
                    if replace_cell_text_full(cell, '2', ''):
                        pass
            # 市内交通行：保持原样
            elif '535.9' in row_str:
                pass  # 保持不变

            # 申报金额总计行（Row 12）：用 set_cell_text 直接写入
            if '申报金额' in row_str and ('￥' in row_str or '（' in row_str):
                for ci, cell in enumerate(cells):
                    cell_text = get_cell_text(cell)
                    if '（' in cell_text:
                        set_cell_text(cell, f'（￥：{total:.2f}）')
                        break

    # ---- Table 1 (index=1): 伙食补助费申报表 ----
    if len(tables) >= 2:
        tbl1 = tables[1]
        date_range = earliest
        if earliest and latest and earliest != latest:
            date_range = f'{earliest} 至 {latest}'
        for row in tbl1.findall(q('tr')):
            cells = row.findall(q('tc'))
            for ci, cell in enumerate(cells):
                txt = get_cell_text(cell)
                if '时间' in txt and '年' in txt:
                    for t in cell.findall(f'.//{q("t")}'):
                        if t.text and '时间' not in t.text and '年' not in t.text:
                            t.text = date_range

    # 写回 docx
    new_docxml = etree.tostring(doc_tree, xml_declaration=True, encoding='UTF-8', standalone=True)
    buf = io.BytesIO()
    out_zip = zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED)
    for item in doc_zip.namelist():
        if item == 'word/document.xml':
            out_zip.writestr(item, new_docxml)
        else:
            out_zip.writestr(item, doc_zip.read(item))
    out_zip.close()
    return buf.getvalue()

bp = Blueprint('documents', __name__, url_prefix='/expense/documents')


@bp.errorhandler(Exception)
def _documents_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    return _safe_error(exc)


def _safe_error(exc, fallback="操作失败"):
    from app.services.expenses import ExpenseError
    if isinstance(exc, ExpenseError):
        return jsonify({'success': False, 'error': exc.message, 'code': exc.code}), exc.status_code
    import logging
    logging.exception("[Documents] %s", fallback)
    return jsonify({'success': False, 'error': fallback}), 500

# ----------------------------------------------------------
# 模板目录（相对于 app/）
# ----------------------------------------------------------
_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(_app_dir, 'document_templates')


# ----------------------------------------------------------
# 页面路由
# ----------------------------------------------------------

@bp.route('/')
def index():
    """单据管理入口页"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/documents.html')


@bp.route('/new/<int:rid>')
def new_document(rid):
    """新建单据页"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    reimbursement = get_reimbursement_by_id(rid)
    if not reimbursement:
        return "报销项不存在", 404
    return render_template('expense/document_new.html', rid=rid, reimbursement=reimbursement)


@bp.route('/templates')
def template_list():
    """模板管理页（管理员查看模板列表）"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/document_templates.html')


@bp.route('/print/<int:rid>')
def print_page(rid):
    """汇总打印页"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    reimbursement = get_reimbursement_by_id(rid)
    if not reimbursement:
        return "报销项不存在", 404
    return render_template('expense/document_print.html', rid=rid, reimbursement=reimbursement)


# ----------------------------------------------------------
# API：模板
# ----------------------------------------------------------

@bp.route('/api/templates')
def api_templates():
    """获取所有模板列表"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    templates = get_document_templates()
    # 不要暴露完整 fields 列表（太长）
    brief = [{'template_id': t['template_id'], 'doc_type': t['doc_type'], 'version': t.get('version', '1.0.0')} for t in templates]
    return jsonify({'success': True, 'templates': brief})


# ----------------------------------------------------------
# API：报销项（供单据页面使用）
# ----------------------------------------------------------

@bp.route('/api/reimbursements', methods=['GET'])
def api_reimbursements():
    """获取报销项列表（简化版，含单据计数）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        records, total, next_offset = current_app.extensions['expense_service'].page_reimbursements(
            limit=limit, offset=offset,
        )
        for r in records:
            docs = get_reimbursement_documents(r['id'])
            r['documents_count'] = len(docs)
        return jsonify({'success': True, 'reimbursements': records, 'total': total, 'next_offset': next_offset})
    except Exception as e:
        import logging
        logging.error(f"[Documents] 获取报销项失败: {e}")
        return _safe_error(e, "获取报销项失败")


@bp.route('/api/reimbursements/<int:rid>', methods=['GET'])
def api_reimbursement_get(rid):
    """获取报销项详情（供单据页面使用）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404
        return jsonify({'success': True, 'reimbursement': reimbursement})
    except Exception as e:
        import logging
        logging.error(f"[Documents] 获取报销项详情失败: {e}")
        return _safe_error(e, "获取报销项详情失败")


# ----------------------------------------------------------
# API：单据 CRUD
# ----------------------------------------------------------

@bp.route('/api/templates/<doc_type>')
def api_template(doc_type):
    """获取某模板的完整定义"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    meta = get_document_template(doc_type)
    if not meta:
        return jsonify({'success': False, 'error': '模板不存在'}), 404
    return jsonify({'success': True, 'template': meta})


@bp.route('/api/templates/<doc_type>/preview')
def api_template_preview(doc_type):
    """获取模板表格预览（字段位置）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        filler = DocumentFiller(templates_dir=TEMPLATE_DIR)
        preview = filler.get_preview(doc_type)
        return jsonify({'success': True, 'preview': preview})
    except Exception as e:
        return _safe_error(e, "模板预览失败")


# ----------------------------------------------------------
# API：单据 CRUD
# ----------------------------------------------------------

@bp.route('/api/reimbursements/<int:rid>/documents')
def api_get_documents(rid):
    """获取报销项下的所有单据"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        docs, total, next_offset = current_app.extensions['expense_service'].page_documents(
            rid, limit=limit, offset=offset,
        )
        return jsonify({'success': True, 'documents': docs, 'total': total, 'next_offset': next_offset})
    except Exception as exc:
        return _safe_error(exc, "获取单据失败")


@bp.route('/api/reimbursements/<int:rid>/documents', methods=['POST'])
def api_add_document(rid):
    """添加单据到报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    data = request.get_json() or {}
    doc = {
        'id': _uuid.uuid4().hex,
        'template_id': data.get('template_id', ''),
        'doc_type': data.get('doc_type', ''),
        'filled_by': session.get('name', ''),
        'filled_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'fields': data.get('fields', {}),
    }
    docs = add_document_to_reimbursement(rid, doc)
    return jsonify({'success': True, 'documents': docs, 'added': doc})


@bp.route('/api/reimbursements/<int:rid>/documents/<doc_id>', methods=['PUT'])
def api_update_document(rid, doc_id):
    """更新某单据"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    data = request.get_json() or {}
    docs = update_document_in_reimbursement(rid, doc_id, data)
    return jsonify({'success': True, 'documents': docs})


@bp.route('/api/reimbursements/<int:rid>/documents/<doc_id>', methods=['DELETE'])
def api_delete_document(rid, doc_id):
    """删除某单据"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    docs = delete_document_from_reimbursement(rid, doc_id)
    return jsonify({'success': True, 'documents': docs})


# ----------------------------------------------------------
# API：自动填充
# ----------------------------------------------------------

@bp.route('/api/reimbursements/<int:rid>/documents/auto_fill', methods=['POST'])
def api_auto_fill(rid):
    """
    自动填充建议
    POST body: {doc_type: "差旅费报销凭证", fields: {field_id: value, ...}}
    返回: {suggestions: {field_id: {value, source, locked}}, available_invoices, available_payments}
    """
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    data = request.get_json() or {}
    doc_type = data.get('doc_type', '')
    user_fields = data.get('fields', {})

    # 获取模板定义
    meta = get_document_template(doc_type)
    if not meta:
        return jsonify({'success': False, 'error': '模板不存在'}), 404

    # 获取当前报销项的发票和支付记录
    invoices, payments = get_reimbursement_children(rid)

    # 构建 session context
    session_user = {'name': session.get('name', ''), 'dept': session.get('dept', ''), 'title': session.get('title', '')}
    context = {
        'session': {
            'name': session_user.get('name', ''),
            'dept': session_user.get('dept', ''),
            'dept_title': session_user.get('dept', '') + ' ' + session_user.get('title', ''),
        },
        'invoices': invoices,
        'payments': payments,
    }

    # 加载填充引擎
    DocumentFiller(templates_dir=TEMPLATE_DIR)

    # 对每个字段计算建议值
    suggestions = {}
    fields_dict = {f['field_id']: f for f in meta.get('fields', [])}

    for field in meta.get('fields', []):
        fid = field['field_id']
        source = field.get('source', '')

        # 用户已填的值优先
        if fid in user_fields and user_fields[fid]:
            suggestions[fid] = {
                'value': user_fields[fid],
                'source': 'user_input',
                'locked': False,
            }
            continue

        # session 数据（不可编辑）
        if source.startswith('session.'):
            key = source.split('.', 1)[1]
            val = context['session'].get(key, '')
            suggestions[fid] = {'value': val, 'source': source, 'locked': True}
            continue

        # auto:current_date
        if source == 'auto:current_date':
            suggestions[fid] = {
                'value': datetime.now().strftime('%Y年%m月%d日'),
                'source': 'auto:current_date',
                'locked': True,
            }
            continue

        # invoice:类型.attr（支持 amount_sum/count/departure_stations/arrival_stations/
        #         arrival_city_list/departure_date_first/departure_date_last/
        #         departure_date_range/travel_mode/seat_type）
        if source.startswith('invoice:'):
            parts = source.split(':')
            if len(parts) == 2:
                inv_type, attr = parts[1].split('.')
                matched = [i for i in invoices if inv_type in i.get('invoice_type', '')]
                cnt = len(matched)

                if attr == 'amount_sum':
                    total = sum((Decimal(str(i.get('amount', 0) or 0)) for i in matched), Decimal('0'))
                    suggestions[fid] = {
                        'value': f'{total:.2f}',
                        'source': f'invoice:{inv_type}.amount ({cnt}条)',
                        'locked': False,
                    }
                elif attr == 'count':
                    suggestions[fid] = {
                        'value': str(cnt),
                        'source': f'invoice:{inv_type}.count',
                        'locked': False,
                    }
                elif attr == 'departure_stations':
                    # 出发站列表（逗号分隔）
                    stations = [i.get('departure_station', '') for i in matched if i.get('departure_station')]
                    val = '、'.join(dict.fromkeys(stations))  # 去重保持顺序
                    suggestions[fid] = {
                        'value': val,
                        'source': f'invoice:{inv_type}.departure_stations ({cnt}条)',
                        'locked': False,
                    }
                elif attr == 'arrival_stations':
                    stations = [i.get('arrival_station', '') for i in matched if i.get('arrival_station')]
                    val = '、'.join(dict.fromkeys(stations))
                    suggestions[fid] = {
                        'value': val,
                        'source': f'invoice:{inv_type}.arrival_stations ({cnt}条)',
                        'locked': False,
                    }
                elif attr == 'arrival_city_list' or attr == '城市列表':
                    # 到达城市列表（去重，排除北京系），顿号分隔
                    beijing_set = {'北京', '北京南', '北京北', '北京西', '北京东', '北京站', 'Beijing', 'BEIJING'}
                    cities = []
                    for i in matched:
                        # 优先用结构化城市字段，其次从到达站提取
                        city = i.get('arrival_city', '')
                        if not city:
                            station = i.get('arrival_station', '')
                            if station:
                                city = station.rstrip('站').rstrip('南').rstrip('北').rstrip('西').rstrip('东')
                        if city and city not in beijing_set:
                            cities.append(city)
                    # 去重保持顺序
                    seen = set()
                    unique = []
                    for c in cities:
                        if c not in seen:
                            seen.add(c)
                            unique.append(c)
                    val = '、'.join(unique) if unique else ''
                    suggestions[fid] = {
                        'value': val,
                        'source': f'invoice:{inv_type}.城市列表 ({cnt}条)',
                        'locked': False,
                    }
                elif attr == 'departure_date_first' or attr == '出发日期':
                    # 最早出发日期
                    dates = [i.get('departure_date', '') for i in matched if i.get('departure_date')]
                    if dates:
                        dates.sort()
                        val = dates[0]
                        suggestions[fid] = {
                            'value': val,
                            'source': f'invoice:{inv_type}.出发日期 ({cnt}条)',
                            'locked': False,
                        }
                elif attr == 'departure_date_last' or attr == '到达日期':
                    # 最晚出发日期（到达日期视作同出发日期）
                    dates = [i.get('departure_date', '') for i in matched if i.get('departure_date')]
                    if dates:
                        dates.sort()
                        val = dates[-1]
                        suggestions[fid] = {
                            'value': val,
                            'source': f'invoice:{inv_type}.到达日期 ({cnt}条)',
                            'locked': False,
                        }
                elif attr == 'departure_date_range' or attr == '日期范围':
                    # 起止日期范围：YYYY-MM-DD ~ YYYY-MM-DD
                    dates = [i.get('departure_date', '') for i in matched if i.get('departure_date')]
                    if dates:
                        dates.sort()
                        val = f'{dates[0]} ~ {dates[-1]}'
                        suggestions[fid] = {
                            'value': val,
                            'source': f'invoice:{inv_type}.日期范围 ({cnt}条)',
                            'locked': False,
                        }
                elif attr == 'travel_mode' or attr == '出行方式':
                    # 出行方式：高铁/飞机/高铁+飞机（按票据类型）
                    has_train = any('火车' in i.get('invoice_type', '') for i in matched)
                    has_flight = any('航空' in i.get('invoice_type', '') for i in matched)
                    if has_train and has_flight:
                        val = '高铁+飞机'
                    elif has_flight:
                        val = '飞机'
                    elif has_train:
                        val = '高铁'
                    else:
                        val = ''
                    suggestions[fid] = {
                        'value': val,
                        'source': f'invoice:{inv_type}.出行方式 ({cnt}条)',
                        'locked': False,
                    }
                elif attr == '到达站':
                    # 最后一张票的到达站
                    if matched:
                        last = matched[-1]
                        val = last.get('arrival_station', '')
                        if not val:
                            val = last.get('arrival_city', '') + '站' if last.get('arrival_city') else ''
                        if val:
                            suggestions[fid] = {
                                'value': val,
                                'source': f'invoice:{inv_type}.到达站 ({cnt}条)',
                                'locked': False,
                            }
                elif attr == 'passenger_name' or attr == '乘客姓名':
                    # 乘客姓名（OCR提取）
                    names = [i.get('passenger_name', '') for i in matched if i.get('passenger_name')]
                    if names:
                        val = names[0]  # 取第一张票的乘客姓名
                    else:
                        val = ''
                    if val:
                        suggestions[fid] = {
                            'value': val,
                            'source': f'invoice:{inv_type}.乘客姓名 ({cnt}条)',
                            'locked': False,
                        }
                elif attr.startswith('item_') and '_' in attr:
                    # invoice:电子发票.item_name_0 → 第1条商品的 name（支持中文如商品名称_0）
                    # 也支持 invoice:电子发票.商品名称_0 格式
                    parts_attr = attr.rsplit('_', 1)
                    if len(parts_attr) == 2:
                        item_attr_name, idx_str = parts_attr
                        try:
                            idx = int(idx_str)
                        except ValueError:
                            idx = -1
                        if idx >= 0:
                            # 从 matched[0] 的 items 列表中取第 idx 条
                            if matched and idx < len(matched[0].get('items', [])):
                                item = matched[0]['items'][idx]
                                # 映射中文属性名到英文键
                                attr_map = {
                                    '商品名称': 'name', '规格型号': 'spec', '数量': 'quantity',
                                    '单价': 'unit_price', '金额': 'amount', '税率': 'tax_rate',
                                    '税额': 'tax_amount', 'item_name': 'name', 'item_spec': 'spec',
                                    'item_quantity': 'quantity', 'item_unit_price': 'unit_price',
                                    'item_amount': 'amount',
                                }
                                db_key = attr_map.get(item_attr_name, item_attr_name)
                                val = str(item.get(db_key, ''))
                                suggestions[fid] = {
                                    'value': val,
                                    'source': f'invoice:{inv_type}.{attr}',
                                    'locked': False,
                                }
                elif attr == 'seat_type':
                    # 座位类型
                    seats = [i.get('seat_type', '') for i in matched if i.get('seat_type')]
                    val = '、'.join(dict.fromkeys(seats)) if seats else ''
                    suggestions[fid] = {
                        'value': val,
                        'source': f'invoice:{inv_type}.seat_type ({cnt}条)',
                        'locked': False,
                    }
            continue

        # payment:类型.amount_sum
        if source.startswith('payment:'):
            parts = source.split(':')
            if len(parts) == 2:
                pay_type, attr = parts[1].split('.')
                matched = [p for p in payments if pay_type in p.get('type', '')]
                if attr == 'amount_sum':
                    total = sum((Decimal(str(p.get('amount', 0) or 0)) for p in matched), Decimal('0'))
                    cnt = len(matched)
                    suggestions[fid] = {
                        'value': f'{total:.2f}',
                        'source': f'payment:{pay_type}.amount ({cnt}条)',
                        'locked': False,
                    }
            continue

        # calculated:amount_to_cn - 延迟计算，等其他字段填好后再算
        if source.startswith('calculated:'):
            suggestions[fid] = {
                'value': '',
                'source': source,
                'locked': True,
            }
            continue

        # fixed:100 — 固定值，直接填充
        if source.startswith('fixed:'):
            fixed_val = source.split(':', 1)[1]
            suggestions[fid] = {
                'value': fixed_val,
                'source': source,
                'locked': True,  # 固定值不锁定，用户可手动修改
            }
            continue

        # user_input 或其他未知类型：空值待填
        suggestions[fid] = {
            'value': '',
            'source': 'user_input',
            'locked': False,
        }

    # 采购模板按全部发票商品形成数组，前端据此渲染并保存多条明细。
    if '采购' in doc_type:
        item_rows = [item for invoice in invoices for item in (invoice.get('items') or [])]
        purchase_fields = {
            'T0[3,1]': 'name', 'T0[4,1]': 'spec', 'T0[4,3]': 'quantity',
            'T0[4,5]': 'unit_price', 'T0[4,7]': 'amount',
        }
        for fid, key in purchase_fields.items():
            if item_rows and not user_fields.get(fid):
                suggestions[fid] = {
                    'value': [str(item.get(key) or '') for item in item_rows],
                    'source': f'invoice:all_items.{key} ({len(item_rows)}条)',
                    'locked': False,
                }

    # 计算 amount_to_cn（基于当前 suggestions 中的申报金额）
    total_amount = Decimal('0')
    for fid, sug in suggestions.items():
        lbl = fields_dict.get(fid, {}).get('label', '')
        if '申报金额' in lbl and '核准' not in lbl:
            try:
                total_amount += Decimal(str(sug.get('value') or 0))
            except (InvalidOperation, ValueError, TypeError):
                pass
    if total_amount > 0:
        suggestions['T0[12,2]'] = {
            'value': _cn_number(total_amount),
            'source': 'calculated:amount_to_cn',
            'locked': True,
        }
        suggestions['T0[12,5]'] = {
            'value': f'{total_amount:.2f}',
            'source': 'calculated:sum_all',
            'locked': True,
        }

    return jsonify({
        'success': True,
        'suggestions': suggestions,
        'available_invoices': [
            {'id': i['id'], 'invoice_type': i.get('invoice_type', ''), 'amount': i.get('amount', 0), 'date': i.get('date', '')}
            for i in invoices
        ],
        'available_payments': [
            {'id': p['id'], 'type': p.get('type', ''), 'amount': p.get('amount', 0), 'pay_date': p.get('pay_date', '')}
            for p in payments
        ],
    })


# ----------------------------------------------------------
# API：生成并下载单据
# ----------------------------------------------------------

@bp.route('/api/reimbursements/<int:rid>/documents/<doc_id>/download', methods=['GET'])
def api_download_document(rid, doc_id):
    """生成并下载填充后的单据"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    docs = get_reimbursement_documents(rid)
    doc = next((d for d in docs if d.get('id') == doc_id), None)
    if not doc:
        return jsonify({'success': False, 'error': '单据不存在'}), 404

    doc_type = doc.get('doc_type', '')
    field_values = doc.get('fields', {})

    # 构建 context
    invoices, payments = get_reimbursement_children(rid)
    session_user = {'name': session.get('name', ''), 'dept': session.get('dept', ''), 'title': session.get('title', '')}
    context = {
        'session': {
            'name': session_user.get('name', ''),
            'dept': session_user.get('dept', ''),
            'dept_title': session_user.get('dept', '') + ' ' + session_user.get('title', ''),
        },
        'invoices': invoices,
        'payments': payments,
    }

    filler = DocumentFiller(templates_dir=TEMPLATE_DIR)
    try:
        docx_bytes = filler.generate(doc_type, field_values, context)
    except FileNotFoundError:
        return jsonify({'success': False, 'error': '模板文件不存在'}), 404
    except Exception as e:
        return _safe_error(e, "单据生成失败")

    buf = io.BytesIO(docx_bytes)
    safe_name = doc_type.replace('/', '_').replace('\\', '_')
    filename = f'{safe_name}_{doc_id[:6]}.docx'
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=filename,
    )


# ----------------------------------------------------------
# API：汇总打印（合并多单据）
# ----------------------------------------------------------

@bp.route('/api/reimbursements/<int:rid>/documents/merge_print', methods=['POST'])
def api_merge_print(rid):
    """
    合并打印：生成含所有已填单据的 docx
    POST body: {doc_ids: ['id1', 'id2'], include_invoices: true}
    """
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    # 支持 JSON 和 form-encoded 两种格式
    if request.is_json:
        data = request.get_json() or {}
        doc_ids = data.get('doc_ids', [])
    else:
        # form-encoded: doc_ids 可以是逗号分隔字符串或列表
        raw = request.form.get('doc_ids', '')
        if isinstance(raw, list):
            doc_ids = raw
        elif raw:
            doc_ids = [i.strip() for i in raw.split(',') if i.strip()]
        else:
            doc_ids = []
    docs = get_reimbursement_documents(rid)

    if doc_ids:
        docs_to_print = [d for d in docs if d.get('id') in doc_ids]
    else:
        docs_to_print = docs

    if not docs_to_print:
        return jsonify({'success': False, 'error': '没有可打印的单据'}), 400

    # 构建 context
    invoices, payments = get_reimbursement_children(rid)
    session_user = {'name': session.get('name', ''), 'dept': session.get('dept', ''), 'title': session.get('title', '')}
    context = {
        'session': {
            'name': session_user.get('name', ''),
            'dept': session_user.get('dept', ''),
            'dept_title': session_user.get('dept', '') + ' ' + session_user.get('title', ''),
        },
        'invoices': invoices,
        'payments': payments,
    }

    filler = DocumentFiller(templates_dir=TEMPLATE_DIR)
    docx_bytes_list = []

    for doc in docs_to_print:
        doc_type = doc.get('doc_type', '')
        field_values = doc.get('fields', {})
        try:
            docx_bytes = filler.generate(doc_type, field_values, context)
            docx_bytes_list.append((doc_type, docx_bytes))
        except FileNotFoundError:
            return jsonify({'success': False, 'error': f'{doc_type or "所选"}模板文件不存在'}), 404
        except Exception as exc:
            return _safe_error(exc, "单据合并失败")

    if not docx_bytes_list:
        return jsonify({'success': False, 'error': '所有单据生成失败'}), 500

    # 如果只有一个，直接返回
    if len(docx_bytes_list) == 1:
        _, docx_bytes = docx_bytes_list[0]
        buf = io.BytesIO(docx_bytes)
        return send_file(
            buf,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            as_attachment=True,
            download_name=f'单据汇总_{rid}.docx',
        )

    # 多个单据必须全部进入输出，不能降级为第一份。
    try:
        buf = io.BytesIO(_merge_docx_and_images(docx_bytes_list, [], []))
    except Exception as exc:
        return _safe_error(exc, "单据合并失败")
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=f'单据汇总_{rid}.docx',
    )


# ----------------------------------------------------------
# API：汇总打印（全量：单据+发票+支付记录图片）
# ----------------------------------------------------------

@bp.route('/api/reimbursements/<int:rid>/documents/merge_print_full', methods=['POST'])
def api_merge_print_full(rid):
    """
    汇总打印全量版：
    - 所有单据内容（docx 段落/表格）
    - 所有发票图片
    - 所有支付记录图片
    合并为一份 Word 下载
    """
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    # 获取所有单据
    docs = get_reimbursement_documents(rid)
    if not docs:
        return jsonify({'success': False, 'error': '没有可打印的单据'}), 400

    # 获取所有发票和支付记录
    invoices, payments = get_reimbursement_children(rid)

    session_user = {
        'name': session.get('name', ''),
        'dept': session.get('dept', ''),
        'title': session.get('title', '')
    }
    context = {
        'session': {
            'name': session_user.get('name', ''),
            'dept': session_user.get('dept', ''),
            'dept_title': session_user.get('dept', '') + ' ' + session_user.get('title', ''),
        },
        'invoices': invoices,
        'payments': payments,
    }

    filler = DocumentFiller(templates_dir=TEMPLATE_DIR)

    # 生成所有 docx
    docx_bytes_list = []
    for doc in docs:
        doc_type = doc.get('doc_type', '')
        field_values = doc.get('fields', {})
        try:
            docx_bytes = filler.generate(doc_type, field_values, context)
            docx_bytes_list.append((doc_type, docx_bytes))
        except FileNotFoundError:
            return jsonify({'success': False, 'error': f'{doc_type or "所选"}模板文件不存在'}), 404
        except Exception as exc:
            return _safe_error(exc, "单据合并失败")

    if not docx_bytes_list:
        return jsonify({'success': False, 'error': '所有单据生成失败'}), 500

    # 合并为一份 Word
    try:
        merged_docx = _merge_docx_and_images(docx_bytes_list, invoices, payments)
    except Exception as exc:
        return _safe_error(exc, "单据合并失败")

    buf = io.BytesIO(merged_docx)
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=f'报销汇总_{rid}.docx',
    )


def _build_report_context(rid):
    """
    构建完整报销单的填充上下文（出差报销项目）。
    从数据库提取发票、支付记录等数据，汇总为模板字段值。
    """
    from app.expense_db import get_reimbursement_by_id, get_reimbursement_children

    reimb = get_reimbursement_by_id(rid)
    if not reimb:
        return None

    invoices, payments = get_reimbursement_children(rid)

    # 发票金额汇总
    total_invoice = sum((Decimal(str(inv.get('amount') or 0)) for inv in invoices), Decimal('0'))
    # 最早/最晚发票日期
    dates = [inv.get('date', '') for inv in invoices if inv.get('date')]
    earliest = min(dates) if dates else ''
    latest = max(dates) if dates else ''

    # 出发/到达站（取第一张发票的OCR解析结果）
    departure = ''
    arrival = ''
    for inv in invoices:
        ocr = inv.get('ocr_text', '') or ''
        # 火车票OCR通常包含"北京南"等站名，简易提取
        if '北京南' in ocr or 'Beijingnan' in ocr:
            departure = '北京南站'
        if '上海虹桥' in ocr or 'Shanghai' in ocr:
            arrival = '上海虹桥站'
        if '宁波' in ocr:
            arrival = '宁波'

    # 大写金额
    from app.document_engine import _cn_number
    total_cn = _cn_number(total_invoice)

    return {
        'reimb': reimb,
        'invoices': invoices,
        'payments': payments,
        'total_invoice': total_invoice,
        'total_invoice_cn': total_cn,
        'earliest_date': earliest,
        'latest_date': latest,
        'departure': departure,
        'arrival': arrival,
    }



@bp.route('/api/reimbursements/<int:rid>/generate_report', methods=['POST'])
def api_generate_report(rid):
    """
    生成完整报销单（出差模板或物资采购模板）。
    将模板填充 + 附件区（支付记录图片/PDF）合并输出。
    """
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    from app.expense_db import get_reimbursement_by_id

    reimb = get_reimbursement_by_id(rid)
    if not reimb:
        return jsonify({'success': False, 'error': '报销项目不存在'}), 404

    reimb_type = reimb.get('reimbursement_type', '')

    # 根据报销类型选择模板
    if '出差' in reimb_type:
        tmpl_name = '出差完整报销单据模板.docx'
        tmpl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', '报销单模板', tmpl_name)
        if not os.path.isfile(tmpl_path):
            return jsonify({'success': False, 'error': '完整出差报销模板缺失'}), 404
    else:
        tmpl_name = '模板.docx'
        tmpl_path = os.path.join(TEMPLATE_DIR, '科研物资采购申请单', tmpl_name)
        if not os.path.isfile(tmpl_path):
            return jsonify({'success': False, 'error': '科研物资采购申请单模板缺失'}), 404

    context = _build_report_context(rid)
    if context is None:
        return jsonify({'success': False, 'error': '报销项目不存在'}), 404

    if '出差' in reimb_type:
        # 用自定义填充函数
        with open(tmpl_path, 'rb') as f:
            tmpl_bytes = f.read()
        docx_bytes = _fill_chuchai_template(tmpl_bytes, context)
    else:
        session_context = {
            'session': {
                'name': session.get('name', ''),
                'dept': session.get('dept', ''),
                'dept_title': (session.get('dept', '') + ' ' + session.get('title', '')).strip(),
            },
            'invoices': context.get('invoices', []),
            'payments': context.get('payments', []),
        }
        field_values = {
            'T0[5,1]': reimb.get('title', '') or '科研物资采购',
            'T0[10,1]': reimb.get('approver', '') or '',
        }
        try:
            docx_bytes = DocumentFiller(templates_dir=TEMPLATE_DIR).generate(
                '科研物资采购申请单', field_values, session_context,
            )
        except (FileNotFoundError, ValueError):
            return jsonify({'success': False, 'error': '科研物资采购申请单模板无效'}), 500

    # 追加发票+支付记录附件到文档末尾
    invoices = context.get('invoices', [])
    payments = context.get('payments', [])
    if invoices or payments:
        try:
            docx_bytes = _merge_docx_and_images([('完整报销单', docx_bytes)], invoices, payments)
        except Exception as exc:
            return _safe_error(exc, "完整报销单合并失败")

    # 直接流式返回，不在运行时目录留下带业务内容的副本。
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    safe_title = reimb.get('title', '报销单').replace('/', '_').replace('\\', '_')
    filename = f'{safe_title}_{ts}_完整报销单.docx'
    # 返回下载
    buf = io.BytesIO(docx_bytes)
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=filename,
    )


def _pdf_or_image_to_png_bytes(payload, original_name):
    """Convert one already-integrity-checked controlled attachment to PNG pages."""
    import fitz
    suffix = os.path.splitext(str(original_name))[1].lower()
    if len(payload) > 20 * 1024 * 1024:
        raise ValueError('attachment size limit exceeded')
    if suffix == '.pdf':
        doc = fitz.open(stream=payload, filetype='pdf')
        if doc.page_count > 50:
            doc.close()
            raise ValueError('attachment page limit exceeded')

        def render_pages():
            total_pixels = 0
            try:
                for page in doc:
                    matrix = fitz.Matrix(2.0, 2.0)
                    page_pixels = int(page.rect.width * matrix.a) * int(page.rect.height * matrix.d)
                    total_pixels += page_pixels
                    if page_pixels > 20_000_000 or total_pixels > 100_000_000:
                        raise ValueError('attachment pixel limit exceeded')
                    rendered = page.get_pixmap(matrix=matrix).tobytes('png')
                    if len(rendered) > 20 * 1024 * 1024:
                        raise ValueError('attachment render limit exceeded')
                    yield rendered
            finally:
                doc.close()
        return render_pages()
    return [payload]


def _controlled_attachments(invoices, payments):
    service = current_app.extensions.get('file_service')
    if service is None:
        return []
    attachments = []
    for object_type, rows, label in (('INVOICE', invoices, '发票明细'), ('PAYMENT', payments, '支付记录')):
        for row in rows:
            for metadata in service.list_for_object(object_type=object_type, object_id=str(row['id'])):
                opened = service.open_version_stream(
                    metadata['fileId'], metadata['versionNo'],
                    object_type=object_type, object_id=str(row['id']),
                )
                try:
                    payload = opened['stream'].read(20 * 1024 * 1024 + 1)
                finally:
                    opened['stream'].close()
                if len(payload) > 20 * 1024 * 1024:
                    raise ValueError('attachment size limit exceeded')
                if object_type == 'INVOICE':
                    info = f'发票号：{row.get("invoice_no", "")}  |  金额：¥{row.get("amount", 0)}  |  销售方：{row.get("seller", "")}'
                else:
                    info = f'凭证号：{row.get("payment_no", "")}  |  金额：¥{row.get("amount", 0)}  |  付款人：{row.get("payer", "")}'
                attachments.append((label, info, payload, metadata['originalName']))
    return attachments


def _resolve_legacy_attachment(app_root, stored_path):
    """Compatibility-only safe resolver retained for migration regression tests.

    Runtime expense/document flows never call this helper; all new reads use
    ``FileService`` object links above.
    """
    from pathlib import Path
    import stat
    from urllib.parse import unquote

    decoded = str(stored_path or '')
    for _ in range(3):
        value = unquote(decoded)
        if value == decoded:
            break
        decoded = value
    supplied = Path(decoded)
    if not decoded or '..' in supplied.parts or '\\' in decoded:
        return None
    root = Path(app_root).resolve()
    allowed_root = (root / 'uploads').resolve()
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        if os.path.commonpath((str(allowed_root), str(candidate.resolve(strict=False)))) != str(allowed_root):
            return None
        relative = candidate.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
            if os.name != 'nt' and stat.S_IMODE(current.stat().st_mode) & 0o022:
                return None
        if not stat.S_ISREG(candidate.stat().st_mode):
            return None
    except (OSError, ValueError):
        return None
    return candidate


def _merge_docx_and_images(docx_bytes_list, invoices, payments):
    """
    将多个 docx 的内容合并为一个 Word，并附加发票/支付记录图片（PDF转PNG后嵌入）。
    发票和支付记录图片统一放在文档末尾。
    返回 bytes。
    """
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    import zipfile
    from lxml import etree

    merged = Document()

    # 设置默认字体（支持中文）
    merged.styles['Normal'].font.name = '宋体'
    merged.styles['Normal']._element.rPr.rFonts.set(
        '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}eastAsia', '宋体')

    # 合并各 docx 内容
    for doc_type, docx_bytes in docx_bytes_list:
        # 标题
        p = merged.add_heading(doc_type, level=2)
        p.runs[0].font.color.rgb = RGBColor(0x00, 0x00, 0x00)

        # 从 docx 读取内容段落/表格
        try:
            buf = io.BytesIO(docx_bytes)
            with zipfile.ZipFile(buf) as zf:
                xml_content = zf.read('word/document.xml')
            tree = etree.fromstring(xml_content)
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

            body = tree.find('.//w:body', ns)
            if body is not None:
                for elem in body:
                    tag = elem.tag.split('}')[1] if '}' in elem.tag else elem.tag
                    if tag in ('p', 'tbl'):
                        # 将元素复制到新文档
                        new_elem = etree.fromstring(etree.tostring(elem))
                        merged.element.body.append(new_elem)
        except Exception as exc:
            raise ValueError("选中单据无法读取，已取消合并") from exc

    # ============================================================
    # 发票和支付记录图片 — 统一放在文档末尾
    # ============================================================
    attachments = _controlled_attachments(invoices, payments)

    if attachments:
        # 分页，附件在文档末尾
        merged.add_page_break()
        merged.add_heading('附件', level=1)

        for label, info_text, payload, original_name in attachments:
            try:
                merged.add_heading(label, level=2)
                merged.add_paragraph(info_text).runs[0].font.size = Pt(9)
                rendered = list(_pdf_or_image_to_png_bytes(payload, original_name))
                if not rendered:
                    raise ValueError("附件无可合并内容")
                for png_bytes in rendered:
                    merged.add_picture(io.BytesIO(png_bytes), width=Inches(5.5))
                    merged.add_paragraph('')
            except Exception as exc:
                raise ValueError("选中附件无法读取，已取消合并") from exc

    buf = io.BytesIO()
    merged.save(buf)
    return buf.getvalue()
