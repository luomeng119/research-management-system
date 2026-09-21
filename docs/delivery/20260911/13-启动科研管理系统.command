#!/bin/zsh
# 当前 Mac 桌面启动入口：自动检查并启动数据库、9B模型和科研管理系统。
set -eu

TASK_RUNTIME='/Users/lm/Documents/Codex/tasks/20260910-科研管理系统复盘与本地模型调整/runtime'
TASK_PYTHON='/Users/lm/Documents/Codex/tasks/20260901-科研管理系统V1产品化/work/dev-workspace/科研管理系统/.venv/bin/python'

if "$TASK_PYTHON" "$TASK_RUNTIME/offline_runtime.py" start; then
  /usr/bin/open 'http://127.0.0.1:8893'
else
  print '启动失败，请查看上方原因及运行目录日志。'
  read '?按回车关闭窗口。'
  exit 1
fi
