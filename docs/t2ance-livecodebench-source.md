# t2ance 对 LiveCodeBench v6 的补充来源

本入口仅针对原始 `test6.jsonl` 的 175 题中、CALIBRI 全量来源选择明确排除的
82 题。它不重选 CALIBRI 已覆盖的题目，也不改变已完成实验。公开来源固定为
`t2ance/code-solutions@eff8865b42b8badf2d8f7912b0ab9cfe5df3dc1a` 的
14 个 `data/lcb/*` Parquet 分片，按发布方 LFS SHA-256 逐个校验。

## 候选门槛

逐条要求题目 ID、标题、题面、平台、日期、难度和 starter code 与冻结的
LiveCodeBench v6 原题完全一致；上游 `passed=true` 且 `num_passed=num_tests>0`；
Python 程序与完整响应逐字对应；去除程序后的非代码说明至少 100 字符；
程序满足既有语法、函数入口和安全执行合同。候选按冻结种子和来源位置哈希
选择，每题一份，不依据 PALS 或模型评分挑选。所有拒绝原因及原始分片仍保留。

本地来源发现为 82 题中 60 题具有这种机械候选，另 22 题仍缺源。60 题共
2438 个原始测试用例等待独立 CPU 复验。用户本轮把复验缩为固定 7 题回归：
按照 functional-hard、functional-medium、stdin-hard、stdin-medium 四层
分别抽 2、1、3、1 题，层内按冻结种子和题目 ID 的哈希选取，不依据模型或
PALS 结果筛选。仅这 7 题执行隔离 CPU 测试，其余 53 题明确记为
`held_without_cpu`，不得称作已独立验收，也不得进入构图入口。外部 `passed`
不代替本项目执行证据；
CPU 通过也不代替 DAG 来源忠实、依赖充分与模型审核，更不是官方/人工 gold。

## 复现与边界

`scripts/prepare_t2ance_source.py` 下载并固定公开分片，输出选择、排除和
逐条来源哈希。`scripts/prepare_t2ance_full_execution.py --canary-seven` 在 B1 CPU 分配中
重建原题测试并准备既有 seccomp 执行器的输入；
`scripts/audit_t2ance_full_execution.py` 从原题、冻结候选和作业产物独立重放
输入及结果。候选代码只在 Linux seccomp 子进程中运行，绝不在下载、导入、
登录节点或本地预检阶段执行；隐藏测试也不发送给构图模型。

这是一类 `t2ance_model_output` 派生数据。它必须与 CALIBRI 派生来源分层
报告；统一 DAG 导出只有在独立 CPU 和语义审核都通过后才能进行。

## 构图入口

`t2ance-lcb-normalize-v1` 沿用 CALIBRI 的节点、依赖和九项额外语义审核
标准，但来源字段明确是 `t2ance_output`，并使用独立提示词与预算账本。
先从 CPU 通过题中按 I/O × 难度分层哈希选 5 题做 canary；只有来源忠实、
依赖闭合和审核协议通过，才考虑剩余题目。代码预设新来源活动上限
240 请求、1600 万保守预留 token；这只是防溢出的上界，不是已经花费或
保证成功的额度。每次准备的更小分配和实际请求另行记录。

本地回归测试含来源哈希、字段严格对齐、外部代码不执行、构图路由和旧
CALIBRI 协议回归。只有被抽样且独立 CPU 测试通过的题目才能准备构图；
回归样本的通过率不外推至剩余 53 题。
