# E3 原生推理轨迹实验：代码实施规范 v1

状态：**实施规范。** E3 代码已按本规范分步落地；真实 B1 canary/正式结果是否完成，以独立审计记录为准。实际命令及安全门槛见 [E3_RUNBOOK_V1.md](E3_RUNBOOK_V1.md)。

编制日期：2026-09-29。用途：交给执行 agent 按步骤实现；不授权自动连接集群、下载模型、启动正式实验或发布结果。

## 1. 实验到底做什么

让三个待测模型分别回答同一批 benchmark 原题，每个“模型 × 题目”只生成一次，输出分步推理和最终答案。之后对生成的推理步骤计算原有 g、平均支持 G、平均负支持惩罚 M，以及新增的轨迹内离散度 W；最终答案单独按 benchmark 规则评分。

这里的“模型评测”指模型解题及其概率评分，**不是让模型用自然语言给另一个模型的答案打分**。不引入 LLM judge。

E1/E2 研究受控轨迹；E3 研究模型真正生成的轨迹。已有 DAG 可帮助追踪题目来源，但 **DAG 步骤、参考答案、参考代码和隐藏测试都不能进入 E3 生成提示**。旧 E3 离线相关性分析保留为历史分析，不能混入本次原生轨迹结果。

本实验回答：真实解题时，各模型怎样利用前文、负支持出现在什么位置、支持强度在一条轨迹中怎样变化，以及这些过程特征在相同或不同答题结果下的表现。

不回答：重复生成是否稳定、分布式部署是否降低过程波动、支持越强是否一定越正确。W 不是 E2 的 D；不预设 W 越低越好。

本计划按 pals-research 的现有指标口径和实验设计规范编制：将数学定义、样本分母、可执行验收与停止条件提前固定，但不预设实验必须出现正向效果。主实验是原生轨迹的描述与诊断；10% 共同评分者检查为解释跨模型结果的配套检查，不追加新一轮生成。

## 2. 已核对的仓库基础

仓库：`https://github.com/xijun-xiang/dag-builder.git`。

检查时远端 main 为 `f740e3fa757b49f14e3a4717736c6a6e37503406`；本地工作分支为 `codex/mmlu-green-27-run`，提交 `dd53523bb732113a6cf12d2f81347f28dbf2fc53`。本次比较中，`pals-validation/` 和 `docs/pals-dag-unified-v1.md` 与该 main 无差异。实施时重新检查；不能覆盖当前 MMLU 工作。

建议另建 `codex/pals-e3-native-trajectory` 分支。分支建立、提交、推送遵循实际工作区状态和用户授权，本文不执行这些操作。

`pals-validation/` 是独立 Python 包。保持这一点：E3 不依赖 infi-evalscope、easy-eval、构图 API、私有 API key，也不为了读原题而导入整套 DAG 构建流水线。

| 已有位置 | 可以复用什么 | E3 必须避免什么 |
|---|---|---|
| `src/pals_validation/metrics.py` | `pair()` 的 g、负部、两侧 NLL；`trajectory()` 的 G/M；bootstrap 基础 | `repeats()` 的 D 是重复间样本标准差，不能用于 W |
| `backend.py` | 模型加载、版本检查、原始 logits 的 `_score()` | `context()` 使用 E2 单步提示；`generate()` 把同一个输入重复多份，不是多题 batch |
| `protocol.py` | 阅读现有提示及边界约定，作为兼容参考 | 当前在第一个 `</step>` 停止，不适用于完整轨迹 |
| `io.py`、`locking.py` | 规范 JSON、哈希、不可覆盖保存、文件锁 | 不绕过锁、不覆盖旧结果、不静默修 JSON |
| `run.py`、`analyze.py` | 运行身份、证据复算、完成性检查的模式 | 当前只接受 E1/E2；代码快照只扫包顶层 `.py`，会漏掉新增 `e3/` |
| `campaign.py` | 八卡分工、原生 masked-loss 核验、数值复测思路 | 现有 canary 依赖 E1 DAG 任务，不能照搬 |
| 根目录 `scripts/verify_livecodebench_reference.py` | 参考已有隔离执行及证据记录方式 | 输出比较器明确不是官方浮点容差评分器；不能把参考代码验收率改名为官方 pass rate |

原则：新增独立 E3 入口；不向 E1/E2 解析器塞“兼容 E3”分支，不改变其配置、指标或历史输入哈希。

## 3. 冻结科学协议

### 3.1 数学定义

