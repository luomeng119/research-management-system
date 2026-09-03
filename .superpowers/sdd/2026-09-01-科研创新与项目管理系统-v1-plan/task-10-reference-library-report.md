# T10 Batch 2 实施报告 — 标准库与参考模板文件库

## 结果

保留标准法规库和科研模板文件库原有入口，已将其元数据读取、目录树、上传、
重命名、归档和下载从旧 SQLite/业务目录迁至 PostgreSQL 元数据与受控
`FileService`。标准和模板的首个文件均使用 T04 已验收的
`FileService.upload_new_object(...)`；它们的 metadata、文件版本、关联和审计在
同一事务边界内写入，异常不遗留标准/模板行或受控文件。

## TDD 与验证

- RED：` .venv/bin/python -m pytest app/tests/test_reference_library.py -q`
  在实现前得到 `1 failed, 5 errors`；每个失败均为预期的
  `ModuleNotFoundError: app.repositories.reference_library`，证明运行时 repository/service
  尚不存在，而非测试拼写或环境错误。
- GREEN：同一 focused suite 最终为 `8 passed`。覆盖标准上传、重复 active 名、
  archive、归档后写拒绝、控制下载、模板文件夹/文件 CRUD、plain/encoded traversal、
  inactive ancestor、stored XSS、旧 download URL、debug/test 路由移除、未登录和
  服务未就绪，以及 FileService 审计失败的首上传回滚。
- 复核修复 RED → GREEN：新增受控预览引用、原始 URI 边界、真实嵌套 multipart、
  文件夹级联归档、同事务 audit rollback、审计日志和 stored-XSS 页面探针后，
  `pytest app/tests/test_reference_library.py -q` 为 `15 passed`。首轮 RED 是
  缺失树节点 file/version 引用、归档后 direct TEMPLATE 写仍可通过、以及 audit
  异常仍提交文件夹；三项均已转绿。
- 终审修复 RED → GREEN：真实 client 使用 `follow_redirects=True` 曾证明
  `%2f/%2F/%5c/%252f` 会先被路由层归一化并下载同名模板；现由模板 blueprint
  注册的 WSGI raw-URI guard 在路由匹配前返回 `INVALID_TEMPLATE_PATH`。TXT、DOCX、
  XLSX 的 `/preview/file` JSON 响应补齐 `success: true`，与共享预览面板的成功契约
  一致；PDF 继续返回受控流。审计日志现在通过 `object_files` 将
  `file_operation_completed / FILE / UPLOAD` 安全归属到 STANDARD 或 TEMPLATE，
  在 module/operator/file/operation 过滤后才截取 100 条。新增回归含标准/模板隔离和
  101 条后仍可找到更早上传事件。
- 影响面回归：
  `pytest app/tests/test_file_service.py app/tests/test_auth_audit.py
  app/tests/test_baseline_security.py app/tests/test_legacy_modules.py -q`
  为 `111 passed, 9 skipped`；`compileall` 与 diff check 通过。
- 最终受影响面回归：
  `pytest app/tests/test_reference_library.py app/tests/test_file_service.py
  app/tests/test_auth_audit.py app/tests/test_baseline_security.py
  app/tests/test_legacy_modules.py -q` 为 `129 passed, 9 skipped`。
- 隔离 PostgreSQL：
  `scripts/test_postgres.sh postgresql://contract_t02` 通过；新增 T10 runtime
  contract 得到 `1 passed, 27 deselected`，并随后通过 legacy、db-contract 与 file
  service 的 PostgreSQL slices。它实际写入标准/模板受控文件，验证下载、受控预览
  引用、级联归档和归档后 direct TEMPLATE 写拒绝，确认 `doc_templates` 行数不变。
- `python -m compileall -q app` 和 `git diff --check` 通过。
- 全量 `pytest app/tests -x -q` 在本次修复实际停于：
  `app/tests/test_generic_tables_req020.py::test_req020_migrate_old_data`，
  SQLite fixture 中 `generic_tables` 为空（`150 passed, 41 skipped, 1 failed`）。
  这是 generic-tables fixture/data contract 的未满足前置条件，不在 T10 文件库范围；
  本报告不将其归为已验证的“既有”问题，也未改动该模块。

## 改变与保留

- 保留 `/standards/`、`/standards/upload`、`/standards/download/<doc_id>`、
  `/standards/delete/<doc_id>` 与模板现有 upload/download/create/rename/delete URLs；
  delete URL 现在执行 archive 并返回“已归档”。
- 模板树只读取 `reference_template_folders`/`reference_template_items`，保留
  财务、会务、公文、方案、其他五个顶级文件分类；“会务模板”仅为文件分类。
- `/templates/download/<path:filepath>` 将路径解析为规范化 logical key，拒绝绝对路径、
  dot/`..`、多次解码、编码分隔符、重复匹配、inactive item/ancestor；仅将
  `FileService.open_version_stream` 的受控流发送给客户端。对于 WSGI 可见的
  RAW_URI/REQUEST_URI，编码 `/` 或 `\\` 在 Werkzeug redirect/route normalization 前被拒绝。
- 删除 `/templates/debug_tree` 和 `/templates/test_tree` 的生产注册；不再创建、遍历、
  重命名或删除业务目录，不再 raw `send_file` 业务 OS 路径。
- 模板页面保留单文件/文件夹上传、创建子目录、下载、预览、重命名、归档及安全
  展开/折叠。文件和目录名通过 DOM `textContent` 与事件监听器处理，不拼接为 HTML/JS。
  标准列表同样只在 autoescaped data attributes 中保存文件名，事件监听器读取后调用
  `openControlledPreview(fileId, versionNo, objectType, objectId, ...)`；共享预览组件以
  四个受控引用参数请求 `/preview/file`。旧 `openPreview` 调用保留给未纳入本批的页面。
  无提交后端动作的旧 checkbox 仅曾传播勾选状态，现已移除其无效控件和死代码，并非移除
  批量业务能力。
- `doc_templates` 未被本库写入，继续只承载 argumentation schema。

## 延后与风险

- T05 仍负责旧 SQLite 业务目录和不在 FileService allowlist 内的 legacy binary 的
  归档/迁移决策；本批没有为兼容旧二进制而放宽文件校验。
- T04 已知的进程在物理 `os.replace` 后、数据库提交前被强制终止时的短暂孤儿文件窗口
  仍由后续 T12 对账处理，未在本批扩展为双阶段存储协议。
- 历史日志页 URL 仍可进入，但不会读取旧 `OperationLogModel`；新文件行为由现有审计服务
  记录，并按旧 operator/file_name/date/operation 参数从 PostgreSQL `audit_events`
  （加 users 与库元数据名称）查询。若需将历史日志迁移至新审计查询，应另行确认范围。
