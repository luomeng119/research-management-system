# T05 SQLite 只读迁移与对账报告

## 实现结果

- 三库仅通过 `mode=ro&immutable=1` + `query_only` 读取；执行 `quick_check` / `foreign_key_check`，拒绝缺库、重复候选、未知表/字段、缺目标表、root/DB/uploads/attachment symlink 以及 WAL/SHM/journal。迁移结束再校验源 dev/ino/size/mtime/hash 和 sidecar。
- manifest 覆盖 DB 内容/schema/行哈希、引用与未引用附件的 size/SHA；不含绝对路径、时间戳或随机 ID。`raw_value` 仅保存 type/length/SHA 安全元数据。
- 单 PostgreSQL 业务事务；进入即取固定 `pg_advisory_xact_lock`并在锁内重查。同 batch 重跑和同 manifest 异 batch 均先校验 report hash + 目标投影指纹，目标漂移/报告篡改硬失败；异 batch 直接复用原报告，不新增 alias/batch/数据。
- 坏日期/金额/JSON 进脱敏 issues 并以 `completed_with_issues` 完成；孤儿关系、PK/自然键/目标约束冲突、JSON 内部 ID 缺失、未知角色属结构错误，整体回滚且不留 completed batch。失败批次不另起事务记录，已在报告明确该边界。
- 无时区旧 datetime 显式按 `Asia/Shanghai` 解释；`admin/管理员` 映射 `SYSTEM_MAINTAINER`，`user/用户` 映射 `BUSINESS_USER`。明文口令不迁移，96 位 legacy hash 可保留，均需改密。
- `inference_server_status` 整表显式跳过；`llm_models.file_path` 无论相对/绝对都不迁，密钥字段不迁。不写 T04 文件表、不复制附件、不建永久 staging。成功批次仅备份恢复，不做逐行反向删除。
- 第三轮终审修正：三库同时验证最小必需表、必需字段及非空业务证据；Decimal 依目标 `Numeric(precision, scale)` 量化，任何非零精度损失整体阻断。提交前按迁移键独立重查目标行、受控字段和 Decimal 汇总。
- 目标指纹已改为批次对象范围：保存每表确定性迁移键，按键重查受控列与生成 id/version/状态；合法新增行不影响旧批次，已迁移行的身份/版本改动会被捕获。issues 按 batch_id 过滤并将 id/batch_id 纳入指纹。
- 所有 SQLite 与附件先复制到 `0700` 私有临时快照，文件为 `0600`。在 discovery 前记录源根目录身份，打开后比对 root fd；其下逐级使用 dirfd + `O_DIRECTORY/O_NOFOLLOW` 打开目录和文件，比对枚举 stat、fstat 及初始 identity/hash，拒绝根目录祖先或附件父目录置换。迁移只读快照，新迁、复用、verify 返回前都再从原源重建完整快照 manifest（含附件）并比对，快照必定清理。

## TDD 与新鲜证据

- 初始 RED：新增主测试后因 `app.legacy_migration` 不存在失败。Review 修正 RED：因 `verify_completed_batch` 未实现而 collection error。
- 本地专项：`.venv/bin/python -m pytest app/tests/test_legacy_migration.py -q` → `17 passed, 1 skipped`。新增覆盖缺必需表/字段、`1.235 -> Numeric(18,2)` 阻断、合法新行不影响批次指纹、版本篡改、数据库文件替换、附件父目录置换及源根目录祖先置换。
- 真实临时 PostgreSQL 14 + `0001_v1_core`：双连接同 batch 并发测试 `1 passed, 17 deselected`；两者返回同报告，仅 1 batch/1 次数据；旧项目最大 ID=11 后新插入 ID=12，identity 序列已对齐。
- 组合回归（T05 + auth/audit + DB contract + file service + runner safety）：`108 passed, 25 skipped`。`compileall`、两 CLI `--help`、行尾空白检查与 `git diff --check` 通过。
- 独立代码与安全终审：`APPROVED`，未发现仍成立的 P0/P1。
- `verify_migration.py` 已复用与迁移重跑完全相同的 manifest/report/目标指纹验证函数。批次成功时写入 `completed_at`。

## 状态与边界

- 未修改 Alembic、现有业务代码或 ledger，未提交 Git。
- 当前正式状态：**迁移程序和夹具验证通过，真实脱敏三库迁移待验收**。
