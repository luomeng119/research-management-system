# T04 受控文件与版本服务

## 结果

在 T03 已验收基线上实现唯一的正式文件存储边界：所有新文件以临时流式写入、内容签名/类型/大小验证、SHA-256、UUID 相对路径和同卷 `os.replace` 入库；PostgreSQL 三表记录逻辑文件、版本和业务对象关联；下载、预览、归档只接受数据库 ID，不接受用户路径。

## 基线与边界

- 基线提交：`0f18fb894e8aa34091f8afd6548c322a5ec1cb7d`。
- T04 只证明新文件服务及 `preview/documents` 的适配边界，不宣称三个项目、标准、模板、报销等旧直读磁盘路由已全部迁移。
- 约 10 个 `BUSINESS_USER` 同权；不增加创建人 ACL、项目成员 ACL、目录权限树或维护角色业务特权。
- 不新增 migration；使用 T02 已存在的 `stored_files`、`stored_file_versions`、`object_files` 唯一约束与索引。

## 允许写入

- `app/repositories/files.py`
- `app/services/files.py`
- `app/web/__init__.py`
- `app/web/files.py`
- `app/routes/preview.py`
- `app/routes/documents.py`（仅持久文件下载/预览适配；内存生成 DOCX 保持）
- `app/__init__.py`（仅构造/注册 FileService 与文件蓝图）
- `config.py`（仅文件根、上限、预览上限）
- `app/tests/test_file_service.py`
- `app/tests/test_postgres_runner_safety.py`、`scripts/test_postgres.sh`（仅接入 T04 真实 PostgreSQL 契约）
- 本任务运行证据 `task-4-report.md`

## 必须实现

1. 严格 RED → GREEN → REFACTOR；先写 `test_file_service.py`，记录安全契约失败，再实现。
2. `FILE_STORAGE_ROOT` 默认 `DATA_DIR/files`，含 `.staging`；新正式文件不得落在源码目录或旧 `uploads`。原文件名只作展示元数据，磁盘名由 UUID 生成。
3. 上传顺序：认证/CSRF → 对象类型白名单与对象存在 → 同卷 staging 流式写入并累计大小/SHA-256 → 扩展名与签名一致性 → PostgreSQL 事务内二次确认对象 → 插入逻辑文件/V1/关联 → `os.replace` → 同事务成功审计 → 提交。移动后任意异常必须回滚 DB、删除本次最终文件并清理临时文件。
4. 支持 PDF、PNG/JPEG/GIF/BMP/WebP、docx/xlsx/pptx、受控文本、ZIP/RAR；未知、空文件、伪扩展、双扩展、NUL 文本或超限返回 415/413。客户端 MIME 不可信；旧 doc/xls/ppt 只可在可靠 OLE 边界内接受，否则拒绝，不增加新依赖。
5. 新版本对逻辑文件 `FOR UPDATE`，验证精确对象关联、ACTIVE、期望逻辑 `version`、同扩展/媒体类型；V1 不变，V2 使用新物理路径，逻辑 `version+1`。冲突返回 409。
6. 归档仅设 `stored_files.status=ARCHIVED`，不删文件、版本和关联；归档后下载、新版本、预览均拒绝。
7. 列表按精确 `objectType+objectId`。下载/预览以 `fileId+versionNo` 查 DB，并验证精确对象关联或至少一个现存业务对象；不存在和对象不匹配统一 404。
8. 即使 DB 中 `storage_path` 被污染，也必须拒绝绝对路径、`..`、反斜线越界、双重编码、symlink、非普通文件；`resolve()` 后必须在存储根内。所有 HTTP 参数不得直接拼磁盘路径。
9. 预览：图片/PDF 内联；文本最多 200 KiB；docx/xlsx 在 `FILE_PREVIEW_MAX_BYTES` 下受控解析，xlsx 最多 5 个 sheet、每表 100 行；不支持、损坏或超限返回 `PREVIEW_UNAVAILABLE` 和同一授权下载 URL，响应不得含 `str(e)`、路径或堆栈，下载仍独立可用；文件响应加 `X-Content-Type-Options: nosniff`。
10. 使用 T03 `business_required`、`current_identity`、`AuditService`；`file_operation_completed` 仅写允许的低敏属性，不记录原文件名、路径或正文。高风险写操作审计失败时失败闭合并补偿。
11. `preview.py` 改为 ID 兼容适配器，停止接受任意 `path`。`documents.py` 只收紧持久文件路径入口，不重写整个报销模块。
12. 不实现病毒扫描平台、OCR、内容审核、对象存储、去重、异步队列、在线 Office 转换、加密文件系统、CDN 或跨对象共享管理。

## 验证

- `.venv/bin/python -m pytest app/tests/test_file_service.py -q`
- `.venv/bin/python -m pytest app/tests/test_auth_audit.py app/tests/test_baseline_security.py app/tests/test_postgres_runner_safety.py -q`
- `scripts/test_postgres.sh postgresql://localhost/rm_v1_t04`，必须在隔离 PostgreSQL 验证真实事务、`FOR UPDATE`、唯一约束和补偿。
- `.venv/bin/python -m compileall -q app scripts`
- `git diff --check 0f18fb894e8aa34091f8afd6548c322a5ec1cb7d..HEAD`

测试至少覆盖：未登录/CSRF/两个业务账号同权；合法类型；`../`、反斜线、绝对路径、双重编码、伪扩展、双扩展、空文件、超限；错误对象；同名不覆盖；V2/V1 内容不变；并发/旧 version 冲突；写入、移动、审计、提交故障补偿；污染 DB 路径和 symlink；归档；全部预览成功与降级下载。

报告必须记录 RED/GREEN、变更文件、提交 SHA、真实 PostgreSQL 输出摘要、完整回归和剩余强杀孤儿窗口。进程在移动后提交前被强杀的极短窗口作为 V1 已知风险，留给 T12 文件/DB 对账，不为此增加两阶段状态机。
