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
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_file
from werkzeug.utils import secure_filename

from app.expense_db import (
    get_all_reimbursements, get_reimbursement_by_id,
    create_reimbursement, update_reimbursement, delete_reimbursement,
    toggle_reimbursement_paid,
    add_invoice, get_invoices, get_invoice_by_id, update_invoice, delete_invoice,
    add_payment, get_payments, get_payment_by_id, update_payment, delete_payment,
    get_unmatched_invoices, get_unmatched_payments, recalculate_reimbursement_total,
    get_db
)
from app.ocr.recognizer import recognize_file, recognize_payment
from app.expense_utils import parse_amount

bp = Blueprint('expense', __name__, url_prefix='/expense')

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'uploads', 'expense')
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'expense_templates')
os.makedirs(UPLOAD_DIR + '/invoices', exist_ok=True)
os.makedirs(UPLOAD_DIR + '/payments', exist_ok=True)

ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp'}

# 出差报销的发票类型
TRAVEL_INVOICE_TYPES = {'火车票', '航空行程单', '出租车发票', '网约车'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_upload_dir(doc_type):
    return os.path.join(UPLOAD_DIR, doc_type, datetime.now().strftime('%Y-%m'))


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
    return render_template('expense/upload.html')


@bp.route('/records')
def records():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/records.html')


@bp.route('/payments')
def payments():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/payments.html')


@bp.route('/pending')
def pending():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/pending.html')


@bp.route('/fill')
def fill():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/fill.html')


@bp.route('/approvals')
def approvals():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('expense/approvals.html')


# ============ API：上传 ============

@bp.route('/api/upload', methods=['POST'])
def api_upload():
    """上传文件并执行 OCR 识别，自动触发匹配"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400

    file = request.files['file']
    doc_type = request.form.get('type', 'invoice')  # invoice 或 payment

    if file.filename == '':
        return jsonify({'success': False, 'error': '没有选择文件'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': f'不支持的文件类型'}), 400

    # 保存文件
    filename = secure_filename(file.filename)
    now = datetime.now()
    save_dir = get_upload_dir('invoices' if doc_type == 'invoice' else 'payments')
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, f"{now.strftime('%Y%m%d%H%M%S')}_{filename}")
    file.save(save_path)

    try:
        # 自动判断是发票还是支付记录：两个 OCR 器并行跑，分数高者胜出
        import threading
        result_invoice_holder = [None]
        result_payment_holder = [None]

        def run_invoice():
            result_invoice_holder[0] = recognize_file(save_path)
        def run_payment():
            result_payment_holder[0] = recognize_payment(save_path)

        t1 = threading.Thread(target=run_invoice)
        t2 = threading.Thread(target=run_payment)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        result_invoice = result_invoice_holder[0]
        result_payment = result_payment_holder[0]

        # 评分：统计有效字段数量
        def score_invoice(r):
            if not isinstance(r, dict): return 0
            fields = r.get('fields', {})
            score = 0
            for k in ('invoice_no', 'date', 'amount', 'seller', 'buyer', 'tax_amount', 'price_ex_tax', 'tax_rate'):
                if fields.get(k): score += 1
            return score

        def score_payment(r):
            if not isinstance(r, dict): return 0
            score = 0
            for k in ('payment_no', 'amount', 'pay_date'):
                if r.get(k): score += 1
            return score

        score_i = score_invoice(result_invoice)
        score_p = score_payment(result_payment)

        # 允许手动 override（前端传 type 参数时尊重选择）
        if doc_type == 'invoice':
            chosen_type = 'invoice'
        elif doc_type == 'payment':
            chosen_type = 'payment'
        else:
            chosen_type = 'invoice' if score_i >= score_p else 'payment'

        logging.info(f"[Expense] OCR 自动判断: invoice分数={score_i}, payment分数={score_p}, 选择={chosen_type}")

        if chosen_type == 'invoice':
            # OCR 发票
            if not isinstance(result_invoice, dict) or 'fields' not in result_invoice:
                logging.error(f"[Expense] OCR 返回格式错误: {type(result_invoice).__name__}")
                return jsonify({'success': False, 'error': 'OCR 识别失败，请重试'}), 500
            fields = result_invoice['fields']
            ocr_text = result_invoice.get('text', '')

            # 金额数字化
            amount = parse_amount(fields.get('amount', '0'))
            inv_date = fields.get('date', '')

            # ---- 服务器端去重：金额+日期相同则跳过 ----
            if amount > 0 and inv_date:
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute(
                        "SELECT id, invoice_no FROM expense_invoice WHERE amount=? AND date=? AND invoice_no=? LIMIT 1",
                        (amount, inv_date, fields.get('invoice_no', ''))
                    )
                    existing = c.fetchone()
                    if existing:
                        logging.info(f"[Expense] 发票去重命中: amount=***, date={inv_date}, existing_id={existing[0]}")
                        return jsonify({
                            'success': False,
                            'error': f'金额 {amount:.2f} + 日期 {inv_date} 的发票已存在（发票号: {existing[1] or existing[0]}），请勿重复上传',
                            'duplicate': True,
                            'existing_id': existing[0],
                        }), 200

            # 保存发票记录
            invoice_id = add_invoice(
                reimbursement_id=None,
                invoice_no=fields.get('invoice_no', ''),
                date=fields.get('date', ''),
                amount=amount,
                tax_amount=parse_amount(fields.get('tax_amount', '0')),
                price_ex_tax=parse_amount(fields.get('price_ex_tax', '0')),
                buyer=fields.get('buyer', ''),
                seller=fields.get('seller', ''),
                content=fields.get('content', ''),
                spec=fields.get('spec', ''),
                invoice_type=fields.get('invoice_type', ''),
                tax_rate=fields.get('tax_rate', ''),
                ocr_text=ocr_text,
                file_path=save_path,
                confidence=fields.get('confidence', '中'),
                items=fields.get('items', []),
                # 火车票/机票结构化字段
                train_no=fields.get('train_no', ''),
                departure_station=fields.get('departure_station', ''),
                arrival_station=fields.get('arrival_station', ''),
                departure_date=fields.get('departure_date', ''),
                seat_type=fields.get('seat_type', ''),
                passenger_name=fields.get('passenger_name', ''),
                id_card_no=fields.get('id_card_no', ''),
                flight_no=fields.get('flight_no', ''),
                departure_airport=fields.get('departure_airport', ''),
                arrival_airport=fields.get('arrival_airport', ''),
                departure_time=fields.get('departure_time', ''),
            )

            # 触发匹配
            match_result = match_invoices_and_payments()

            return jsonify({
                'success': True,
                'type': 'invoice',
                'record_id': invoice_id,
                'fields': fields,
                'ocr_text': ocr_text[:500] if ocr_text else '',
                'filename': filename,
                'confidence': fields.get('confidence', '中'),
                'matched': match_result.get('new_matches', []) if match_result else [],
            })

        else:
            # OCR 支付记录
            if not isinstance(result_payment, dict):
                logging.error(f"[Expense] 支付凭证 OCR 返回格式错误: {type(result_payment).__name__}")
                return jsonify({'success': False, 'error': '支付凭证识别失败，请重试'}), 500

            amount = parse_amount(result_payment.get('amount', '0'))
            pay_date = result_payment.get('pay_date', '')

            # ---- 服务器端去重：金额+日期相同则跳过 ----
            if amount > 0 and pay_date:
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute(
                        "SELECT id, payment_no FROM expense_payment WHERE amount=? AND pay_date=? LIMIT 1",
                        (amount, pay_date)
                    )
                    existing = c.fetchone()
                    if existing:
                        logging.info(f"[Expense] 支付记录去重命中: amount=***, pay_date={pay_date}, existing_id={existing[0]}")
                        return jsonify({
                            'success': False,
                            'error': f'金额 {amount:.2f} + 日期 {pay_date} 的支付记录已存在（凭证号: {existing[1] or existing[0]}），请勿重复上传',
                            'duplicate': True,
                            'existing_id': existing[0],
                        }), 200

            payment_id = add_payment(
                reimbursement_id=None,
                payment_no=result_payment.get('payment_no', ''),
                amount=amount,
                pay_date=result_payment.get('pay_date', ''),
                payer=result_payment.get('payer', ''),
                ocr_text=result_payment.get('ocr_text', ''),
                file_path=save_path
            )

            # 触发匹配
            match_result = match_invoices_and_payments()

            return jsonify({
                'success': True,
                'type': 'payment',
                'record_id': payment_id,
                'fields': {
                    'payment_no': result_payment.get('payment_no', ''),
                    'amount': result_payment.get('amount', ''),
                    'pay_date': result_payment.get('pay_date', ''),
                    'payer': result_payment.get('payer', ''),
                },
                'ocr_text': result_payment.get('ocr_text', '')[:500],
                'filename': filename,
                'matched': match_result.get('new_matches', []) if match_result else [],
            })

    except Exception as e:
        logging.error(f"[Expense] OCR 失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/reimbursements/<int:rid>/toggle_type', methods=['POST'])
def api_reimbursement_toggle_type(rid):
    """切换报销类型（仅草稿状态可操作）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404
        if reimbursement.get('status') not in ('草稿',):
            return jsonify({'success': False, 'error': '已确认的报销项不可切换类型'}), 400
        current = reimbursement.get('reimbursement_type', '采购报销')
        new_type = '出差报销' if current == '采购报销' else '采购报销'
        update_reimbursement(rid, reimbursement_type=new_type)
        return jsonify({'success': True, 'reimbursement_type': new_type})
    except Exception as e:
        logging.error(f"[Expense] 切换报销类型失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/reimbursements/<int:rid>/toggle_paid', methods=['POST'])
def api_reimbursement_toggle_paid(rid):
    """手工切换 is_paid 状态（已报销/未报销）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404
        new_val = toggle_reimbursement_paid(rid)
        return jsonify({'success': True, 'is_paid': new_val})
    except Exception as e:
        logging.error(f"[Expense] 切换报销状态失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/reimbursements/<int:rid>/confirm', methods=['POST'])
def api_reimbursement_confirm(rid):
    """确认报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404
        # 检查是否有发票和支付记录
        invoices = get_invoices(reimbursement_id=rid)
        payments = get_payments(reimbursement_id=rid)
        if not invoices:
            return jsonify({'success': False, 'error': '报销项没有发票'}), 400
        if not payments:
            return jsonify({'success': False, 'error': '报销项没有支付记录'}), 400
        update_reimbursement(rid, status='已确认', confirmed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 确认报销项失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
            update_reimbursement(rid, status='已生成文档')

            # 依次发送两个文件
            if len(docs_to_send) == 1:
                name, buf = docs_to_send[0]
                buf.seek(0)
                return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document', as_attachment=True, download_name=name)
            else:
                # 返回第一个，第二个通过另一个链接（唯一文件名，防止并发覆盖）
                name1, buf1 = docs_to_send[0]
                buf1.seek(0)
                # 生成唯一临时文件名：approval_{rid}_{uuid4}.docx
                file_uuid = _uuid.uuid4().hex
                tmp_basename = f'approval_{rid}_{file_uuid}.docx'
                tmp_path = os.path.join(tempfile.gettempdir(), tmp_basename)
                docs_to_send[1][1].seek(0)
                with open(tmp_path, 'wb') as f:
                    f.write(docs_to_send[1][1].read())
                return jsonify({
                    'success': True,
                    'settlement': True,
                    'approval_path': f'/expense/api/download_approval/{rid}/{file_uuid}',
                })

        except ImportError:
            return jsonify({'success': False, 'error': 'python-docx 未安装'}), 500

    except Exception as e:
        logging.error(f"[Expense] 生成文档失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/download_approval/<int:rid>/<path:file_uuid>', methods=['GET'])
def api_download_approval(rid, file_uuid):
    """下载审批单（UUID 保证只能下载自己触发的文件）"""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    # 权限校验：检查报销项是否存在（防止枚举攻击）
    reimb = get_reimbursement_by_id(rid)
    if not reimb:
        return jsonify({'success': False, 'error': '报销项不存在'}), 404
    # 验证 uuid 格式（防止路径遍历）
    if not file_uuid or len(file_uuid) < 8:
        return jsonify({'success': False, 'error': '无效的文件标识'}), 400
    import tempfile
    tmp_basename = f'approval_{rid}_{file_uuid}.docx'
    tmp_path = os.path.join(tempfile.gettempdir(), tmp_basename)
    if os.path.exists(tmp_path):
        return send_file(tmp_path, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document', as_attachment=True, download_name='审批单.docx')
    return jsonify({'success': False, 'error': '文件不存在或已过期'}), 404


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/invoices/<int:iid>', methods=['PUT'])
def api_invoice_update(iid):
    """更新发票"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        data = request.get_json() or {}
        # 金额字段数字化
        if 'amount' in data:
            data['amount'] = parse_amount(data['amount'])
        if 'tax_amount' in data:
            data['tax_amount'] = parse_amount(data['tax_amount'])
        update_invoice(iid, **data)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 更新发票失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/payments/<int:pid>', methods=['PUT'])
def api_payment_update(pid):
    """更新支付记录"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        data = request.get_json() or {}
        if 'amount' in data:
            data['amount'] = parse_amount(data['amount'])
        update_payment(pid, **data)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 更新支付记录失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/reimbursements/<int:rid>/unmatch_invoice/<int:iid>', methods=['POST'])
def api_unmatch_invoice(rid, iid):
    """从报销项中移除发票（取消匹配）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        invoice = get_invoice_by_id(iid)
        if not invoice:
            return jsonify({'success': False, 'error': '发票不存在'}), 404
        update_invoice(iid, reimbursement_id=None, status='未匹配', matched_payment_ids='[]')
        recalculate_reimbursement_total(rid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 取消匹配失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500



@bp.route('/api/reimbursements/<int:rid>/unmatch_payment/<int:pid>', methods=['POST'])
def api_unmatch_payment(rid, pid):
    """从报销项中移除支付记录（取消匹配）"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        payment = get_payment_by_id(pid)
        if not payment:
            return jsonify({'success': False, 'error': '支付记录不存在'}), 404
        update_payment(pid, reimbursement_id=None, status='未匹配', matched_invoice_ids='[]')
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 取消匹配失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bp.route('/api/reimbursements/<int:rid>/add_invoice/<int:iid>', methods=['POST'])
def api_add_invoice_to_reimbursement(rid, iid):
    """手工添加发票到报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        invoice = get_invoice_by_id(iid)
        if not invoice:
            return jsonify({'success': False, 'error': '发票不存在'}), 404
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404
        update_invoice(iid, reimbursement_id=rid, status='已匹配')
        recalculate_reimbursement_total(rid)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 添加发票到报销项失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/reimbursements/<int:rid>/add_payment/<int:pid>', methods=['POST'])
def api_add_payment_to_reimbursement(rid, pid):
    """手工添加支付记录到报销项"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        payment = get_payment_by_id(pid)
        if not payment:
            return jsonify({'success': False, 'error': '支付记录不存在'}), 404
        reimbursement = get_reimbursement_by_id(rid)
        if not reimbursement:
            return jsonify({'success': False, 'error': '报销项不存在'}), 404
        update_payment(pid, reimbursement_id=rid, status='已匹配')
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Expense] 添加支付记录到报销项失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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

        # 如果没有指定报销项，创建新的
        if not rid:
            title = f"{datetime.now().strftime('%Y年%m月')}报销"
            approver = session.get('name', '')
            rid, _ = create_reimbursement(title=title, approver=approver)

        # 绑定发票
        for iid in invoice_ids:
            update_invoice(iid, reimbursement_id=rid, status='已匹配')

        # 绑定支付记录
        for pid in payment_ids:
            update_payment(pid, reimbursement_id=rid, status='已匹配')

        # 重算总金额
        recalculate_reimbursement_total(rid)

        return jsonify({'success': True, 'reimbursement_id': rid})
    except Exception as e:
        logging.error(f"[Expense] 手工匹配失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 匹配引擎核心逻辑 ============

def match_invoices_and_payments():
    """
    核心匹配逻辑（子集求和版本）：
    - 对每条未匹配支付记录，在剩余未匹配发票中穷举所有子集
    - 找金额之和等于支付记录金额的发票组合（允许多张发票凑一张支付记录）
    - 每张发票只能使用一次（用完从剩余池移除）
    - 返回匹配结果统计
    """
    from itertools import combinations

    def find_best_subset(candidates, target, pay_date):
        """
        找出一组发票其金额之和最接近 target（优先完全相等，最少发票数）
        candidates: [(id, amount, date), ...] 未被使用的发票
        target: 目标金额
        pay_date: 支付记录日期（发票日期必须 <= 支付日期）
        返回: [invoice_id, ...] 或 None
        """
        if target <= 0 or not candidates:
            return None
        # 不做日期过滤：允许发票日期晚于支付日期（如先消费后开票场景）
        valid_candidates = candidates
        max_subset_size = min(len(valid_candidates), 10)
        for size in range(1, max_subset_size + 1):
            for combo in combinations(valid_candidates, size):
                if abs(sum(inv[1] for inv in combo) - target) < 0.01:
                    return [inv[0] for inv in combo]
        return None

    unmatched_invoices = get_unmatched_invoices()
    unmatched_payments = get_unmatched_payments()

    new_matches = []
    used_invoice_ids = set()   # 全局已使用发票ID（不可重复）
    used_payment_ids = set()  # 全局已使用支付记录ID

    # 按金额降序处理支付记录（大金额优先，更容易组合）
    payments_sorted = sorted(
        unmatched_payments,
        key=lambda p: float(p.get('amount') or 0),
        reverse=True
    )

    for payment in payments_sorted:
        pid = payment['id']
        if pid in used_payment_ids:
            continue
        pay_amount = float(payment.get('amount') or 0)
        pay_date = payment.get('pay_date', '')

        if pay_amount <= 0:
            continue

        # 可用的发票候选（排除已用的）
        available = [
            (i['id'], float(i['amount'] or 0), i.get('date', ''))
            for i in unmatched_invoices
            if i['id'] not in used_invoice_ids
        ]

        matched_inv_ids = find_best_subset(available, pay_amount, pay_date)

        if not matched_inv_ids:
            continue

        # 找到匹配：创建或复用报销项
        title = f"{datetime.now().strftime('%Y年%m月')}报销"
        approver = session.get('name', '')

        # 归属冲突检测：检查所有发票是否已属于不同报销项
        existing_rids = set()
        for iid in matched_inv_ids:
            inv_obj = get_invoice_by_id(iid)
            if inv_obj and inv_obj.get('reimbursement_id'):
                existing_rids.add(inv_obj['reimbursement_id'])

        if len(existing_rids) > 1:
            # 多张发票已分属不同报销项，跳过此支付记录（冲突）
            logging.warning(f"[Expense] 匹配冲突：发票 {matched_inv_ids} 分属不同报销项 {existing_rids}，跳过支付记录 {pid}")
            continue
        elif len(existing_rids) == 1:
            rid = existing_rids.pop()
        else:
            # 根据发票类型判断报销类型
            matched_inv_types = [get_invoice_by_id(iid).get('invoice_type', '') for iid in matched_inv_ids]
            is_travel = any(t in TRAVEL_INVOICE_TYPES for t in matched_inv_types)
            reimbursement_type = '出差报销' if is_travel else '采购报销'
            rid, _ = create_reimbursement(title=title, approver=approver, reimbursement_type=reimbursement_type)

        # 绑定支付记录
        update_payment(pid, reimbursement_id=rid, status='已匹配')
        used_payment_ids.add(pid)

        # 收集匹配的发票对象并绑定
        matched_invoice_objs = []
        for iid in matched_inv_ids:
            update_invoice(iid, reimbursement_id=rid, status='已匹配')
            used_invoice_ids.add(iid)
            inv_obj = get_invoice_by_id(iid)
            if inv_obj:
                matched_invoice_objs.append(inv_obj)

        # 重算总金额
        recalculate_reimbursement_total(rid)

        new_matches.append({
            'reimbursement_id': rid,
            'payment': payment,
            'invoices': matched_invoice_objs,
        })

    return {
        'new_matches': new_matches,
        'total_invoices_matched': len(used_invoice_ids),
        'total_payments_matched': len(used_payment_ids),
    }


# ============ 首页统计数据 ============

@bp.route('/api/stats', methods=['GET'])
def api_stats():
    """获取报销助手首页统计数据"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        with get_db() as conn:
            c = conn.cursor()

            # 待确认报销项数（状态=草稿 且 有发票）
            c.execute('''SELECT COUNT(DISTINCT r.id) FROM expense_reimbursement r
                JOIN expense_invoice i ON i.reimbursement_id = r.id
                WHERE r.status = '草稿' ''')
            draft_count = c.fetchone()[0]

            # 待整理发票数
            c.execute("SELECT COUNT(*) FROM expense_invoice WHERE status='未匹配'")
            unmatched_invoice_count = c.fetchone()[0]

            # 待整理支付记录数
            c.execute("SELECT COUNT(*) FROM expense_payment WHERE status='未匹配'")
            unmatched_payment_count = c.fetchone()[0]

            # 三单完整性告警：报销项有发票但无支付记录，或有支付记录但无发票
            c.execute('''SELECT COUNT(DISTINCT r.id) FROM expense_reimbursement r
                WHERE r.status NOT IN ('已作废', '已完成')
                AND EXISTS (SELECT 1 FROM expense_invoice i WHERE i.reimbursement_id = r.id)
                AND NOT EXISTS (SELECT 1 FROM expense_payment p WHERE p.reimbursement_id = r.id)''')
            missing_payment = c.fetchone()[0]

            c.execute('''SELECT COUNT(DISTINCT r.id) FROM expense_reimbursement r
                WHERE r.status NOT IN ('已作废', '已完成')
                AND NOT EXISTS (SELECT 1 FROM expense_invoice i WHERE i.reimbursement_id = r.id)
                AND EXISTS (SELECT 1 FROM expense_payment p WHERE p.reimbursement_id = r.id)''')
            missing_invoice = c.fetchone()[0]

            # 最近报销项
            c.execute('SELECT * FROM expense_reimbursement ORDER BY created_at DESC LIMIT 5')
            rows = c.fetchall()
            cols = [d[0] for d in c.description]
            recent = [dict(zip(cols, r)) for r in rows]


            return jsonify({
                'success': True,
                'stats': {
                    'draft_count': draft_count,
                    'unmatched_invoice_count': unmatched_invoice_count,
                    'unmatched_payment_count': unmatched_payment_count,
                    'missing_payment_count': missing_payment,
                    'missing_invoice_count': missing_invoice,
                },
                'recent_reimbursements': recent,
            })
    except Exception as e:
        logging.error(f"[Expense] 获取统计失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
