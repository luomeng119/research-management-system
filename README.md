# 科研管理系统

> 面向科研院所的设备知识库、科研项目、专家库、通用表格、文档管理一体化内网管理系统。

## 项目简介

科研管理系统是一套运行在内网（离线）环境的 Flask Web 应用，覆盖科研管理全流程：

- **设备知识库**：设备选型、设备组、批量导入（Excel）、研制单位匹配（精确/模糊）
- **科研项目**：科研项目 / 密码项目 / 安全项目三类项目的立项、文档、日志管理
- **专家库**：专家信息、专家分组、批量导入
- **通用表格**：可自定义的通用表格管理，支持版本快照、行染色、列拖拽、列宽调整
- **文档管理**：项目文档上传、在线预览（Word/Excel/PDF/图片/文本）
- **报销管理**：报销单据、审批流、文档打印
- **标准法规**：标准法规库管理
- **方案论证**：方案论证文档管理
- **AI 辅助**：本地小模型文档校对、智能检索（可选，需模型文件）

## 技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | Flask 3.1.3 |
| 模板引擎 | Jinja2 3.1.6 |
| 数据库 | SQLite（标准库 sqlite3，无 ORM） |
| Session | Flask-Session 0.8.0（filesystem 存储） |
| 文档解析 | python-docx / openpyxl / pdfminer.six / PyMuPDF |
| OCR | RapidOCR（onnxruntime） |
| AI 推理 | llama-cpp-python（GGUF 模型，CPU）+ Node.js 推理服务器 |
| 前端 | 原生 HTML/CSS/JS + 本地化 Quill 编辑器（无外网 CDN） |
| Python | 3.13.x |

## 快速开始

### 环境要求

- Python 3.13.x（3.14 尚未发布，务必用 3.13）
- Node.js（仅文档校对/推理服务器需要，缺失会自动跳过）
- 目标平台：Windows 10/11 x64（内网离线环境）

### 安装依赖

```bash
# 联网环境
pip install -r requirements.txt

# 离线环境：使用 offline_packages/（需自行携带，未随包分发）
# offline_packages/install.bat
```

### 启动

```bash
# 项目根目录下
python run.py
```

启动后访问 `http://127.0.0.1:5001/`，默认账号 `admin` / `admin123`。

> 注：`run.py` 会异步尝试启动 Node.js 推理服务器（端口 18789）。若 node 未安装或模型文件缺失，会自动跳过，**不影响 Flask 启动**，仅文档校对功能不可用。

### 首次启动

- 自动创建 `data/research.db`、`data/generic_tables.db` 等数据库文件
- 自动创建默认管理员账户 `admin / admin123`
- 自动初始化主机设备分类、研制单位、知识子类等基础字典数据

## 目录结构概览

```
04-科研管理系统/
├── app/                    # 应用源码（核心）
│   ├── routes/             # 路由（20+ 个功能模块）
│   ├── models*.py          # 数据模型（SQLite）
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
├── data/                   # 运行时数据库（不随包分发，首次启动自动生成）
└── uploads/                # 上传文件目录（运行时生成）
```

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
