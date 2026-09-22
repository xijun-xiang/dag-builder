# CALIBRI → LiveCodeBench v6：小规模来源与执行验收

本轮只复用 CALIBRI 的参考材料，不修改 PALS 公式、E1/E2 算子或旧数据。
运行状态与数值以私有目录中的清单、逐题结果、哈希验证为准。

## 固定来源和样本

- 数据集：`lavis-nlp/CALIBRI`，revision
  `7a4a7dc7aceb1b4a426c48761d0f128f2daf6a54`。
- Qwen3 与 GPT-OSS 的 LCB 配置各有 train/validation/test 三文件；
  全部六文件下载后按固定字节数与 SHA256 校验，原文件不改写。
- 对照既有 LCB `test6.jsonl` 的 175 题，以题号、标题、题目全文精确匹配，
  不使用模糊匹配，不把累计 release_v6 的其他题混进本批。
- 每模型每题十个原始输出和通过标签；上游通过标签只是筛选条件，
  不是本项目执行通过，也不是解释或 DAG 正确的保证。
- 候选还要求程序在原输出中原样存在、代码静态格式有效、围栏完整且
  至少 100 字符的非代码文本。这是便宜的候选发现规则，不是语义质量标准。

| 覆盖层级 | Qwen3 | GPT-OSS | 两者并集 |
|---|---:|---:|---:|
| 本批题号匹配 | 175 | 175 | 175 |
| 至少一个上游测试通过输出 | 69 | 82 | 93 |
| 至少一个通过上述机械规则的候选 | 61 | 82 | 93 |

本轮使用 `calibri-lcb-source-v2`。v1 对 GPT-OSS 紧接输出标记的代码围栏
处理不当，曾少算候选；v2 已修正并增加回归测试。历史 v1 不删除、不用于
最终覆盖统计。合格推理、合格 DAG 的数量尚不能从上表得出。

按本项目源题难度，候选覆盖为 easy 43/43、medium 29/52、hard 21/80。
这是明显的选择偏向；若仅使用这些候选，需报告为筛选子集，不能声称
完整 v6 的代表性结果。这里的“候选”仍不等于实际复验通过。

仍用原来 seed=20260922 的五题，不替换失败题。每题在两个模型全部
机械合格输出中按 SHA256(seed, origin) 选一个，不按 PALS 或置信度选。

| 原题 | 来源模型 / sample_index（从 0 开始） | 本次计划测试数 |
|---|---|---:|
| 3709 | GPT-OSS / 9 | 33 |
| abc396_a | Qwen3 / 5 | 45 |
| 3759 | GPT-OSS / 8 | 42 |
| abc398_c | GPT-OSS / 4 | 42 |
| 3781 | 两个模型的二十份输出均无通过标签，排除、不替换 | 未执行（43） |

## 已实现的可复现入口

```bash
PYTHONPATH=src python -m dag_builder.calibri_source \
  --cache /absolute/private/calibri-cache \
  --source /absolute/private/original-lcb-canary5 \
  --output /absolute/private/calibri-canary5 --download

PYTHONPATH=src python scripts/prepare_calibri_execution.py \
  --calibri /absolute/private/calibri-canary5 \
  --source /absolute/private/original-lcb-canary5 \
  --output /absolute/private/calibri-execution/bundle
```

第一步需要可选依赖 `pyarrow`；其他校验沿用标准库。二者都不执行候选代码，
也不调用模型。输出目录必须为本人私有目录，已有不同内容拒绝覆盖。

原始 Parquet、选中行、候选索引、固定选样、程序/测试哈希分别保存。
参考执行包将代码和对应测试冻结，记录代码版本与实现文件哈希。
公开仓库仅保存实现、合成测试、提示和汇总，不发布题目/隐藏测试/响应。

## B1 的小型执行门槛

用户本轮仅批准在 `/work/projects/polyullm/xxj/PALS` 内复验四份程序。
`scripts/b1_verify_calibri_canary.sbatch` 是本次日期与路径固定的作业，
不是通用集群启动器；2 CPU、4GB、20 分钟上限、无 GPU、不自动重排队。

先传到唯一 `transfer-staging` 目录，四文件 SHA256 与本地一致后发布到
唯一 `runs` 目录。Slurm 只提交一次，候选代码只在计算节点运行。
沿用 `lcb-reference-seccomp-v1`：默认拒绝文件/网络访问、每用例新进程、
内存/时间/输出限制、五个正负控制；期望答案不进入候选进程。
程序的系统调用需求若被隔离策略拒绝，记设施/执行问题，不径直判逻辑错误。

只取回结果、日志与执行清单；若执行输入使用提交前本地副本拼成验证快照，
明确记录这一点。`livecodebench_dag.verify_execution` 核对输入清单、执行器、
逐测试身份及结果哈希和全部分母。COMPLETED 不能替代这个应用级验收。
通过有限测试是可用参考程序的证据，不是完整正确性证明。

## 后续规范化约束（尚未接入模型调用）

`calibri_normalize.py` 与 `calibri-lcb-normalize-v1` 提示目前是独立纯函数
合同与提示草案，不是已经启动的构图流水线，没有生成合格 DAG。

1. Python 为原题、原输出、已验证程序分配行级来源锚点和精确字符区间；
   行只作定位，不强制一行等于一步。
2. 模型只提议自包含步骤、来源锚点、补充推导/省略说明；编号、原引文、
   末端完整代码均由程序填入，避免机械抄写改变来源。
3. 程序检查引用、顺序、根节点、依赖闭合与不变的代码附件；语义审核
   另查事实、算法一致性、依赖充分/最小、未披露修补与错误的根前提。
4. 模型补充推导明确标记；得到的是“来源约束的参考解释”，不冒充
   CALIBRI 原生 CoT、官方 gold 或独立人工验证。
5. 不因 E1 需要分支而人为造节点/边；代码附件不参与 PALS 步骤。

必须先完成四程序实际验收，再决定规范化试用。没有启动175题全量实验。

## 2026-09-22 本轮运行状态

源文件审计和本地回归已完成：332项构图库、63项PALS测试通过。
四候选包传输显示100%后结束握手超时，独立SSH哈希检查也未返回。
不能认定远端输入完整，尚未发布正式run或提交Slurm作业；无CPU结果。
已停止本轮传输/检查客户端，保留远端暂存和共享主连接，等待用户批准
重建复用连接。没有新增API调用，没有DAG或PALS正式结果。
