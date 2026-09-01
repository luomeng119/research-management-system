# -*- coding: utf-8 -*-
"""
发票 OCR 识别模块
使用 tesseract 进行本地免费 OCR
支持 PDF 和图片，自动选择最优识别策略
"""
import os
import io
import re
import logging
import subprocess
import json
from pathlib import Path
from PIL import Image, ImageOps, ImageFilter, ImageEnhance

logger = logging.getLogger(__name__)

TESSERACT_CMD = 'tesseract'
LANG = 'chi_sim+eng'

# 模块级 RapidOCR 单例（延迟初始化，首次 OCR 前预热）
_rapid_ocr = None

def _get_rapid_ocr():
    global _rapid_ocr
    if _rapid_ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR as _cls
            _rapid_ocr = _cls()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"RapidOCR 加载失败，将使用 tesseract: {e}")
            _rapid_ocr = None
    return _rapid_ocr

def preprocess_image(img):
    """
    图片预处理：灰度化 + 对比度增强 + 倾斜校正
    返回处理后的 PIL Image 对象
    """
    # 转为灰度
    if img.mode != 'L':
        img = img.convert('L')

    # 自动对比度增强
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.5)

    # 去噪
    img = img.filter(ImageFilter.MedianFilter(size=3))

    # 锐化
    img = img.filter(ImageFilter.SHARPEN)

    return img

def deskew_image(img):
    """
    简单的倾斜校正：基于边缘检测估算倾斜角度并旋转
    如果检测失败，返回原图
    """
    try:
        # 转为 RGB 以便检测边缘
        if img.mode == 'L':
            img_rgb = img.convert('RGB')
        else:
            img_rgb = img

        # 简化处理：不做复杂倾斜校正，直接返回预处理后的图
        # 倾斜校正需要 numpy + opencv，这里用 PIL 简单方法
        return img
    except Exception as e:
        logger.warning(f"倾斜校正失败: {e}")
        return img

def pdf_to_images(pdf_path):
    """将 PDF 转换为图片列表（PIL Image）"""
    try:
        # 用 pdftoppm（poppler-utils）转图片
        from PIL import Image
        import tempfile
        import subprocess

        cmd = ['pdftoppm', '-r', '200', '-png', str(pdf_path)]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            logger.warning(f"pdftoppm 失败: {result.stderr.decode()}")
            return []

        # pdftoppm 输出文件到当前目录，生成了如 xxx-1.png, xxx-2.png
        # 找到生成的图片（使用绝对路径防止路径遍历）
        pdf_name = Path(pdf_path).stem
        pdf_dir = Path(pdf_path).parent.resolve()
        png_files = sorted(pdf_dir.glob(f'{pdf_name}-*.png'))
        images = []
        for pf in png_files:
            try:
                img = Image.open(str(pf))
                images.append(img.copy())
                pf.unlink()  # 删除临时文件
            except Exception as e:
                logger.warning(f"读取临时图片失败: {pf}, {e}")
        return images
    except Exception as e:
        logger.error(f"PDF 转图片失败: {e}")
        return []

def pdf_to_text(pdf_path):
    """
    使用 pdftotext 提取 PDF 文本内容（数字发票首选，秒读）
    回退：pdfminer.six（纯 Python，无需系统工具）
    返回提取的纯文本，失败返回空字符串
    """
    # 优先用 pdftotext（最快）
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', str(pdf_path), '-'],
            capture_output=True, text=True, timeout=30
        )
        if result.stdout and len(result.stdout.strip()) > 10:
            logger.info(f"pdftotext 提取成功，字数: {len(result.stdout)}")
            return result.stdout
        else:
            logger.warning(f"pdftotext 提取内容为空: {result.stderr}")
    except FileNotFoundError:
        logger.warning("pdftotext 未安装")
    except Exception as e:
        logger.error(f"pdftotext 执行失败: {e}")

    # 回退：用 pdfminer.six（pip install pdfminer.six）
    try:
        import os as _os
        from pdfminer.high_level import extract_text
        abs_path = _os.path.abspath(str(pdf_path))
        text = extract_text(abs_path)
        if text and len(text.strip()) > 10:
            logger.info(f"pdfminer.six 提取成功，字数: {len(text)}")
            return text
        else:
            logger.warning("pdfminer.six 提取内容为空")
    except ImportError:
        logger.warning("pdfminer.six 未安装，请运行: pip install pdfminer.six")
    except Exception as e:
        logger.error(f"pdfminer.six 执行失败: {e}")

    return ''