一条完整生成包含推理步骤 `S_1,…,S_n` 和最终答案 A。评分模型记为 θ。对 `i=2,…,n`：

```
full_i    = 固定任务提示 + S_1 + … + S_(i-1) + 当前 step 的开启标签
deleted_i = full_i 中仅删除 S_(i-1) 的完整标签块
target_i  = S_i 的正文，两个条件使用完全相同的 target token IDs

g_i = mean_token(log pθ(target_i | full_i)
                 - log pθ(target_i | deleted_i))
v_i = max(-g_i, 0)
K   = n - 1
G   = mean_i(g_i)
M   = mean_i(v_i)
W   = sqrt(mean_i((g_i - G)^2))
```

评分始终使用 T=1 的原始模型分布，不施加采样 temperature、top-p、top-k、重复惩罚。生成配置只影响产生什么轨迹。

第一步没有可删除的前一步，不评分；最终答案、完整代码及格式标签不作为评分 target。代码题评分的是自然语言算法推理，而不是代码 token。

每一步先按自己的 token 数归一化；之后各步骤等权。不能改成整条轨迹 token 加权，也不能把 `M` 写成 `max(-G,0)`。

使用自然对数，g/G/M/W 的数值单位均按 nats/token 解释。保存每一步原始 g；不转换成旧版 MAD、0–100 分或 G/M/W 加权总分。主报告不沿用 E2 的 N/D 命名，避免把步骤维度与重复采样维度混淆。

边界规则：

| 完整轨迹中可评分的 K | G / M | W |
|---|---|---|
| 0 | null | null |
| 1 | 有定义 | null，并注明不足以描述轨迹内离散度 |
| ≥2 | 有定义 | 上式总体标准差，`ddof=0` |

非有限数不得进入统计。某一步概率评分失败时，该轨迹的主分析 G/M/W 全部为 null；已成功步骤只留作诊断，不以“剩余步骤平均”补齐。

### 3.2 模型、数据和设置

三个模型：Qwen2.5-7B-Instruct、Phi-4-mini-instruct、Qwen3-14B。复用已核实的权重修订和 tokenizer，不能仅按模型名重新下载最新版。主分析由生成该轨迹的模型给自己做条件概率评分。

| benchmark | 正式目标范围 | 目标题数 |
|---|---|---:|
| GPQA Diamond | 原始完整 Diamond | 198 |
| GSM8K | test | 1319 |
| HumanEval | 原始任务全集 | 164 |
| LiveCodeBench | v6 第六批新增题，不是累计 release_v6 | 175 |
| MMLU | 之前约定的五领域唯一题目集合，不冒充整个 MMLU | 1876 |

这些是**待来源盘点确认的目标数**，不是已经验收的本地可用数。合计目标 3732 题、11196 条生成。若原题或评分材料不齐，先列缺口并询问用户，不能用 DAG 接受子集悄悄替代全集。

旧参考轨迹集合（118/530/79/94/134）仅作为来源明确的重叠子集，不决定新实验分母。匹配必须使用稳定题号和原题哈希；文本相似匹配不能直接认定同题。

冻结生成参数：T=0.7、top_p=1、top_k=0、repetition_penalty=1、num_beams=1、do_sample=true、每题一次。Qwen3 使用已经支持并验证过的 `enable_thinking=false`；不得将隐藏 reasoning 字段与正式输出混用。

知识/数学题 `max_new_tokens=8192`，代码题 `16384`。这是拟定协议；实施前必须逐题检查“输入长度 + 输出预算 ≤ 该模型核实的上下文容量”。不静默截断题面、不在运行中自动改预算。

主种子拟定为 `2026092903`。实际生成 batch 的种子由主种子、模型身份和有序题号列表确定；算法和派生结果写入冻结任务表。不要承诺改变 batch 后仍逐 token 相同。

### 3.3 一个必要的共同评分者检查

在任何输出产生前，每个 benchmark 按稳定哈希抽取 `ceil(10% × 题数)` 道原题。三个模型在这些题目上的轨迹均额外交给固定 Qwen3-14B 评分，检验主结果是否主要来自各模型自己的评分尺度。

该子集不重新生成答案，不按正确率、g 或格式成功率选题。选中题失败后不换题。按目标范围预计 375 道原题、1125 条待复核轨迹，其中 Qwen3 自评分可在协议完全相同且哈希核验通过时直接引用，不能伪装成独立复测。

