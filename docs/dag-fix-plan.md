# DAG 构建兼容性修复计划

## 1. 背景与当前基线

目标是让 `deepseek-v4-flash` 稳定生成满足项目严格契约的 GSM8K 与 MMLU 推理 DAG，同时保留现有的来源追踪、不可覆盖产物、预算限制和人工审核边界。

当前已准备但尚未启动全量构图的数据：

| 数据组 | 可构图样本数 | 正式目录 API 响应数 |
| --- | ---: | ---: |
| GSM8K main/test | 1,319 | 0 |
| MMLU 数学 5 个子集 | 1,063 | 0 |
| MMLU 心理与社会 4 个子集 | 1,476 | 0 |
| 合计 | 3,858 | 0 |

真实 smoke 基线为 2 题、11 次响应、47,284 tokens，严格校验通过数为 0：

- GSM8K 在 `dependencies` 阶段失败：存在不属于答案祖先的重复事实节点。
- MMLU 完成全部 7 个阶段，但 `review_dag.issues` 返回对象数组，而当前契约要求字符串数组；审核内容同时发现答案选项缺少可引用来源。

## 2. 开发原则

1. 不修改已有 `gsm8k-v1`、`v1`、`mmlu-thinking-v1` Prompt 的历史语义；新增版本化协议和配置。
2. 不通过放松验证器来提高通过率。结构无效或证据不足的图仍必须失败。
3. 不把 gold label 注入独立解题请求。公开选项文本可以作为构图阶段的来源，但正确答案标签不能作为正确性的证据。
4. 格式错误、结构错误和语义拒绝分别统计，不能都归为模型答错。
5. 先通过小规模分阶段验收，再创建全量运行；全量任务沿用不可覆盖、可续跑和预算护栏。

## 3. 问题一：审核输出 schema 不明确

### 根因

`mmlu-thinking-v1/review_dag.md` 写的是“Issues must name affected nodes and evidence”，但没有像通用 `v1` Prompt 那样明确说明每个 issue 必须是字符串。模型因此返回结构合理但不符合校验器的对象数组。

### 真实失败 case

当前模型输出：

```json
{
  "decision": "reject",
  "issues": [
    {
      "node_id": 10,
      "evidence": "The answer choice is not present in the parent set."
    }
  ]
}
```

当前校验器要求：

```json
{
  "decision": "reject",
  "issues": [
    "Node 10: the answer choice is not present in the parent set."
  ]
}
```

### 改动计划

- 新增 `mmlu-thinking-v2/review_dag.md`，明确：
  - `issues` 必须是 JSON 字符串数组；
  - 每个字符串同时包含节点 ID 和证据；
  - 禁止返回 issue object、嵌套数组或额外字段。
- 对 `review_solution` 使用相同的精确类型描述，防止同类问题转移到其他审核阶段。
- 保留 `schemas.validate_review` 的严格校验，不自动把对象静默转换成字符串。
- 增加回归测试：对象数组必须失败，字符串数组必须通过。

## 4. 问题二：MMLU 选项缺少可引用来源

### 根因

MMLU 请求包含 A/B/C/D 四个公开选项，但 `atomize` 当前只允许 `question` 和 `solution` 两种 `source_field`。终端节点可以说“选择 B”，却无法引用 B 对应的真实选项文本。最终审核因此发现选项映射没有进入图中。

### 真实失败 case

题目选项 B：

```text
6x^2 + 4x + 6
```

当前终端节点：

```json
{
  "node_id": 10,
  "kind": "answer",
  "statement": "The product is 6x^2 + 4x + 6, and the selected option is B.",
  "source_field": "solution",
  "source_quote": "Thus the product is 6x^2 + 4x + 6, which is option B."
}
```

问题是 DAG 中没有任何可验证来源说明“B 的文本就是该多项式”。

### 目标形式

构图输入增加公开来源：

```json
{
  "reference_sources": {
    "choice_A": "2x^2 + 5",
    "choice_B": "6x^2 + 4x + 6",
    "choice_C": "0",
    "choice_D": "x^2 + 1"
  }
}
```

终端答案节点可以引用实际选项：

```json
{
  "node_id": 10,
  "kind": "answer",
  "statement": "The computed product 6x^2 + 4x + 6 matches option B.",
  "source_field": "choice_B",
  "source_quote": "6x^2 + 4x + 6"
}
```

其父节点仍必须提供多项式计算结果；选项文本只证明标签映射，不能证明计算正确。

### 改动计划

