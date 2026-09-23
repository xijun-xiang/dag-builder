# GPQA-Diamond / HumanEval 冻结数据格式（2026-09-23）

本文仅记录**历史冻结文件的实际格式**，供复核已运行实验。以后统一构造最终数据请使用 [PALS 最终 DAG 数据统一格式 v1](pals-dag-unified-v1.md)及其可机读 JSON Schema。实验原题和完整 DAG 不随本代码仓库发布；同事需要另外取得对应的冻结 JSONL。HTML 用于查看，JSONL 是程序输入。

## 版本与文件

| 数据 | 冻结 JSONL | 查看器 | 行数 | JSONL SHA256 | 行级 `schema_version` |
| --- | --- | --- | ---: | --- | --- |
| GPQA-Diamond | `gpqa-dag-builder/diamond-full-198-c32-v1/exports/final-fbc44484ee1b/model_accepted.jsonl` | 同目录 `dag_viewer.html` | 118 | `537403ba3190dd8e2f8b423554b8441cddd279a37f0f04068c0810fc20c129d8` | `gpqa_all_outcomes_v1` |
| HumanEval | `pals-formal-gpqa-humaneval-20260921/releases/humaneval-dag-79-v1/humaneval_dag_79_v1.jsonl` | 同目录 `humaneval_dag_79_v1.html` | 79 | `0848c7229a776213a259c5d5318c8b21d01fa3c7ec100fe46585ed02d04ed20d` | `humaneval_validation_export_v1` |

上述路径相对于本地实验产物目录 `artifacts/`，不是本 Git 仓库内的文件。GPQA 的完整目录另有 `all_results.jsonl`（198 题）与 `manifest.json`；118 条文件仅保留模型接受的记录。HumanEval 发布目录另有 `manifest.json`、`inventory_164.json` 和 `SHA256SUMS`；79 条是 164 题中的冻结工作集。

两份 JSONL 都是 UTF-8、**每行一个 JSON object**，以 `item_id` 标识题目。`model_accepted=true`、`human_approved=false` 对两份冻结文件的每一行均成立。接受状态表示当前构图协议的模型审核结果，不表示逐题人工语义认证、官方 gold 推理或 HumanEval 程序测试通过。

## 共同的 DAG 节点

两份文件中可用的 DAG 都采用 `reference_dag_v1`，节点数组按推理顺序排列；每条依赖边由后续节点的 `parents` 指向先前节点的 `node_id`。共有四种 `kind`：`given`（题面或可直接观察的事实）、`knowledge`（引用的知识前提）、`derived`（推导）和 `answer`（终端答案）。

| 节点字段 | 类型 | 含义 |
| --- | --- | --- |
| `node_id` | integer | 本题内的节点 ID；与其他题的 ID 无关。 |
| `kind` | string | 上述四种节点类型之一。 |
| `statement` | string | 节点正文；PALS 的自然语言步骤从非 `answer` 节点取此字段。 |
| `parents` | integer[] | 直接前提的节点 ID；空数组表示根节点。 |
| `source_field` | string | 引文来自题目、解释、正确答案或参考代码等哪个来源字段。 |
| `source_quote` | string | 可供人工核查的来源原文。 |
| `justification` | string | 对节点及依赖的文字说明。 |

`source_quote` 和 `justification` 是审查证据，**不应直接拼进 PALS 的评分轨迹**。PALS 适配器还会排除末尾 `answer` 节点：GPQA 的末尾是选择题答案，HumanEval 的末尾是官方参考代码。节点的文本语义仍须按各题复核，图结构正确本身不证明推理正确。

## GPQA-Diamond：118 条 accepted 导出

### 外层字段

118 行都含以下字段：

