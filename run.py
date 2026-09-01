# -*- coding: utf-8 -*-
import subprocess
import os
import sys
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import urllib.request
import urllib.error

# 启动 Node.js 推理服务器（后台异步，不阻塞 Flask 启动）
def _start_inference_server_blocking():
    """实际启动逻辑：失败/超时立即返回，绝不抛异常"""
    port = 18789

    # 检查是否已有服务在运行
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/complete",
            data=b'{"prompt":"test"}',
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            print(f"[启动] 推理服务器已在运行 (端口 {port})")
            return
    except Exception:
        pass

    # 检查 node 是否可用
    try:
        subprocess.run(['node', '--version'], capture_output=True, check=True, timeout=3)
    except Exception:
        print(f"[启动] node 未安装或不在 PATH，跳过推理服务器（文档校对功能将不可用）")
        return

    # 检查模型文件是否存在
    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models', 'chinese-text-correction-1.5b.Q4_K_M.gguf')
    if not os.path.exists(model_path):
        print(f"[启动] 模型文件不存在 ({model_path})，跳过推理服务器（文档校对功能将不可用）")
        return

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inference_server.js')
    if not os.path.exists(script_path):
        print(f"[启动] 推理脚本不存在 ({script_path})，跳过")
        return

    # 启动 Node.js 推理服务器（不等待）
    print(f"[启动] 启动推理服务器（后台）...")
    try:
        subprocess.Popen(
            ['node', script_path, model_path, str(port)],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[启动] 推理服务器已交由后台启动，Flask 不等待")
    except Exception as e:
        print(f"[启动] 推理服务器启动失败: {e}")

def ensure_inference_server():
    """非阻塞：把推理服务器启动放到后台线程，Flask 立刻继续"""
    t = threading.Thread(target=_start_inference_server_blocking, daemon=True, name="inference-server-starter")
    t.start()

ensure_inference_server()

from app import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)