主结果和共同评分者结果分表，不能混合后平均。不同模型 tokenizer 导致量纲尺度不同，也不能仅凭跨模型 G 大小排出“推理质量榜”。

## 4. 代码分工与边界

新增目录 `pals-validation/src/pals_validation/e3/`：

| 文件 | 唯一职责与主要接口 |
|---|---|
| `schema.py` | 数据类型、状态枚举、配置严格校验；未知字段拒绝 |
| `data.py` | `prepare_sources()`：五类原题适配、来源去重、公开/私有材料分离、固定抽样 |
| `protocol.py` | `render_prompt()`、`parse_trace()`、`render_score_pair()`；所有提示版本和标签规则 |
| `backend.py` | `E3HFBackend` / `E3MockBackend`；完整轨迹多题生成、显式上下文概率评分 |
| `metrics.py` | `summarize_trace()`：调用原 `metrics.pair()`，增加 W 和严格 null 语义 |
| `run.py` | 初始化、不可变任务表、一次生成状态机、各阶段 worker、恢复约束 |
| `evaluate.py` | 选择题/数值答案适配及隔离代码执行接口，绝不调用 LLM judge |
| `audit.py` | 输入/代码/输出哈希、解析和逐 token 独立重放、预算与覆盖率核对 |
| `analyze.py` | 题级聚合、配对比较、区间、长度分层、冻结案例选择 |
| `report.py` | 只读取审计通过的分析产物，输出中文 Markdown 和 CSV |
| `cli.py` | 命令参数路由，不内嵌科学逻辑或自动 SSH |

新增 `configs/e3-native-v1.example.json`、`configs/e3-mock.json`、`scripts/e3_canary.py`、`scripts/b1-e3-gpu.sbatch`、`scripts/b1-e3-cpu.sbatch` 及对应测试。在 `pyproject.toml` 增加独立入口 `pals-e3 = pals_validation.e3.cli:main`。

根目录 DAG 构建器保持不变。需要复用参考代码验证的安全模式时，明确迁入独立适配器并测试，不通过修改 `sys.path` 隐式依赖某个本地 checkout。

后端首版采用小型子类：复用现有 `HFBackend` 的加载与 `_score()`，新增明确的 E3 上下文和生成函数。**不得调用旧 `score()` 或旧 `generate()` 冒充 E3。** 通过测试锁定所依赖的底层接口；未来重构为公开共享接口另作变更。

## 5. 输入、输出和数据隔离合同

### 5.1 原题记录

公开 `problems.jsonl` 每行至少有：

```
schema_version, problem_id, benchmark, subset, source_id,
source_revision, source_record_sha256,
question, choices, language, entry_point, public_examples,
prompt_payload_sha256
```

`problem_id` 为带 benchmark 命名空间的稳定 ID。无选项时 choices=null；有选项时明确标签和顺序。不能默认所有题都是四选一。HumanEval 保留原接口与 docstring，要求输出完整函数实现，不混用“completion 拼接”与“完整代码”两种评测方式。

私有 `grading/` 单独保存标准答案、测试及来源哈希。`problems.jsonl` 不放 answer、solution、gold、reference_code、DAG nodes 等字段。构造 prompt 只读取公开字段白名单；用含唯一秘密字符串的合成题做泄露单元测试。

GPQA 沿用构图项目中已核验的固定选项排列，同一题对三个模型相同。原始 198 题中有 2 题存在重复选项文本；用户于 2026-09-29 决定保留它们的生成和过程分数，但将最终答案正确率标为 N/A。因此 GPQA 全范围仍为 198 题，答案可判分上限为 196 题，最终报告必须同时列出两个分母。

### 5.2 生成与评分记录

| 产物 | 必须包含的内容 |
|---|---|
| `generation_batches/<batch_id>.json` | 全批 raw 输出、输入/输出 token IDs、attention mask 或可重建信息、每行实际结束位置、批种子、模型/tokenizer/prompt 哈希、采样配置、长度、结束原因、耗时 |
| `parsed/<generation_id>.json` | raw 哈希、每个 step 及 answer 的字符区间与原文、独立 process/answer 状态、所有格式错误 |
| `scores/<score_id>.json` | generation_id、scorer 身份、所有目标 step 的 g/v/NLL、同一 target IDs、两侧 context IDs、两侧逐 token logprob、G/M/W、失败位置 |
| `outcomes/<generation_id>.json` | 答案提取原文、确定性解析结果、correct/incorrect 等状态、测试通过数/总数、评测器与测试哈希、执行限制及错误 |
| `items.jsonl` | benchmark/题号/生成模型/评分模型、完整状态、G/M/W/NLL、步骤数/token 数、答题结果及上述证据链接 |
| `coverage.json` | 原始来源数、去重数、预期任务数、已尝试数、生成完整数、过程可评分数、W 可用数、答案可评分数、共同样本数及逐类缺失原因 |

