"""Bounded, source-faithful text extraction from an already authorized file handle."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import PurePath
from zipfile import ZipFile

from docx import Document
from PIL import Image, UnidentifiedImageError
from docx.table import Table
from docx.text.paragraph import Paragraph
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer

from app.services.research_reports import ResearchReportServiceError

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_DOCX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_SOURCE_CHARACTERS = 12000
EXTRACTION_VERSION = 'research-source-v1'
IMAGE_EXTRACTION_VERSION = 'research-image-source-v1'
MAX_IMAGE_PIXELS = 20_000_000


def _fail(message):
    raise ResearchReportServiceError('REPORT_SOURCE_EXTRACTION_FAILED', message, 422)


def _valid_character(character):
    number = ord(character)
    return character in '\t\n\r' or 0x20 <= number <= 0xD7FF or 0xE000 <= number <= 0xFFFD or 0x10000 <= number <= 0x10FFFF


def _docx_segments(data):
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(item.file_size for item in entries) > MAX_DOCX_EXPANDED_BYTES:
            _fail('Word资料解压后过大，请拆分后重新选择')
        # Only body text/tables are supported. Other content-bearing package parts
        # cannot be silently omitted even when the main body contains readable text.
        allowed_word_parts = {
            'word/document.xml', 'word/styles.xml', 'word/stylesWithEffects.xml',
            'word/settings.xml', 'word/webSettings.xml', 'word/fontTable.xml',
            'word/numbering.xml',
        }
        if any(
            item.filename.startswith('word/')
            and item.filename not in allowed_word_parts
            and not item.filename.startswith(('word/_rels/', 'word/theme/'))
            for item in entries
        ):
            _fail('Word资料含图片、页眉页脚、脚注尾注或其他未支持内容，请整理为仅含正文段落和表格的资料')
        # Reject structures whose text python-docx does not expose; do not lose them silently.
        xml = archive.read('word/document.xml')
        from lxml import etree
        root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        unsupported = {
            'txbxContent', 'altChunk', 'ins', 'del', 'sdt', 'drawing', 'pict',
            'object', 'footnoteReference', 'endnoteReference', 'commentReference',
            'headerReference', 'footerReference', 'oMath', 'oMathPara', 'sym',
            'fldSimple', 'fldChar', 'instrText', 'AlternateContent',
        }
        if any(etree.QName(node).localname in unsupported for node in root.iter() if isinstance(node.tag, str)):
            _fail('Word资料含图片、文本框、修订或特殊内容，请先转换为普通段落和表格')
    document = Document(BytesIO(data))
    segments = []
    paragraph_no = table_no = 0
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            paragraph_no += 1
            segments.append({'locator': f'段落{paragraph_no}', 'text': block.text})
        elif isinstance(block, Table):
            table_no += 1
            for row_no, row in enumerate(block.rows, 1):
                if any(cell.tables for cell in row.cells):
                    _fail('Word资料含嵌套表格，请先整理为普通表格')
                segments.append({'locator': f'表格{table_no}/行{row_no}', 'text': '\t'.join(cell.text for cell in row.cells)})
    return segments


def _pdf_segments(data):
    segments = []
    character_count = 0
    for index, page in enumerate(extract_pages(BytesIO(data)), 1):
        if index > MAX_PDF_PAGES:
            _fail(f'PDF超过{MAX_PDF_PAGES}页，请拆分后重新选择')
        # A scanned body with a readable page number is still an incomplete
        # extraction. Reject images, figures and vector content rather than feed
        # only the incidental text to the report generator.
        if any(not isinstance(element, LTTextContainer) for element in page):
            _fail(f'PDF第{index}页含图片、图形或其他未支持内容，请先提供完整文字版')
        text = ''.join(element.get_text() for element in page)
        if not text.strip():
            _fail(f'PDF第{index}页无可提取文字，可能为扫描页，请先提供文字版')
        character_count += len(text) + (1 if segments else 0)
        if character_count > MAX_SOURCE_CHARACTERS:
            _fail(f'单份资料超过{MAX_SOURCE_CHARACTERS}字符，请拆分后重新选择')
        segments.append({'locator': f'第{index}页', 'text': text})
    return segments


def _image_source(data, suffix, opened, vision_assistant):
    if vision_assistant is None:
        _fail('本地图像理解功能未就绪，请稍后重试')
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ResearchReportServiceError('REPORT_SOURCE_EXTRACTION_FAILED', '图片内容无效或无法安全读取', 422) from error
    expected = {'.png': ('PNG', 'image/png'), '.jpg': ('JPEG', 'image/jpeg'), '.jpeg': ('JPEG', 'image/jpeg')}[suffix]
    if image_format != expected[0] or width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        _fail('图片格式与扩展名不符或像素尺寸过大')
    try:
        observation = vision_assistant.generate(
            image_bytes=data, media_type=expected[1], filename=opened['originalName'],
        )
    except (InterruptedError, TimeoutError):
        raise
    except ValueError as error:
        raise ResearchReportServiceError('MODEL_INPUT_LIMIT', str(error), 422) from error
    except Exception as error:
        raise ResearchReportServiceError('MODEL_UNAVAILABLE', '本地模型暂未完成图片提取，请稍后重试', 503) from error
    try:
        lines = ['【图片资料：以下为本地模型提取结果，保存前须人工核对】']
        segments = []
        for label, key in [('资料类型', 'documentType'), ('摘要', 'summary')]:
            value = observation[key]
            if value.strip():
                line = f'{label}：{value}'
                lines.append(line); segments.append({'locator': label, 'text': line})
        for label, key in [('可见文字', 'visibleText'), ('可核验事实', 'facts'), ('图表信息', 'chartFindings'), ('待确认', 'uncertainties')]:
            for index, value in enumerate(observation[key], 1):
                line = f'{label}{index}：{value}'
                lines.append(line); segments.append({'locator': f'{label}{index}', 'text': line})
        text = '\n'.join(lines)
        if len(text) > MAX_SOURCE_CHARACTERS or any(not _valid_character(character) for character in text):
            raise ValueError('invalid observation')
        return {
            'text': text, 'segments': segments, 'sourceKind': 'IMAGE',
            'extractionVersion': IMAGE_EXTRACTION_VERSION,
            'textSha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'image': {'width': width, 'height': height, 'mediaType': expected[1]},
            'visionModel': observation.get('model'),
            'visionPromptVersion': observation.get('promptVersion'),
            'visionUsage': observation.get('usage', {}),
        }
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ResearchReportServiceError('MODEL_INVALID_RESPONSE', '本地模型图片提取结果无效，请稍后重试', 503) from error


def extract_report_source(opened: dict, *, vision_assistant=None) -> dict:
    """Take ownership of ``opened['stream']`` and close it on success or failure.

    UTF-8 text is retained byte-for-byte after decoding (including BOM and CRLF).
    Word blocks and PDF pages are separated by a single newline, with no Unicode
    normalization. Unsupported or partial extraction produces no successful result.
    """
    stream = opened.get('stream')
    try:
        if stream is None:
            _fail('资料读取失败，请重新选择文件版本')
        suffix = PurePath(str(opened.get('originalName', ''))).suffix.lower()
        if suffix not in {'.txt', '.md', '.csv', '.docx', '.pdf', '.png', '.jpg', '.jpeg'}:
            _fail('资料格式暂不支持，请使用TXT、MD、CSV、DOCX、文字版PDF、PNG或JPG')
        size = opened.get('sizeBytes')
        if type(size) is not int or not 0 < size <= MAX_INPUT_BYTES:
            _fail('资料为空或超过10MiB，请拆分后重新选择')
        data = stream.read(MAX_INPUT_BYTES + 1)
        if not isinstance(data, bytes) or len(data) != size or len(data) > MAX_INPUT_BYTES:
            _fail('资料大小校验失败，请重新选择文件版本')
        if suffix in {'.png', '.jpg', '.jpeg'}:
            return _image_source(data, suffix, opened, vision_assistant)
        if suffix in {'.txt', '.md', '.csv'}:
            segments = [{'locator': '全文', 'text': data.decode('utf-8')}]
        elif suffix == '.docx':
            segments = _docx_segments(data)
        else:
            segments = _pdf_segments(data)
        text = '\n'.join(segment['text'] for segment in segments)
        if not text.strip('\ufeff \t\n\r'):
            _fail('资料无可提取正文，请提供文字版')
        if len(text) > MAX_SOURCE_CHARACTERS:
            _fail(f'单份资料超过{MAX_SOURCE_CHARACTERS}字符，请拆分后重新选择')
        if any(not _valid_character(character) for character in text):
            _fail('资料含无效控制字符，请先整理文本')
        return {'text': text, 'segments': segments, 'sourceKind': 'TEXT', 'extractionVersion': EXTRACTION_VERSION,
                'textSha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
    except ResearchReportServiceError:
        raise
    except Exception as error:
        raise ResearchReportServiceError('REPORT_SOURCE_EXTRACTION_FAILED', '资料解析失败，请核对编码、文件完整性和是否加密', 422) from error
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception as error:
                raise ResearchReportServiceError(
                    'REPORT_SOURCE_EXTRACTION_FAILED', '资料读取结束失败，请重新选择文件版本', 422
                ) from error