- 在 `stages.stage_input` 的 MMLU 构图阶段加入 `choice_A` 至 `choice_D`。
- 不加入 `correct_answer`，避免把数据集标签变成推理前提。
- 扩展 v2 atomize Prompt 的合法 `source_field` 列表和选项使用规则。
- 使用现有 `validate_nodes(..., extra_sources=...)` 做逐字引文验证。
- 增加测试：
  - solve 请求仍不能包含 gold label；
  - atomize 可以引用公开 choice 文本；
  - 非 answer 节点不能把错误选项当作已知事实；
  - answer 节点必须通过计算父节点和 choice 来源共同完成映射。

## 5. 问题三：GSM8K 重复节点导致图不闭合

### 根因

atomize 同时保留了题目和 solution 中语义相同的事实：

```json
{"node_id": 2, "statement": "5 people ... leave with 7 new records between them."}
{"node_id": 3, "statement": "The 5 people leave with 7 new records total."}
```

dependencies 正确地只使用其中一个节点。项目又要求每个节点都是答案节点的祖先，并禁止为闭合图而伪造边，因此校验失败。

### 目标 DAG 示例

```json
{
  "nodes": [
    {
      "node_id": 1,
      "kind": "given",
      "statement": "Two old records can be traded for one new record."
    },
    {
      "node_id": 2,
      "kind": "given",
      "statement": "The group leaves with seven new records."
    },
    {
      "node_id": 3,
      "kind": "derived",
      "statement": "Seven new records require 7 x 2 = 14 old records."
    },
    {
      "node_id": 4,
      "kind": "answer",
      "statement": "The group brought 14 old records."
    }
  ],
  "parents": [
    {"node_id": 1, "parents": []},
    {"node_id": 2, "parents": []},
    {"node_id": 3, "parents": [1, 2]},
    {"node_id": 4, "parents": [3]}
  ]
}
```

### 改动计划

- 新增 `gsm8k-v2/atomize.md`，要求：
  - 不同时保留语义等价的 question/solution 事实；
  - 每个节点必须对终端答案有实际贡献；
  - 不保留人数等与计算无关的信息；
  - 来源更完整时优先保留题目原文。
- dependencies Prompt 继续禁止伪造边，不用错误边掩盖 atomize 问题。
- 增加一个显式的“不可连接节点”诊断结果，区分重复节点、无关节点和真实缺失前提。
- 第一阶段不做通用语义自动去重；若 Prompt 仍不稳定，再设计有界的 `review_atomize` 阶段，而不是在校验器里猜测两个节点是否等价。

## 6. 问题四：thinking 与 token 成本过高

### 当前观测

MMLU 单题 completion tokens：

| 阶段 | completion tokens |
| --- | ---: |
| solve | 310 |
| structure_solution | 1,345 |
| review_solution | 1,021 |
| atomize | 7,248 |
| dependencies | 7,103 |
| justify | 5,861 |
| review_dag | 7,625 |

高强度 thinking 对 solve 有价值，但对固定 JSON 转换阶段造成了明显浪费。

### 改动计划

- 为配置增加可选的 stage overrides，而不是对所有阶段复用一个 `thinking` 和 `max_tokens`：

```json
{
  "stage_overrides": {
    "solve": {"thinking": "enabled", "reasoning_effort": "high", "max_tokens": 8192},
    "structure_solution": {"thinking": "disabled", "max_tokens": 2048},
    "atomize": {"thinking": "disabled", "max_tokens": 4096},
    "dependencies": {"thinking": "disabled", "max_tokens": 2048},
    "justify": {"thinking": "disabled", "max_tokens": 4096},
    "review_dag": {"thinking": "disabled", "max_tokens": 4096}
  }
}
```

- stage override 必须进入冻结配置和 payload hash，保证续跑一致性。
- 老配置不提供 overrides 时行为完全不变。
- 如果任务只要求最终 DAG、不要求保存原生 reasoning，可评估使用六阶段 `v2`，省去 `structure_solution`；该选择必须作为单独实验，不能与 native-thinking 结果混合。
- GSM8K 优先使用 `canonical_official_rationale`，跳过 solve 请求。1,319 题可直接节省 1,319 次模型调用。

## 7. 文件级实施清单

预计修改或新增：

- `src/dag_builder/config.py`
  - 增加并校验 stage overrides。
- `src/dag_builder/stages.py`
  - 构造 MMLU choice sources；应用逐阶段请求参数；注册 v2 Prompt。
