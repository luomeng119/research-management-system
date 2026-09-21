# -*- coding: utf-8 -*-
import os
from datetime import timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA_ROOT = os.environ.get('APP_DATA_ROOT')
RUNTIME_ROOT = (
    os.path.abspath(os.path.expanduser(APP_DATA_ROOT))
    if APP_DATA_ROOT
    else BASE_DIR
)

# Flask配置
SECRET_KEY = None
DEBUG = False

# 路径配置
DATA_DIR = os.path.join(RUNTIME_ROOT, 'data')
UPLOAD_DIR = os.path.join(RUNTIME_ROOT, 'uploads')
DOCUMENTS_DIR = os.path.join(RUNTIME_ROOT, 'documents')
BACKUP_DIR = os.path.join(RUNTIME_ROOT, 'backups')
SESSION_FILE_DIR = os.path.join(DATA_DIR, 'flask_sessions')
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', '').lower() in {'1', 'true', 'yes'}
PERMANENT_SESSION_LIFETIME = timedelta(minutes=30)
SESSION_REFRESH_EACH_REQUEST = True

# Security defaults. V1's in-process limiter and assistant cancellation registry
# require the supported single-process run.py deployment; do not add web worker
# processes without first moving those two states to a shared store.
SECURITY_AUTH_ENABLED = None
CSRF_ENABLED = None
LOGIN_RATE_LIMIT_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 15 * 60
LOGIN_RATE_LIMIT_MAX_ENTRIES = 2048
LOG_FILE = os.path.join(DATA_DIR, 'logs', 'app.jsonl')
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# 文件上传配置
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'zip', 'rar', 'png', 'jpg', 'jpeg'}
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100MB
FILE_STORAGE_ROOT = os.path.join(DATA_DIR, 'files')
FILE_MAX_BYTES = MAX_CONTENT_LENGTH
FILE_PREVIEW_MAX_BYTES = 10 * 1024 * 1024

# 推理参数
LLM_N_ctx = 1024           # Qwen 最大上下文 token 数
LLM_CORRECTOR_N_ctx = 512  # 纠错模型上下文（较小，省显存）
LLM_N_threads = 4          # CPU 推理线程数（根据机器 CPU 核心数调整）
LLM_MAX_TOKENS = 256       # 生成最大 token 数

# ============ LLM（AI 模型）配置 ============
# Product-level visibility gate.  The local AI implementation remains available
# for an explicit deployment override, but is not part of the default delivery.
AI_FEATURES_VISIBLE = False

# 旧版进程内推理开关；当前交付由外部Qwen3.5-9B llama.cpp服务提供推理。
ENABLE_LLM = False

# 科研提案、报告和框选助手。当前交付使用本机OpenAI兼容接口，不调用云端模型。
DEPLOYMENT_MODE = os.environ.get('DEPLOYMENT_MODE', 'DEVELOPMENT').upper()
AI_PROVIDER = os.environ.get('AI_PROVIDER', 'DISABLED').upper()
# The confirmed client workflow limits the model to proposal/report drafting,
# report revision and selected-text suggestions. Legacy proofreading, summary
# and AI-search endpoints stay closed unless a separate deployment opts in.
AUXILIARY_AI_ENABLED = os.environ.get('AUXILIARY_AI_ENABLED', '').lower() in {'1', 'true', 'yes'}
LOCAL_MODEL_BASE_URL = os.environ.get('LOCAL_MODEL_BASE_URL', 'http://127.0.0.1:18081')
LOCAL_MODEL_NAME = os.environ.get('LOCAL_MODEL_NAME', 'Qwen3.5-9B')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')

# GGUF 模型所在目录
LLM_MODEL_DIR = os.path.join(BASE_DIR, 'models')

# 模型文件
LLM_CORRECTOR_MODEL = 'chinese-text-correction-1.5b.Q4_K_M.gguf'
LLM_QWEN_MODEL = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'

# 版本号 - 每次功能升级递增
# 修订号：bug修复、小优化
# 次版本：新增功能
# 主版本：重大架构变更
VERSION = '1.17.0'

# Only a deployment-supplied local controller can start or stop model processes.
LOCAL_MODEL_CONTROLLER_PATH = os.environ.get("LOCAL_MODEL_CONTROLLER_PATH")
