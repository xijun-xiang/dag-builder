# LiveCodeBench v6：审计后的 CPU60 流转版本

`livecodebench_cpu53_export.py` 不重新构图、不更改旧 CALIBRI 回修的 35 个终态，也不重新选择题目。它从已审计的 175 题分层流转版本出发，独立重放新 53 题四个 CPU 批次的冻结来源、输入、隔离控制及逐题结果；只有原先标为 `held_without_cpu` 的 t2ance 行可以变为 `dag_not_run` 或 `cpu_not_passed`。旧七题与新 53 题必须互斥且恰好覆盖 60 个源候选，接受候选 JSONL 和统一 DAG 查看器字节不变。

此版本把“参考代码通过 CPU”与“DAG 被模型审核”拆开。CPU60 更新后的 175 题分母仍有 45 条模型审核接受候选、55 条 CPU 已过但未构 DAG、40 条待复核、11 条拒绝、22 条无来源、2 条 CALIBRI CPU 限额失败。`human_approved=0`、`formal_eligible=false`。这不能称为 50% 高质量 DAG 覆盖。
