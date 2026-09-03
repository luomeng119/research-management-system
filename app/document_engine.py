# -*- coding: utf-8 -*-
"""
报销单据 Word 模板填充引擎
根据 template.json 定义，将字段值填入 docx 表格单元格
"""
import os
import re
import io
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from datetime import datetime
from docx import Document


def _cn_number(amount):
    """人民币大写金额转换，支持最大到千亿"""
    if amount is None:
        return ""
    try:
        value = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        cents = int(value * 100)
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if cents == 0:
        return "零元整"

    chinese_num = "零壹贰叁肆伍陆柒捌玖"
    unit = ["", "拾", "佰", "仟"]
    section_unit = ["", "万", "亿"]

    def section_convert(num):
        """将0-9999的数字转换为大写字符串（不带节单位）"""
        if num == 0:
            return ""
        digit = [num // 1000, (num // 100) % 10, (num // 10) % 10, num % 10]
        res = []
        leading = True
        trailing_zeros = 0  # 记录末尾连续零的个数
        for i, d in enumerate(digit):
            if d == 0:
                if not leading:
                    trailing_zeros += 1
                    res.append("零")
            else:
                leading = False
                # 去掉之前累积的末尾零
                if trailing_zeros > 0:
                    res = res[:-trailing_zeros]
                    trailing_zeros = 0
                res.append(chinese_num[d] + unit[3 - i])
        # 去掉最后一个零（如果有的话）
        if trailing_zeros > 0 and res:
            res = res[:-trailing_zeros]
        return "".join(res)

    yuan = cents // 100
    jiao = (cents // 10) % 10
    fen = cents % 10

    parts = []
    y = yuan
    if y > 0:
        sections = []
        while y > 0:
            sections.append(y % 10000)
            y //= 10000
        for i in range(len(sections) - 1, -1, -1):
            sec_str = section_convert(sections[i])
            if sec_str:  # 只在节有内容时才加（忽略全零节）
                if parts and sections[i] < 1000:
                    sec_str = "零" + sec_str
                parts.append(sec_str + section_unit[i])
    integer_part = "".join(parts)
    integer_part = integer_part.replace("亿万", "亿")

    decimal_part = ""
    if jiao == 0 and fen == 0:
        decimal_part = "整"
    else:
        if jiao != 0:
            decimal_part += chinese_num[jiao] + "角"
        if fen != 0:
            decimal_part += chinese_num[fen] + "分"
        if fen == 0 and not decimal_part.endswith("整"):
            decimal_part += "整"

    if integer_part == "":
        return decimal_part
    else:
        return integer_part + "元" + decimal_part


def _parse_amount(val):
    """解析金额字符串为 Decimal。"""
    if isinstance(val, Decimal):
        return val
    if not val:
        return Decimal('0')
    # 去掉￥ ¥ ，空格
    val = re.sub(r'[￥¥,\s]', '', str(val))
    try:
        return Decimal(val)
    except (InvalidOperation, ValueError):
        return Decimal('0')


class DocumentFiller:
    """Word 模板填充引擎"""

    def __init__(self, templates_dir=None):
        if templates_dir is None:
            templates_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'document_templates'
            )
        self.templates_dir = Path(templates_dir)

    # ----------------------------------------------------------
    # 模板加载
    # ----------------------------------------------------------

    def load_template(self, doc_type):
        """加载模板定义和原始 docx 文件

        Returns:
            (meta: dict, doc: Document)
        """
        name = str(doc_type or "")
        if not name or Path(name).name != name or any(marker in name for marker in ("/", "\\", "..")):
            raise FileNotFoundError("模板不存在")
        tpl_dir = self.templates_dir / name
        if not tpl_dir.is_dir():
            raise FileNotFoundError(f"模板目录不存在: {doc_type}")

        meta_path = tpl_dir / 'template.json'
        if not meta_path.exists():
            raise FileNotFoundError(f"模板定义文件不存在: {meta_path}")

        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        docx_filename = meta.get('docx_filename', '模板.docx')
        docx_path = tpl_dir / docx_filename
        if not docx_path.exists():
            raise FileNotFoundError(f"模板 docx 文件不存在: {docx_path}")

        doc = Document(str(docx_path))
        return meta, doc

    # ----------------------------------------------------------
    # 字段填充
    # ----------------------------------------------------------

    def fill(self, doc, field_values, table_index=None):
        """
        将 field_values {field_id: value} 填入 docx 的表格单元格

        field_id 格式: "T0[3,2]" = Table0 Row3 Col2
        若 table_index 指定，只填充对应表格；否则按 field_id 前缀匹配
        """
        for table_i, table in enumerate(doc.tables):
            if table_index is not None and table_i != table_index:
                continue
            for row_i, row in enumerate(table.rows):
                for col_i, cell in enumerate(row.cells):
                    field_id = f"T{table_i}[{row_i},{col_i}]"
                    if field_id in field_values:
                        val = field_values[field_id]
                        if val is not None and str(val).strip():
                            cell.text = str(val)

    def resolve_auto_fields(self, meta, field_values, context=None):
        """
        解析 auto:* 和 calculated:* 来源的字段值
        context: {
            'session': {'name': ..., 'dept': ...},
            'invoices': [...],   # [{'invoice_type': ..., 'amount': ...}]
            'payments': [...],
        }
        """
        context = context or {}
        resolved = dict(field_values)
        session = context.get('session', {})

        def resolve_source(source, field_id):
            if source == 'user_input' or source is None:
                return field_values.get(field_id)
            if source.startswith('session.'):
                key = source.split('.', 1)[1]
                return session.get(key, field_values.get(field_id))
            if source.startswith('auto:'):
                auto_type = source.split(':', 1)[1]
                if auto_type == 'current_date':
                    return datetime.now().strftime('%Y年%m月%d日')
                if auto_type == 'index':
                    return '1'
                return field_values.get(field_id)
            if source.startswith('calculated:'):
                calc = source.split(':', 1)[1]
                # amount_to_cn: 计算申报金额大写（取所有 amount 字段之和）
                if calc == 'amount_to_cn':
                    total = 0.0
                    for fid, val in field_values.items():
                        if '金额' in meta['fields_dict'].get(fid, {}).get('label', '') or '申报金额' in fid:
                            total += _parse_amount(val)
                    return _cn_number(total)
                # sum_all: 所有申报金额字段之和
                if calc == 'sum_all':
                    total = 0.0
                    for fid, val in field_values.items():
                        lbl = meta['fields_dict'].get(fid, {}).get('label', '')
                        if '申报金额' in lbl and '核准' not in lbl:
                            total += _parse_amount(val)
                    return f'{total:.2f}'
                # days×standard: 天数 × 标准
                if '×' in calc or '*' in calc:
                    parts = re.split(r'[×*]', calc)
                    if len(parts) == 2:
                        days_fid, std_fid = parts[0].strip(), parts[1].strip()
                        days = _parse_amount(field_values.get(days_fid, 0))
                        std = _parse_amount(field_values.get(std_fid, 0))
                        return f'{days * std:.2f}'
                # days_from_dates: 计算天数（结束日期 - 开始日期 + 1）
                if calc == 'days_from_dates':
                    # 找开始日期和结束日期字段
                    start_val, end_val = '', ''
                    for fid, val in field_values.items():
                        lbl = meta['fields_dict'].get(fid, {}).get('label', '')
                        if '开始日期' in lbl:
                            start_val = val
                        elif '结束日期' in lbl:
                            end_val = val
                    if start_val and end_val:
                        try:
                            s = datetime.strptime(start_val, '%Y-%m-%d')
                            e = datetime.strptime(end_val, '%Y-%m-%d')
                            days = (e - s).days + 1
                            return str(max(1, days))
                        except (TypeError, ValueError):
                            pass
                    return '1'
                return field_values.get(field_id)
            if source.startswith('fixed:'):
                # fixed:100 — 返回固定值
                return source.split(':', 1)[1]
            if source.startswith('invoice:'):
                # invoice:类型.amount_sum
                parts = source.split(':')
                if len(parts) == 2:
                    inv_type, field = parts[1].split('.')
                    invoices = context.get('invoices', [])
                    matched = [i for i in invoices if inv_type in i.get('invoice_type', '')]
                    if field == 'amount_sum':
                        total = sum(_parse_amount(i.get('amount', 0)) for i in matched)
                        return f'{total:.2f}'
                    if field == 'count':
                        return str(len(matched))
            if source.startswith('payment:'):
                parts = source.split(':')
                if len(parts) == 2:
                    pay_type, field = parts[1].split('.')
                    payments = context.get('payments', [])
                    matched = [p for p in payments if pay_type in p.get('type', '')]
                    if field == 'amount_sum':
                        total = sum(_parse_amount(p.get('amount', 0)) for p in matched)
                        return f'{total:.2f}'
                    if field == 'count':
                        return str(len(matched))
            return field_values.get(field_id)

        # 构建 fields_dict 方便查找
        meta['fields_dict'] = {f['field_id']: f for f in meta.get('fields', [])}

        for field in meta.get('fields', []):
            fid = field['field_id']
            source = field.get('source', '')
            if source not in ('user_input', None, '') and fid not in resolved:
                resolved[fid] = resolve_source(source, fid)

        return resolved

    # ----------------------------------------------------------
    # 生成文档
    # ----------------------------------------------------------

    def generate(self, doc_type, field_values, context=None):
        """
        生成填充后的 docx，返回字节串

        Args:
            doc_type: 模板类型（目录名）
            field_values: {field_id: value} 字典
            context: {'session': {...}, 'invoices': [...], 'payments': [...]}
        """
        meta, doc = self.load_template(doc_type)
        table_index = meta.get('table_index')

        # 解析自动字段
        resolved = self.resolve_auto_fields(meta, field_values, context)

        # 填充单元格
        self.fill(doc, resolved, table_index=table_index)

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def get_preview(self, doc_type, max_rows=20):
        """
        获取模板表格预览（用于页面展示字段位置）
        返回列表: [{field_id, row, col, text}]
        """
        _, doc = self.load_template(doc_type)
        meta_path = self.templates_dir / doc_type / 'template.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        table_index = meta.get('table_index', 0)
        result = []
        if table_index < len(doc.tables):
            table = doc.tables[table_index]
            for row_i, row in enumerate(table.rows):
                if row_i >= max_rows:
                    break
                for col_i, cell in enumerate(row.cells):
                    text = cell.text.strip().replace('\n', ' ')
                    if text:
                        result.append({
                            'field_id': f"T{table_index}[{row_i},{col_i}]",
                            'row': row_i,
                            'col': col_i,
                            'text': text[:50],
                        })
        return result


# ----------------------------------------------------------
# 辅助：获取可用发票类型列表
# ----------------------------------------------------------

def get_invoice_types():
    """返回系统支持的发票类型（用于 source 字段匹配）"""
    return [
        '增值税专用发票',
        '增值税普通发票',
        '定额发票',
        '出租车票',
        '航空行程单',
        '火车票',
        '电子发票',
        '其他',
    ]
