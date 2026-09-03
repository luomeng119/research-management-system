# T10 通用表格渐进迁移任务契约与报告

## 范围

将现有通用表格从运行时 SQLite 直连迁移到已发布的 PostgreSQL/Alembic `generic_*` 表。保留现有页面、URL、返回结构和表格、版本、动态列、行、颜色、分页、统计、快照、回滚、比较、Excel 导入导出能力。不得新增一级模块、会议/任务管理、复杂权限、AI、向量库、RAG 或 Agent；不得修改已发布 schema，若证实 schema 不足则停止并退回 T02。

## 文件所有权

- 新增：`app/repositories/generic_tables.py`、`app/services/generic_tables.py`
- 适配：`app/models_generic_tables.py`（仅作旧调用兼容门面）、`app/routes/generic_tables.py`、`app/routes/api.py`、`app/__init__.py`
- 必要时最小修改：`app/templates/generic_tables/`
- 测试：新建或改造通用表格专项测试、`app/tests/test_legacy_modules.py`、`scripts/test_postgres.sh`
- 本文件末尾追加真实 RED/GREEN、变更、验证、风险和提交 SHA；不再新建本批其他 Markdown。

## 实现约束

1. Repository 只执行 SQLAlchemy 查询，不提交事务；Service 拥有事务、校验和审计边界。运行时不得再含 `sqlite3`、`DB_PATH`、`PRAGMA`、DDL、`ALTER TABLE` 或 `init_db()`。
2. 所有写操作先以 `SELECT ... FOR UPDATE` 锁定父表；版本必须存在且属于该表，只有 `current_version_id` 指向的未锁版本可编辑。历史/锁定/跨表写入均拒绝；不得删除 current 版本，允许受控删除非 current 历史版本。
3. 创建快照和回滚必须单事务完成：在父表锁内分配唯一递增版本号，通过数据库内复制列和行；任一步失败全部回滚。并发创建不得产生重复版本号或多个 current。
4. `set_current_version`、比较、回滚、复制和删除必须验证同表关系；不得用调用者提供的跨表版本 ID 绕过边界。列表与详情默认打开 current，历史 URL 保持只读。
5. JSON 行必须是对象；只允许 JSON 标量及有界列表/对象，限制单元格、单行、批量行数和嵌套深度，拒绝 NaN/Infinity 和不可序列化日期。PostgreSQL 时间转换为旧接口可接受的字符串。
6. 导入只接受 `.xlsx`，明确拒绝 `.xls`；先解析/校验，再在单事务中执行 replace 或 append。上传临时文件在成功和异常路径均清理；设置合理文件、工作表、行列上限，避免无界内存/数据库扫描。
7. 导出 `.xlsx` 保持现有列顺序和标题；字符串以 `= + - @` 开头时作公式注入防护。临时导出文件须在响应结束后删除，不能永久遗留。
8. 用户数据不得进入 `innerHTML` 或内联 `onclick` 字符串；必要模板改用 Jinja 转义、`data-*` 与 `textContent`。保持现有视觉与功能，不做 UI 重设计。
9. 搜索/分页在数据库侧有界执行；`page_size=-1` 仍需受 V1 最大行数约束。统计和比较不得跨表，不得依赖全库 Python 扫描。
10. 路由从 `app.extensions` 获取 service；兼容门面保持必要旧签名/字典形状，供仪表盘及未迁移调用方使用，但不得回退 SQLite。

## TDD 与验收

- 先提交/保留 RED：证明当前构造访问 SQLite 或无法在 Alembic head schema 下工作，并为上述保护写失败测试。
- 覆盖表/版本/列/行 CRUD，current/locked/cross-table 保护，快照/回滚故障原子性和 PostgreSQL 并发，导入 replace/append/非法 JSON/日期/上限/`.xls` 拒绝，导出公式防护和临时文件清理，历史只读页面、比较、搜索/分页、旧 URL/响应兼容，以及源码无运行时 SQLite。
- 运行专项测试、`app/tests/test_legacy_modules.py`、受影响回归、隔离 PostgreSQL runner、`compileall` 和 `git diff --check`。
- 独立复核必须得到 Spec PASS、Quality PASS、Critical 0、Important 0 才可登记完成。

## 执行报告

### TDD 证据

- RED：`.venv/bin/python -m pytest app/tests/test_generic_tables_postgres.py -q` 在收集阶段因 `app.repositories.generic_tables` 不存在失败，证明原实现没有 PostgreSQL repository/service 边界。
- GREEN：通用表格专项、REQ-020、版本回归、原有模块回归及立项回归合计 `56 passed, 4 skipped`；扩展后受影响集为 `60 passed, 4 skipped, 1 deselected`。
- 隔离 PostgreSQL：`scripts/test_postgres.sh postgresql://localhost/rm_v1_t10` 通过；并发快照为 `1 passed, 6 deselected`，迁移往返、最小运行角色 ACL、其他必跑回归均通过，临时集群已清理。
- 全量：`322 passed, 48 skipped, 2 failed`。其中 1 个是本批修改拒绝文案后的断言，已兼容旧字串；剩余 1 个是基线已存在的 T04 runner 旧计数断言（期望 `3 passed`，当前真实为 `7 passed`），不在本批文件所有权内；排除该已知旧断言后为 `60 passed, 4 skipped, 1 deselected`。
- 静态：`python -m compileall -q app`、`bash -n scripts/test_postgres.sh`、`git diff --check` 均通过；新运行时边界无 `sqlite3/DB_PATH/PRAGMA/DDL/init_db`，模板不再把用户值插入 `onclick` 或动态 `innerHTML`。

### 变更与边界

- 新增 SQLAlchemy repository 和 service，兼容门面仅从 `app.extensions` 取服务；无 SQLite 回退。
- 写操作锁定父表并校验 current/未锁/同表；快照、回滚为单事务数据库内复制，注入失败会全部回滚。
- 保留表、版本、列、行、统计、比较、颜色、页宽、分页和 Excel URL/返回形状；历史版本只读，跨表比较/回滚/切换被拒绝。
- 导入仅允许 `.xlsx`，有 20 MiB、200 列、5000 行、JSON 深度/单元格/单行上限；日期 ISO 化，NaN/Infinity 拒绝。导出防公式注入，上传异常路径和响应完成后均清理临时文件。
- 未修改 Alembic schema，未新增一级模块、AI、会议、任务、角色、向量库、RAG 或 Agent。

### 已知 V1 取舍与提交

- `page_size=-1` 的“全部”仍受 5000 行上限约束；这是防止内存和数据库无界扫描的 V1 边界。
- `.xls` 明确不支持，用户需转为 `.xlsx`。
- 实现提交：`1b15afc feat: migrate generic tables to PostgreSQL`。
- 导出表头/嵌套 JSON 及模板 JS 安全加固：`7d8e484 fix: harden generic table export values`；加固后专项 `11 passed, 1 skipped`（PostgreSQL 项仅在隔离 runner 中执行）。
