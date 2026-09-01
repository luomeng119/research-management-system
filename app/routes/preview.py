# -*- coding: utf-8 -*-
import os
from flask import Blueprint, request, jsonify, send_file, Response
from werkzeug.utils import secure_filename
from app.models import get_db

bp = Blueprint('preview', __name__, url_prefix='/preview')

# 允许预览的文件扩展名
IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'}
DOCUMENT_EXTS = {'pdf'}
TEXT_EXTS = {'txt', 'log', 'json', 'xml', 'md', 'csv', 'yml', 'yaml'}


def get_file_path(file_path, file_type):
    """根据文件类型解析文件路径"""
    # 动态计算项目根目录（兼容容器化部署时路径为 /app 的情况）
    app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    uploads_dir = os.path.join(app_root, 'uploads')

    # 处理旧数据：容器路径 /app/uploads/ -> 实际 uploads_dir
    if file_path and file_path.startswith('/app/uploads/'):
        # 从 '/app/uploads/standards/xxx.docx' 提取 'standards/xxx.docx'
        parts = file_path.split('/app/uploads/', 1)
        relative_subpath = parts[1] if len(parts) > 1 else ''
        file_path = os.path.join(uploads_dir, relative_subpath)
        return file_path

    if file_type == 'project':
        # file_path 可能是：
        #   1. /uploads/SP2026001/xxx.docx  (有 /uploads/ 前缀，来自 openPreview)
        #   2. 任务输入文件/xxx.docx         (无前缀，来自 detail.html 的 f.path)
        # 两种情况都拼到 uploads_dir/SP2026001/ 下
        p = file_path.lstrip('/')
        if p.startswith('uploads/'):
            p = p[7:]  # 去掉 '/uploads/' (7字符)
        # p 现在是 SP2026001/任务输入文件/xxx.docx 或 任务输入文件/xxx.docx
        return os.path.join(uploads_dir, p.lstrip('/'))
    elif file_type == 'template':
        # file_path 是相对于 TEMPLATE_DIR 的路径，如 '会务模板/xxx.docx'
        template_dir = os.path.join(app_root, 'templates')
        p = file_path.lstrip('/')
        return os.path.join(template_dir, p)
    elif file_type == 'standard':
        if os.path.isabs(file_path):
            # 路径是绝对的，先尝试直接使用
            if os.path.exists(file_path):
                return file_path
            # 不存在时，尝试映射容器 /app 前缀
            if file_path.startswith('/app/'):
                relative_subpath = file_path[5:]  # 去掉 '/app' -> 'uploads/...'
                return os.path.join(app_root, relative_subpath)
            return file_path
        return os.path.join(uploads_dir, 'standards', file_path)
    elif file_type in ('image', 'pdf', 'other'):
        # 辅助工具（待整理区）文件：路径通常是绝对路径
        if os.path.isabs(file_path) and os.path.exists(file_path):
            return file_path
        # 处理 /uploads/ 开头的相对路径（来自 previewById 的 URL 转换）
        # 实际文件在 app/uploads/ 下，而非 uploads/ 下
        if file_path.startswith('/uploads/'):
            app_uploads = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
            rel_path = file_path[9:]  # 去掉 '/uploads/' (9字符)
            # 直接在 app_uploads 下查找（不走 os.path.join，避免双层 uploads）
            full_path = app_uploads + os.sep + rel_path
            if os.path.exists(full_path):
                return full_path
        # 尝试 app/uploads 目录
        app_uploads = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
        if os.path.exists(os.path.join(app_uploads, file_path)):
            return os.path.join(app_uploads, file_path)
        return file_path if os.path.isabs(file_path) else None
    return None


def get_ext(filename):
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


@bp.route('/file')
def preview_file():
    """预览文件内容，返回HTML片段或JSON"""
    file_path = request.args.get('path', '')
    file_type = request.args.get('type', 'project')  # project | template | standard
    # 优先用 name（展示名），但如果没扩展名则降级用 file_path 本身
    raw_name = request.args.get('name', '')
    filename = raw_name if raw_name else os.path.basename(file_path)
    # name 可能只有展示名没有扩展名，此时用 file_path 来判断类型
    ext = get_ext(filename)
    if not ext:
        filename = os.path.basename(file_path)
        ext = get_ext(filename)
    
    full_path = get_file_path(file_path, file_type)
    if not full_path or not os.path.exists(full_path):
        return jsonify({'success': False, 'message': '文件不存在'})
    
    ext = get_ext(filename)
    
    # 图片直接返回URL
    if ext in IMAGE_EXTS:
        return jsonify({
            'success': True,
            'type': 'image',
            'url': f'/preview/img?path={file_path}&type={file_type}'
        })
    
    # PDF iframe预览
    if ext == 'pdf':
        return jsonify({
            'success': True,
            'type': 'pdf',
            'url': f'/preview/img?path={file_path}&type={file_type}'
        })
    
    # 文本文件
    if ext in TEXT_EXTS:
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(200 * 1024)  # 限制200KB
            return jsonify({
                'success': True,
                'type': 'text',
                'filename': filename,
                'content': content,
                'truncated': len(content) >= 200 * 1024
            })
        except Exception as e:
            return jsonify({'success': False, 'message': f'读取文件失败: {str(e)}'})
    
    # Word文档
    if ext in {'docx'}:
        try:
            from docx import Document
            doc = Document(full_path)
            paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            tables = []
            for table in doc.tables:
                rows = []
                for row in table.rows:
                    rows.append([cell.text.strip() for cell in row.cells])
                tables.append(rows)
            return jsonify({
                'success': True,
                'type': 'word',
                'filename': filename,
                'paragraphs': paragraphs,
                'tables': tables
            })
        except ImportError:
            return jsonify({'success': False, 'message': '服务器未安装python-docx库'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'读取Word文档失败: {str(e)}'})
    
    # Excel文档
    if ext in {'xlsx', 'xls'}:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(full_path, data_only=True, read_only=True)
            sheets = []
            for sheet_name in wb.sheetnames[:5]:  # 最多5个sheet
                ws = wb[sheet_name]
                rows_data = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 100:  # 最多100行
                        rows_data.append(['...（超过100行省略）'])
                        break
                    rows_data.append([str(cell) if cell is not None else '' for cell in row])
                sheets.append({'name': sheet_name, 'rows': rows_data})
            wb.close()
            return jsonify({
                'success': True,
                'type': 'excel',
                'filename': filename,
                'sheets': sheets
            })
        except ImportError:
            return jsonify({'success': False, 'message': '服务器未安装openpyxl库'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'读取Excel文档失败: {str(e)}'})
    
    # 不支持预览
    return jsonify({
        'success': False,
        'message': f'暂不支持预览.{ext}类型文件，请下载后查看'
    })


@bp.route('/img')
def preview_img():
    """直接提供图片/PDF文件流"""
    file_path = request.args.get('path', '')
    file_type = request.args.get('type', 'project')
    
    full_path = get_file_path(file_path, file_type)
    if not full_path or not os.path.exists(full_path):
        return '文件不存在', 404
    
    ext = get_ext(os.path.basename(file_path))
    if ext in IMAGE_EXTS:
        return send_file(full_path)
    elif ext == 'pdf':
        return send_file(full_path, mimetype='application/pdf')
    
    return '不支持', 404