ID 规则：generation_id 只由协议、生成模型和 problem_id 确定，不含 worker 编号。score_id 再加入评分模型与评分协议；改变评分者不能触发重新生成。

Mock 记录必须携带 `scientific_evidence=false`，科学报告入口拒绝把 mock 当正式结果。

## 6. 完整轨迹提示、解析与评分

### 6.1 提示必须写入版本化资源，不散落在各 benchmark 适配器

公共 system 提示的含义固定如下；实施时保存实际使用的英文原文及哈希：

> Solve the task. Express your reasoning as a sequence of coherent steps. Put each reasoning step inside <step>...</step>. Use as many steps as the solution actually needs; do not split a sentence merely to create more steps. Then put the final answer inside one <answer>...</answer> block. Do not write text outside these blocks. Stop after </answer>.

仅增加任务所需的固定后缀：选择题 answer 只给选项标签；GSM8K 给最终数值；代码题 step 为算法解释，answer 为完整 Python 代码，不使用 Markdown 围栏。不要求至少三步，不人为制造相同的步骤数，不提供参考解。

Qwen3 原生 thinking 关闭通过 chat-template 参数实现，不通过解析时偷偷丢弃 `<think>` 实现。

### 6.2 解析规则

使用确定性状态机。允许标签块之间的空白；拒绝嵌套标签、step/answer 混杂、多个 answer、answer 后再出现 step，以及标签之外的非空白正文。每个 step 非空，answer 非空。标签字面量与代码内容冲突时记录格式冲突，不猜测修复。

保留原始字符区间和正文空白；不让 LLM 分步、不改句子、不重编号、不删除重复步骤。重复生成循环是观测结果，不是清洗理由。

生成在闭合 `</answer>` 后结束，而非 `</step>`。必须逐行跟踪 batch 内各样本完成状态。模型 EOS 只有在结构确实完整时才算完整生成；达到预算上限却未闭合的输出标记 truncated。

process 与 answer 的可用性分开：有唯一合法答案块而 step 格式坏掉，仍可计算任务结果；但其 G/M/W 不进入主分析。截断轨迹已闭合的前几步仅供诊断，不当作一条完整轨迹。

严格区分生成内容和为已经完成的 batch 行填入的 padding。不能把停止后的 pad token 当作模型输出，也不能静默删掉停止前的异常正文。

### 6.3 teacher-forcing 评分规则

评分提示使用**同一个 E3 完整轨迹任务提示**，不能使用 E2 的“只继续一步”指令。q 在公式中包含这套固定提示与原题。

对目标 step，full 上下文由 chat-template 的 assistant 开始位置和 raw 输出中目标正文之前的真实前缀组成。deleted 仅移除紧邻前一步的整个 `<step>…</step>` 块；其他文本及块外分隔空白保持不变。目标之后的任何步骤、最终答案都不得出现。

为延续已有 PALS 的边界约定，两侧 context 分别编码；目标正文只编码一次，再连接到两侧 context 后进行 teacher forcing。目标的两套 logprob 必须逐 token 对应。

这测量的是冻结文本序列化约定下的条件支持，不宣称逐 token 复现采样时跨边界 tokenizer 的全部细节。记录序列化版本、tokenizer 与 IDs；不能在一部分样本使用 raw generation IDs、另一部分使用重新编码 IDs。

逐 token 分数以原 `metrics.pair()` 重算；full/deleted 的目标原生 masked-loss 均与均值 NLL 对照。沿用现有 canary 容差：重复前向最大差 1e-5，原生 loss 差 5e-3；超过则停止验收，不通过扩大容差掩盖问题。

## 7. 多卡、batch 与只生成一次

拟定正式布局：一个模型作业使用八张 GPU，各 GPU 一个独立 worker；一个 generation batch 含最多 8 道**不同题目**。按 benchmark/输出预算分组，再按冻结题号顺序切批。尾批可以不足 8；不得复制题目补齐。

评分首版每 GPU 按目标对顺序评分，复用已核验 `_score()`；不为了生成 batch=8 就冒然改写评分数值内核。CPU 答案测试单独运行，不让 GPU 等待代码执行。

