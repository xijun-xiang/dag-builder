# E3 V2 扩展运行：四基准全量、MMLU 100 题

用户于 2026-09-30 明确确认：GPQA 198、GSM8K 1319、HumanEval 164、
LiveCodeBench v6 175 全量，MMLU 约 100。冻结为 1956 题／模型，三模型共
5868 条输出预算；不是 5868 道独立题。沿用 V2 生成提示和已验收的解耦解析，
不修改采样温度、token 预算、g/G/M/W 或安全代码判分器。

## 抽样和复用

- 原 MMLU 库存为 1876 道唯一原题、21 个子学科，不新引入数据。
- 固定 100 题：保留已生成 V2 第二批的 8 道开发题；对其余题按子学科分层，
  尽量按库存题量比例分配名额（每科至少 1 道），用固定 SHA256 排序选取。
  名额已扣除上述 8 题，不查答案或过程分数。8 道是显式保留的开发层，不冒充
  100 道均为全新随机抽样。manifest 保存每科名额与完整题号。
- 四个全量基准保留原 batch 成员和 seed；MMLU 保留旧 8 题 batch，其余
  92 题确定性组成至多 8 题的批次。三模型使用同一问题和批次成员。
- Phi 现有 V2 40 条原文与 attempts 按字节复制进新运行，校验哈希；不再调用
  generate。旧 run 及其失败记录只读。Qwen V1 不能冒充 V2，单独归档。
- 外层 expansion_id 固定本次题量、解析和执行代码；raw 的 source protocol_id
  仍指相同冻结 V2 生成内核。两层身份均验证，不把派生产物冒充旧 canary。
- manifest 额外保存 V1/V2 已接触开发题号，正式分析可同时报告去除这些题的
  敏感性结果。共同评分者子集按原规则在新范围内固定，197 题，尚未执行。

## 执行边界

原有 V2 三模型配置／数值参考必须先通过。新的 prepare 在 Slurm CPU 作业内
核验源协议、源文件和未改变的生成／评分／判分内核，建立全新运行根。
首 wave 为原 V2 五个第二批共 40 题；其后每 wave 至多 8 个 batch（64 题）。
每个模型共 32 waves。每 wave 顺序：8 GPU generate → 8 GPU score → 无 GPU
CPU evaluate＋audit。阶段分别落盘，不自动重试；started 无 raw 立即报错。
前 wave 未审计通过不能推进该模型下一 wave。单次 GPU 作业至多沿用 4 小时
上限，不提高 batch 或上下文预算。调度控制最多一个八卡阶段同时运行。

生成作业不打开 grading 内容；CPU 判分保持既有 seccomp、文件／网络拒绝探针
和资源上限。运行代码安全故障保留证据并停止，不能记作模型答错后继续。
审计保存每题过程与答案状态、g/G/M/W、逐 token 证据、原始输出哈希和真实分母。

已有总体 90%、每基准 75% 的工程覆盖门槛作为继续运行信号；不因正常 M=0、
负 g 或错误答案停止或筛题。某 wave 覆盖不足时保留全部记录并报告，不自动
更改提示或采样。少量无效题从相应过程统计中排除，但不删除原始记录、不填零。
W 不足步骤时 N/A。准确率按独立答案状态报告：未评测／歧义／隔离器不支持与
普通答错分开，不能把过程无效题静默从任务正确率分母删除。

## 代码入口

`python -m pals_validation.e3.expanded prepare --source-root SOURCE --root NEW --mmlu-count 100`

`python -m pals_validation.e3.expanded generate|score|evaluate --root NEW --slot MODEL --wave N --shard I`

`python -m pals_validation.e3.expanded audit --root NEW --slot MODEL --wave N`

`b1-e3-expanded-prepare.sbatch` 只冻结输入，`b1-e3-expanded.sbatch` 配合
`e3_expanded_launch.py` 执行单个有界阶段；这些入口都不自行提交后续作业。
