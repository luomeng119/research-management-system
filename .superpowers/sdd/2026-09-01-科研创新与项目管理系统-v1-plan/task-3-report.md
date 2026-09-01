# T03 实施报告

- Base：`b33245d5a0d1c8c257aa632494e1166ce1cf3c90`
- 结果：建立 PostgreSQL 账号认证、两类正式账号、统一业务权限、维护边界、服务端 Session、CSRF、登录限速、追加式审计与本地脱敏日志。

## TDD 证据

- 初始 RED：认证/审计首轮用例在实现前失败；独立审查补充用例再次得到 `9 failed, 23 passed`，覆盖模型维护接口越权、旧会话未失效、模板遗留角色分支、失败操作未审计及客户端伪造 request ID。
- GREEN：`.venv/bin/python -m pytest app/tests/test_auth_audit.py -q` 首次得到 `34 passed`；独立复审补充日志旁路用例并修复后得到 `35 passed`。
- 认证、T01 安全基线与 runner safety：`.venv/bin/python -m pytest app/tests/test_auth_audit.py app/tests/test_baseline_security.py app/tests/test_postgres_runner_safety.py -q` 得到 `47 passed`。
- 真实 PostgreSQL：`scripts/test_postgres.sh postgresql://localhost/rm_v1_t03` 在隔离临时集群完成 Alembic 升降级、运行角色权限验证、`43 passed` 数据库契约及 `1 passed` 真实认证/审计链路；临时集群随后清理。
- 完整测试：`.venv/bin/python -m pytest app/tests -q` 得到 `70 passed, 28 skipped, 1 failed`。唯一失败仍是既有 `test_req020_migrate_old_data` 对空夹具强制要求存量表格，不属于 T03 回归。
- 静态角色/负责人权限扫描无命中；模型状态与重载端点均由 `maintenance_required` 保护。
- `.venv/bin/python -m compileall -q app scripts` 与 `git diff --check` 均通过。

## 关键实现

- `users` 正式账号经 SQLAlchemy Core repository 访问；新密码使用 bcrypt，旧明文及 salted-SHA256 仅在成功登录后升级。
- Session 保存最小身份与账号版本；账号停用、密码重置或角色/版本变化立即使旧会话失效。
- 两个正式角色为 `BUSINESS_USER` 和 `SYSTEM_MAINTAINER`；业务账号之间不再按目录或项目负责人分权，账号及模型维护操作单独隔离。
- 账号启停、重置、登录、改密、退出、维护越权及 CSRF 拒绝写入受控审计事件；高风险账号操作在审计失败时回滚。
- 请求 ID 始终由服务端生成；限流器并发访问加锁；日志消息与结构化字段采用允许清单，不输出任意消息正文或敏感值。
- 应用、根及模块 logger 统一使用脱敏 JSON handler，应用 logger 关闭传播，避免异常原文绕过允许清单。

## 范围与剩余风险

- 未新增复杂 RBAC、会议/任务/专家评审、AI 业务能力、Redis、外部认证或 UI 重设计。
- 登录限速按 V1 单进程约束采用进程内有界存储，进程重启后计数清零；后续若部署形态改为多进程需重新评估。
- 完整套件的既有空库夹具失败保留给对应数据迁移任务处理，不在 T03 越界修补。