批次列表、派生种子、8 路分片在 init 时固定。禁止运行时更换 batch size、动态重分片或 OOM 后自动降配置继续。这些操作会改变采样条件；确需修改时另立协议并请求用户决定。

实现时先通过 batch=1 的合成正确性测试，再通过真实多题 batch=8 的边界、padding、种子及原生 loss 测试。不同 batch 产生的采样文本不要求逐字一致；要求任务无重复、输入各异、停止正确、概率计算满足既定容差。

状态机：

```
planned → attempt_started → raw_committed → parsed
                                        ├→ self_scored
                                        ├→ outcome_evaluated
                                        └→ common_scored（固定子集）
```

启动生成前先不可覆盖写入 attempt_started；模型返回后先原子保存**整批 raw 结果**，再解析和评分。不能先解析成功才保存 raw。

恢复规则：

1. 已有 raw：只重放解析、评分或答案评测，绝不重新生成。
2. 已 started 但没有完整 raw：标为 uncertain，不自动重发；向用户报告可能已发生生成。
3. 截断、格式不合格、错误答案：是终态样本，不重采样“救回”。
4. 概率评分或 CPU 基础设施故障：保留旧失败；仅在明确授权/预先批准的恢复范围内重算同一份 raw，使用新的派生产物目录。
5. `resume` 默认不能恢复生成，只可继续未启动的已冻结任务及重放已有 raw；遇 uncertain 必须退出并说明。

E3 自己实现递归代码快照：按包内相对路径哈希 `pals_validation/**/*.py`、提示资源、配置、入口脚本和评测器依赖标识。不得沿用只扫顶层文件的旧 `code_hashes()`。

## 8. 最终答案怎么验收

选择题解析固定标签，GSM8K 进行确定性数值规范化。不能从推理正文里寻找更有利的答案，也不能将选项中的任意字母误识别为答案。使用浮点/分数/单位等哪些容忍规则，必须对齐选定的 benchmark 评测协议并冻结。

HumanEval 和 LCB 在受隔离的 CPU 作业运行生成代码，网络关闭，环境不含凭据，限制时间、内存、进程与输出大小。测试不能进入生成/评分提示。普通 subprocess 或仅 AST 黑名单不构成足够隔离。

实施第一阶段就查明并固定：官方任务版本、官方或明确兼容的评分器提交、比较规则、测试集合、Python 环境与超时政策。LCB 尤其需区分 stdin 与 functional、浮点容差等；旧参考解验证器不能替代这个确认。

若缺少可靠评测器或集群可用的安全执行环境，停止**代码答案评测阶段**并报告；可以保留已经授权生成/评分的证据，不能把基础设施不支持计为模型答错，也不能宣布已得到官方 pass@1。

状态至少区分：`correct`、`incorrect`、`invalid_answer`、`program_error`、`program_timeout`、`harness_unsupported`、`infrastructure_error`、`not_evaluated`。在冻结的有效执行政策下，程序错误/程序超时属于任务未通过；框架故障或不支持属于未知。

每题一次时报告“单次生成准确率/通过率”。正常生成但没有合法最终答案计任务未通过；基础设施未知另报分母、已评样本比例和必要的上下界，不能默默排除后声称全量通过率。

## 9. 分析输出必须预先写死

### 表一：真实轨迹主结果

每个 benchmark × 生成模型一行：交付题数、生成完成数、过程有效数、W 有效数、答案已评数、单次正确率/通过率、G/M/W 的题均值及 95% 区间、平均 full NLL、步骤数与长度摘要。

G/M 只对可评分完整轨迹聚合；W 用自己的有效分母，不把不足步骤样本填零。各模型自己的完整集合是描述性主表，跨模型差值另用共同可用题目集合，并同时报告交集损耗。

### 表二：结果与过程的互补性

在每个 benchmark × 模型内部，将可评分题目按答对/答错分组，比较 G/M/W 与普通 full NLL，报告数量、分布与题级区间。正确率不是 PALS 的训练标签，不按这些结果调整公式或阈值。

补充相同答题结果下的过程差异。跨模型同题可比较整条轨迹摘要，但不能把“各自第三步”当作同一语义步骤配对。

### 表三：共同评分者与长度检查

固定 10% 子集分别列自评分和 Qwen3 共同评分结果；记录缺失题。按步骤数和目标 token 数做预先固定的分层描述，判断 W/M 差异是否主要伴随长度变化。分层边界在看结果前确定；不能挑出最好看的范围作为主结论。

