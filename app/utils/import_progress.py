# -*- coding: utf-8 -*-
"""
设备导入进度跟踪器（内存版，threading 安全）

用法：
    from app.utils.import_progress import (
        create_task, update_progress, mark_done,
        mark_failed, mark_cancelled, request_cancel,
        get_progress, cleanup_task
    )

特性：
- 单进程内存存储（重启即清空，可接受 — 导入任务通常 < 5s 不会跨重启）
- threading.Lock 保护并发读写
- task_id 用 uuid4 hex，无碰撞
- 任务结束后 5 分钟自动清理（防止内存泄漏）
- 进度状态：pending / running / done / failed / cancelled

注意：
- 这是简化的 V1 实现，V2 改用 Redis（跨进程/重启保留任务）
- 同一用户多标签页可能产生多个 task_id（互不影响，但 UI 上需明确展示）
"""
import threading
import time
import uuid
from typing import Optional, Dict, Any


_lock = threading.Lock()
_progress_store: Dict[str, Dict[str, Any]] = {}


def create_task(total: int, user: str = '', extra: Optional[dict] = None) -> str:
    """创建导入任务，返回 task_id"""
    task_id = uuid.uuid4().hex
    with _lock:
        _progress_store[task_id] = {
            'task_id': task_id,
            'user': user,
            'status': 'pending',
            'current': 0,
            'total': total,
            'current_name': '',
            'started_at': time.time(),
            'updated_at': time.time(),
            'error': '',
            'failed_row': None,
            'cancel_requested': False,
            'saved_new': 0,
            'saved_overwrite': 0,
            'skipped': 0,
            'relations_created': 0,
            'skipped_relations': [],
            'extra': extra or {},
        }
    return task_id


def update_progress(task_id: str, current: int, current_name: str = '') -> None:
    """更新当前进度（导入循环每条调用一次）"""
    with _lock:
        if task_id in _progress_store:
            p = _progress_store[task_id]
            p['current'] = current
            p['current_name'] = current_name
            p['updated_at'] = time.time()
            if p['status'] == 'pending':
                p['status'] = 'running'


def mark_done(task_id: str, saved_new: int = 0, saved_overwrite: int = 0,
              skipped: int = 0, relations_created: int = 0,
              skipped_relations: Optional[list] = None) -> None:
    """标记任务完成（成功）"""
    with _lock:
        if task_id in _progress_store:
            p = _progress_store[task_id]
            p['status'] = 'done'
            p['current'] = p['total']
            p['updated_at'] = time.time()
            p['saved_new'] = saved_new
            p['saved_overwrite'] = saved_overwrite
            p['skipped'] = skipped
            p['relations_created'] = relations_created
            p['skipped_relations'] = skipped_relations or []


def mark_failed(task_id: str, error: str, failed_row: Optional[dict] = None) -> None:
    """标记任务失败（事务回滚后）"""
    with _lock:
        if task_id in _progress_store:
            p = _progress_store[task_id]
            p['status'] = 'failed'
            p['updated_at'] = time.time()
            p['error'] = error
            p['failed_row'] = failed_row


def mark_cancelled(task_id: str, saved_count: int = 0) -> None:
    """标记任务被取消（事务回滚后）"""
    with _lock:
        if task_id in _progress_store:
            p = _progress_store[task_id]
            p['status'] = 'cancelled'
            p['updated_at'] = time.time()
            p['saved_new'] = 0  # 取消意味着回滚，0
            p['saved_overwrite'] = 0


def request_cancel(task_id: str) -> bool:
    """请求取消任务（导入循环每 10 行检查此标志）

    返回 True 表示成功标记，False 表示任务不存在
    """
    with _lock:
        if task_id in _progress_store:
            p = _progress_store[task_id]
            if p['status'] in ('pending', 'running'):
                p['cancel_requested'] = True
                p['updated_at'] = time.time()
                return True
    return False


def get_progress(task_id: str) -> Optional[Dict[str, Any]]:
    """读取当前进度（无锁 - 浅拷贝即可，调用方只读不写）"""
    with _lock:
        if task_id in _progress_store:
            # 返回浅拷贝（避免外层修改内部状态）
            return dict(_progress_store[task_id])
    return None


def is_cancel_requested(task_id: str) -> bool:
    """导入循环内每 10 行检查此标志"""
    with _lock:
        p = _progress_store.get(task_id)
        if p:
            return p.get('cancel_requested', False)
    return False


def cleanup_task(task_id: str) -> None:
    """任务结束清理进度"""
    with _lock:
        _progress_store.pop(task_id, None)


def cleanup_old_tasks(max_age_seconds: int = 300) -> int:
    """清理超过 max_age_seconds 的已完成/失败/取消任务

    返回清理数量
    """
    now = time.time()
    removed = 0
    with _lock:
        for tid in list(_progress_store.keys()):
            p = _progress_store[tid]
            if p['status'] in ('done', 'failed', 'cancelled'):
                if now - p['updated_at'] > max_age_seconds:
                    _progress_store.pop(tid, None)
                    removed += 1
    return removed
