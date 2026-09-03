# T05 SQLite 只读迁移与对账报告

## 本次修复

- 标准 `standards.file_path` 和旧参考模板 `doc_templates.file_path` 已纳入受控文件迁移；旧模板只进入 `reference_template_items`，不写论证编制 `doc_templates`。
- CLI 显式要求 `--storage-root`，`--dry-run` 只读输出确定性计划，不建 batch、不写库、不创建存储目录。
- 五个模板分类只按现存 `ACTIVE` 根目录的中文全称唯一匹配；未知、歧义、缺失、路径异常、格式/内容/大小不符、对象多候选和目标冲突均生成脱敏 issue，不自动归入“其他模板”。
- 运行时上传和迁移共用 `validate_file_candidate`；未扩大旧 Office 扩展名白名单。
- `file_id/version_id/link_id` 为绑定 manifest、源表/键、对象和 SHA-256 的 UUIDv5；物理路径为 `legacy/<manifest-prefix>/<file-id>/v1.<ext>`，不跨对象按内容去重。
- 文件先复制到私有 `.staging` 并复核，再在既有 PostgreSQL advisory-lock 事务内写受控元数据和确定性最终路径；普通失败回滚 DB 并清理本次 staging/最终文件，同 hash 强杀孤儿可复用，异 hash 硬失败。
- 报告和 target fingerprint 覆盖 `reference_template_items`、三张受控文件表及物理文件清单；已提交文件缺失/篡改时 verify 和重跑都返回 `BatchConflict`。
- 通用表格 current 必须指向本表已迁入版本；存在未锁版本时始终选择 `(version_number desc, version_id desc)` 的最新项，不保留过时 current，也不额外生成 working。仅当全部版本已锁定时，确定性复制最新版本中成功迁入的列和行为新的未锁定 working 版本，原历史不变；所有版本 `row_count` 按目标表实际行数重算并纳入对账指纹。
- `expense_invoice.file_path` 与 `expense_payment.file_path` 按 `INVOICE/<id>` 和 `PAYMENT/<id>` 迁入同一受控文件链；两类对象数值 ID 相同时仍独立生成确定性文件记录。只有目标财务对象成功迁入才建立关联；对象被业务转换拒绝时生成脱敏 issue。`expense_reimbursement.documents` 仅作为业务 JSON 保留，不进入文件路径解析。

## TDD 与验证证据

- RED：新增二进制迁移行为测试后，因 `plan_legacy_binaries` 尚未实现在 collection 阶段按预期失败。通用表格补充 RED 证实旧流程会把已锁定 `GTV-001` 直接设为 current，且在坏 JSON 行被拒绝后仍保留了虚假 `row_count=2`。财务附件补充 RED 证实原算法未盘点发票/支付 `file_path`，也未生成 `INVOICE`/`PAYMENT` 受控关联。
- 独立复核修复：补齐同内容 root/DB/附件身份替换阻断、commit 异常的外层文件清理、storage 全链 no-follow/dir-fd 操作，并将受控表默认生成的审计列纳入指纹。最终一致性复核在同一批 fd 绑定句柄上同时比对身份与 SHA-256，并在事务体末尾和 SQLAlchemy commit 事件边界各复核一次。失败清理按原绑定目录 fd 与 dev/inode/type 精确删除；本次文件被改名时在绑定目录内按 inode 找回，不删除后来占用原名的对象。
- focused + 受影响回归：`134 passed, 10 skipped`（legacy migration、FileService、reference library、legacy modules）。
- 隔离 PostgreSQL head schema runner：同 batch 双连接并发、标准+模板+发票+支付物理迁移、`FileService.open_version_stream`、回滚、Alembic/ACL 和完整 DB contract 通过。
- 交付包现存 `app/expense.db` 以 SQLite `mode=ro&immutable=1` 对账：`expense_reimbursement=0`、`expense_invoice=0`、`expense_payment=0`，SHA-256 为 `0e2b491225540a80af467029352d40ac7c624cb5a94ac46e89668badb5c1ba7f`。因真实源为空，本次只证明程序、合成夹具和空库对账，不声称已迁移真实历史财务附件。
- `compileall`、两个 CLI `--help` 和 `git diff --check` 通过。

## 边界

- 未修改 Alembic/schema、T10 路由/服务/模板、ledger 或 Graphify；未写普通用户上传审计，未伪造 actor。
- 来源文件系统与 PostgreSQL 无法参与同一原子事务；即使第二次绑定校验已紧邻 DBAPI commit，校验返回到实际提交之间仍有无法完全消除的极小窗口。正式迁移必须使原始介质只读并冻结迁移窗口；来源未冻结时，`custody_verified` 不得作为绝对证明。
- 若业务异常后数据库 rollback 本身再次失败，则提交结果可能不确定，且后续文件清理可能未执行。此复合失败下必须停止自动重跑，保留日志，核查 batch 记录、确定性物理路径和 SHA-256，在人工确认/恢复后再重跑。
- 当前结论仍为：**迁移程序和夹具验证通过，真实脱敏三库与真实附件迁移待人工验收**。
