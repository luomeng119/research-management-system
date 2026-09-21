# 科研管理系统

> 面向科研院所的跨平台 Web 内网管理系统，覆盖科研提案、项目、专家、设备资源、通用表格和文档管理。

> **2026-09-21 当前交付状态：**项目本地生成模型及下载残片不进入本次交付，AI 功能默认关闭并从界面隐藏。系统默认启动只依赖 Python、PostgreSQL 和本地文件存储，不需要启动、下载或配置本地模型。2026-09-11 文档中的 Qwen/llama.cpp/AI 演示内容是历史交付证据，已由[当前交付说明](docs/delivery/20260921/README.md)取代。

公开源码仓库：[luomeng119/research-management-system](https://github.com/luomeng119/research-management-system)。任何同事可无需邀请直接克隆；推送仍需个人 GitHub 身份和仓库写权限。

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
- **人工科研报告**：支持报告创建、编辑、版本、敏感词规则和 DOCX 导出
- **AI 扩展边界**：相关实现代码仅作为未来受控扩展保留；当前交付默认不展示、不初始化、不可调用

## 技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | Flask 3.1.3 |
| 模板引擎 | Jinja2 3.1.6 |
| 数据库 | PostgreSQL + SQLAlchemy Core + Alembic；SQLite 仅用于旧数据迁移和测试 |
| Session | Flask-Session 0.8.0 + cachelib 本地文件存储 |
| 文档解析 | python-docx / openpyxl / pdfminer.six / PyMuPDF |
| OCR | RapidOCR（onnxruntime） |
| AI | 当前交付无运行依赖；相关扩展代码默认关闭并隐藏 |
| 前端 | 原生 HTML/CSS/JS + 本地化 Quill 编辑器（无外网 CDN） |
| Python | 3.13.x |

## 快速开始

### 环境要求

- Python 3.13.x（当前已验证的项目运行基线）
- Node.js（仅浏览器端到端测试和开发工具需要，不是生产运行前提）
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

默认配置 `AI_FEATURES_VISIBLE=False`、`AI_PROVIDER=DISABLED`，无需模型文件、模型端口或云端 API。当前交付中的 AI 专用入口和路由不属于可用功能；手工提案、项目管理和人工报告链路独立运行。

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
│   ├── llm/                # 保留的 AI 扩展代码（默认不加载）
│   ├── ocr/                # OCR 识别
│   ├── utils/              # 工具（模糊匹配/导入进度）
│   ├── templates/          # Jinja2 模板
│   └── tests/              # 测试
├── run.py                  # 启动入口
├── config.py               # 全局配置
├── requirements.txt        # Python 依赖清单
├── inference_server.js     # 保留的历史推理适配器（默认不启动）
├── SPEC/                   # 需求文档（REQ-001 ~ REQ-020）
├── docs/                   # 技术文档 + 开发文档（本包新增）
├── migrations/             # Alembic 数据库迁移
├── scripts/                # 离线安装、启停、备份、恢复与验收
└── data/                   # 开发/兼容数据；正式数据位于 APP_DATA_ROOT
```

`scripts/build_offline_bundle.py` 和 PowerShell 脚本是已实现的 Windows x64 适配层，不是跨平台产品的唯一部署边界。Linux/macOS 与其他架构需生成各自的依赖和 PostgreSQL 运行制品，不得复用 Windows 二进制包冒充验收。

POSIX 验收脚本面向 macOS/Linux 开发机；当前已在 macOS 实测以下平台中立核心运行链，Linux 仍需在真机单独复验：

```bash
bash scripts/test_postgres.sh postgresql://localhost/rm_v1_t10 -- \
  bash scripts/acceptance_posix.sh
```

该命令使用隔离的真实 PostgreSQL、原始 `run.py`/Waitress、正式登录与 Chromium 业务页面，并保留脱敏验收证据；当前证据只证明本次 macOS 开发主机的运行链，不等于 Linux、Windows 或 macOS 净机离线安装包验收。

## 文档导航

| 文档 | 说明 |
|---|---|
| `docs/技术文档.md` | 系统架构、模块划分、数据库设计、部署方式 |
| `docs/开发文档.md` | 目录结构、启动/测试方法、开发规范、踩坑记录 |
| `SPEC/索引.md` | 全部需求文档索引（REQ-001 ~ REQ-020） |
| `SPEC.md` | 需求池总览 |
| `ENGINEERING_REQUIREMENTS.md` | 工程规范（数据库访问/前端/内网/文档预览等约束） |
| `ERROR_KNOWLEDGE.md` | Bug 模式知识库（历史 bug 及修复模式） |
| [`docs/delivery/20260921/README.md`](docs/delivery/20260921/README.md) | 2026-09-21 当前交付状态、运行边界与文档索引 |
| [`docs/delivery/20260911/README.md`](docs/delivery/20260911/README.md) | 2026-09-11 历史产品交付文件及其被取代说明 |
| [`docs/release/2026-09-21-no-local-model-handoff.md`](docs/release/2026-09-21-no-local-model-handoff.md) | 无本地生成模型交付说明与发布边界 |

## 版本

当前版本 `1.17.0`（见 `config.py` 的 `VERSION`）。