| 字段 | 类型与用途 |
| --- | --- |
| `schema_version` | 固定为 `gpqa_all_outcomes_v1`。沿用 198 题总清单的导出格式，文件名仅说明筛选范围。 |
| `item_id` | string；与 `source.item_id` 一致。 |
| `cohort_role` | string；记录先导批或扩展批等来源角色。 |
| `source` | object；原题、选项、正确答案与来源信息。 |
| `status` | string；此冻结文件里有 `model_accepted` 4 条、`repaired_model_accepted` 111 条、`model_accepted_diagnostic` 3 条。 |
| `model_accepted` / `human_approved` | boolean；在此文件里分别恒为 `true` / `false`。 |
| `dag` | object 或 `null`；115 条有完整 DAG，3 条诊断接受记录为 `null`。 |
| `candidate` | object；各条均保留构图候选的 `nodes`、`parents`、`justifications`。 |
| `history` | array；历史尝试和结果，用于溯源，不是当前评分轨迹。 |
| `reason` / `limitation` | string；当前判定和适用边界。 |

`source` 的 118 条共有字段为：`item_id`, `dataset`, `revision`, `split`, `subset`, `row`, `record_id`, `task_type`, `domain`, `subdomain`, `question`, `choices`, `gold_answer`, `official_explanation`, `official_difficulty`, `choice_source_fields`, `source_fields`, `source_content_sha256`, `source_eligibility_issue`。其中 `subset` 为 `gpqa_diamond`，`choices` 是按 A、B、C、D 排列的四个字符串，`gold_answer` 是选项字母。来源、难度及解释等字段服务于构图和审核；评测时应通过适配器选择需要的字段。

有 DAG 的 115 条中，`dag.source == source`，`dag.item_id == item_id`，`dag.nodes` 是上述节点数组；`dag.reference_solution`、`dag.solution_review` / `dag.dag_review`、`dag.calculation_check` 和协议/回捞字段保存构建证据。具体协议字段随历史批次而异，不应以某一行的完整键集约束全部 115 条。

**3 条诊断接受记录是重要例外。**其 `dag` 为 `null`，需按 `candidate.nodes[*].node_id` 将 `candidate.parents[*].parents` 和 `candidate.justifications[*].text` 接回对应节点，得到可用于实验的节点序列。现有 `pals-validation/src/pals_validation/data.py` 的 `normalize()` 实现了这个分支及拓扑校验。不能直接遍历所有行的 `dag.nodes`，也不能丢弃这 3 条后仍声称使用了完整 118 题。

### GPQA HTML

`dag_viewer.html` 的 `<script type="application/json" id="data">` 内嵌的是 `{ "rows": [...], "manifest": {...} }`。其中 **`rows` 包含全部 198 题的状态**，包括未接受、争议和暂停样本；不是 118 条 accepted JSONL 的等量镜像。页面本身可离线查看内嵌内容；页首指向 JSONL 和 manifest 的链接需要相邻文件才可打开。不要将 HTML 的 198 条全部作为 PALS 输入。HTML 的摘要和筛选界面也不是独立的数据审核结果。

## HumanEval：79 条冻结发布

### 外层字段

79 行都含以下字段：

| 字段 | 类型与用途 |
| --- | --- |
| `schema_version` | 固定为 `humaneval_validation_export_v1`。 |
| `item_id` | string；与 `source.item_id`、`dag.item_id` 一致。 |
| `source` | object；原题 prompt、官方参考代码与来源哈希。 |
| `dag` | object；79 条均有完整 DAG，`dag.source == source`。 |
| `dag_sha256` | string；对 DAG 的规范 JSON 内容计算的摘要，用于校验来源与内容一致。 |
| `model_accepted` / `human_approved` | boolean；分别恒为 `true` / `false`。 |
| `status` | 固定为 `model_accepted`。 |
| `release` | object；发布 ID `humaneval-dag-79-v1`、冻结范围和批准范围。 |
| `provenance` | object；来源批次、原 DAG 路径及 DAG、结果、配置的文件哈希。历史路径仅供溯源，读取 JSONL 不要求本机存在该路径。 |
| `eligibility` | object；基于冻结图和 seed 算出的 E1/E2 算子适用性。 |
| `intervention_preview` | object；该 seed 下的顺序、交换和 E2 锚点预览，不改变原始 DAG。 |

