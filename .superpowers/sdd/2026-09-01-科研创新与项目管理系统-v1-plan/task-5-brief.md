# T05 SQLite 只读迁移与对账任务契约

- Base：`1413b6707c4541fd1fa3aaaafe13990b19de72cf`
- 依赖：T02 PostgreSQL schema、T04 受控文件边界已验收。
- 写入面：`app/legacy_migration/`、`scripts/migrate_sqlite.py`、`scripts/verify_migration.py`、`app/tests/fixtures/legacy/`、`app/tests/test_legacy_migration.py`。

## 结果契约

1. 严格只读发现 `research.db`、`generic_tables.db`、`expense.db`；缺失、重复、symlink、活跃 WAL、完整性或外键检查失败时阻断。
2. 数据库 schema/content 与附件相对路径/大小/SHA-256 形成确定性 manifest；报告不含绝对路径、时间戳或随机 ID。
3. 同 batch 同 manifest 重跑不重复且报告哈希一致；同 batch 换源必须拒绝；约束、关联、主键、金额或对账失败时整个业务事务回滚。
4. Decimal、日期/时间、布尔和 JSON 显式转换；坏数据进入异常清单，敏感原值不进报告。
5. 附件仅做库存、安全路径、存在性、大小与哈希盘点；不复制、不直接写入 T04 文件表。
6. 对账至少覆盖记录转换/插入/复用/拒绝数、键与关联、规范化行哈希、金额汇总、JSON 问题和附件问题。

## 边界

- 不修改 Alembic，不建永久 staging，不调用旧 `init_db()`，不做双写、CDC、在线同步或 ETL 平台。
- `inference_server_status` 不迁移；`llm_models` 不带入绝对路径、密钥或运行状态。
- 成功批次的回退依赖迁移前 PostgreSQL 备份，不承诺逐行反向删除。
- 当前仅能验收迁移程序与合成脏数据夹具；未取得三份真实脱敏库前，结论必须保持“待实库验收”。

## 验收证据

- `python -m pytest app/tests/test_legacy_migration.py -q`。
- 同一脏数据夹具连续两次执行，目标数量不变，规范化报告哈希一致。
- batch 同名换源被拒绝；故障注入后业务表整体回滚。
- 三份 SQLite 与附件执行前后 SHA-256 不变；`compileall` 和 `git diff --check` 通过。
