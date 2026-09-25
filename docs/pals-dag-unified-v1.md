# PALS 最终 DAG 数据统一格式 v1

从本版起，面向 PALS 的**最终接受样本**统一导出为 `pals_dag_unified_v1.jsonl` 和 `pals_dag_unified_v1.html`。一题一行，所有 benchmark 使用同一组顶层字段、同一套节点/依赖边字段，以及同一个 HTML 查看器模板。可机读的行结构定义在 [`schemas/pals-dag-unified-v1.schema.json`](../schemas/pals-dag-unified-v1.schema.json)；Python 的图约束和导出逻辑在 `src/dag_builder/unified.py`。

这是一种**新的交付格式**。此前 GPQA-Diamond 118 题和 HumanEval 79 题的冻结文件、SHA256、正式实验记录均保持原样；历史格式见[对照文档](gpqa-humaneval-frozen-data-schema.md)。新导出不能被追溯性地称为当时 GPU 实验读取的文件。

## 每行的固定结构

```json
{
  "schema_version": "pals_dag_unified_v1",
  "item_id": "稳定的题目 ID",
  "benchmark": "gpqa_diamond",
  "problem": {
    "question": "完整题面或函数说明",
    "domain": "学科或 code",
    "choices": ["选项 A", "选项 B", "选项 C", "选项 D"],
    "entry_point": null
  },
  "answer": {"kind": "choice", "value": "A"},
  "dag": {
    "schema_version": "pals_step_dag_v1",
    "nodes": [
      {"node_id": 1, "kind": "given", "statement": "...", "parents": [], "source_field": "question", "source_quote": "...", "justification": "..."},
      {"node_id": 2, "kind": "answer", "statement": "...", "parents": [1], "source_field": "correct_answer", "source_quote": "...", "justification": "..."}
    ],
    "nodes_sha256": "64 位小写十六进制摘要"
  },
  "review": {
    "source_status": "model_accepted",
    "model_accepted": true,
    "human_approved": false,
    "quality_status": "模型审核状态或 null",
    "construction_protocol": "构图协议或 null"
  },
  "provenance": {
    "dataset": "原始数据集名",
    "subset": "来源子集",
    "split": "来源划分",
    "revision": "来源版本",
    "source_row": 0,
    "source_id": "原题号",
    "source_content_sha256": "64 位摘要",
    "source_schema_version": "原记录格式版本",
    "source_file_sha256": "原 JSONL 文件 SHA256",
    "source_record_sha256": "原记录内容摘要",
    "source_dag_sha256": "原始完整 DAG 内容摘要或 null"
  }
}
```

上例展示字段形状，不是可直接验哈希的真实数据行。MMLU 的 57 个独立学科也使用此结构：`benchmark="mmlu"`，`problem.domain` 与 `provenance.subset` 保留学科名，四个选项与标准答案分开存储；模型通过候选仍标记 `human_approved=false`，见[全学科协议](mmlu-all-subjects.md)。HumanEval 使用相同结构：`benchmark="humaneval"`，`problem.choices=null`，`problem.entry_point` 为函数名，`answer={"kind":"code","value":"官方 completion"}`。LiveCodeBench v6 同样使用这组字段：`benchmark="livecodebench_v6"`；functional 题保留函数入口，并在 `problem.question` 后附上原始 `starter_code` 供 PALS 看见完整公开上下文；stdin 题的 `problem.entry_point=null` 且无 starter code。`answer.value` 是经固定 CPU 测试通过的 CALIBRI 派生参考程序，**不是官方 gold**。将来接入其他 benchmark 时须仍输出同一组字段；需要新字段时发布 v2，不在 v1 中悄悄加字段。

### 字段语义与约束

- `item_id` 是本构图项目的稳定题目 ID；`provenance.source_id` 是原数据集题号，`source_row` 保留原文件行号，`problem.domain` 保留分组领域。两类 ID 不要求字符串相同。
- `problem` 保留被测模型真正看到的题面。四选题在 `choices` 中按 A–D 排列；代码题的原始函数 prompt 放入 `question`，入口函数放入 `entry_point`。
- `answer` 单独保留标准答案；`dag.nodes` 保留终端 `answer` 节点供审核。给 PALS 评分时排除终端节点，也不把标准答案、`source_quote` 或 `justification` 混入待生成前缀。
- `dag.nodes` 保留原节点顺序和 ID。`parents` 只能引用本行中更早出现的节点；恰有一个 `answer` 节点，且位于最后。代码题的终端 `answer.statement` 必须与 `answer.value` 一致。
- `dag.nodes_sha256` 是对 **`nodes` 数组**用 `json.dumps(ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)` 加末尾 LF 编码后的 SHA256。`provenance.source_record_sha256` 与 `source_dag_sha256` 用相同方式计算原记录及原完整 DAG。`source_file_sha256` 是源 JSONL 原始字节的 SHA256。不同层次的哈希不能互换。
- `review.source_status` 原样保留来源状态；`model_accepted` 和 `human_approved` 不互相替代。`quality_status` / `construction_protocol` 不存在于旧记录时填 `null`，不能凭推测补一个审核结论。
- `provenance` 允许从原文件追溯审核历史。它不复制原文件中所有历史响应和额外元数据；要复核这些内容，仍需保留经哈希固定的原文件。
- `eligibility`、拆树顺序、E2 锚点、模型生成和 PALS 分数不属于最终 DAG 数据本身；它们依赖具体实验协议，应由后续 `prepare` 和运行记录另行冻结。