`source` 的 79 条共有字段为：`item_id`, `dataset`, `revision`, `split`, `subset`, `row`, `task_id`, `task_type`, `domain`, `question`, `entry_point`, `canonical_solution`, `source_content_sha256`, `test_sha256`, `reference_origin`, `reference_execution`。`dataset` 为 `openai/human-eval`，`subset` 为 `humaneval`；`question` 是原始 prompt，`canonical_solution` 是官方参考代码 completion。JSONL **没有测试程序原文**，只保存 `test_sha256`。题号以 `source.task_id`（如 `HumanEval/0`）读取，不从行序推断。

`dag.reference_solution` 保存模型对官方代码的解释及单独的代码答案；`dag.nodes` 的最后一个 `answer.statement` 与 `source.canonical_solution` 一致。`dag.solution_review`、`dag.dag_review`、`dag.calculation_check` 和 `dag.construction_protocol` 记录审核与构图协议。79 条有不同构图/回捞历史：55 条未带 `recovery_provenance`，24 条带有该字段。不要据此假定经过统一重新审核。

`eligibility` 的固定字段是 `seed`, `adapter_pass`, `reasoning_nodes`, `total_nodes`, `total_edges`, `legal_forest`, `original_break`, `forest_break`, `fair_legal_break_pair`, `e2`, `parent_control`, `legal_unavailability_reason`。这些布尔值表示当前算子是否有结构上可执行的操作，**不是实验效果或 PALS 分数**。本版 seed 为 `20260915`；79 题都适用原序破坏、森林破坏和 E2，18 题适用拆树合法交换及公平配对，76 题适用匹配无关前提对照。`intervention_preview` 含 `forest`, `original_break`, `forest_break`, `e2_anchor`, `parent_control` 和相同的 `seed`；具体子对象可能为 `null`。

### HumanEval HTML

`humaneval_dag_79_v1.html` 是单文件离线查看器；`<script id="cohort-data" type="application/json">` 内嵌 `{ "rows": [...], "metadata": {...} }`。`rows` 与 79 行 JSONL **逐对象相等**，已在冻结脚本中检查。页面展示依赖图、全文、来源批次和结构适用性；无需联网或相邻 JSONL。它不是另一套数据 schema，也不是 PALS 的程序输入。

## 读取与复核约定

1. 先核对文件 SHA256、行数、`schema_version` 和 `item_id` 唯一性，再读取题目。
2. GPQA 使用现有适配器的 `normalize()`：它处理 `dag`/`candidate` 两条路径，检查父节点、拓扑顺序、一个终端答案及至少两个推理步骤。
3. HumanEval 使用现有适配器的 `normalize_humaneval()`：它检查来源一致性、`dag_sha256`、审核状态、构图协议和代码答案隔离。
4. 评分问题包含 `source.question`；GPQA 还包含 `source.choices`。推理轨迹由非 `answer` 节点的 `statement` 构成，`parents` 用来设计干预。HumanEval 的 `canonical_solution`、测试哈希、引用、说明和预览元数据都不送入被测模型；GPQA 的正确答案也不能作为待生成步骤的前缀。
5. 引用结果时分别报告 **题目总数、模型接受数、算子适用数、实际评分成功数**。GPQA 的 118/198 与 HumanEval 的 79/164 有不同的分母，不能混成一个接受率。

对应实现：`src/dag_builder/export.py`、`src/dag_builder/humaneval_export.py`、`pals-validation/src/pals_validation/data.py`、`pals-validation/src/pals_validation/humaneval.py`。HumanEval 79 题冻结脚本位于外部实验产物目录；上述字段与数量已与冻结 JSONL、HTML、manifest 逐项对照。