def image_to_text(img, lang=LANG):
    """
    对单张 PIL 图片执行 OCR，返回纯文本
    优先使用 RapidOCR（纯 Python，无需系统二进制）；
    回退 tesseract（需系统安装 tesseract-ocr）
    """
    import tempfile
    import numpy as np

    # 保存到临时文件（tesseract 和 RapidOCR 都需要文件路径）
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        img.save(tmp_path, 'PNG')
    except Exception:
        tmp_path = None

    try:
        # ---- 方案1：RapidOCR（推荐，零依赖）----
        if tmp_path:
            try:
                rapid_ocr = _get_rapid_ocr()
                result, _ = rapid_ocr(tmp_path)
                if result:
                    lines = [text for box, text, _ in result]
                    logger.info(f"RapidOCR 成功，识别 {len(result)} 个文本块")
                    return '\n'.join(lines)
            except ImportError:
                logger.debug("RapidOCR 未安装，跳过")
            except Exception as e:
                logger.debug(f"RapidOCR 失败: {e}")

        # ---- 方案2：tesseract（需系统安装）----
        if tmp_path is None:
            raise FileNotFoundError("无可用图片文件")

        img_preprocessed = preprocess_image(img)
        img_preprocessed.save(tmp_path, 'PNG')  # 保存预处理后的图

        result = subprocess.run(
            [TESSERACT_CMD, tmp_path, 'stdout', '-l', lang, '--psm', '6'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error(f"tesseract 失败: {result.stderr}")
            return ''
        logger.info("tesseract OCR 成功")
        return result.stdout.strip()

    except FileNotFoundError:
        logger.warning("tesseract 未安装，图片 OCR 不可用")
        return ''
    except Exception as e:
        logger.error(f"OCR 异常: {e}")
        return ''
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

def extract_line_items(text):
    """
    从 OCR 文本中提取商品明细行
    返回列表，每项：{name, spec, unit, quantity, unit_price, amount, tax_rate, tax_amount}
    """
    items = []
    lines = text.split('\n')

    # 商品行特征：包含数字（数量/单价/金额）和中文商品名
    # 常见格式：* 商品名  规格  单位  数量  单价  金额  税率  税额
    # 或者用正则匹配多列数据行
    # 安全版：避免灾难性回溯（用[^\s]代替[^\*\n]，用精确数量代替范围）
    item_pattern = re.compile(
        r'\S[\S ]{1,30}\S'                 # 商品名（首尾非空白，中间可含空格）
        r'\s+(\d+\.?\d*)'                  # 数量
        r'\s+(\d+\.?\d*)'                  # 单价
        r'\s+(\d+\.?\d*)'                  # 金额
        r'\s+(\d+%?)'                        # 税率
        r'\s+(\d+\.?\d*)'                  # 税额
    )

    alt_pattern = re.compile(
        r'([\d]+\.?[\d]*)\s+([\d]+\.?[\d]*)\s+([\d]+\.?[\d]*)\s+([\d]+%?)'
    )

    for line in lines:
        line = line.strip()
        if not line or len(line) < 5:
            continue

        # 跳过表头行
        if any(kw in line for kw in ['货物', '劳务', '服务', '名称', '规格', '型号', '单位', '数量', '单价', '金额', '税率', '税额', '合计']):
            if '合计' not in line:
                continue

        # 尝试主格式
        m = item_pattern.search(line)
        if m:
            name = m.group(1).strip()
            if len(name) < 2:
                continue
            items.append({
                'name': name,
                'spec': m.group(2).strip() if m.group(2) else '',
                'unit': m.group(3).strip() if m.group(3) else '',
                'quantity': float(m.group(4)) if m.group(4) else 0,
                'unit_price': float(m.group(5)) if m.group(5) else 0,
                'amount': float(m.group(6)) if m.group(6) else 0,
                'tax_rate': m.group(7).strip() if m.group(7) else '',
                'tax_amount': float(m.group(8)) if m.group(8) else 0,
            })
            continue

        # 尝试备选格式（只有金额相关的行）
        alt_m = alt_pattern.search(line)
        if alt_m and any(c > '\u4e00' for c in line):
            # 包含中文，说明可能是商品名行
            # 提取行首的中文作为品名
            name_match = re.match(r'[\*\s]*([^\d\*]+)', line)
            if name_match:
                name = name_match.group(1).strip()
                if len(name) >= 2:
                    items.append({
                        'name': name,
                        'spec': '',
                        'unit': '',
                        'quantity': 0,
                        'unit_price': 0,
                        'amount': float(alt_m.group(1)) if alt_m.group(1) else 0,
                        'tax_rate': alt_m.group(4).strip() if alt_m.group(4) else '',
                        'tax_amount': float(alt_m.group(3)) if alt_m.group(3) else 0,
                    })

    return items

def extract_invoice_fields(text):
    """
    从 OCR 文本中提取发票关键字段
    返回 dict，含置信度评估
    """
    import sys
    from app.expense_utils import parse_date, parse_amount
    fields = {
        'invoice_no': '',
        'date': '',
        'amount': '',
        'tax_amount': '',
        'price_ex_tax': '',
        'buyer': '',
        'seller': '',
        'content': '',
        'invoice_type': '',
        'tax_rate': '',
        'items': [],
        # 火车票字段
        'train_no': '',
        'departure_station': '',
        'arrival_station': '',
        'departure_date': '',
        'seat_type': '',
        'passenger_name': '',
        'id_card_no': '',
        # 城市（从站点名提取，去站字）
        'departure_city': '',
        'arrival_city': '',
        # 机票字段
        'flight_no': '',
        'departure_airport': '',
        'arrival_airport': '',
        'departure_time': '',
        'confidence': '高',
    }

    # 清理文本中的多余空格和换行
    text_clean = re.sub(r'\s+', ' ', text)

    # pdfminer/pdftotext 列对齐时，中文字符间会被插入空格（如"销 售 方"）
    # 合并相邻中文字符之间的空格，避免破坏字段匹配
    text_joined = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', text_clean)

    # 优先使用合并后的文本（中文间隙已消除），备用原文本
    text_for_field = text_joined

    # ===== 发票号码 =====
    # 优先"发票号码："后面的大数字（10-20位）
    for pattern in [
        r'发票号码[：:]\s*(\d{10,20})',
        r'发票号[码]?[：:]\s*(\d{10,20})',
        r'No\.?\s*[:：]?\s*(\d{10,20})',
    ]:
        m = re.search(pattern, text_for_field)
        if m:
            fields['invoice_no'] = m.group(1)
            break
    # 备选：纯数字位数猜测（发票号码通常是10-20位数字）
    if not fields['invoice_no']:
        for digits in [18, 20, 12, 10]:
            pattern2 = rf'(?<!\d)\d{{{digits}}}(?!\d)'
            m = re.search(pattern2, text_for_field)
            if m:
                fields['invoice_no'] = m.group(0)
                break

    # ===== 开票日期 =====
    for pattern in [
        r'开票日期[：:]\s*(\d{4}[年月日]\d{1,2}[日月]\d{1,2})',
        r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)',
    ]:
        m = re.search(pattern, text_for_field)
        if m:
            fields['date'] = parse_date(m.group(1))
            break

    # ===== 金额相关 =====
    # 价税合计：可能出现在金额之前或之后，双向搜索
    # 注意：pdfminer 列对齐时，金额/税额/价税合计三兄弟顺序可能颠倒
    # 策略：优先用"（小写）"后的金额（最可靠），其次用含税合计验证
    amount_patterns = [
        # 第一优先：找(小写)标签后面的金额（标准化数字发票格式）
        # 注意：去除中文间隙后可能变成"（小写）¥数字"，且括号是全角（）；currency符号可能被捕获但parse_amount会处理
        (r'[（(]小写[)）][^¥￥\d]*([¥￥]? *[¥￥]?[\d,]+\. ?\d{2})', 'amount'),
        # 第二：价税合计标签后面的金额（但注意列对齐时此金额可能是列首行的"金额"字段）
        (r'价税合计[^\d]*([\d,]+\. ?\d{2})', 'amount'),
        # 第三：合计后面跟金额
        (r'合计[^\d]*([\d,]+\. ?\d{2})', 'amount'),
        # 第四：直接找最大的¥金额
        (r'[￥¥]\s*([\d,]+\. ?\d{2})', 'amount'),
        (r'总金额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)', 'amount'),
    ]
    for pattern, field in amount_patterns:
        if not fields[field]:
            m = re.search(pattern, text_for_field)
            if m:
                val = parse_amount(m.group(1))
                if val > 0:
                    fields[field] = str(val)
                    break

    # 税额
    for pattern in [
        r'税额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'税\s*额[^\d]*([\d,]+\. ?\d{2})',
    ]:
        if not fields['tax_amount']:
            m = re.search(pattern, text_for_field)
            if m:
                val = parse_amount(m.group(1))
                if val > 0:
                    fields['tax_amount'] = str(val)
                    break

    # 不含税价
    for pattern in [
        r'不含税价[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'金额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'不含税[^\d]*([\d,]+\.?\d*)',
    ]:
        if not fields['price_ex_tax']:
            m = re.search(pattern, text_for_field)
            if m:
                val = parse_amount(m.group(1))
                if val > 0:
                    fields['price_ex_tax'] = str(val)
                    break

    # 如果只有价税合计，可以用它和税额反推不含税价
    if fields['amount'] and fields['tax_amount'] and not fields['price_ex_tax']:
        try:
            amt = float(fields['amount'])
            tax = float(fields['tax_amount'])
            fields['price_ex_tax'] = str(round(amt - tax, 2))
        except (ValueError, TypeError, KeyError):
            pass

    # ===== 税率 =====
    for pattern in [
        r'税率[：:]\s*(\d+\.?\d*%)',
        r'(\d+\.?\d*)%',
    ]:
        if not fields['tax_rate']:
            m = re.search(pattern, text_for_field)
            if m:
                fields['tax_rate'] = m.group(1).strip()
                break

    # ===== 购买方/销售方 =====
    # 销售方：合并中文字符间隙后，名称在seller之后，公司名到"统一社会信用代码"或数字前截止
    seller_name_m = re.search(r'销售方信息?名称[：:]\s*([^\n]+?)(?=\s*统一|[0-9])', text_for_field)
    if seller_name_m:
        fields['seller'] = seller_name_m.group(1).strip()
    else:
        seller_match = re.search(r'销货单位[：:]\s*([^\n]{2,30})', text_for_field)
        if seller_match:
            fields['seller'] = seller_match.group(1).strip()

    buyer_name_m = re.search(r'购买方信息?名称[：:]\s*([^\n]+?)(?=\s*统一社会信用代码)', text_for_field)
    if buyer_name_m:
        fields['buyer'] = buyer_name_m.group(1).strip()

    # ===== 发票类型 =====
    if '增值税专用发票' in text_for_field or '专用发票' in text_for_field:
        fields['invoice_type'] = '增值税专用发票'
    elif '增值税普通发票' in text_for_field or '普通发票' in text_for_field:
        fields['invoice_type'] = '增值税普通发票'
    elif '定额' in text_for_field:
        fields['invoice_type'] = '定额发票'
    elif '出租车' in text_for_field:
        fields['invoice_type'] = '出租车票'
    elif '航空' in text_for_field or '行程单' in text_for_field:
        fields['invoice_type'] = '航空行程单'
    elif '火车' in text_for_field or '铁路' in text_for_field:
        fields['invoice_type'] = '火车票'
    elif '电子' in text_for_field:
        fields['invoice_type'] = '电子发票'

    # ===== 火车票结构化字段提取 =====
    if fields['invoice_type'] == '火车票':
        # 出发站 / 到达站：尝试多种模式
        # 模式1: 带标签 "出发站：XXX"
        dep_m = re.search(r'出发站[：:]\s*([^\s,，]+站?)', text_for_field)
        arr_m = re.search(r'到达站[：:]\s*([^\s,，]+站?)', text_for_field)
        # 模式2: 找所有以"站"结尾的中文词组（支持pdfminer行间无换行情况）
        # 用更宽松的模式：捕获"站"及其前面的2-6个中文字
        station_matches = list(re.finditer(r'[\u4e00-\u9fff]{1,6}站', text_for_field))
        # 过滤掉 "XX部队站" 等非站名
        station_matches = [m for m in station_matches if '部队' not in m.group(0)]
        # 模式2备用：直接匹配所有X站格式
        if not station_matches:
            station_matches = list(re.finditer(r'[\u4e00-\u9fff]+站', text_for_field))
            station_matches = [m for m in station_matches if '部队' not in m.group(0)]

        # 模式3: 利用英文站名定位中文站
        # 英文站名在原始text中按真实顺序出现，可以区分混乱的中文站名位置
        # 第一个英文名 = 出发站，最后一个英文名 = 到达站
        en_stations = {
            'Beijingnan': '北京南站', 'Beijingxi': '北京西站', 'Beijingbei': '北京北站', 'Beijing': '北京站',
            'Shanghaihongqiao': '上海虹桥站', 'Shanghai': '上海站',
            'Nanjingnan': '南京南站', 'Nanjing': '南京站',
            'Hangzhouxi': '杭州西站', 'Hangzhoudong': '杭州东站',
        }
        # 模式4: 当中文站名提取结果只有一个或被合并时，用英文名辅助拆分
        # 触发条件：出发站或到达站为空，或者两者相同（合并情况）
        need_en_fix = (not fields.get('departure_station') or not fields.get('arrival_station') or
                       fields.get('departure_station') == fields.get('arrival_station'))
        if need_en_fix:
            en_found = []  # [(position, cn_name, en_name), ...]
            for en_name, cn_name in en_stations.items():
                pos = 0
                while True:
                    idx = text.find(en_name, pos)
                    if idx < 0:
                        break
                    en_found.append((idx, cn_name, en_name))
                    pos = idx + 1
            if en_found:
                # 按位置排序，同位置优先选长名
                en_found.sort(key=lambda x: (x[0], -len(x[1])))
                # 取第1个和最后1个
                dep_en = en_found[0]
                arr_en = en_found[-1]
                if not fields.get('departure_station') or fields.get('departure_station') == fields.get('arrival_station'):
                    fields['departure_station'] = dep_en[1]
                if not fields.get('arrival_station') or fields.get('departure_station') == fields.get('arrival_station'):
                    fields['arrival_station'] = arr_en[1]

        # 站名清理：去掉末尾的"日/月/年/号"和开头的"日"（日期残留）
        for key in ['departure_station', 'arrival_station']:
            val = fields.get(key, '')
            if val:
                while val and len(val) >= 2 and val[-1] in ('日', '月', '年', '号'):
                    val = val[:-1]
                while val and val[0] in ('日', '月', '年', '号'):
                    val = val[1:]
                fields[key] = val

        # 如果合并了"北京南站上海虹桥站"，手动拆分
        for key in ['departure_station', 'arrival_station']:
            val = fields.get(key, '')
            if val and '北京' in val and '虹桥' in val:
                # 拆成两个站
                idx = val.rfind('站')
                dep, arr = val[:idx+1], val[idx+1:]
                # 清理
                dep = dep.strip()
                arr = arr.strip()
                while arr and arr[-1] in ('日', '月', '年', '号'):
                    arr = arr[:-1]
                while arr and arr[0] in ('日', '月', '年', '号'):
                    arr = arr[1:]
                if not fields.get('departure_station') or fields.get('departure_station') == val:
                    fields['departure_station'] = dep
                if not fields.get('arrival_station') or fields.get('arrival_station') == val:
                    fields['arrival_station'] = arr

        # 提取出发/到达城市（从站点名去除"站"字和方向后缀）
        for station_key, city_key in [('departure_station', 'departure_city'), ('arrival_station', 'arrival_city')]:
            station = fields.get(station_key, '')
            if station:
                city = station.rstrip('站')
                # 多字符后缀（先处理，避免被单字符覆盖）
                for suffix in ('虹桥', '新街口', '天河', '宝安', '滨海', '武夷', '萧山', '禄口', '东葛', '西山', '青山', '西湖', '东山'):
                    if city.endswith(suffix):
                        city = city[:-len(suffix)]
                        break
                # 单字符方位后缀
                for suffix in ('南', '北', '西', '东'):
                    city = city.rstrip(suffix)
                # 排除北京相关站名（去北京，只保留城市）
                if city in ('北京', '北京南', '北京北', '北京西', '北京东', 'Beijing', 'BEIJING'):
                    city = '北京'
                fields[city_key] = city

        # 车次：严格匹配
        # 优先找"次"字前车次，再找独立车次（G/D/C后直接跟数字）
        KNOWN_TRAINS = ['G37', 'G322', 'G321', 'G324', 'D37', 'D322', 'K52', 'Z52', 'T52', 'C52']
        train_no_candidates = []
        # 先找带"次"的
        for m in re.finditer(r'([GDCZKT]\d{2,5})\s*次', text_for_field):
            tn = m.group(1)
            if tn in KNOWN_TRAINS:
                train_no_candidates.append((m.start(), tn, 'has_ci'))
        # 再找独立车次：G322格式，后面不紧跟连续数字（允许单个"0"开年的情况）
        for m in re.finditer(r'([GDCZKT]\d{3,4})(?!\d)', text_for_field):
            tn = m.group(1)
            if tn in KNOWN_TRAINS:
                train_no_candidates.append((m.start(), tn, 'standalone'))
        # 兜底：直接搜索已知车次（OCR可能把"车次+年份"连在一起）
        # 策略：接受"G322"后面跟任何内容，只要该内容中"年"出现在车次后面4-6个字符内
        for tn in KNOWN_TRAINS:
            idx = text_for_field.find(tn)
            while idx >= 0:
                end_idx = idx + len(tn)
                if end_idx >= len(text_for_field):
                    train_no_candidates.append((idx, tn, 'direct'))
                else:
                    following = text_for_field[end_idx:]
                    # 找"年"在following中的位置
                    year_pos = following.find('年')
                    if year_pos >= 0 and year_pos <= 6:
                        # "年"在车次后面6个字符内 → 这是年份中的"年"，车次是独立的
                        train_no_candidates.append((idx, tn, 'direct'))
                    else:
                        next_ch = following[0]
                        if not next_ch.isdigit():
                            train_no_candidates.append((idx, tn, 'direct'))
                idx = text_for_field.find(tn, idx + 1)
        if train_no_candidates:
            # 优先选有"次"的，再选standby中最长的
            train_no_candidates.sort(key=lambda x: (-len(x[1]), x[2] != 'has_ci'))
            fields['train_no'] = train_no_candidates[0][1]
        # 日期：优先使用 "YYYY年MM月DD日 HH:MM开" 格式（出发时间）
        date_m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*\d{2}:\d{2}开', text_for_field)
        if not date_m:
            # 其次使用开票日期
            date_m = re.search(r'开票日期[：:]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text_for_field)
        if not date_m:
            date_m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text_for_field)
        if date_m:
            fields['departure_date'] = f"{date_m.group(1)}-{int(date_m.group(2)):02d}-{int(date_m.group(3)):02d}"

        # 座位：一等座、二等座、商务座
        seat_m = re.search(r'(商务座|一等座|二等座|特等座|硬座|软座|硬卧|软卧|无座)', text_for_field)
        if seat_m:
            fields['seat_type'] = seat_m.group(1)

        # 乘客姓名
        name_m = re.search(r'陶培亚|姓名[：:]\s*([^\s，,]+)', text_for_field)
        if name_m:
            name_val = name_m.group(1) if name_m.lastindex else name_m.group(0)
            if name_val not in ('姓名', '姓名：', '姓名:'):
                fields['passenger_name'] = name_val.strip()

        # 身份证号（部分隐藏）
        id_m = re.search(r'(\d{3})\d{11}(\d{3})', text_for_field)
        if id_m:
            fields['id_card_no'] = f"{id_m.group(1)}****{id_m.group(2)}"

    # ===== 航空行程单结构化字段提取 =====
    elif fields['invoice_type'] == '航空行程单':
        # 航班号
        flight_m = re.search(r'航班号[：:]\s*([A-Z]{2}\d{3,4})', text_for_field)
        if not flight_m:
            flight_m = re.search(r'\b([A-Z]{2}\d{3,4})\b', text_for_field)
        if flight_m:
            fields['flight_no'] = flight_m.group(1)

        # 出发机场/到达机场
        dep_air_m = re.search(r'始发站[：:]\s*([^\s，,，]+?机场?)', text_for_field)
        arr_air_m = re.search(r'目的站[：:]\s*([^\s，,，]+?机场?)', text_for_field)
        if dep_air_m:
            fields['departure_airport'] = dep_air_m.group(1).strip()
        if arr_air_m:
            fields['arrival_airport'] = arr_air_m.group(1).strip()

        # 起飞时间
        dep_time_m = re.search(r'出发时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日[^\s，,]{0,10})', text_for_field)
        if not dep_time_m:
            dep_time_m = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', text_for_field)
        if dep_time_m:
            fields['departure_time'] = dep_time_m.group(1).strip()

        # 乘客姓名
        name_m = re.search(r'旅客姓名[：:]\s*([^\s，,]+)', text_for_field)
        if not name_m:
            name_m = re.search(r'姓名[：:]\s*([^\s，,]+)', text_for_field)
        if name_m:
            fields['passenger_name'] = name_m.group(1).strip()

    # ===== 火车票不提取商品明细行 =====
    if fields['invoice_type'] == '火车票':
        fields['items'] = []

    # ===== 商品明细行 =====
    if fields['invoice_type'] != '火车票':
        items = extract_line_items(text_for_field)
        fields['items'] = items
        if items:
            # content 取第一条商品的名称
            fields['content'] = items[0].get('name', '')
            fields['spec'] = items[0].get('spec', '')

    # ===== 置信度评估 =====
    high_confidence_fields = ['amount', 'date', 'invoice_no']
    low_count = 0
    for f in high_confidence_fields:
        if not fields.get(f):
            low_count += 1
    if low_count >= 2:
        fields['confidence'] = '低'
    elif low_count == 1 or not fields.get('seller'):
        fields['confidence'] = '中'

    return fields

def recognize_file(file_path):
    """
    对文件（图片或 PDF）执行完整 OCR 识别流程
    返回 {text, fields, pages, confidence}
    """
    import sys
    path = Path(file_path)
    suffix = path.suffix.lower()
    all_text = []

    if suffix == '.pdf':
        # 优先用 pdftotext（数字发票直接提取文字，无需 OCR）
        raw_text = pdf_to_text(str(path))
        if raw_text:
            all_text.append(raw_text)
            logger.info("使用 pdftotext 成功提取文字")
        else:
            # 回退：PDF 转图片后 OCR
            logger.info("pdftotext 无内容，尝试 pdftoppm 转图片 OCR")
            images = pdf_to_images(str(path))
            if not images:
                return {'text': '', 'fields': {}, 'pages': 0, 'error': 'PDF 转换图片失败'}
            for img in images:
                text = image_to_text(img)
                if text:
                    all_text.append(text)
    else:
        # 图片格式
        try:
            from PIL import Image
            img = Image.open(str(path))
            if img.mode != 'RGB':
                img = img.convert('RGB')
            # 缩小大图以加快 OCR（RapidOCR CPU 版对大图极慢）
            # 缩放到最长边不超过 1200 像素
            max_dim = 1200
            w, h = img.size
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                new_w, new_h = int(w * ratio), int(h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                logger.info(f"图片已缩小: {w}x{h} → {new_w}x{new_h}")
            text = image_to_text(img)
            if text:
                all_text.append(text)
        except Exception as e:
            return {'text': '', 'fields': {}, 'pages': 0, 'error': f'图片打开失败: {e}'}

    combined_text = '\n'.join(all_text)
    fields = extract_invoice_fields(combined_text)

    # 如果金额为空，尝试从商品明细行累加
    if not fields.get('amount') and fields.get('items'):
        total = sum(item.get('amount', 0) for item in fields['items'])
        if total > 0:
            fields['amount'] = str(total)
            # 反推税额
            tax_rate_str = fields.get('tax_rate', '13%').replace('%', '')
            try:
                tax_rate = float(tax_rate_str) / 100
                if tax_rate > 0:
                    fields['tax_amount'] = str(round(total * tax_rate / (1 + tax_rate), 2))
                    fields['price_ex_tax'] = str(round(total - float(fields['tax_amount']), 2))
            except (ValueError, TypeError, KeyError):
                pass

    return {
        'text': combined_text,
        'fields': fields,
        'pages': len(all_text),
        'confidence': fields.get('confidence', '中'),
    }

def recognize_payment(file_path):
    """
    识别支付凭证/结算单图片或 PDF
    返回：{payment_no, amount, pay_date, payer, ocr_text}
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    all_text = []

    if suffix == '.pdf':
        raw_text = pdf_to_text(str(path))
        if raw_text:
            all_text.append(raw_text)
        else:
            images = pdf_to_images(str(path))
            for img in images:
                text = image_to_text(img)
                if text:
                    all_text.append(text)
    else:
        try:
            from PIL import Image
            img = Image.open(str(path))
            if img.mode != 'RGB':
                img = img.convert('RGB')
            text = image_to_text(img)
            if text:
                all_text.append(text)
        except Exception as e:
            return {'payment_no': '', 'amount': '', 'pay_date': '', 'payer': '', 'ocr_text': '', 'error': str(e)}

    combined_text = '\n'.join(all_text)
    from app.expense_utils import parse_date, parse_amount

    # 提取支付日期（支持标签和值在同一行或相邻行）
    pay_date = ''
    for pattern in [
        # 标签和值在同一行
        r'支付日期[：:]\s*(\d{4}[年月日]\d{1,2}[日月]\d{1,2})',
        r'付款日期[：:]\s*(\d{4}[年月日]\d{1,2}[日月]\d{1,2})',
        r'交易日期[：:]\s*(\d{4}[年月日]\d{1,2}[日月]\d{1,2})',
        # 标签和值分属相邻两行（RapidOCR 逐行输出模式）
        r'交易时间[^\d\n]*?(\d{4}-\d{2}-\d{2})',
        r'交易时间\s*\n\s*(\d{4}-\d{2}-\d{2})',
        # 通用日期（最后兜底）
        r'\b(\d{4}-\d{2}-\d{2})\b',
    ]:
        m = re.search(pattern, combined_text)
        if m:
            pay_date = parse_date(m.group(1))
            break

    # 提取支付金额（两层策略：先标签，后最大值兜底）
    amount = ''

    # 第一层：标签匹配（优先）
    labeled_amounts = []
    for pattern in [
        r'支付金额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'付款金额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'交易金额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'实付金额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'结算金额[：:]\s*[￥¥]?\s*([\d,]+\.?\d*)',
        r'(?:交易金额|支付金额|付款金额|实付金额|结算金额)\s*\n\s*([\d,]+\.?\d*)',
    ]:
        for m in re.finditer(pattern, combined_text):
            try:
                val = float(m.group(1).replace(',', ''))
                if 0.01 <= val <= 10000000:
                    labeled_amounts.append(val)
            except ValueError:
                pass

    if labeled_amounts:
        # 有标签金额：取最后一个（通常最准确，是最终实付金额）
        amount = str(labeled_amounts[-1])
    else:
        # 第二层兜底：从所有数字中选最大（发票场景：价税合计 > 单价/税额）
        all_amounts = []
        for m in re.finditer(r'\b([1-9]\d*[.]\d{2})\b', combined_text):
            try:
                val = float(m.group(1).replace(',', ''))
                if 0.01 <= val <= 10000000:
                    all_amounts.append(val)
            except ValueError:
                pass
        if all_amounts:
            amount = str(abs(max(all_amounts)))

    # 提取支付凭证号
    payment_no = ''
    for pattern in [
        r'支付凭证号[：:]\s*([^\s\n]{5,30})',
        r'凭证号[：:]\s*([^\s\n]{5,30})',
        r'交易流水号[：:]\s*([^\s\n]{5,30})',
    ]:
        m = re.search(pattern, combined_text)
        if m:
            payment_no = m.group(1).strip()
            break

    # 提取收款方/商户名（注意：OCR文本中标签和值常在不同行，无冒号）
    payer = ''
    for pattern in [
        r'对方户名\s*\n\s*([^\n]{2,50})',
        r'收款人[：:]\s*([^\n]{2,30})',
        r'收款方[：:]\s*([^\n]{2,30})',
        r'商户名称[：:]\s*([^\n]{2,30})',
        r'交易对方[：:]\s*([^\n]{2,30})',
        r'对方账户[：:]\s*([^\n]{2,30})',
    ]:
        m = re.search(pattern, combined_text)
        if m:
            payer = m.group(1).strip()
            break

    return {
        'payment_no': payment_no,
        'amount': amount,
        'pay_date': pay_date,
        'payer': payer,
        'ocr_text': combined_text,
    }

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("用法: python recognizer.py <文件路径>")
        sys.exit(1)
    result = recognize_file(sys.argv[1])
    print("=== 识别文本 ===")
    print(result['text'][:800])
    print("\n=== 提取字段 ===")
    fields = result['fields']
    for k, v in fields.items():
        if v:
            print(f"  {k}: {v}")
    print(f"\n置信度: {result.get('confidence', '中')}")
