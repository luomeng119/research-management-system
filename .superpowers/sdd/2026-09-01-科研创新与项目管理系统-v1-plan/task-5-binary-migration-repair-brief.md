# T05 修复：标准/模板旧二进制迁入受控存储

## 目标与边界

仅补齐现有只读 SQLite 迁移器对 `standards.file_path` 与旧参考模板 `doc_templates.file_path` 的二进制迁移。原三库和附件只读；不改 Alembic/schema，不新增业务功能，不迁其他附件，不调用普通 `FileService.upload()`，不扩大 `.doc/.xls/.ppt` 等旧格式白名单，不把未知模板分类自动归入“其他模板”。

旧参考模板是模板库数据：成功迁移到 `reference_template_items`，不得作为论证编制 schema 写入 `doc_templates`。标准对象使用 `STANDARD/standards.doc_id`，模板对象使用 `TEMPLATE/reference_template_items.template_id`。旧 `file_path` 不进入运行时数据库；成功迁移后相应旧表兼容字段必须为 `NULL`。

## 允许写入面

- 新增 `app/legacy_migration/binaries.py`
- 修改 `app/legacy_migration/{core.py,__init__.py}`
- 修改 `app/services/files.py`，只允许抽出供运行时上传和迁移共同使用的纯文件名/内容校验函数
- 修改 `scripts/{migrate_sqlite.py,verify_migration.py}`
- 修改 `app/tests/{test_legacy_migration.py,test_file_service.py}` 与必要的 legacy fixture
- 更新既有 `task-5-report.md`

不得修改 Alembic、`app/repositories/files.py`、T10 路由/服务/模板、论证编制运行时或其他业务模块；不得修改 ledger/Graphify。

## 必须实现

1. 提供只读 dry-run 计划：不创建 batch、不写数据库、不创建 storage root。每项报告 sourceTable/sourceKey/fieldName/sourceLogicalPath（不安全值仅类型、长度、hash）/exists/sizeBytes/sha256/objectType/objectId/fileId/versionNo/storagePath/issueCode；汇总 referenced/eligible/planned/issues/totalBytes。
2. CLI 显式接收受控 `--storage-root`；迁移目标必须是 PostgreSQL（测试专用 SQLite 仍仅限既有显式开关）。source root 与 storage root 不得互相包含，storage root/逐级目录不得是 symlink；POSIX 下拒绝 group/world writable。
3. 有效源文件必须沿用 T04 的文件名、扩展名、大小与内容策略。把该策略抽成共享纯校验入口，`FileService._stage()` 也使用它，禁止复制漂移的第二套策略。未知/不支持格式、内容不匹配、超限、缺失、路径异常、对象标识缺失、同对象多候选、目标冲突或模板分类无唯一匹配，形成脱敏 issue；源根/数据库/附件目录 symlink、身份变化、TOCTOU 仍硬阻断。
4. 模板分类只接受当前存在、`ACTIVE`、根级、名称完全相同且唯一的五类目录；不推断英文别名。没有明确匹配时不创建模板对象/文件关联，报告 issue，保留待人工确认边界。
5. 每项使用 UUIDv5 确定性 `file_id/version_id/link_id`，输入至少绑定 source manifest、source table/key、object type/id 与文件 SHA-256；versionNo=1；确定性 storagePath 不含日期和用户路径，例如 `legacy/<manifest-prefix>/<file-id>/v1.<ext>`。不跨对象按内容去重，不写普通用户上传审计，不伪造 actor。
6. 先从私有绑定快照复制到 storage `.staging` 并复核 size/hash，再在现有 advisory-lock PostgreSQL 事务内：创建模板对象（如适用）、写 `stored_files/stored_file_versions/object_files`、issues、batch summary，并以排他/no-follow 方式落到确定性最终路径。不得覆盖既有目标；既有普通文件只有 size/hash 完全一致时可作为上次强杀孤儿复用，否则硬失败。
7. 普通异常回滚数据库，并只删除本次创建的最终文件和 staging。进程强杀留下的确定性孤儿允许下次验证接管。已提交批次的文件缺失或 size/hash 变化时，`verify_completed_batch` 与同 batch 重跑必须 `BatchConflict`，不得静默重建。
8. 报告、target fingerprint 与 completed-batch verification 必须覆盖 `reference_template_items`、`stored_files`、`stored_file_versions`、`object_files` 以及受控物理文件清单；同 batch 重跑无新增行/文件且报告一致，同 manifest 异 batch保持既有复用语义；源三库及附件迁移前后 hash 不变。