统计规则：题目是基本单位；bootstrap 5000 次，冻结种子，95% 区间。跨模型配对差值以相同原题索引共同重采样；不得将步骤/token 当作独立样本扩充分母。二分类通过率使用全体已确定结果的题级记录；不同表不同分母必须显示。

若做很多关联分析，放在探索性附表并报告检验范围；不要把少量模型/benchmark 聚合点的相关性当作主要验证。CoEvalChain 历史 Table 2 仅作来源明确的旁证表，不宣称来自同一轮分布式实验。

案例自动固定选择：每 benchmark × 模型在过程有效集合中列出 M 最大、W 最大、按稳定哈希选择的一个普通样本，去重后展示；另外列出格式失败/截断案例。缺少某类则明确 N/A。每个案例展示完整步骤、全部 g、最终结果，不只截最有利的一步。

可验证的主结论是“真实轨迹中的条件支持画像及其相对于最终分数的补充信息”。不要求 M/W 与错误率显著正相关才能完成实验，也不以效果好坏决定保留样本。

## 10. CLI 与目录合同

以下均为**实施后的目标接口**，当前不能直接运行：

```
pals-e3 prepare  --sources SOURCE_MANIFEST --out NEW_PREPARED_DIR
pals-e3 init     --prepared PREPARED_DIR --config MODEL_CONFIG --out NEW_RUN_DIR
pals-e3 worker   --run RUN_DIR --stage generate|score|evaluate|common-score --shard I --shards 8
pals-e3 audit    --run RUN_DIR --out NEW_AUDIT_DIR
pals-e3 analyze  --runs RUN_A RUN_B RUN_C --audit AUDIT_MANIFEST --out NEW_ANALYSIS_DIR
pals-e3 report   --analysis ANALYSIS_DIR --out NEW_REPORT_DIR
```

`prepare/init/audit/analyze/report` 不触发网络、模型生成或代码执行。worker 必须显式选择阶段；`evaluate` 还要验证 CPU 隔离环境。CLI 的 help 写清哪些命令有算力副作用。已有目录只允许核验和恢复，不覆盖；所有原始产物保持不可变。

配置分为协议部分和部署部分。协议包括所有模型/提示/采样/评分/解析/答案评测版本、batch 和种子；部署部分包括明确的本地路径和允许的作业资源。禁止 `model=latest`、隐藏默认 temperature、无限重试和未固定的远端代码。

建议固定以下配置结构，执行 agent 不要自行另起一套字段。示例中的大写占位符必须替换并校验，不能带占位符启动；`runtime_versions`、模型文件哈希和评测器版本须从实际环境冻结：

```json
{
  "schema_version": "pals_e3_config_v1",
  "backend": "hf",
  "protocol_version": "native-trace-v1",
  "prompt_version": "native-trace-prompt-v1",
  "parser_version": "strict-tag-v1",
  "scoring_version": "adjacent-deletion-explicit-boundary-v1",
  "model": {
    "id": "Qwen2.5-7B-Instruct",
    "path": "REPLACE_WITH_APPROVED_LOCAL_PATH",
    "revision": "REPLACE_WITH_EXACT_REVISION",
    "files_manifest": "REPLACE_WITH_MODEL_FILE_HASH_MANIFEST"
  },
  "hf_runtime": {
    "dtype": "bfloat16",
    "attention": "sdpa",
    "max_context": 32768,
    "cpu_threads": 4,
    "chat_template_kwargs": {},
    "runtime_versions": {"torch": "REPLACE", "transformers": "REPLACE"},
    "reviewed_local_code": null
  },
  "generation": {
    "samples_per_question": 1,
    "temperature": 0.7,
    "top_p": 1.0,
    "top_k": 0,
    "repetition_penalty": 1.0,
    "num_beams": 1,
    "do_sample": true,
    "batch_size": 8,
    "max_new_tokens": {"knowledge_math": 8192, "code": 16384},
    "master_seed": 2026092903
  },
  "scoring": {"temperature": 1.0, "target_batch_size": 1},
  "common_scorer": {
    "model_id": "Qwen3-14B",
    "fraction": 0.1,
    "rounding": "ceil",
    "selection_seed": 2026092903
  },
  "execution": {"shards": 8, "automatic_generation_retries": 0},
  "evaluation_manifest": "REPLACE_WITH_PINNED_EVALUATOR_AND_TEST_MANIFEST",
  "analysis": {"bootstrap_draws": 5000, "bootstrap_seed": 2026092903}
}
```

