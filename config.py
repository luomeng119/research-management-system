# -*- coding: utf-8 -*-
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Flask配置
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'dev-fallback-key-change-in-production')
DEBUG = False

# 路径配置
DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DOCUMENTS_DIR = os.path.join(BASE_DIR, 'documents')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

# 文件上传配置
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'zip', 'rar', 'png', 'jpg', 'jpeg'}
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100MB

# 推理参数
LLM_N_ctx = 1024           # Qwen 最大上下文 token 数
LLM_CORRECTOR_N_ctx = 512  # 纠错模型上下文（较小，省显存）
LLM_N_threads = 4          # CPU 推理线程数（根据机器 CPU 核心数调整）
LLM_MAX_TOKENS = 256       # 生成最大 token 数

# ============ LLM（AI 模型）配置 ============
# 是否启用本地小模型推理（需手动开启，会占用约 2-3GB 内存）
ENABLE_LLM = False

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
