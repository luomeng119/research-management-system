# T05 SQLite 只读迁移与对账报告

## 本次修复

- 标准 `standards.file_path` 和旧参考模板 `doc_templates.file_path` 已纳入受控文件迁移；旧模板只进入 `reference_template_items`，不写论证编制 `doc_templates`。
- CLI 显式要求 `--storage-root`，`--dry-run` 只读输出确定性计划，不建 batch、不写库、不创建存储目录。
- 五个模板分类只按现存 `ACTIVE` 根目录的中文全称唯一匹配；未知、歧义、缺失、路径异常、格式/内容/大小不符、对象多候选和目标冲突均生成脱敏 issue，不自动归入“其他模板”。
- 运行时上传和迁移共用 `validate_file_candidate`；未扩大旧 Office 扩展名白名单。
- `file_id/version_id/link_id` 为绑定 manifest、源表/键、对象和 SHA-256 的 UUIDv5；物理路径为 `legacy/<manifest-prefix>/<file-id>/v1.<ext>`，不跨对象按内容去重。
- 文件先复制到私有 `.staging` 并复核，再在既有 PostgreSQL advisory-lock 事务内写受控元数据和确定性最终路径；普通失败回滚 DB 并清理本次 staging/最终文件，同 hash 强杀孤儿可复用，异 hash 硬失败。
- 报告和 target fingerprint 覆盖 `reference_template_items`、三张受控文件表及物理文件清单；已提交文件缺失/篡改时 verify 和重跑都返回 `BatchConflict`。

## TDD 与验证证据

- RED：新增二进制迁移行为测试后，因 `plan_legacy_binaries` 尚未实现在 collection 阶段按预期失败。
- focused + 受影响回归：`114 passed, 10 skipped`（legacy migration、FileService、reference library、legacy modules）。
- 隔离 PostgreSQL head schema runner：同 batch 双连接并发、标准+模板物理迁移、`FileService.open_version_stream`、回滚、Alembic/ACL 和完整 DB contract 通过。
- `compileall`、两个 CLI `--help` 和 `git diff --check` 通过。

## 边界

- 未修改 Alembic/schema、T10 路由/服务/模板、ledger 或 Graphify；未写普通用户上传审计，未伪造 actor。
- 当前结论仍为：**迁移程序和夹具验证通过，真实脱敏三库与真实附件迁移待人工验收**。