## 测试先行与门禁

先提交或在报告中保留可核对 RED 证据，再实现 GREEN。至少覆盖：dry-run 零写入和确定性；标准+模板真实迁移并由 `FileService.open_version`/stream 读取；旧路径置空和模板域分离；幂等/并发；missing、absolute/UNC、`..`、控制字符、unsupported、bad content、too large、未知/歧义分类；source/storage 包含、symlink、TOCTOU；事务失败零 DB/FS 残留；同 hash 孤儿恢复、异 hash 拒绝；已提交文件缺失/篡改验证失败；报告 hash/fingerprint；真实 PostgreSQL head schema 下迁移、并发、回滚和读取。

运行 T05 focused、T04 file-service 回归、T10 reference-library/legacy 回归、完整隔离 PostgreSQL runner、`compileall` 与 `git diff --check`。提交一个边界清晰的实现 commit；报告真实测试结果和残余风险。

## T10 前置一致性补充：通用表格

现有 schema 足够，不新增迁移表。旧数据迁移必须在同一 T05 事务内满足：

- `generic_tables.current_version_id` 必须存在、属于同一 `table_id`，且指向可写版本；跨表引用或无版本属于结构错误，不得静默指向别表。
- 若旧表已有版本但全部 `is_locked=true`，按既有 REQ-020 语义复制最新版本为新的未锁定 working version，并将 current 指向它；原锁定历史不得解锁或覆盖。新版本标识、编号和复制结果必须确定、幂等并进入报告指纹。
- 所有 `generic_table_versions.row_count` 必须在坏 JSON 等行被拒绝后按实际成功迁入的 `generic_table_data` 数量重算；不得保留旧库声明值。
- 测试覆盖跨表 current 拒绝、全锁定版本生成未锁定 current、坏行后 row_count=实际数、同批/同 manifest 复用、故障回滚和真实 PostgreSQL 迁移对照。

## T10 财务前置：旧发票与支付附件

在进入财务运行时迁移前，沿用本任务既有受控文件算法，将 `expense.db` 中 `expense_invoice.file_path` 与 `expense_payment.file_path` 纳入迁移。交付包当前 `expense.db` 的报销、发票、支付均为 0 行，因此只能验收程序、合成夹具和真实空库对账，不得声称迁过真实历史附件。

- 发票映射 `object_type=INVOICE`、支付映射 `object_type=PAYMENT`，`object_id` 使用已迁入 PostgreSQL 的原数值 `id` 字符串；必须先验证对象存在且类型匹配。
- 仅接受相对 `source_root` 的受控路径，复用既有文件名/内容/大小、no-follow、来源保管、确定性 UUID/路径、事务、回滚、孤儿恢复、物理校验和指纹规则；不得新增任意旧路径运行时 fallback。
- 成功后对应 PostgreSQL `file_path` 必须为 `NULL`，文件通过 `stored_files`、`stored_file_versions`、`object_files` 和 `FileService.open_version_stream()` 读取；失败或歧义生成脱敏 issue，不静默丢弃路径。
- `expense_reimbursement.documents` 在当前产品中是单据模板及字段 JSON，不是物理附件路径，本批保持原 JSON，不把其中普通字段误迁成文件。
- 不修改 Alembic，不扩展文件白名单，不引入财务核算、OCR/AI、角色或新模块。
- TDD 至少覆盖 invoice/payment 成功读取、missing/invalid/unsupported、对象缺失、两类同 ID 不冲突、同批/同 manifest 幂等、故障回滚、文件篡改、源 hash 不变、真实 PostgreSQL head schema 及交付包真实空库 dry-run/对账。
