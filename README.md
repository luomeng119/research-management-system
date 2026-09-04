# 科研管理系统

> 面向科研院所的跨平台 Web 内网管理系统，覆盖科研提案、项目、专家、设备资源、通用表格和文档管理。

## 项目简介

科研管理系统是一套本地部署的 Flask Web 应用，断网时核心业务仍可运行：

- **设备与科研资源**：设备台账、设备选型、设备组、项目关联、数量/位置登记、批量导入和研制单位匹配
- **科研项目**：科研项目 / 密码项目 / 安全项目三类项目的立项、文档、日志管理
- **专家库**：专家信息、专家分组、批量导入
- **通用表格**：可自定义的通用表格管理，支持版本快照、行染色、列拖拽、列宽调整
- **文档管理**：项目文档上传、在线预览（Word/Excel/PDF/图片/文本）
- **经费登记**：采购相关的报销、发票、付款和附件登记；不包含预算、会计核算或审批流
- **标准法规**：标准法规库管理
- **方案论证**：方案论证文档管理
- **原 AI 辅助**：保留原本地文档校对与检索代码，默认关闭，不作为 V1 核心验收项
- **科研提案助手**：可选调用在线 DeepSeek API；未配置或断网时不影响手工提案及其他业务

## 技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | Flask 3.1.3 |
| 模板引擎 | Jinja2 3.1.6 |
| 数据库 | PostgreSQL + SQLAlchemy Core + Alembic；SQLite 仅用于旧数据迁移和测试 |
| Session | Flask-Session 0.8.0 + cachelib 本地文件存储 |
| 文档解析 | python-docx / openpyxl / pdfminer.six / PyMuPDF |
| OCR | RapidOCR（onnxruntime） |
| AI | 可选 DeepSeek 在线 API；原本地校对/检索代码默认关闭 |
| 前端 | 原生 HTML/CSS/JS + 本地化 Quill 编辑器（无外网 CDN） |
| Python | 3.13.x |

## 快速开始

### 环境要求

- Python 3.13.x（当前已验证的项目运行基线）
- Node.js（仅原本地校对代码或离线验收工具需要，不是核心业务运行前提）
- 浏览器客户端：Windows、Linux、macOS 上的现代 Chromium 内核浏览器
- 服务端核心：Python 3.13 + Waitress + PostgreSQL，不绑定单一操作系统；安装包需按目标 OS/CPU 单独构建和真机验证

### 安装依赖

```bash
# 联网环境
pip install -r requirements.txt
```

```powershell
# 已有 Windows 平台适配层（不代表产品只支持 Windows）
$env:APP_DATA_ROOT = 'C:\ResearchManagementData'
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -OfflineRoot .\offline
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

### 启动

```bash
# 开发环境；正式运行必须同时配置 DATABASE_URL
export DATABASE_URL='<PostgreSQL 连接地址>'
export FLASK_SECRET_KEY='<由部署环境安全保管的稳定随机值>'
python run.py
```

启动后访问 `http://127.0.0.1:5001/`。非测试环境未配置 `FLASK_SECRET_KEY` 时应用会拒绝启动。

> AI 是可选增强。未配置 DeepSeek API、断网或原本地模型不可用时，手工提案及其他核心业务仍可正常运行。

### 首次启动

- 部署前显式配置并持久保管稳定的 `FLASK_SECRET_KEY`；多实例必须使用同一值
- 通过受控的部署初始化流程准备数据库结构和基础数据，不依赖应用启动执行 DDL
- 通过受控的管理员初始化流程显式创建首个管理员；系统不创建默认账号或默认密码

## 目录结构概览

```
04-科研管理系统/
├── app/                    # 应用源码（核心）
│   ├── routes/             # 路由（20+ 个功能模块）
│   ├── repositories/       # PostgreSQL 数据访问
│   ├── services/           # 业务服务
│   ├── llm/                # 本地大模型（校对/检索）
│   ├── ocr/                # OCR 识别
│   ├── utils/              # 工具（模糊匹配/导入进度）
│   ├── templates/          # Jinja2 模板
│   └── tests/              # 测试
├── run.py                  # 启动入口
├── config.py               # 全局配置
├── requirements.txt        # Python 依赖清单
├── inference_server.js     # Node.js 推理服务器
├── SPEC/                   # 需求文档（REQ-001 ~ REQ-020）
├── docs/                   # 技术文档 + 开发文档（本包新增）
├── migrations/             # Alembic 数据库迁移
├── scripts/                # 离线安装、启停、备份、恢复与验收
└── data/                   # 开发/兼容数据；正式数据位于 APP_DATA_ROOT
```

`scripts/build_offline_bundle.py` 和 PowerShell 脚本是已实现的 Windows x64 适配层，不是跨平台产品的唯一部署边界。Linux/macOS 与其他架构需生成各自的依赖和 PostgreSQL 运行制品，不得复用 Windows 二进制包冒充验收。当前可在 macOS/Linux 使用 `bash scripts/test_postgres.sh postgresql://localhost/rm_v1_t10` 对平台中立的 PostgreSQL 核心执行真实集成测试。

## 文档导航

| 文档 | 说明 |
|---|---|
| `docs/技术文档.md` | 系统架构、模块划分、数据库设计、部署方式 |
| `docs/开发文档.md` | 目录结构、启动/测试方法、开发规范、踩坑记录 |
| `SPEC/索引.md` | 全部需求文档索引（REQ-001 ~ REQ-020） |
| `SPEC.md` | 需求池总览 |
| `ENGINEERING_REQUIREMENTS.md` | 工程规范（数据库访问/前端/内网/文档预览等约束） |
| `ERROR_KNOWLEDGE.md` | Bug 模式知识库（历史 bug 及修复模式） |

## 版本

当前版本 `1.17.0`（见 `config.py` 的 `VERSION`）。
