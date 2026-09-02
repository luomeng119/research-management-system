# T07 验收复核

- 产品边界：业务系统本地部署；V1 仅选配在线 DeepSeek，不配置本地提案模型。无 Key、断网、超时或 API 异常不阻断手工业务。
- 工程证据：`194 passed, 34 skipped, 1 deselected`；编译、diff、代码复审和安全复审通过，无 P0/P1/P2。
- 真实 DeepSeek V13 评测：`20/20 schema`、`20/20 forbidden`、p95 `3.421s`、最大 `3.636s`；provider 原输出和 finalized 输出均保留且哈希可复核。
- “张老师”、“李老师”为 AI 代理模拟评审，不是真人验收；保守评分为事实 `51/52`、字段 `21/22`、缺失信息 `46/50`，严重幻觉 `0`。
- 不可变原始 API 证据 SHA256：`91bd7906a262a64a398c42b2f44b7a53b8b49692804bb70e512fd999edbdb095`；追加代理评审后证据 SHA256：`63e0d9f4319810fe440fb69a73fb2bed9d0b517b42eafaf471d4fafabbff4dc8`。两份 JSON 均与本验收记录同目录保管。

T07 工程及内部代理质量门结论：**APPROVED**。该结论不代表真人 UAT，也不代表已完成最终部署环境验收。
