# -*- coding: utf-8 -*-
"""
报销助手路由
文件上传 → OCR 识别 → 智能匹配 → 报销项管理
"""
import os
import io
import re
import json
import logging
import uuid as _uuid
from datetime import datetime
from pathlib import Path
from flask import Blueprint, current_app, render_template, request, jsonify, session, redirect, url_for, send_file

from app.expense_db import (
    get_all_reimbursements, get_reimbursement_by_id,
    create_reimbursement, update_reimbursement, delete_reimbursement,
    toggle_reimbursement_paid,
    mark_reimbursement_documents_generated,
    add_invoice, get_invoices, get_invoice_by_id, update_invoice, delete_invoice,
    add_payment, get_payments, get_payment_by_id, update_payment, delete_payment,
    get_unmatched_invoices, get_unmatched_payments, recalculate_reimbursement_total,
    get_expense_stats, find_duplicate_invoice, find_duplicate_payment,
)
from app.ocr.recognizer import recognize_file, recognize_payment
from app.expense_utils import parse_amount

bp = Blueprint('expense', __name__, url_prefix='/expense')

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'expense_templates')

# 出差报销的发票类型
TRAVEL_INVOICE_TYPES = {'火车票', '航空行程单', '出租车发票', '网约车'}


def _service():
    return current_app.extensions["expense_service"]


def _safe_error(exc, fallback="操作失败"):
    from app.services.expenses import ExpenseError
    if isinstance(exc, ExpenseError):
        return jsonify({'success': False, 'error': exc.message, 'code': exc.code}), exc.status_code
    logging.exception("[Expense] %s", fallback)
    return jsonify({'success': False, 'error': fallback}), 500


# ============ 页面路由 ============

@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return redirect(url_for('expense.records'))


@bp.route('/upload')
def upload():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return redirect(url_for('expense.records'))


@bp.route('/records')
def records():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/records.html')


@bp.route('/payments')
def payments():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return redirect(url_for('expense.records'))


@bp.route('/pending')
def pending():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return redirect(url_for('expense.records'))


@bp.route('/fill')
def fill():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return redirect(url_for('expense.records'))


@bp.route('/approvals')
def approvals():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/approvals.html')


# ============ API：上传 ============

