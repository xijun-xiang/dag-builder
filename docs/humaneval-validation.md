# HumanEval：参考代码 → 推理 DAG → PALS E1/E2

最新质量改进与回捞入口见 [HumanEval v4 回捞说明](humaneval-recovery.md)。下文保留 v1–v3 的构建与历史验收背景；新协议不覆盖旧实验，真实数据是否可进入正式评分须以独立验收记录为准。

构图协议 `humaneval-reference-v3` 修正HumanEval专属的前提分类：参考程序是
已给定对象，准确的初始化/更新/返回行为观察可以是given；“算法正确”或
“不变量成立”不是代码观察，仍须推导。循环证明按基例、条件保持引理、
归纳结论、返回性质排列，不把待证不变量当根。末尾代码是对应参考答案的
附件（不参与PALS），不是声称题意唯一推出那一段代码。结构门槛、语义
审核、引用校验均不放宽；v1/v2的失败产物不改写。通过
`--prompt-version humaneval-reference-v3` 在新目录使用，不能原地改旧run。

2026-09-21 后续授权：长输出探针保持失败，但不再作为所有构图的总阻塞。
用户允许采用逐响应内容验收、`32768/high` 和至多32 workers。
`prepare_humaneval_campaign.py --allow-failed-cap-probe` 只接受已有短JSON通过、
长上限失败的已记录配置，不把失败改成通过。扩大总预算须显式传入
`--total-call-budget` / `--total-token-budget`，旧调用继续计入总账。

`content_gated_response=true` 时，输出上限与报告值不符但总量未超过已预留
额度只记告警；报告总量超过预留仍停止新增请求，包括在客户端限流器里
等待、尚未发出的请求。已在途请求收回并记录，不能保证服务端即时取消。
每个响应都保存 `contract_check-v2.json`。非stop、非法JSON一律拒绝，
不修补截断；GPQA/HumanEval只解析content，原生CoT严格分开字段且拒绝
两个字段完全相同，不做相互兜底。模型拒绝/格式失败不重新采样直到通过。

离线质量审查可在 `quality_exclusions.json` 记录只拒绝、不提升的裁定，
必须绑定任务ID与对应solve输出哈希。导出器保留原模型决策，并从候选输出
中剔除审查拒绝项，记录审查与导出实现哈希。构建运行使用冻结快照；最终
导出用当前带质量审查检查的仓库版本，不用旧构建快照中的历史导出器。
语义验收不能由JSON合法或同模型自审替代；输出仍称模型审核候选。

状态（2026-09-21后续更新）：逐响应政策下，首轮固定五题最终0题通过；针对程序事实与循环证明分类修正提示后，同五题复验1题model_accepted、4题拒绝或needs_review。已逐步阅读通过题，并决定按同一冻结协议处理剩余158个候选，workers=32。低产出率和失败样本保留，不声称数据已全量合格。尚未通过HumanEval真实GPU canary，也不是正式PALS结果。历史接口问题见[API契约修复与验收](api-response-contract.md)。此模块不执行Python解答或测试，不包含pass@k评测和E3。

## 测量对象与不变项

参考实现用来构造自然语言算法解释，解释再拆成 DAG。它不是官方 gold CoT；同模型生成与审核也不等于独立语义验证。循环用不变量、更新规则等静态推理描述，不逐次展开，不把程序控制流图直接当推理 DAG。

PALS 的 g、v、M、N、D、换序规则及 E2 哈希选点不变。N 是平均负支持，D 是同前缀多次续写 g 的样本标准差。E2 要续写一个自然语言算法步骤，不是完整代码。GPQA 提示与预处理格式保持不变；HumanEval 使用独立 `humaneval-validation-v1` 协议。

三种数据严格区分：

| 数据 | 构图模型 | PALS 被测模型 |
|---|---|---|
| 原始 prompt（签名、docstring、题面例子） | 提供 | 原样提供 |
| canonical_solution（官方 completion） | 用于解释、审核；终端 answer 单独保存 | 不注入 |
| test（官方检查程序） | 不提供 | 不提供 |
| 审核后的推理步骤 | 用于构图 | E1 的相应前缀/目标，E2 的祖先前缀 |

