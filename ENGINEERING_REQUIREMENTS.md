# 工程规范

## 数据库访问
- [2026-04-13] 禁止用位置索引（r[0]/r[1]等），必须用 `SELECT *` + `cursor.description` + `dict(zip(cols, row))` 按字段名访问
- [2026-04-13] 禁止依赖表列顺序，`ALTER TABLE ADD COLUMN` 可能导致顺序错乱
- [2026-04-13] 启动时对关键表使用 `DROP TABLE IF EXISTS` + `CREATE TABLE` 确保 schema 一致

## 前端
- [2026-04-13] HTML 标签嵌套必须符合规范（ul>li>ul，div 不得直接包 li）
- [2026-04-13] 检索/筛选功能必须即时响应：输入文字或切换分类后，列表**立即更新**，不得等用户按回车或点按钮
- [2026-04-13] 实现方式：监听输入框 `input` 事件或分类切换事件，触发检索更新（不得依赖 `change` 事件）

## 内网环境
- [2026-04-14] 前端资源（JS/CSS）必须使用本地静态文件，不得引用外网 CDN
- [2026-04-14] Quill 富文本编辑器已本地化，路径：`/static/quill/`
- [2026-04-14] 文档预览服务不得依赖 LibreOffice，用纯 Python 库实现：
  - Word 预览：`python-docx` 提取文字
  - Excel 预览：`openpyxl` 读取单元格
- [2026-04-14] python-docx 已加入 `requirements.txt`

## 文档预览
- [2026-04-14] 预览方式：侧边栏预览（方案B），默认关闭，点击按钮滑出
- [2026-04-14] 预览面板宽度约 55%，右侧固定定位
- [2026-04-14] 三个模块都要接入：项目文档、科研模板、标准法规
- [2026-04-14] 预览文件类型：图片（直接展示）、PDF（iframe）、Word（文字+表格）、Excel（多Sheet表格）、文本文件（原始内容）
- [2026-04-14] 预览功能无需权限控制，登录用户均可使用
- [2026-04-14] 文件路径解析：标准法规的 file_path 为绝对路径，项目文档为相对路径，科研模板在项目根目录 templates/ 下
- [2026-04-14] 预览面板共享组件路径：`app/templates/components/preview_panel.html`

## 用户界面显示
- [2026-04-14] 用户名显示友好名称（name 字段），而非 username：登录时 `session['name'] = user['name']`，无 name 时回退
- [2026-04-14] 新建项目时 project_id 留空应自动生成，不应报错

## 性能

## 安全

## AI / LLM 集成
- [2026-04-27] LLM 模块路径：`app/llm/`（pool.py / corrector.py / qwen.py）
- [2026-04-27] 模型文件目录：`models/`（不动，不进 venv）
- [2026-04-27] 使用 llama-cpp-python 推理 GGUF 模型（CPU，无需 GPU）
- [2026-04-27] 模型懒加载：首次调用时加载，不在 `create_app()` 中预加载
- [2026-04-27] 全局单例：`app.llm.get_pool()` 获取模型池，禁止重复创建 Llama 实例
- [2026-04-27] 内存约束：两个 1.5B Q4 模型同时加载约需 3~4 GB RAM，建议 gunicorn 单 worker（`-w 1`）
- [2026-04-27] 纠错 Prompt 不改变原文表达风格，只修正明确错误
- [2026-04-27] 智能检索用 `is_relevant` 二分判断，不做评分排序（成本太高）

## 项目结构
- [2026-04-13] 科研管理系统项目路径：`/root/.openclaw/workspace/程序设计/科研管理系统/`
- [2026-04-13] 数据库文件：`data/research.db`（SQLite）
- [2026-04-13] 上传文件目录：`uploads/`
