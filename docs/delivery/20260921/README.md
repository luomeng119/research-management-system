# 2026-09-21 当前交付说明

## 当前状态

- 当前源码交付不依赖本地生成模型；项目模型权重、下载分片和模型运行数据不属于仓库内容。
- `AI_FEATURES_VISIBLE=False`、`AI_PROVIDER=DISABLED` 是默认运行边界：不初始化生成模型服务，不展示 AI 功能入口，AI 专用路由不可作为当前能力使用。
- 科研提案、项目、资料、专家、设备、简单经费、人工科研报告、版本、敏感词规则和 DOCX 导出仍按独立业务链运行。
- 相关 AI 实现代码仅作为未来受控扩展边界保留；重新启用需要单独配置、验证和发布，不属于本次交付。
- 源码以公开仓库 [luomeng119/research-management-system](https://github.com/luomeng119/research-management-system) 交付；无需邀请即可克隆，推送仍受 GitHub 身份与写权限控制。

## 默认启动

当前启动只需要 Python、PostgreSQL 和受控本地文件目录，不需要模型文件、模型进程、模型端口或云端 AI 密钥。

```bash
pip install -r requirements.txt
export DATABASE_URL='<PostgreSQL 连接地址>'
export FLASK_SECRET_KEY='<由部署环境安全保管的稳定随机值>'
python run.py
```

默认开发地址为 `http://127.0.0.1:5001/`。正式部署仍应通过受控流程初始化数据库和首个管理员，不使用默认口令。

## 当前依据

| 文档 | 用途 |
| --- | --- |
| [需求与验收](01-需求与验收.md) | 当前交付范围、排除项、决策与AC-01—AC-06验收标准 |
| [执行计划与状态](02-执行计划与状态.md) | 工作包、工作流/Goal/Orca状态、完成证据与下一步 |
| [结果与验收](03-结果与验收.md) | 当前完成情况、逐项验收状态和远端发布结果 |
| [模型资产清理证据](04-模型资产清理证据.md) | 删除数量、释放空间、残片扫描和受保护模型复核 |
| [真实浏览器端到端验收](05-真实浏览器端到端验收.md) | 登录、提案、报告 v1/v2、DOCX、敏感词库和 AI 隐藏的真实浏览器结果 |
| [现状审计](../../reports/2026-09-21-remove-local-model-audit.md) | 记录模型资产、AI 入口、人工报告链路和仓库安全基线 |
| [差异分析](../../reports/2026-09-21-remove-local-model-gap.md) | 定义无本地模型目标状态及修复/验证矩阵 |
| [实施计划](../../plans/2026-09-21-remove-local-model-refactor.md) | 定义门禁、资产清理、文档、验证与发布顺序 |
| [设计实现基线](../../design-to-code/2026-09-21-ai-hidden-fidelity-baseline.md) | 固化 AI 隐藏后的页面与交互契约 |
| [发布交接](../../release/2026-09-21-no-local-model-handoff.md) | 当前发布边界、历史材料说明和检查入口 |
| [回滚说明](../../release/2026-09-21-no-local-model-rollback.md) | 代码、模型能力、数据和GitHub发布的恢复边界 |
| [仓库卫生验证](../../release/2026-09-21-repository-hygiene-verification.md) | 记录复制完整性、链接、忽略规则和发布文件检查结果 |
| [2026-09-11 历史交付](../20260911/README.md) | 保留 `00`—`13` 原始产品材料，不作为当前模型/AI 状态说明 |

## 历史说明

2026-09-11 的文档和验收样例保留当时 Qwen3.5-9B、llama.cpp、本地模型端口及 AI 演示结果，是可追溯历史证据。涉及“当前模型”“启动模型”或“AI 入口可见”的文字已由本页明确取代；不得用历史成功记录证明 2026-09-21 交付仍包含模型能力。
