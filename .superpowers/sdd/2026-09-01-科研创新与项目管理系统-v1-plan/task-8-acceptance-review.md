# T08 验收复核

- 真实临时 PostgreSQL：`0001 → 0003`、降级保护、再升级、全局编号唯一、双线程立项竞争、DB contract 和 ACL 全部通过。
- 最终回归：`222 passed, 35 skipped, 1 deselected`。排除项是既有 `test_req020_migrate_old_data`，它依赖当前工作区已有通用表格数据，与 T08 无关。
- 专项回归：`27 passed, 1 skipped`；跳过项只是未单独传入外部 PostgreSQL URL 时的并发用例，该用例已由临时 PostgreSQL runner 实际通过。
- 代码、数据库、安全复核均为 **APPROVED**，P0/P1/P2 为 0；长期大数据量下 OFFSET 分页与索引优化仅作 P3 观察，当前 10 人本地部署 V1 不扩展设计。

T08 工程验收结论：**APPROVED**。该结论不代表真人 UAT 或最终部署环境验收。
