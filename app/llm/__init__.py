# -*- coding: utf-8 -*-
"""
AI 模型模块
提供中文纠错和通用意图理解能力（基于 GGUF + llama.cpp）
"""
from app.llm.pool import ModelPool

# 全局模型池实例
_pool = None

def get_pool():
    """获取全局模型池（单例）"""
    global _pool
    if _pool is None:
        _pool = ModelPool()
    return _pool

def release():
    """释放所有模型，清理内存"""
    global _pool
    if _pool is not None:
        _pool.release_all()
        _pool = None