原始 source 文件保留测试文本供溯源；归一化构图 items 只留 test 哈希。PALS 再次白名单取字段，删除代码答案、引用原文、justification 和其他元数据。此隔离是字段层面的防泄漏；自然语言是否忠实、是否包含不必要答案提示仍需语义审核。

## 1. 安装与获取官方数据

仓库根目录安装构图工具；PALS 可独立安装，无彼此运行依赖：

```bash
python -m pip install -e .
python -m pip install -e ./pals-validation
```

真实评分的推理依赖另装 `./pals-validation[inference]`，优先使用已批准的集群环境。构图模型名称与端点必须由操作者确认可用，不把配置模板当作已验证的服务快照。

本次核验的官方来源：

- GitHub：`openai/human-eval`
- revision：`6d43fb980f9fee3c892a914eda09951f772ad10d`
- 文件：`data/HumanEval.jsonl.gz`
- 文件 SHA256：`b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef`
- [固定下载地址](https://raw.githubusercontent.com/openai/human-eval/6d43fb980f9fee3c892a914eda09951f772ad10d/data/HumanEval.jsonl.gz)

导入不自动联网、不调用模型，必须传本地文件、revision 与预期哈希。调用者负责 revision 与文件的来源对应关系；仅填写一个 SHA 并不能证明其他来源文件是官方数据。

```bash
dag-builder prepare-humaneval \
  --root /absolute/private/path/humaneval-dag-v1 \
  --source-file /absolute/path/HumanEval.jsonl.gz \
  --revision 6d43fb980f9fee3c892a914eda09951f772ad10d \
  --expected-sha256 b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef \
  --count 164
```

源文件必须含原版 164 个唯一 ID。小样本构图可用 `--count 5` 在另一新 run 中预选；排序只依赖 seed/item ID，不看模型输出。不能改变已存在 run 的 count、seed 或源文件。简单题不强造三步或分支。

## 2. 构图（需要另行批准模型调用与预算）

旧模板 `configs/humaneval-reference.json` 和 `configs/humaneval-reference-contract-v2.json` 保留供历史追溯。长输出探针失败不能自动放量；只有取得明确授权后才可采用上文记录的逐响应内容门槛例外。HumanEval 当前并发上限为 32；客户端请求/预留预算不能代替服务端费用硬限额。

```bash
dag-builder run --root /absolute/private/path/humaneval-dag-v1 \
  --config /absolute/path/humaneval-reference.json --limit 5
dag-builder report --root /absolute/private/path/humaneval-dag-v1
```

六阶段：`solve`（沿用阶段名，但这里只解释参考代码，不独立解题）→解释审核→原子化→依赖→justification→DAG 审核。每题完整成功路径 6 次模型请求；164 题最多 984 次基础请求，传输重试另计。审核拒绝/格式错误保留，不重采样直到通过。确认构图风格和配置后去掉 `--limit` 继续；缓存阶段不重复收费。改提示/配置/代码需新 run。

只有完整代码答案出现在终端 answer 节点，必须逐字保留官方 completion。推理步骤引用 question 或生成解释，不能把 code answer 当作推理来源节点；审核检查算法含义和依赖，不把代码行顺序当逻辑边。

## 3. 导出与 PALS 准备

全部所选题达到终态后：

```bash
dag-builder export-humaneval-validation --root /absolute/private/path/humaneval-dag-v1
```

产生 `exports/<hash>/model_accepted.jsonl` 及 manifest。每题状态及失败原因保留，已接受 DAG 与源数据/结果哈希核验。不完整 cohort 拒绝导出。输出明确 `human_approved=false`；这是模型审核候选，不冒充人工 release。原有 `release --human-review` 仍是独立人工审核入口。

使用返回的实际导出路径：

```bash
pals-validation prepare --benchmark humaneval \
  --source /absolute/path/exports/HASH/model_accepted.jsonl \
  --output /absolute/path/humaneval-validation-prepared-v1
```

可再加 `--expected-sha256` 固定导出文件。保持默认 `node_only` 视图，不拼接 justification。`inventory.json` 保留 E1/E2 不适用题：如只有一个推理节点、无可破坏相邻边、无两棵可交换非平凡子树。合法对照用同题同基线与共同目标；拆出的树不算独立题。

## 4. 真实 canary → 全量 E1/E2

之后使用 PALS 现有 `init / worker / analyze` 命令，分别创建每模型 E1/E2 的独立 run。HumanEval 的输入适配由 prepared 数据冻结，不需要 worker 再猜 benchmark。

五开放模型逐一做真实 canary，重点检查函数签名完整、单步自然语言输出、原生 masked loss 对齐、八分片、断点续跑。GPQA 的 canary 不能替代它。E2 默认 T=0.3/0.7/1.2，每档 8 repeats，评分 T=1，预算 2048 个新 token；代码输出、嵌套标签、空步骤及截断记无效，不补零或自动补样。格式检测只能识别明显代码块/函数等结构，不证明文字语义一定正确。

全量统计沿用题目配对 bootstrap 和完整共同题集，同时报告覆盖率。DAG 模型接受率、结构适用率、E2 输出有效率是三个不同分母，不能混为“164 题全成功”。若合法操作很少，诚实报告，不为制造分支重写算法。

## 本地验收记录（2026-09-21）

- DAG Builder：191 项测试通过，含 10 项新增 HumanEval 测试。
- 独立 PALS 包：34 项测试通过，含 8 项新增 HumanEval 测试。
- 固定版本的官方 164 题：来源字段、唯一 ID、参考代码与测试的 Python 语法全部通过静态检查，未执行代码。
- 原 GPQA 118 题：相同参数重新预处理后，manifest 和全部输入文件哈希与旧版一致。适用数仍为合法 44、原图破坏 97、拆树破坏 104、公平配对 44、E2 104。
- 两个项目分别构建 wheel；构图包包含六个新提示文件，PALS 包不依赖构图包。

单元测试使用合成数据和模拟模型。这些检查证明导入、字段隔离、哈希校验、断点续跑与管线衔接符合代码契约；不证明模型构造的 DAG 语义正确，也不构成 PALS 有效性的实验结果。

可复核测试命令（仓库根目录）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=pals-validation/src python -m unittest discover -s pals-validation/tests
```

## 目录与安全

在 B1 所有写入仍限于 `/work/projects/polyullm/xxj/PALS/`：代码 `src/`、输入与构图产物 `artifacts/humaneval/`、配置 `configs/`、实验 `runs/`。所有原题、代码答案、模型输出均不进入 Git；仓库只含合成测试。构图预算、密钥和集群启动需单独批准。

本模块只用 `ast.parse` 验证语法，没有 `exec` 或运行测试。将来做代码正确性评测必须单独实现安全执行环境；普通 Slurm 分配、Python 超时或 HumanEval 的 reliability_guard 都不能替代隔离沙箱。
# Reference construction v2 (thinking enabled)

`humaneval-reference-v2` keeps the same six-stage schema and separates native
`reasoning_content` from the final JSON in `message.content`. Only the reviewed
final rationale becomes the trajectory; thinking is archived in raw responses.
V1 prompts and outputs are not overwritten. V2 requires premises before derived
conclusions before atomization; downstream stages cannot silently rewrite or
reorder the frozen explanation. Loops are explained by finite invariant reasoning,
not by creating a cyclic dependency graph. No minimum step count or branching is
manufactured. The original canonical code stays the exact terminal answer.

`prepare-humaneval --exclusions exclusions.json` accepts a pre-construction list
of `{task_id, reason, evidence}` source-quality decisions. The importer preserves
all 164 source records and their denominator, excludes these IDs from selection,
and records them in the export manifest. `--count 164` selects all eligible tasks.
Review rejection is retained without score-based replacement or repeated semantic
sampling. Model-reviewed explanations are **not official or human-certified gold**;
reference code and tests have only been syntax-checked, never executed by this tool.

Explicit thinking uses `thinking.type=enabled` and omits temperature (the official
thinking API ignores it). `reasoning_effort=low` is an optional fixed setting, not
a determinism guarantee. Requested and returned model aliases are recorded; a
private proxy alias alone does not establish an immutable upstream model revision.
Strict usage/model/output-cap checks remain enabled. Compatibility probes accept
thinking but are bounded to two requests with at most 4096 output tokens each.
Unknown transport attempts retain their full allowance in the budget; changing
run directories never authorizes resetting the campaign-wide accounting.