此例 `max_context=32768` 不是对所有模型容量的断言：每模型配置先验证后冻结。Phi 的必要本地自定义模型代码仍按现有 `model_policy.py` 的 revision/hash 白名单审核；不得用全局 `trust_remote_code=true` 绕过。Qwen3 配置明确写入 `chat_template_kwargs.enable_thinking=false`。

E3 后端只将 `hf_runtime`、核实的 `model_revision` 和 worker 实际设备映射成旧 `HFBackend.__init__` 所需字段，不将 E3 配置传进旧 `run.validate_config()`。生成参数、逐任务预算与 E3 提示由新后端处理。每个 worker 只能选择其 Slurm 分配的 GPU，不能硬编码占用宿主机某张卡。

题目排序、10% 抽样及批种子统一用显式 SHA-256 域分隔：例如 `e3-common-v1|seed|benchmark|problem_id`。按摘要升序选题，相同摘要以 problem_id 决胜；批种子取包含模型身份及有序题号的批摘要前 8 字节，再对 `2^63` 取模。禁止 Python `hash()` 或依赖字典遍历顺序。

分析分层预先固定为总推理步骤数 `1、2、3–4、5–8、≥9`，平均目标正文 token 数 `≤32、33–64、65–128、>128`；两者分别分层，不默认交叉成大量稀疏格子。少于 10 题的格子只显示数量与描述值，不据此作稳健比较。

项目目录示意（日期和 run_id 由实际冻结记录产生）：

```
/work/projects/polyullm/xxj/PALS/
  src/<immutable-code-snapshot>/
  configs/e3/<protocol-id>/
  artifacts/e3/<prepared-id>/      # 原题来源及私有评分材料分开放置
  runs/<date>-e3-<protocol-id>/
    qwen25/                      # 每模型独立身份与锁
    phi4mini/
    qwen3/
    common-scorer/
  logs/e3/<run-id>/
```

运行内部保留 manifest、source/code/model hashes、jobs、attempts、generation_batches、parsed、scores、outcomes、failure_records、stage_completions 和 audit。敏感数据权限使用项目允许的私有策略（通常 `umask 077`）；不改变 PALS 外目录权限，不在登录节点运行生成或候选代码。

Git 只提交代码、合成测试夹具、协议与不含私有题目的工程记录。题面、测试、模型、完整输出、凭据不入 Git。仓库的 example 配置只能使用明显占位路径；实际机器路径在私有部署配置中冻结。

## 11. 测试与验收：执行 agent 不得省略

所有新测试使用标准 `unittest` 或项目现有框架，核心离线测试不依赖下载模型。新增测试按协议/数学/运行生命周期/答案适配/审计拆文件，E1/E2 原测试全部继续通过。

| 测试类别 | 必须覆盖的具体断言 |
|---|---|
| 数学 | g=[-1,1,2] 时 G=2/3、M=1/3、W=sqrt(14)/3；g=[0,2] 时 W=1 而不是 sqrt(2)；空/单目标 null 规则；非有限数拒绝 |
| token 证据 | full/deleted target IDs 完全相同；只差指定前一步；两侧 NLL 重算；打乱一个 logprob/ID 必须审计失败 |
| 提示与泄露 | 参考答案/代码/DAG/隐藏测试的唯一哨兵字符串不得出现在任一生成或评分提示；GPQA 选项映射正确 |
| 解析 | 1/2/多步、多个 answer、嵌套、标签外文字、缺结束标签、EOS、截断、代码内标签冲突、重复步骤；原文区间可重建 |
| 多题 batch | 8 个不同问题不是同题重复 8 次；不同长度 left padding；各行不同结束时间；完成行 padding 排除；尾批不复制 |
| 一次生成 | 进程在 started/raw/parse 三处中断；已有 raw 不再调用 generate；uncertain 不自动重发；同锁双 worker 拒绝 |
| 输出评分 | 正确/错误标签、数值规范化、代码函数接口、浮点输出、程序超时与 harness 故障分离；不从 step 搜答案 |
| 安全 | 测试候选代码无法联网/读取凭据/逃逸资源限制；平台不支持隔离时拒绝执行；私有测试不进入 prompt |
| 聚合 | 按题等权，先截负部再平均；W 缺失不补零；三模型配对用原题交集；共同评分者不复制样本扩大 n |
| 完整性 | 改 `e3/` 任一模块、提示、模型身份、输入或产物后 audit 失败；重复/缺失任务、非法状态转移均检出 |
| 兼容 | 原 E1/E2 的配置、生成边界、指标、冻结输入及结果复核不变；mock 禁止进入正式科学报告 |

