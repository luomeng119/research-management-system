# -*- coding: utf-8 -*-
"""
报销助手工具函数
"""
import re
from datetime import datetime

def amount_to_cn(amount):
    """将数字金额转换为中文大写"""
    if not amount or amount == 0:
        return '零元整'

    num_map = ['零', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖']
    unit_map = ['', '拾', '佰', '仟', '万', '拾', '佰', '仟', '亿', '拾', '佰', '仟']

    # 处理负数
    negative = False
    if amount < 0:
        negative = True
        amount = abs(amount)

    # 整数和小数部分
    integer_part = int(amount)
    decimal_part = round((amount - integer_part) * 100)  # 保留两位小数

    if integer_part == 0 and decimal_part == 0:
        return '零元整'

    result = ''
    integer_str = str(integer_part)
    length = len(integer_str)

    i = 0
    while i < length:
        digit = int(integer_str[i])
        if digit != 0:
            result += num_map[digit] + unit_map[length - i - 1]
        elif result and result[-1] != '零':
            result += '零'
        i += 1

    # 去掉末尾多余的零
    result = result.rstrip('零')
    if not result:
        result = '零'

    # 添加整数部分单位
    if result.endswith(('元', '万', '亿')):
        pass
    else:
        result += '元'

    # 处理小数部分
    if decimal_part > 0:
        jiao = decimal_part // 10
        fen = decimal_part % 10
        if jiao > 0:
            result += num_map[jiao] + '角'
        if fen > 0:
            result += num_map[fen] + '分'
    else:
        result += '整'

    if negative:
        result = '负' + result

    return result


def generate_reimbursement_no():
    """生成报销项编号：REI+YYYYMMDD+3位序号（注意：序号由调用方管理）"""
    return 'REI' + datetime.now().strftime('%Y%m%d')


def generate_payment_no():
    """生成支付记录编号：PAY+YYYYMMDD+3位序号"""
    return 'PAY' + datetime.now().strftime('%Y%m%d')


def parse_date(date_str):
    """将各种日期格式标准化为 YYYY-MM-DD"""
    if not date_str:
        return ''
    # 去掉空格
    date_str = date_str.strip()
    # 替换年月日分隔符
    date_str = re.sub(r'年|月|日', '-', date_str)
    # 去掉多余的零
    date_str = re.sub(r'-+', '-', date_str)
    date_str = date_str.strip('-')
    # 补全不完整日期
    parts = date_str.split('-')
    if len(parts) == 3:
        year, month, day = parts
        year = year.zfill(4)
        month = month.zfill(2)
        day = day.zfill(2)
        return f'{year}-{month}-{day}'
    elif len(parts) == 2:
        year, month = parts
        return f'{year.zfill(4)}-{month.zfill(2)}-01'
    elif len(parts) == 1 and len(parts[0]) == 8:
        # 纯8位数字
        return f'{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]}'
    return date_str


def parse_amount(amount_str):
    """从字符串中提取金额数字"""
    if not amount_str:
        return 0.0
    amount_str = str(amount_str).strip()
    # 去掉人民币符号和空格（一次即可）
    amount_str = re.sub(r'[￥¥,，元]', '', amount_str)
    parts = amount_str.split()
    amount_str = ''.join(p for p in parts if p)
    # 提取数字
    match = re.search(r'[\d]+\.?[\d]*', amount_str)
    if match:
        try:
            return round(float(match.group()), 2)
        except (ValueError, AttributeError):
            return 0.0
    return 0.0


def normalize_invoice_no(text):
    """从 OCR 文本中提取发票号码"""
    text = text.strip()
    # 优先找"发票号码："后面的大数字
    patterns = [
        r'发票号码[：:]\s*(\d{10,20})',
        r'发票号[码]?[：:]\s*(\d{10,20})',
        r'No\.?\s*[:：]?\s*(\d{10,20})',
        r'(?<!\d)\d{18,20}(?!\d)',  # 18-20位纯数字
        r'(?<!\d)\d{12}(?!\d)',       # 12位纯数字
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ''


def normalize_seller_name(text):
    """提取销售方名称"""
    patterns = [
        r'销售方[：:]\s*([^\n电合税总]{2,30})',
        r'销货单位[：:]\s*([^\n电合税总]{2,30})',
        r'销售商家[：:]\s*([^\n电合税总]{2,30})',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1).strip()
    return ''


def normalize_buyer_name(text):
    """提取购买方名称"""
    patterns = [
        r'购买方[：:]\s*([^\n电合税总]{2,30})',
        r'购货单位[：:]\s*([^\n电合税总]{2,30})',
        r'购买商家[：:]\s*([^\n电合税总]{2,30})',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1).strip()
    return ''
