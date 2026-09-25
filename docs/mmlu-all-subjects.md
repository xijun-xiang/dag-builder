# MMLU 57 学科构图协议（代码就绪，尚未运行新 API 实验）

## 范围与边界

使用 `cais/mmlu` 的 57 个独立学科配置；不把数据集的汇总配置当作学科。
各学科独立保留 `revision/subset/split/row`、原题与选项哈希、抽样清单、
原始 API 请求/响应、模型审核结果和失败原因。数据版本必须是 40 位提交 SHA。
默认用于正式验证的 split 是 `test`；抽样先导与全量不得混称。机械排除
仅限原有的重复题/疑似缺图规则，不按模型答案或 DAG 好坏补题。

新提示词 `mmlu-general-thinking-v1` 覆盖所有七阶段，不复用偏物理的
`mmlu-thinking-v1` 文案。独立解题时不提供标准答案；原生
`reasoning_content` 与正式 `content` 分开解析，答案不符即终止该题，
不生成迎合标准选项的替代解释。模型自审不等于人工或官方 gold。
知识记忆题可能没有可验证的多步依赖，不能为提高产量编造节点和边。

## 建议运行顺序

下例只说明接口，**本次代码推广没有调用 API 或下载全集**。先用私有目录
运行 57 学科各 5 题的固定先导，人工检查通过候选及失败原因，再冻结协议。
`REVISION` 应替换为经核对的 40 位数据提交 SHA；不要把密钥写进仓库。

```bash
dag-builder prepare-mmlu-all --root /private/project/mmlu-57-pilot \
  --revision "$REVISION" --split test --count-per-subject 5

# 有费用：先只跑两个代表学科。此配置每学科至多 60 次调用、300 万预留 token。
dag-builder run-mmlu-all --root /private/project/mmlu-57-pilot \
  --config configs/mmlu-general-thinking-pilot.json --key-file /private/key \
  --subjects econometrics,formal_logic --limit-per-subject 1 \
  --max-total-calls 120 --max-total-reserved-tokens 6000000
```

`run-mmlu-all` 只运行显式列出的学科或显式 `--all-subjects`；
要求总预算至少覆盖所选学科的本地最坏情形上限，逐学科顺序运行，
任一学科暂停就停下。`--limit-per-subject` 是固定抽样序列的前缀，
不是遇到失败后补取后续题。已完成阶段依靠原有不可变结果续跑。
先导和正式全量应使用不同根目录；若先导后改变提示词或规则，
必须另开协议版本，不把两版混合为同一确认性批次。

通过先导后，再用 `prepare-mmlu-all` 不带 `--count-per-subject`
冻结 57 学科全部机械合格题。依据实际题数和先导用量制定全量配置与
显式总预算，之后才运行 `--all-subjects`。配置文件中的 `max_calls` 和
`max_reserved_tokens` 是**每学科**上限，总上限不是每学科额度。
全量所需成本不能从示例先导配置直接推定。

## 导出与 PALS 验收

每学科处理完毕后可运行：

```bash
dag-builder export-mmlu-subject --root /private/project/mmlu-57/subjects/econometrics \
  --output-dir /private/project/exports/econometrics-v1
```

57 学科全部达到明确的终态后，运行 `export-mmlu-all` 合并：

```bash
dag-builder export-mmlu-all --campaign-root /private/project/mmlu-57 \
  --output-dir /private/project/exports/mmlu-57-v1
```

导出含 `all_outcomes.jsonl`、`model_accepted.jsonl`、
`pals_eligible_candidates.jsonl`、每学科分母与哈希清单，以及可供 PALS 读取的
`unified/pals_dag_unified_v1.jsonl` 与 HTML。模型接受图若少于两个非答案步骤，
仍保留在前两份文件，但不会进入 PALS 文件；不能把这类题偷偷算作可评分。
若某学科零张合格图，仍保留它的全部流转；全体零张合格图则不产生统一 PALS 文件。
`human_approved=false`，并不意味着正式 E1/E2 已经通过。PALS 读取端仍需
在 `prepare` 阶段分别计算 E1 合法/破坏配对数、E2 锚点数；对一步推理、
无合格依赖或无锚点的题计不适用，不填零、不造图。

正式报告逐学科列出原题、机械合格、答案一致、模型接受、人工抽查、
E1 可用、E2 可用及失败原因。全集合并数仅是补充，不可掩盖不同学科的
接受率与过程结构差异。
