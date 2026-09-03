# T10 财务辅助工具渐进迁移任务契约与报告

## 定位与范围

本模块是“辅助工具”中的报销材料整理，不扩展为财务核算系统。保留报销单、发票、发票明细、支付记录、手工/自动匹配、确认、单据填写和既有模板生成；不增加预算、总账、供应商、会计期间、审批链、财务接口、新角色或 AI 强依赖。交付包 `app/expense.db` 当前四张表均为 0 行，验收以程序、合成夹具和真实空库对账为准。

## 实现边界

- 新增 `app/repositories/expenses.py`、`app/services/expenses.py`；Service 管事务/校验/审计，Repository 只执行 SQLAlchemy 且不提交。
- `app/expense_db.py` 只保留必要旧签名兼容门面，不得包含 `sqlite3`、`DB_PATH`、`PRAGMA`、DDL、运行时建表或回退。
- 最小适配 `app/routes/expense.py`、`app/routes/documents.py`、`app/__init__.py`、必要的 `app/templates/expense/`、`app/document_engine.py` 与现有审计 allowlist；新增一个财务专项测试文件并复用现有回归。本文件末尾追加报告，不再新建本批 Markdown。
- 先等待 T05 发票/支付旧附件迁移通过；运行时不得接受任意 legacy 绝对路径 fallback。新附件只走受控 FileService，AI/OCR 不可用时手工流程仍可运行。

## 必须保留

- 页面与关键 URL：`/expense/`、`/expense/records`、`/expense/upload`、`/expense/payments`、`/expense/pending`、`/expense/fill`、`/expense/approvals`、`/expense/documents/`、`/new/<rid>`、`/templates`、`/print/<rid>`；当前缺模板页面可明确重定向到 records，不能 500。
- 现有 reimbursements/invoices/payments/match/pending/manual_match/stats、单据 CRUD/auto_fill/download/merge/generate_report API 和旧响应关键字段。
- 草稿、已确认、已生成文档等现有状态；采购报销/出差报销；未匹配/已匹配；精确金额的手工与自动匹配。不得把测试示例中的“日期过滤”或新会计规则擅自加入生产语义。
- 三个已有 DOCX 模板可真实生成；只有 JSON、缺少 DOCX 的科研物资采购申请单必须明确返回模板缺失，不能假成功。合并下载必须真实包含全部选定内容，不能只返回第一份。

## 正确性与安全

1. 所有金额用 `Decimal`/数据库 `NUMERIC`，严格日期与 JSON 上限；响应序列化保持旧格式，不暴露绝对路径、OCR 全文、完整证件号、数据库 DSN 或内部异常。
2. 报销单行锁保护状态、类型、支付标记、删除和 documents JSON；专用操作控制 `status/reimbursement_id/matched_*`，通用更新不得越权修改这些字段。
3. attach/detach/manual match/confirm/delete/总额重算单事务完成，逐个重验对象存在、同一报销单关系和未匹配状态；拒绝跨报销单 ID 篡改。
4. 自动匹配在 PostgreSQL 事务 advisory lock 与行锁下重新检查候选，候选数量和组合工作量封顶，超限提示手工匹配；并发不得重复占用发票或支付。
5. 报销编号、支付编号在并发下唯一且可重试，不使用无锁 `COUNT/MAX+1`；不新增未经确认的业务唯一键。
6. 新上传先按 FileService 策略验证；OCR/AI 是可选增强，失败时允许用户进入手工录入或返回清晰可恢复状态，不能阻断报销 CRUD、匹配、单据填写和下载。
7. 受控附件创建要定义业务行、file/version/link、物理文件和审计的提交/清理边界；删除业务对象必须按既有受控文件生命周期处理，不留孤儿。
8. 用户值使用 Jinja 转义、`textContent`/安全 DOM，不能进入动态 `innerHTML`、内联 `onclick` 或下载头；CSRF、登录和现有单一普通用户模式保持。
9. 列表、未匹配查询和明细必须数据库分页/有界。现有索引足够 V1 正确性；长期部分索引仅作为真实数据量与 `EXPLAIN` 后的 T02 性能候选，不在本批凭空加 migration。
10. `match_group_id` 是目标 schema 不存在且无实际功能的死字段，删除兼容门面引用，不新增列。

## TDD 与门禁

- 先保留 RED：Alembic head 下旧模块仍触碰 SQLite/不存在字段/缺模板页面或路径上传失败。
- 覆盖 CRUD、列表分页、状态与字段白名单、Decimal/date/JSON、发票明细、attach/detach/manual/auto match、确认/删除/总额、cross-rid、故障回滚、两连接编号和双匹配竞态、documents JSON 并发。
- 覆盖受控上传/读取/下载/删除、OCR 不可用的手工降级、异常无孤儿、无路径和敏感信息泄漏、XSS、三个真实 DOCX 生成、模板缺失、真实多文档合并。
- 运行财务专项、legacy/FileService/migration/主线受影响回归、隔离 PostgreSQL runner、全量测试、`compileall`、Shell 语法和 `git diff --check`。
- 独立复核必须 Spec PASS、Quality PASS、Critical 0、Important 0 才能登记完成。

## 执行报告

### 2026-09-03 实现批次

- RED：首个 Service 契约测试因 `app.services.expenses` 不存在失败；真实 DOCX 生成测试暴露 `resolve_auto_fields` 的 `datetime` 局部遮蔽异常。两项均在实现后转绿。
- 实现：报销、发票/明细、支付记录已迁移到 PostgreSQL Repository/Service；兼容门面不再包含 SQLite、DDL 或运行时建表。金额/日期/JSON 限额、字段白名单、状态锁、跨报销项拒绝、总额重算和审计均由 Service 管理。
- 并发/原子性：报销和支付编号使用 PostgreSQL advisory lock；自动匹配使用事务级 advisory lock + 行锁且封顶 20 个候选/50,000 次组合；attach/detach/manual/auto/confirm/delete/documents JSON 均在单事务内完成。故障注入验证业务行与匹配关系不会部分提交。
- 附件：新上传只通过 FileService，列表/详情返回受控 `fileId/versionNo`，前端使用 `/preview/file` 受控引用；删除发票/支付时在同一事务归档文件元数据、解除对象链接并删除业务行，不遗留活动孤儿链接。OCR 不可用时仍保存受控附件和待手工补录记录。
- 文档：三个现有 DOCX 模板均已真实生成 Word；仅有 JSON 的“科研物资采购申请单”明确返回模板缺失；多单据合并会真实复制全部选定内容，全量合并从 FileService 读取附件并控制文件/页数/像素上限。
- 安全/兼容：保留契约中页面和 API URL，缺页面转到 records；响应屏蔽 OCR 全文、绝对路径和完整证件号，内部异常不回显；修复报销页面中用户值进入动态 HTML/内联事件的相关 XSS sink。
- GREEN：本地财务专项 `11 passed, 12 skipped`（PostgreSQL 用例未注入时跳过）；隔离 PostgreSQL runner 为 `1 + 11 + 2 + 1 + 23 + 54 = 92 passed`，其中财务专项 `23 passed`；受影响回归 `150 passed, 53 skipped`；最终全量 `360 passed, 60 skipped`；`compileall`、`bash -n scripts/test_postgres.sh`、`git diff --check` 均通过。
- 残余边界：交付包财务四表为空，本批证据为真实 PostgreSQL 空库+合成业务数据，不代表已验收真实历史财务数据迁移；DOCX 结构与内容完整性已自动验证，最终版式仍需人工视觉验收。独立 Spec/Quality 复核未由本实现者代替。