- `src/dag_builder/schemas.py`
  - 保持严格 review schema；必要时改善错误定位，但不自动修复模型输出。
- `src/dag_builder/prompts/mmlu-thinking-v2/`
  - 新版 atomize、review_solution、review_dag；其余 Prompt 显式复制或按受控 fallback 读取。
- `src/dag_builder/prompts/gsm8k-v2/`
  - 新版 atomize、dependencies 和 review Prompt。
- `configs/`
  - 新增低成本 smoke 配置和分数据组 pilot 配置。
- `tests/test_thinking.py`
  - MMLU 选项来源、gold label 隔离、逐阶段 thinking 配置测试。
- `tests/test_builder.py`
  - GSM8K 去重提示契约、依赖闭合和官方 rationale 路由测试。
- 可选新增 `tests/test_dag_v2.py`
  - 固化本计划中的两个真实失败 case，作为端到端回归测试。

## 8. 实施顺序

### Phase A：离线契约修复

1. 建立两个失败 fixture，不包含凭据和外部响应元数据。
2. 新增 v2 Prompt 目录及 Config 路由。
3. 加入 MMLU choice sources。
4. 加入 stage overrides。
5. 补齐单元测试，并确保原有 181 个主项目测试继续通过。

### Phase B：结构阶段小样本

使用新运行目录，分别抽取：

- GSM8K 10 题；
- MMLU 数学 10 题；
- MMLU 心理与社会 10 题。

先运行到 `dependencies`，验收条件：

- JSON/schema 失败数为 0；
- 未知、未来或重复 parent 数为 0；
- 非答案祖先节点数为 0；
- MMLU answer 节点具有可验证的 choice 映射；
- 不通过虚构依赖边实现闭合。

### Phase C：完整审核 pilot

对 Phase B 的 30 题继续运行到 `review_dag`：

- 协议格式失败数为 0；
- semantic reject 与 protocol failure 分开报告；
- 每题阶段数、tokens、耗时、失败原因写入 `data/logs/data-processing.log`；
- completion token 中位数相对当前 smoke 至少下降 50%。

### Phase D：分批上线

通过 pilot 后按以下顺序运行：

1. GSM8K 50 题；
2. MMLU 每个子集各 20 题；
3. 汇总通过率、拒绝原因和成本；
4. 再决定是否运行剩余 3,858 条数据。

每个批次使用独立目录，不改变旧 smoke 和已有准备目录。

## 9. 完成定义

代码层完成条件：

- 全部历史离线测试通过；
- 新增 case 测试覆盖三个已知失败模式；
- v1 配置与序列化行为保持兼容；
- v2 配置、Prompt 和代码快照可复现；
- 日志和产物中不存在密钥。

模型侧完成条件：

- 30 题 pilot 无 schema/protocol failure；
- 结构失败必须提供可操作且分类明确的原因；
- semantic reject 不被自动转换为 accept；
- token 成本达到阶段预算目标；
- 只有通过严格审核的 DAG 才进入后续人工审核或 release。

## 10. 本轮实施结果（dag_fix）

已完成：

- 新增 `mmlu-thinking-v2` 和 `gsm8k-v2` 版本化协议，不修改 v1 历史运行。
- MMLU 构图阶段加入公开 `choice_A` 至 `choice_D` 来源；solve 请求仍不含 gold label。
- review Prompt 明确要求 `issues` 为字符串数组；校验器继续拒绝 issue object。
- v2 结构化阶段关闭 thinking 并设置阶段 token cap。
- GSM8K atomize 强制保留实体、单位和限定条件，减少重复节点及答案上下文丢失。
- dependencies 增加确定性传递冗余边校验，禁止一个直接父节点已是另一个直接父节点的祖先。
- 新增回归测试及 unified v1 导出测试；当前合并测试集 195/195 离线测试通过。

真实 smoke 结果：

- MMLU v2：1/1 `model_accepted`，从 v1 的 `invalid issues` 修复；旧版 38,889 tokens，新版 28,733 tokens，下降约 26%。
- GSM8K v2 首次运行：1/1 `model_accepted`，从 v1 的断连图修复；但确定性复核发现答案节点存在传递冗余边 `[2, 3]`，因此不能作为最终协议验收。
- GSM8K 加入最小性校验和上下文强化后的重跑因服务端 3 次超时暂停，未获得新的最终 verdict；该暂停不是语义通过或失败。

当前结论：MMLU v2 已达到单题协议验收；GSM8K v2 的 atomize/dependencies 还需要在服务稳定后重新验证，不能直接开始全量付费运行。