真实 canary 上限：每模型 × benchmark 一个 8 题 batch，共最多 120 条新生成，题号在看输出前冻结。它用于验证输入、格式、预算、多卡、概率内核和答案评分，不用于挑模型效果。

canary 不得因为 M 不大、W 不升或答案错了而失败；若某模型/benchmark 整批没有一条可解析完整轨迹，或存在数值/隔离/协议错误，则报告原因，不能自行改 prompt 重抽。正式运行配置完全相同且证据齐全时 canary 可作为预先指定的正式批次直接保留，不能再生成一次；配置改动后旧 canary 单独归档。

正式验收区分两件事：所有任务是否已有可追踪终态，科学指标是否对每条样本都有效。前者完成不意味着后者 100%；真实格式失败应完整报告。Slurm 成功也不替代应用和证据审计。

## 12. 实施顺序、资源与停机规则

1. **来源与差异审计**：只读检查当前分支/用户改动、原题范围、三模型修订、代码评测器和隔离环境。输出逐 benchmark 来源清单与缺口，不启动生成。
2. **纯函数层**：实现 schema/data/protocol/metrics 和合成测试；先冻结 g/G/M/W、解析、缺失值与 prompt 合同。
3. **运行层**：实现独立 CLI、递归快照、不可变批次、锁、一次生成状态机；用 mock 完成从 prepare 到 report 的全流程和故障注入。
4. **概率和答案后端**：接入已有 HF 数值内核、多题生成和独立 CPU 评测；E1/E2 回归通过，公开/私有材料隔离测试通过。
5. **交付可审查版本**：给用户代码差异、测试结果、实际题数、模型/评测器版本、预计调用量和 canary 配置。获得实际执行所需授权后才能进集群。
6. **有界 canary**：测真实耗时、峰值显存、步骤/长度分布和失败类型；按实际生成与评分成本估计全量 GPU-hours/CPU-hours，告知用户后决定正式资源。不要凭旧单步 E2 用时估计完整 E3。
7. **正式一次生成 → 评分 → CPU 答案测试 → 固定子集共同评分**：可分阶段流水处理，但严格沿用冻结身份。完成后离线审计、报告，不按结果好坏修改公式。

资源账本分别记录生成输出 token、两侧评分输入/目标 token、GPU 时间、CPU 测试数与时间。正式生成请求上限为冻结的 `3 × 唯一题数`，canary 若复用不重复占一次生成；共同评分只增加前向计算，不增加生成。具体硬上限和 Slurm walltime 在 canary 后提出并确认，不能默认无界。

必须 `STOP_AND_ASK` 的情形：原题范围/来源与目标不符；缺失模型修订或安全评测器；需要更换提示、模型、预算、batch、数据排除规则；发生无法判定是否已生成的中断；评分数值不一致；隔离或数据完整性失败。可以完成不受影响的离线测试，但不得闷头改变实验。

不需要把正常的负 g、零 M、高 W、错误答案当成工程故障。保留它们，它们就是实验观测。

## 13. 最终交付与给执行 agent 的指令

代码交付：独立可安装的 E3 入口、离线 mock 示例、全部测试、配置示例、八卡/CPU 作业模板、运行/恢复/审计说明、E1/E2 未改变的回归证据。

实验交付（获得授权执行之后）：不可变来源与运行清单、每题完整 raw、全部逐 token 证据、独立答案判定、明确缺失原因、三张正式表、逐题 CSV、完整案例和中文结论。结论只依据本次真实轨迹，不能用旧 DAG 结果替补失败样本。

可直接给执行 agent 的任务：

> 阅读本文件及仓库现有 E1/E2 实现，按第 12 节顺序实施 E3。不要改变已有实验协议，不运行旧 E2 生成器冒充完整轨迹，不接入 LLM judge。先完成离线模块、测试和 mock 全流程，并提交来源/模型/评测器缺口清单；在用户批准算力执行前，不连接 B1、不启动模型生成或候选代码执行。所有有争议的科学决策按本文冻结；遇到 STOP_AND_ASK 条件，给出具体证据和最小需要用户决定的问题，不自行兜底。