@bp.route('/api/upload', methods=['POST'])
def api_upload():
    """受控上传；OCR 不可用时仍创建可手工补录的记录。"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    uploaded = request.files.get('file')
    if uploaded is None or not uploaded.filename:
        return jsonify({'success': False, 'error': '没有选择文件'}), 400
    doc_type = request.form.get('type', 'invoice')
    if doc_type not in {'invoice', 'payment'}:
        return jsonify({'success': False, 'error': '文件类型无效'}), 400

    raw = uploaded.stream.read()
    uploaded.stream.seek(0)
    if not raw:
        return jsonify({'success': False, 'error': '空文件不允许上传'}), 415

    recognized = None
    manual_required = False
    try:
        import tempfile
        suffix = Path(uploaded.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(suffix=suffix) as staged:
            staged.write(raw)
            staged.flush()
            recognized = recognize_file(staged.name) if doc_type == 'invoice' else recognize_payment(staged.name)
    except Exception:
        logging.warning("[Expense] OCR unavailable; preserving manual workflow", exc_info=True)
        manual_required = True

    try:
        actor_user_id = int(session.get('user_id') or 0)
        request_id = getattr(request, 'request_id', 'expense-upload')
        stream = io.BytesIO(raw)
        if doc_type == 'invoice':
            fields = recognized.get('fields', {}) if isinstance(recognized, dict) else {}
            amount = parse_amount(fields.get('amount', '0'))
            inv_date = fields.get('date', '')
            existing = find_duplicate_invoice(str(amount), inv_date, fields.get('invoice_no', '')) if amount > 0 and inv_date else None
            if existing:
                return jsonify({'success': False, 'error': '相同金额、日期和发票号的发票已存在', 'duplicate': True, 'existing_id': existing['id']}), 200
            iid, stored = _service().create_invoice_with_upload(
                stream, uploaded.filename, actor_user_id=actor_user_id, request_id=request_id,
                invoice_no=fields.get('invoice_no', ''), date=inv_date, amount=str(amount),
                tax_amount=str(parse_amount(fields.get('tax_amount', '0'))),
                price_ex_tax=str(parse_amount(fields.get('price_ex_tax', '0'))),
                buyer=fields.get('buyer', ''), seller=fields.get('seller', ''),
                content=fields.get('content', ''), spec=fields.get('spec', ''),
                invoice_type=fields.get('invoice_type', ''), tax_rate=fields.get('tax_rate', ''),
                confidence=fields.get('confidence', '中'), items=fields.get('items', []),
                train_no=fields.get('train_no', ''), departure_station=fields.get('departure_station', ''),
                arrival_station=fields.get('arrival_station', ''), departure_date=fields.get('departure_date', ''),
                seat_type=fields.get('seat_type', ''), passenger_name=fields.get('passenger_name', ''),
                id_card_no=fields.get('id_card_no', ''), flight_no=fields.get('flight_no', ''),
                departure_airport=fields.get('departure_airport', ''), arrival_airport=fields.get('arrival_airport', ''),
                departure_time=fields.get('departure_time', ''), ocr_text=(recognized or {}).get('text', ''),
            )
            match_result = match_invoices_and_payments() if not manual_required else {'new_matches': []}
            return jsonify({'success': True, 'type': 'invoice', 'record_id': iid, 'fields': fields,
                            'ocr_text': '', 'filename': stored['originalName'],
                            'confidence': fields.get('confidence', '待补录'),
                            'manual_required': manual_required, 'matched': match_result.get('new_matches', [])})
        fields = recognized if isinstance(recognized, dict) else {}
        amount = parse_amount(fields.get('amount', '0'))
        pay_date = fields.get('pay_date', '')
        existing = find_duplicate_payment(str(amount), pay_date) if amount > 0 and pay_date else None
        if existing:
            return jsonify({'success': False, 'error': '相同金额和日期的支付记录已存在', 'duplicate': True, 'existing_id': existing['id']}), 200
        pid, stored = _service().create_payment_with_upload(
            stream, uploaded.filename, actor_user_id=actor_user_id, request_id=request_id,
            payment_no=fields.get('payment_no', ''), amount=str(amount), pay_date=pay_date,
            payer=fields.get('payer', ''), ocr_text=fields.get('ocr_text', ''),
        )
        match_result = match_invoices_and_payments() if not manual_required else {'new_matches': []}
        return jsonify({'success': True, 'type': 'payment', 'record_id': pid,
                        'fields': {key: fields.get(key, '') for key in ('payment_no', 'amount', 'pay_date', 'payer')},
                        'ocr_text': '', 'filename': stored['originalName'],
                        'manual_required': manual_required, 'matched': match_result.get('new_matches', [])})
    except Exception as exc:
        return _safe_error(exc, "上传处理失败")


# ============ API：报销项 ============

@bp.route('/api/reimbursements', methods=['GET'])
def api_reimbursements():
    """获取报销项列表"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        status = request.args.get('status', '')
        keyword = request.args.get('keyword', '')
        records = get_all_reimbursements(
            status=status if status else None,
            keyword=keyword if keyword else None,
        )
        return jsonify({'success': True, 'reimbursements': records})
    except Exception as e:
        logging.error(f"[Expense] 获取报销项失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>', methods=['GET'])
def api_reimbursement_get(rid):
    """获取报销项详情（含发票+支付记录）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404

        invoices = get_invoices(reimbursement_id=rid)
        payments = get_payments(reimbursement_id=rid)

        return jsonify({
            'success': True,
            'reimbursement': reimbursement,
            'invoices': invoices,
            'payments': payments,
        })
    except Exception as e:
        logging.error(f"[Expense] 获取报销项详情失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements', methods=['POST'])
def api_reimbursement_create():
    """新建空白报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        data = request.get_json() or {}
        title = data.get('title', f"{datetime.now().strftime('%Y年%m月')}报销")
        approver = session.get('name', '')
        reimbursement_type = data.get('reimbursement_type', '采购报销')
        rid, rno = create_reimbursement(title=title, approver=approver, reimbursement_type=reimbursement_type)
        return jsonify({'success': True, 'id': rid, 'reimbursement_no': rno})
    except Exception as e:
        logging.error(f"[Expense] 创建报销项失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>', methods=['PUT'])
def api_reimbursement_update(rid):
    """更新报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        data = request.get_json() or {}
        update_reimbursement(rid, **data)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 更新报销项失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>', methods=['DELETE'])
def api_reimbursement_delete(rid):
    """删除报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404
        if reimbursement.get('status') not in ('草稿',):
            return jsonify({'success': False, 'error': '只有草稿状态的报销项可以删除'}), 400
        delete_reimbursement(rid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 删除报销项失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>/toggle_type', methods=['POST'])
def api_reimbursement_toggle_type(rid):
    """切换报销类型（仅草稿状态可操作）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        new_type = _service().toggle_type(rid)
        return jsonify({'success': True, 'reimbursement_type': new_type})
    except Exception as e:
        logging.error(f"[Expense] 切换报销类型失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>/toggle_paid', methods=['POST'])
def api_reimbursement_toggle_paid(rid):
    """手工切换 is_paid 状态（已报销/未报销）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        new_val = _service().toggle_paid(rid)
        return jsonify({'success': True, 'is_paid': new_val})
    except Exception as e:
        logging.error(f"[Expense] 切换报销状态失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>/confirm', methods=['POST'])
def api_reimbursement_confirm(rid):
    """确认报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        _service().confirm(rid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 确认报销项失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>/generate_docs', methods=['POST'])
def api_reimbursement_generate_docs(rid):
    """生成结算单+审批单 Word 文档"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404

        invoices = get_invoices(reimbursement_id=rid)
        payments = get_payments(reimbursement_id=rid)

        from app.expense_utils import amount_to_cn
        total_amount = float(reimbursement.get('total_amount') or 0)
        total_cn = amount_to_cn(total_amount)
        applicant = session.get('name', '')

        # 尝试用模板
        settlement_template = os.path.join(TEMPLATE_DIR, 'settlement_template.docx')
        approval_template = os.path.join(TEMPLATE_DIR, 'approval_template.docx')

        try:
            from docx import Document
            docs_to_send = []

            # 结算单
            if os.path.exists(settlement_template):
                doc = Document(settlement_template)
            else:
                doc = Document()

            # 替换占位符
            def replace_placeholders(doc, mapping):
                for para in doc.paragraphs:
                    for key, val in mapping.items():
                        if val is None:
                            val = ''
                        para.text = para.text.replace(f'{{{{{key}}}}}', str(val))

            # 全局字段
            global_fields = {
                'reimbursement_no': reimbursement.get('reimbursement_no', ''),
                'title': reimbursement.get('title', ''),
                'total_amount': f'{total_amount:.2f}',
                'total_amount_cn': total_cn,
                'approver': applicant,
                'applicant': applicant,
                'remark': reimbursement.get('remark', ''),
                'created_at': reimbursement.get('created_at', ''),
            }

            # 发票列表字段（首条）
            if invoices:
                inv = invoices[0]
                global_fields.update({
                    'invoice_no': inv.get('invoice_no', ''),
                    'invoice_date': inv.get('date', ''),
                    'invoice_amount': f"{inv.get('amount', 0):.2f}",
                    'invoice_content': inv.get('content', ''),
                    'invoice_seller': inv.get('seller', ''),
                    'invoice_tax_rate': inv.get('tax_rate', ''),
                })

            if payments:
                pay = payments[0]
                global_fields.update({
                    'payment_no': pay.get('payment_no', ''),
                    'payment_date': pay.get('pay_date', ''),
                    'payment_amount': f"{pay.get('amount', 0):.2f}",
                })

            replace_placeholders(doc, global_fields)

            # 发票表格替换（如果有表格）
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for key, val in global_fields.items():
                            if val is None:
                                val = ''
                            cell.text = cell.text.replace(f'{{{{{key}}}}}', str(val))

            output = io.BytesIO()
            doc.save(output)
            output.seek(0)
            docs_to_send.append(('结算单.docx', output))

            # 审批单
            if os.path.exists(approval_template):
                doc2 = Document(approval_template)
            else:
                doc2 = Document()
                doc2.add_heading('报销审批单', 0)
                doc2.add_paragraph(f"报销编号：{reimbursement.get('reimbursement_no', '')}")
                doc2.add_paragraph(f"申请人：{applicant}")
                doc2.add_paragraph(f"报销事由：{reimbursement.get('remark', '')}")
                doc2.add_paragraph(f"申请时间：{reimbursement.get('created_at', '')}")

                # 发票明细表
                doc2.add_heading('发票明细', 3)
                tbl = doc2.add_table(rows=1, cols=5)
                tbl.style = 'Light Grid Accent 1'
                hdr = tbl.rows[0].cells
                hdr[0].text = '发票号'
                hdr[1].text = '日期'
                hdr[2].text = '金额'
                hdr[3].text = '内容'
                hdr[4].text = '销售方'
                for inv in invoices:
                    row = tbl.add_row().cells
                    row[0].text = str(inv.get('invoice_no', ''))
                    row[1].text = str(inv.get('date', ''))
                    row[2].text = f"{inv.get('amount', 0):.2f}"
                    row[3].text = str(inv.get('content', ''))
                    row[4].text = str(inv.get('seller', ''))

                doc2.add_paragraph(f"\n合计金额：{total_amount:.2f}（{total_cn}）")

            replace_placeholders(doc2, global_fields)
            for table in doc2.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for key, val in global_fields.items():
                            if val is None:
                                val = ''
                            cell.text = cell.text.replace(f'{{{{{key}}}}}', str(val))

            output2 = io.BytesIO()
            doc2.save(output2)
            output2.seek(0)
            docs_to_send.append(('审批单.docx', output2))

            # 更新状态
            mark_reimbursement_documents_generated(rid)

            # 依次发送两个文件
            if len(docs_to_send) == 1:
                name, buf = docs_to_send[0]
                buf.seek(0)
                return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document', as_attachment=True, download_name=name)
            else:
                file_service = current_app.extensions.get('file_service')
                if file_service is None:
                    return jsonify({'success': False, 'error': '附件服务不可用'}), 503
                approval = docs_to_send[1][1]
                approval.seek(0)
                stored = file_service.upload(
                    approval, original_name='审批单.docx', object_type='EXPENSE', object_id=str(rid),
                    actor_user_id=int(session.get('user_id') or 0),
                    request_id=getattr(request, 'request_id', 'expense-generate-docs'),
                )
                return jsonify({
                    'success': True,
                    'settlement': True,
                    'approval_path': f'/expense/api/download_approval/{rid}/{stored["fileId"]}',
                })

        except ImportError:
            return jsonify({'success': False, 'error': 'python-docx 未安装'}), 500

    except Exception as e:
        logging.error(f"[Expense] 生成文档失败: {e}")
        return _safe_error(e)


@bp.route('/api/download_approval/<int:rid>/<path:file_uuid>', methods=['GET'])
def api_download_approval(rid, file_uuid):
    """从受控存储读取审批单。"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    # 权限校验：检查报销项是否存在（防止枚举攻击）
    reimb = get_reimbursement_by_id(rid)
    if not reimb:
        return jsonify({'success': False, 'error': '报销项不存在'}), 404
    try:
        _uuid.UUID(file_uuid)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '无效的文件标识'}), 400
    file_service = current_app.extensions.get('file_service')
    if file_service is None:
        return jsonify({'success': False, 'error': '附件服务不可用'}), 503
    try:
        metadata = next((item for item in file_service.list_for_object(object_type='EXPENSE', object_id=str(rid)) if item['fileId'] == file_uuid), None)
        if metadata is None:
            return jsonify({'success': False, 'error': '文件不存在或已过期'}), 404
        opened = file_service.open_version_stream(file_uuid, metadata['versionNo'], object_type='EXPENSE', object_id=str(rid))
        return send_file(opened['stream'], mimetype=opened['mediaType'], as_attachment=True, download_name=opened['originalName'])
    except Exception as exc:
        return _safe_error(exc, '下载审批单失败')


# ============ API：发票 ============

@bp.route('/api/invoices', methods=['GET'])
def api_invoices():
    """获取发票列表"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement_id = request.args.get('reimbursement_id')
        status = request.args.get('status', '')
        rid = int(reimbursement_id) if reimbursement_id else None
        invoices = get_invoices(reimbursement_id=rid, status=status if status else None)
        return jsonify({'success': True, 'invoices': invoices})
    except Exception as e:
        logging.error(f"[Expense] 获取发票失败: {e}")
        return _safe_error(e)


@bp.route('/api/invoices/<int:iid>', methods=['GET'])
def api_invoice_get(iid):
    """获取单个发票详情（供悬浮卡片使用）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        invoice = get_invoice_by_id(iid)
        if not invoice:
            return jsonify({'success': False, 'error': '发票不存在'}), 404
        return jsonify({'success': True, 'invoice': invoice})
    except Exception as e:
        logging.error(f"[Expense] 获取发票详情失败: {e}")
        return _safe_error(e)


@bp.route('/api/invoices/<int:iid>', methods=['PUT'])
def api_invoice_update(iid):
    """更新发票"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        data = request.get_json() or {}
        # JSON 数字先转成十进制文本，避免 float 直接进入金额层。
        if 'amount' in data:
            data['amount'] = str(data['amount'])
        if 'tax_amount' in data:
            data['tax_amount'] = str(data['tax_amount'])
        if 'price_ex_tax' in data:
            data['price_ex_tax'] = str(data['price_ex_tax'])
        update_invoice(iid, **data)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 更新发票失败: {e}")
        return _safe_error(e)


@bp.route('/api/invoices/<int:iid>', methods=['DELETE'])
def api_invoice_delete(iid):
    """删除发票"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        invoice = get_invoice_by_id(iid)
        if not invoice:
            return jsonify({'success': False, 'error': '发票不存在'}), 404
        # 删除关联的报销项的总金额重算
        rid = invoice.get('reimbursement_id')
        delete_invoice(iid)
        if rid:
            recalculate_reimbursement_total(rid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 删除发票失败: {e}")
        return _safe_error(e)


# ============ API：支付记录 ============

@bp.route('/api/payments', methods=['GET'])
def api_payments_list():
    """获取支付记录列表"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement_id = request.args.get('reimbursement_id')
        status = request.args.get('status', '')
        rid = int(reimbursement_id) if reimbursement_id else None
        payments = get_payments(reimbursement_id=rid, status=status if status else None)
        return jsonify({'success': True, 'payments': payments})
    except Exception as e:
        logging.error(f"[Expense] 获取支付记录失败: {e}")
        return _safe_error(e)


@bp.route('/api/payments/<int:pid>', methods=['GET'])
def api_payment_get(pid):
    """获取单个支付记录详情（供悬浮卡片使用）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        payment = get_payment_by_id(pid)
        if not payment:
            return jsonify({'success': False, 'error': '支付记录不存在'}), 404
        return jsonify({'success': True, 'payment': payment})
    except Exception as e:
        logging.error(f"[Expense] 获取支付记录详情失败: {e}")
        return _safe_error(e)


@bp.route('/api/payments/<int:pid>', methods=['PUT'])
def api_payment_update(pid):
    """更新支付记录"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        data = request.get_json() or {}
        if 'amount' in data:
            data['amount'] = str(data['amount'])
        update_payment(pid, **data)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 更新支付记录失败: {e}")
        return _safe_error(e)


@bp.route('/api/payments/<int:pid>', methods=['DELETE'])
def api_payment_delete(pid):
    """删除支付记录"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        payment = get_payment_by_id(pid)
        if not payment:
            return jsonify({'success': False, 'error': '支付记录不存在'}), 404
        rid = payment.get('reimbursement_id')
        delete_payment(pid)
        if rid:
            recalculate_reimbursement_total(rid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 删除支付记录失败: {e}")
        return _safe_error(e)


# ============ API：匹配 ============

@bp.route('/api/match', methods=['POST'])
def api_match():
    """手动触发匹配"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        result = match_invoices_and_payments()
        return jsonify({'success': True, **result})
    except Exception as e:
        logging.error(f"[Expense] 匹配失败: {e}")
        return _safe_error(e)


@bp.route('/api/pending', methods=['GET'])
def api_pending():
    """获取待整理区数据"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        invoices = get_unmatched_invoices()
        payments = get_unmatched_payments()
        return jsonify({
            'success': True,
            'invoices': invoices,
            'payments': payments,
            'invoice_count': len(invoices),
            'payment_count': len(payments),
        })
    except Exception as e:
        logging.error(f"[Expense] 获取待整理区失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>/unmatch_invoice/<int:iid>', methods=['POST'])
def api_unmatch_invoice(rid, iid):
    """从报销项中移除发票（取消匹配）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        _service().detach_invoice(rid, iid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 取消匹配失败: {e}")
        return _safe_error(e)



@bp.route('/api/reimbursements/<int:rid>/unmatch_payment/<int:pid>', methods=['POST'])
def api_unmatch_payment(rid, pid):
    """从报销项中移除支付记录（取消匹配）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        _service().detach_payment(rid, pid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 取消匹配失败: {e}")
        return _safe_error(e)

@bp.route('/api/reimbursements/<int:rid>/add_invoice/<int:iid>', methods=['POST'])
def api_add_invoice_to_reimbursement(rid, iid):
    """手工添加发票到报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        _service().attach_invoice(rid, iid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 添加发票到报销项失败: {e}")
        return _safe_error(e)


@bp.route('/api/reimbursements/<int:rid>/add_payment/<int:pid>', methods=['POST'])
def api_add_payment_to_reimbursement(rid, pid):
    """手工添加支付记录到报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        _service().attach_payment(rid, pid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 添加支付记录到报销项失败: {e}")
        return _safe_error(e)


@bp.route('/api/manual_match', methods=['POST'])
def api_manual_match():
    """手工匹配：将选中的发票和支付记录匹配到一个报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        data = request.get_json() or {}
        invoice_ids = data.get('invoice_ids', [])
        payment_ids = data.get('payment_ids', [])
        rid = data.get('reimbursement_id')

        if not invoice_ids or not payment_ids:
            return jsonify({'success': False, 'error': '请选择至少一张发票和一条支付记录'}), 400

        rid = _service().manual_match(
            invoice_ids, payment_ids, int(rid) if rid else None,
            title=f"{datetime.now().strftime('%Y年%m月')}报销",
            approver=session.get('name', ''),
        )

        return jsonify({'success': True, 'reimbursement_id': rid})
    except Exception as e:
        logging.error(f"[Expense] 手工匹配失败: {e}")
        return _safe_error(e)


# ============ 匹配引擎核心逻辑 ============

def match_invoices_and_payments():
    """在单个 PostgreSQL 事务中执行有界精确金额匹配。"""
    return _service().auto_match(approver=session.get('name', ''))


# ============ 首页统计数据 ============

@bp.route('/api/stats', methods=['GET'])
def api_stats():
    """获取报销助手首页统计数据"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        stats, recent = get_expense_stats()
        return jsonify({'success': True, 'stats': stats, 'recent_reimbursements': recent})
    except Exception as e:
        logging.error(f"[Expense] 获取统计失败: {e}")
        return _safe_error(e)
