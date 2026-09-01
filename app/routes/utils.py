# -*- coding: utf-8 -*-
"""
辅助工具路由 - 文档校对、模型配置、状态监控
"""
import os
import logging
import time
import psutil
import urllib.request
import urllib.error
import json
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from werkzeug.utils import secure_filename
from app.security.auth import maintenance_required

bp = Blueprint('utils', __name__, url_prefix='/utils')

# 允许的文件类型
ALLOWED_EXTENSIONS = {'txt', 'md', 'docx', 'pdf'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# 推理服务器地址
INFERENCE_URL = "http://127.0.0.1:18789"


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@bp.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('utils/index.html')


@bp.route('/document_correction')
def document_correction():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('utils/document_correction.html')


# ============ 模型配置页面 ============
@bp.route('/model_config')
@maintenance_required
def model_config():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('utils/model_config.html')


@bp.route('/api/models', methods=['GET'])
@maintenance_required
def api_models_list():
    """获取所有模型配置"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        from app.models import LLMModel
        models = LLMModel().get_all()
        return jsonify({'success': True, 'models': models})
    except Exception as e:
        logging.error(f"[Utils] 获取模型列表失败: {e}")
        return jsonify({'success': False, 'error': '模型配置读取失败'}), 500


@bp.route('/api/models', methods=['POST'])
@maintenance_required
def api_models_create():
    """新建模型配置"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        from app.models import LLMModel
        data = request.get_json()
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'success': False, 'error': '模型名称不能为空'}), 400

        LLMModel().create(
            name=name,
            model_type=data.get('model_type', 'corrector'),
            file_path=data.get('file_path', ''),
            n_ctx=int(data.get('n_ctx', 512)),
            n_threads=int(data.get('n_threads', 4)),
            temperature=float(data.get('temperature', 0.3)),
            max_tokens=int(data.get('max_tokens', 512)),
            prompt_template=data.get('prompt_template', ''),
            is_active=int(data.get('is_active', 0)),
            description=data.get('description', ''),
        )
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Utils] 创建模型失败: {e}")
        return jsonify({'success': False, 'error': '模型配置创建失败'}), 500


@bp.route('/api/models/<int:id>', methods=['PUT'])
@maintenance_required
def api_models_update(id):
    """更新模型配置"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        from app.models import LLMModel
        data = request.get_json()
        update_fields = {}
        for field in ['name', 'model_type', 'file_path', 'n_ctx', 'n_threads',
                      'temperature', 'max_tokens', 'prompt_template', 'is_active', 'description']:
            if field in data:
                update_fields[field] = data[field]
        LLMModel().update(id, **update_fields)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Utils] 更新模型失败: {e}")
        return jsonify({'success': False, 'error': '模型配置更新失败'}), 500


@bp.route('/api/models/<int:id>', methods=['DELETE'])
@maintenance_required
def api_models_delete(id):
    """删除模型配置"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        from app.models import LLMModel
        LLMModel().delete(id)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Utils] 删除模型失败: {e}")
        return jsonify({'success': False, 'error': '模型配置删除失败'}), 500


@bp.route('/api/models/activate/<int:id>', methods=['POST'])
@maintenance_required
def api_models_activate(id):
    """激活指定模型"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        from app.models import LLMModel
        model = LLMModel().get_by_id(id)
        if not model:
            return jsonify({'success': False, 'error': '模型不存在'}), 404

        # 先取消所有激活状态
        all_models = LLMModel().get_all()
        for m in all_models:
            LLMModel().update(m['id'], is_active=0)

        # 激活目标模型
        LLMModel().update(id, is_active=1)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"[Utils] 激活模型失败: {e}")
        return jsonify({'success': False, 'error': '模型激活失败'}), 500


# ============ 状态监控页面 ============
@bp.route('/monitor')
@maintenance_required
def monitor():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return render_template('utils/monitor.html')


@bp.route('/api/monitor/status', methods=['GET'])
@maintenance_required
def api_monitor_status():
    """获取推理服务器状态"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        from app.models import InferenceServerStatus

        # 检查推理服务是否可达
        server_status = 'stopped'
        model_loaded = ''
        latency_ms = 0
        error_message = ''

        try:
            start = time.time()
            req = urllib.request.Request(
                f"{INFERENCE_URL}/status",
                data=b'{}',
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                latency_ms = round((time.time() - start) * 1000)
                data = json.loads(resp.read().decode('utf-8'))
                server_status = 'running'
                model_loaded = data.get('model', '')
        except urllib.error.URLError:
            server_status = 'stopped'
            error_message = '推理服务器未启动'
        except Exception as e:
            server_status = 'error'
            error_message = '推理服务状态异常'

        # 获取系统资源
        mem = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=0.1)

        # 从数据库获取历史统计
        status = InferenceServerStatus().get()

        return jsonify({
            'success': True,
            'server_status': server_status,
            'model_loaded': model_loaded,
            'latency_ms': latency_ms,
            'memory_usage_mb': round(mem.used / 1024 / 1024, 1),
            'memory_total_mb': round(mem.total / 1024 / 1024, 1),
            'memory_percent': mem.percent,
            'cpu_percent': cpu_percent,
            'total_requests': status['total_requests'] if status else 0,
            'avg_latency_ms': status['avg_latency_ms'] if status else 0,
            'error_message': error_message,
        })
    except Exception as e:
        logging.error(f"[Utils] 获取监控状态失败: {e}")
        return jsonify({'success': False, 'error': '状态读取失败'}), 500


