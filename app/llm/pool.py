# -*- coding: utf-8 -*-
"""
模型加载池 - 统一管理 GGUF 模型的加载、缓存与释放
"""
import os
import threading
import logging
from config import LLM_MODEL_DIR, LLM_CORRECTOR_MODEL, LLM_QWEN_MODEL

logger = logging.getLogger(__name__)

# 延迟导入 llama_cpp，避免启动时强制依赖
_llama = None

def _get_llama():
    global _llama
    if _llama is None:
        try:
            from llama_cpp import Llama
            _llama = Llama
        except ImportError:
            logger.warning("llama-cpp-python 未安装，AI 功能将不可用。运行: pip install llama-cpp-python")
            return None
    return _llama


class ModelPool:
    """模型池，支持多模型懒加载与线程安全访问"""

    def __init__(self):
        self._lock = threading.RLock()
        self._models = {}  # name -> Llama instance
        self._configs = {
            'corrector': {
                'model_path': os.path.join(LLM_MODEL_DIR, LLM_CORRECTOR_MODEL),
                'n_ctx': getattr(__import__('config'), 'LLM_CORRECTOR_N_ctx', 512),
                'n_threads': getattr(__import__('config'), 'LLM_N_threads', 4),
                'verbose': False,
            },
            'qwen': {
                'model_path': os.path.join(LLM_MODEL_DIR, LLM_QWEN_MODEL),
                'n_ctx': getattr(__import__('config'), 'LLM_N_ctx', 1024),
                'n_threads': getattr(__import__('config'), 'LLM_N_threads', 4),
                'verbose': False,
            },
        }

    def get_model(self, name: str):
        """
        获取指定名称的模型实例，未加载则自动加载。
        name: 'corrector' | 'qwen'
        """
        with self._lock:
            if name not in self._models:
                self._load_model(name)
            return self._models.get(name)

    def _load_model(self, name: str):
        """内部：加载指定模型"""
        Llama_cls = _get_llama()
        if Llama_cls is None:
            raise RuntimeError("llama-cpp-python 未安装")

        cfg = self._configs.get(name)
        if cfg is None:
            raise ValueError(f"未知模型: {name}")

        model_path = cfg['model_path']
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        logger.info(f"[ModelPool] 正在加载模型: {name} ({model_path})")
        try:
            # 动态获取配置，支持 config 未定义时使用默认值
            from importlib import import_module
            cfg_module = import_module('config')
            n_ctx = getattr(cfg_module, 'LLM_CORRECTOR_N_ctx', 512) if name == 'corrector' else getattr(cfg_module, 'LLM_N_ctx', 1024)
            n_threads = getattr(cfg_module, 'LLM_N_threads', 4)
        except Exception:
            n_ctx = 512 if name == 'corrector' else 1024
            n_threads = 4

        instance = Llama_cls(
            model_path=model_path,
            n_ctx=n_ctx,
            n_threads=n_threads,
            verbose=False,
        )
        self._models[name] = instance
        logger.info(f"[ModelPool] 模型加载完成: {name}")

    def is_loaded(self, name: str) -> bool:
        """检查模型是否已加载"""
        with self._lock:
            return name in self._models

    def release(self, name: str):
        """卸载指定模型，释放内存"""
        with self._lock:
            if name in self._models:
                del self._models[name]
                logger.info(f"[ModelPool] 模型已卸载: {name}")

    def release_all(self):
        """卸载所有模型"""
        with self._lock:
            for name in list(self._models.keys()):
                del self._models[name]
            logger.info("[ModelPool] 所有模型已卸载")

    def status(self) -> dict:
        """返回当前模型加载状态"""
        with self._lock:
            return {
                'corrector': self.is_loaded('corrector'),
                'qwen': self.is_loaded('qwen'),
                'models': list(self._models.keys()),
            }