JSON Schema 检查字段类型与必需键；Python `validate_row()` 另外检查拓扑顺序、唯一节点、唯一终端答案、跨字段一致性和摘要。格式验证不证明推理语义正确。

## 从两份历史冻结文件导出

在仓库根目录安装本包后，可先只做读取和验证：

```bash
python scripts/export_pals_dag_unified_v1.py \
  --benchmark gpqa_diamond \
  --source /absolute/path/model_accepted.jsonl \
  --expected-sha256 537403ba3190dd8e2f8b423554b8441cddd279a37f0f04068c0810fc20c129d8

python scripts/export_pals_dag_unified_v1.py \
  --benchmark humaneval \
  --source /absolute/path/humaneval_dag_79_v1.jsonl \
  --expected-sha256 0848c7229a776213a259c5d5318c8b21d01fa3c7ec100fe46585ed02d04ed20d
```

加入 `--output-dir /absolute/private/new-directory` 才会新建 `pals_dag_unified_v1.jsonl`、`pals_dag_unified_v1.html` 和 `manifest.json`；输出目录必须尚不存在，不覆盖历史冻结文件。两个 benchmark 的 HTML 都在 `<script id="cohort-data" type="application/json">` 中使用同一个包装：`{"schema_version":"pals_dag_unified_view_v1","benchmark":"...","rows":[...]}`，其中 `rows` 与 JSONL 逐对象相等。查看器只负责呈现，不是 PALS 的输入。GPQA 115 条完整 DAG 直接保留节点，3 条 `model_accepted_diagnostic` 按节点 ID 接回 `candidate.parents` 和 `candidate.justifications`；在统一记录中 `review.source_status` 仍明确标为诊断接受，`provenance.source_dag_sha256=null`。HumanEval 79 条在导出前检查原 DAG 哈希和终端代码答案。

现有 PALS `prepare --benchmark gpqa`、`--benchmark humaneval` 或 `--benchmark livecodebench` 会按行级 `schema_version` 识别新格式，并继续生成各自的 `cases.json`、`jobs.json`、`selection.json`、`inventory.json`。它也继续接受 GPQA/HumanEval 对应历史格式，既有实验无需重跑。预处理器校验新格式的图哈希和结构；来源文件应再用 `--expected-sha256` 固定。新格式不会把旧格式里的全部审核详情复制进去，因此独立复核模型审查仍需原冻结文件。

对新的 GPQA、HumanEval 或后续 benchmark，构图器应先完成各自的来源与语义审核，再把接受的节点转换到上述固定结构，调用 `validate_row()` 验证，并为数据集发布独立的行数、排除原因与文件哈希清单。导出脚本支持已验收的 GPQA/HumanEval 历史输入、完成全链路审计后的 LiveCodeBench v6 接受子集，以及按 `mmlu_model_candidates_v1` 冻结的 MMLU 模型接受候选；它不把未完成批次或非接受样本转换为实验输入。MMLU 失败题和 57 学科完整分母另存于 `all_outcomes.jsonl` 和各学科清单。其他 benchmark 仍需要明确的来源适配，不能仅改 `benchmark` 字符串绕过校验。

## 已做的兼容性核对

以两份已冻结来源分别导出 118 条 GPQA 和 79 条 HumanEval 后，使用同一 `--parent-probe` 预处理配置，与历史正式实验的准备产物逐文件比较：两组 `jobs.json`、`selection.json`、`inventory.json` 均逐字节相同；`cases.json` 去掉仅表示输入适配器名称的 `adapter` 字段后逐对象相同。这说明本次转换没有改变这两组实验的题目、步骤、换序选择或评分任务；**并不意味着**旧实验曾读取新文件，也不证明未来 benchmark 的语义质量。