@bp.route('/api/monitor/restart', methods=['POST'])
@maintenance_required
def api_monitor_restart():
    """重启推理服务器"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    try:
        import subprocess
        # 杀掉现有进程
        subprocess.run(['pkill', '-f', 'node.*inference_server'], stderr=subprocess.DEVNULL)
        time.sleep(2)

        # 启动新进程
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.Popen(
            ['node', 'inference_server.js', 'models/chinese-text-correction-1.5b.Q4_K_M.gguf', '18789'],
            cwd=base_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({'success': True, 'message': '推理服务器重启中，约需3-5分钟加载模型'})
    except Exception as e:
        logging.error(f"[Utils] 重启推理服务器失败: {e}")
        return jsonify({'success': False, 'error': '重启请求未完成'}), 500


# ============ 文档校对 API ============

@bp.route('/api/correct', methods=['POST'])
def api_correct():
    """文本校对API"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    text = request.form.get('text', '').strip()
    if not text:
        return jsonify({'success': False, 'error': '文本不能为空'}), 400

    if len(text) > 10000:
        return jsonify({'success': False, 'error': '文本过长，请控制在10000字以内'}), 400

    try:
        from app.llm.corrector import Corrector
        corrector = Corrector()
        result = corrector.correct(text)

        return jsonify({
            'success': True,
            'original': text,
            'corrected': result.get('corrected', ''),
            'truncated': result.get('truncated', False),
            'error': result.get('error', '')
        })
    except Exception as e:
        logging.error(f"[Utils] 校对失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/upload_and_correct', methods=['POST'])
def api_upload_and_correct():
    """文件上传并校对"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': '没有选择文件'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': f'不支持的文件类型，支持: {", ".join(ALLOWED_EXTENSIONS)}'}), 400

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        return jsonify({'success': False, 'error': '文件过大，最大10MB'}), 400

    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[1].lower()

    try:
        text = ''
        if ext == 'txt' or ext == 'md':
            text = file.read().decode('utf-8', errors='ignore')
        elif ext == 'docx':
            text = extract_docx_text(file)
        elif ext == 'pdf':
            text = extract_pdf_text(file)
    except Exception as e:
        logging.error(f"[Utils] 文件读取失败: {e}")
        return jsonify({'success': False, 'error': f'文件读取失败: {str(e)}'}), 500

    if not text.strip():
        return jsonify({'success': False, 'error': '未能提取到文本内容'}), 400

    try:
        from app.llm.corrector import Corrector
        corrector = Corrector()
        result = corrector.correct(text)

        return jsonify({
            'success': True,
            'filename': filename,
            'original': text[:5000] if len(text) > 5000 else text,
            'original_full_length': len(text),
            'corrected': result.get('corrected', ''),
            'truncated': result.get('truncated', False),
            'error': result.get('error', '')
        })
    except Exception as e:
        logging.error(f"[Utils] 校对失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def extract_docx_text(file):
    import zipfile
    import io
    try:
        zip_data = zipfile.ZipFile(io.BytesIO(file.read()))
        with zip_data.open('word/document.xml') as f:
            import re
            xml = f.read().decode('utf-8')
            text = re.sub(r'<[^>]+>', '', xml)
            text = re.sub(r'\s+', ' ', text).strip()
            return text
    except Exception as e:
        raise Exception(f"docx解析失败: {e}")


def extract_pdf_text(file):
    try:
        import PyPDF2
        pdf_file = io.BytesIO(file.read())
        reader = PyPDF2.PdfReader(pdf_file)
        text_parts = []
        for page in reader.pages:
            text_parts.append(page.extract_text() or '')
        return '\n'.join(text_parts)
    except Exception as e:
        raise Exception(f"pdf解析失败: {e}，请确保是文本型PDF")
