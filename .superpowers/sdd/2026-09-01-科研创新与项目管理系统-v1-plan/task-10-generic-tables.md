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

### 最终实现

- 运行时已切换为 SQLAlchemy repository/service，兼容门面仅从 `app.extensions` 取服务，无 SQLite 回退或运行时 DDL。
- 父表锁、current/锁定/同表校验、快照和回滚事务、并发版本号唯一性、跨表防护已落地。未锁但非 current 的历史版本在后端、页面控件和 JavaScript 中都只读。
- `.xlsx` 导入保留水平表头和垂直数据合并语义；列表页改为“先解析，再单事务创建/填充/切换 current”，旧两步导入 URL 返回稳定 `ATOMIC_IMPORT_REQUIRED` 且不产生空版本。
- 导入有 20 MiB 压缩文件、100 MiB 解压总量、50 MiB 单条目、200 倍压缩比、200 列和 5000 新增行上限；空/仅表头文件也先校验可编辑目标。
- 列表及搜索使用数据库 `limit/offset`。导出、比较、统计和删列遇到超过 5000 行时显式返回 `RESULT_TOO_LARGE`，不再静默截断或部分成功；`page_size=-1` 同样显式受该上限约束。
- 版本比较包含 `row_color`；导出表头/值防公式注入，嵌套 JSON 稳定序列化；临时导出文件在响应迭代器先关闭后删除，兼容 Windows。未预期异常只返回稳定代码和非敏感文案。
- 导出文件名会移除 CR/LF、控制字符和路径分隔符；`send_file` 响应构造失败时也立即清理临时文件。详情页 GET 遇到缺失 `current_version_id` 时稳定返回 409，不再自动写库修复。
- 未修改 Alembic schema，未引入新一级模块、AI、会议、任务、复杂权限、向量库、RAG 或 Agent。

### 最终验证证据

- 专项：`.venv/bin/python -m pytest app/tests/test_generic_tables_postgres.py app/tests/test_generic_tables_req020.py app/tests/test_generic_tables_versions.py -q` → `25 passed, 1 skipped in 1.16s`；跳过的是需要隔离 PostgreSQL 的并发项。
- 受影响回归：通用表格专项 + `test_legacy_modules.py` + `test_project_establishment.py` 最终为 `72 passed, 4 skipped in 2.03s`；条件跳过项均由隔离 runner 覆盖。
- 隔离 PostgreSQL：`scripts/test_postgres.sh postgresql://localhost/rm_v1_t10` 最终退出码 `0`；通用表格并发快照 `1 passed, 14 deselected`，迁移往返、ACL 失败回滚、最小运行角色、立项/生命周期/专家库/数据库合同回归均通过，临时集群已删除。
- 全量：`.venv/bin/python -m pytest -q` → `340 passed, 48 skipped in 56.37s`，无失败。
- 静态：`.venv/bin/python -m compileall -q app`、`bash -n scripts/test_postgres.sh`、`git diff --check` 均退出 `0`；运行时目标文件搜索无 `sqlite3/DB_PATH/PRAGMA/ALTER TABLE/CREATE TABLE/init_db`。

### 提交

- 初始迁移与加固：`1b15afc`、`7d8e484`。
- 独立复核 10 项 Important 修复：`7ab9b6e fix: close generic table review findings`。
- PostgreSQL runner 安全测试改用稳定完成标记：`479c139 test: use stable postgres runner completion marker`。
- 导出异常清理与 GET 只读完整性修复：`f901151 fix: close generic export and read integrity gaps`。
