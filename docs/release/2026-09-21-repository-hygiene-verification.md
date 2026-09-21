# 2026-09-21 仓库卫生验证记录

## 文件结果

### 原样复制到 `docs/delivery/20260911`

以下 14 个产品交付文件从 `/Users/lm/Desktop/LJ` 按原文件名复制，逐文件 `cmp` 与 SHA-256 一致：

1. `00-文档目录与阅读顺序.md`
2. `01-需求确认.md`
3. `02-产品设计与功能范围.md`
4. `03-系统架构与本地AI设计.md`
5. `04-系统流程与界面设计.md`
6. `05-执行与测试合并计划.md`
7. `06-系统介绍与演示入口.md`
8. `07-最终交付验收表.xlsx`
9. `08-测试与回归验证报告.md`
10. `09-现场演示遗留问题复核.md`
11. `10-端到端验证样例科研报告.docx`
12. `11-现场启动与演示说明.md`
13. `12-交付文件校验值.txt`
14. `13-启动科研管理系统.command`

`14`—`25` 号 dev-workflow 技能材料在 `docs/delivery` 中均不存在。`12-交付文件校验值.txt` 内仍保留原始来源目录的历史条目，不表示这些技能材料被复制。

### 新增或更新的当前说明

- 更新：`.gitignore`、`app/.gitignore`、`README.md`
- 新增：`docs/delivery/20260911/README.md`
- 新增：`docs/delivery/20260921/README.md`
- 新增：`docs/release/2026-09-21-no-local-model-handoff.md`
- 新增：`docs/release/2026-09-21-repository-hygiene-verification.md`
- 增补历史状态提示：`docs/release/2026-09-11-leadership-intro-handoff.md`、`docs/release/2026-09-11-leadership-intro-rollback.md`

## 检查结果

| 检查 | 结果 |
| --- | --- |
| `00`—`13` 数量 | 14 个，PASS |
| 来源与复制件逐文件 `cmp` | 14/14 一致，PASS |
| `14`—`25` 文件排除 | 0 个进入 `docs/delivery`，PASS |
| XLSX/DOCX ZIP 完整性 | 两个压缩包均无错误，PASS |
| 历史 `.command` 权限 | 仍为可执行，PASS；仅保留取证，不作为当前启动入口 |
| 当前 README 本地链接 | 20 个检查目标全部存在，PASS（新增本记录链接后复查为 21 个） |
| 发布排除样例 | 12 个样例覆盖模型/分片、`.env`、secrets、数据库、uploads、sessions、logs、evidence、cache 和 build，均被忽略，PASS |
| 必需保留样例 | `app/tests/fixtures/**` 与 `docs/delivery/**` 均不被忽略，PASS |
| 文本差异格式 | `git diff --check` 通过 |

## 边界

本记录验证仓库文档与忽略规则，不替代应用功能回归、模型资产删除验证、Git 历史秘密扫描或远端仓库克隆复核。共享工作树中的其他应用代码和测试改动未由本包修改、重置或验收。
