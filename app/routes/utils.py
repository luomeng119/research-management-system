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
from functools import wraps
from flask import Blueprint, abort, current_app, render_template, request, jsonify, session, redirect, url_for
from werkzeug.utils import secure_filename
from app.security.auth import maintenance_required

bp = Blueprint('utils', __name__, url_prefix='/utils')

# 允许的文件类型
ALLOWED_EXTENSIONS = {'txt', 'md', 'docx', 'pdf'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# 推理服务器地址
INFERENCE_URL = "http://127.0.0.1:18789"


def local_model_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_app.config.get('AI_FEATURES_VISIBLE') is not True:
            abort(404)
        if str(current_app.config.get('AI_PROVIDER', 'DISABLED')).upper() != 'LOCAL' and not current_app.config.get('ENABLE_LLM', False):
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def _is_local_runtime():
    return str(current_app.config.get('AI_PROVIDER', 'DISABLED')).upper() == 'LOCAL'


def _auxiliary_ai_enabled():
    return (current_app.config.get('AI_FEATURES_VISIBLE') is True
            and current_app.config.get('AUXILIARY_AI_ENABLED') is True)


def _local_get_json(url):
    """Bounded loopback GET; environment proxies and redirects are disabled."""
    from urllib.parse import urlsplit
    from app.ai.local_model import _validated_base_url
    parsed = urlsplit(url)
    _validated_base_url(f"{parsed.scheme}://{parsed.netloc}")
    if parsed.path not in {'/health', '/v1/models'} or parsed.query or parsed.fragment:
        raise ValueError('invalid status endpoint')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(url, method='GET'), timeout=2) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('status response too large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('invalid status response')
    return value


def _local_runtime_status():
    from app.services.local_model_runtime import configured, runtime_status, public_status
    if configured(current_app.config):
        try:
            result = public_status(runtime_status(current_app.config))
            return {**result, 'success': result['state'] != 'failed',
                    'server_status': 'running' if result.get('state') == 'ready' else 'unavailable',
                    'model_loaded': result.get('model') if result.get('state') == 'ready' else None,
                    'total_requests': None, 'avg_latency_ms': None, 'last_request_time': None}
        except Exception:
            return {**public_status({'state': 'failed', 'errorCode': 'RUNTIME_UNAVAILABLE'}),
                    'success': False, 'server_status': 'unavailable', 'model_loaded': None}
    from app.ai.local_model import _validated_base_url
    result = {'success': True, 'server_status': 'unavailable', 'model_loaded': None,
              'configured_model': current_app.config.get('LOCAL_MODEL_NAME'),
              'latency_ms': None, 'total_requests': None, 'avg_latency_ms': None,
              'last_request_time': None, 'error_message': ''}
    try:
        base = _validated_base_url(current_app.config.get('LOCAL_MODEL_BASE_URL'))
        started = time.monotonic()
        health = _local_get_json(base + '/health')
        models = _local_get_json(base + '/v1/models')
        model_ids = [item.get('id') for item in models.get('data', []) if isinstance(item, dict)]
        expected = current_app.config.get('LOCAL_MODEL_NAME')
        if health.get('status') != 'ok' or expected not in model_ids:
            raise ValueError('expected model not ready')
        result.update(server_status='running', model_loaded=expected,
                      latency_ms=round((time.monotonic() - started) * 1000))
    except Exception:
        result['error_message'] = '本地模型尚未就绪或配置不匹配，请检查本版本运行器。'
    return result


def _local_config_readonly():
    return jsonify({'success': False, 'error': '本版本使用固定的本地模型配置；请通过本 Mac 运行器维护，不使用旧模型启动器。'}), 409


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
    if not _auxiliary_ai_enabled():
        abort(404)
    return render_template('utils/document_correction.html')


# ============ 模型配置页面 ============
@bp.route('/model_config')
@local_model_required
@maintenance_required
def model_config():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    if _is_local_runtime():
        return render_template('utils/local_model_status.html')
    return render_template('utils/model_config.html')


@bp.route('/api/models', methods=['GET'])
@local_model_required
@maintenance_required
def api_models_list():
    if _is_local_runtime():
        from app.services.local_model_runtime import configured
        if configured(current_app.config):
            status = _local_runtime_status()
            return jsonify(success=status.get('success', False), readonly=False,
                           models=status.get('profiles', []), state=status.get('state'))
        return jsonify({'success': True, 'readonly': True, 'models': [{
            'name': current_app.config.get('LOCAL_MODEL_NAME'),
            'base_url': current_app.config.get('LOCAL_MODEL_BASE_URL'),
            'provider': 'LOCAL'}]})
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
@local_model_required
@maintenance_required
def api_models_create():
    if _is_local_runtime():
        return _local_config_readonly()
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
@local_model_required
@maintenance_required
def api_models_update(id):
    if _is_local_runtime():
        return _local_config_readonly()
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
@local_model_required
@maintenance_required
def api_models_delete(id):
    if _is_local_runtime():
        return _local_config_readonly()
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
@local_model_required
@maintenance_required
def api_models_activate(id):
    if _is_local_runtime():
        return _local_config_readonly()
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
@local_model_required
@maintenance_required
def monitor():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    if _is_local_runtime():
        return render_template('utils/local_model_status.html')
    return render_template('utils/monitor.html')


@bp.route('/api/monitor/status', methods=['GET'])
@local_model_required
@maintenance_required
def api_monitor_status():
    if _is_local_runtime():
        return jsonify(_local_runtime_status())
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
        except Exception:
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
@local_model_required
@maintenance_required
def api_monitor_restart():
    if _is_local_runtime():
        return _local_config_readonly()
    """重启推理服务器"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    if not current_app.config.get('ENABLE_LLM', False):
        return jsonify({
            'success': False,
            'error': 'V1 未启用本地模型，请使用人工校对',
        }), 503
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
    if not _auxiliary_ai_enabled():
        return jsonify({'success': False, 'error': '当前交付未启用自动校对'}), 404
    if str(current_app.config.get('AI_PROVIDER', 'DISABLED')).upper() != 'LOCAL':
        return jsonify({
            'success': False,
            'error': 'V1 未启用本地模型，请使用人工校对',
        }), 503

    text = request.form.get('text', '').strip()
    if not text:
        return jsonify({'success': False, 'error': '文本不能为空'}), 400

    if len(text) > 10000:
        return jsonify({'success': False, 'error': '文本过长，请控制在10000字以内'}), 400

    try:
        from app.llm.corrector import Corrector
        corrector = Corrector()
        result = corrector.correct(text)
        if result.get('error'):
            return jsonify({'success': False, 'error': '自动校对未完成，请使用人工校对'}), 503

        return jsonify({
            'success': True,
            'original': text,
            'corrected': result.get('corrected', ''),
            'errors': result.get('errors', []),
            'truncated': result.get('truncated', False),
            'error': result.get('error', '')
        })
    except Exception as e:
        logging.error(f"[Utils] 校对失败: {e}")
        return jsonify({'success': False, 'error': '自动校对未完成，请使用人工校对'}), 503


@bp.route('/api/upload_and_correct', methods=['POST'])
def api_upload_and_correct():
    """文件上传并校对"""
    if 'user' not in session:
        return jsonify({'success': False, 'error': '未登录'}), 401
    if not _auxiliary_ai_enabled():
        return jsonify({'success': False, 'error': '当前交付未启用自动校对'}), 404
    if str(current_app.config.get('AI_PROVIDER', 'DISABLED')).upper() != 'LOCAL':
        return jsonify({
            'success': False,
            'error': 'V1 未启用本地模型，请使用人工校对',
        }), 503

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
    ext = file.filename.rsplit('.', 1)[1].lower()

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
        if result.get('error'):
            return jsonify({'success': False, 'error': '自动校对未完成，请使用人工校对'}), 503

        return jsonify({
            'success': True,
            'filename': filename,
            'original': text[:5000] if len(text) > 5000 else text,
            'original_full_length': len(text),
            'corrected': result.get('corrected', ''),
            'errors': result.get('errors', []),
            'truncated': result.get('truncated', False),
            'error': result.get('error', '')
        })
    except Exception as e:
        logging.error(f"[Utils] 校对失败: {e}")
        return jsonify({'success': False, 'error': '自动校对未完成，请使用人工校对'}), 503


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
        from io import BytesIO
        from pdfminer.high_level import extract_text
        return extract_text(BytesIO(file.read()))
    except Exception as e:
        raise Exception(f"pdf解析失败: {e}，请确保是文本型PDF")


@bp.post('/api/local-model/load')
@local_model_required
@maintenance_required
def api_local_model_load():
    return _operate_local_model('load')


@bp.post('/api/local-model/unload')
@local_model_required
@maintenance_required
def api_local_model_unload():
    return _operate_local_model('unload')


def _operate_local_model(action):
    from app.services.local_model_runtime import controller, RuntimeUnavailable, public_status
    if not _is_local_runtime():
        abort(404)
    payload = request.get_json(silent=True)
    expected = {'profileId'} if action == 'load' else set()
    if (not isinstance(payload, dict) or set(payload) != expected
            or (action == 'load' and (not isinstance(payload['profileId'], str)
                                      or not 1 <= len(payload['profileId']) <= 64))):
        return jsonify(success=False, errorCode='INVALID_OPERATION', error_message='模型操作参数无效'), 422
    try:
        runtime = controller(current_app.config)
        if action == 'load':
            profiles = runtime.model_status().get('profiles', [])
            if payload['profileId'] not in {item['id'] for item in profiles}:
                return jsonify(success=False, errorCode='UNKNOWN_PROFILE', error_message='请选择本机已有模型'), 422
        result = runtime.model_operation(action, payload.get('profileId'))
        result = public_status(result)
        return jsonify(success=result['state'] != 'failed', **result), (503 if result['state'] == 'failed' else 200)
    except RuntimeUnavailable:
        return jsonify(success=False, errorCode='NOT_CONFIGURED', error_message='本机模型控制器未配置'), 409
    except BlockingIOError:
        return jsonify(success=False, errorCode='MODEL_BUSY', error_message='模型正在使用或切换，请稍后重试'), 409
    except (ValueError, FileNotFoundError):
        return jsonify(success=False, errorCode='MODEL_NOT_AVAILABLE', error_message='模型尚未准备完成或参数无效'), 422
    except Exception:
        current_app.logger.error('local model operation failed')
        return jsonify(success=False, errorCode='MODEL_OPERATION_FAILED', error_message='模型操作未完成，请刷新查看实际状态'), 503
