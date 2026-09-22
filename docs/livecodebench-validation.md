# LiveCodeBench v6：执行协议与进度

2026-09-22 用户确认：仅第六批新增题 `v6`（test6.jsonl，175 题），
不是累计 1055 题的 `release_v6`，也不使用可变 `release_latest`。

## 当前实现范围

已实现严格离线导入、5 题固定选样、单候选参考代码生成、隔离验证器和执行证据约束的构图入口；参考代码验证、DAG 构建和 PALS 正式
执行各有独立验收门槛，未完成前不得把本页写成“LiveCodeBench 实验通过”。
旧 GPQA/HumanEval 的图算子、g/M/N/D 与统计方法不变。

源：`livecodebench/code_generation_lite`，revision
`0fe84c3912ea0c4d4a78037083943e8f0c4dd505`，文件 `test6.jsonl`，SHA256
`bb4c364f71921c4495a6ad15abe1a927350b720009f4933e2e71f8af0f6fd1f5`。
对应官方 LFS 字节数 134303240。导入不执行 HF 数据集加载脚本、不解码 pickle，
不执行题目中的代码。旧容器缓存仅在字节哈希一致时复用。

```bash
PYTHONPATH=src python -m dag_builder.livecodebench_source \
  --root /absolute/private/lcb-v6-canary \
  --source-file /absolute/path/test6.jsonl --count 5
```

## 固定五题的选择

在 stdin/functional × easy/medium/hard 分层中，按 easy、medium、hard 依次
轮转两种 I/O，每层内部按 SHA256(seed,item_id) 排序；seed=20260922。
不是挑容易出正结果的题，失败后不换题。分层五题不是全量统计的代表性估计。
实际不存在的层跳过；选样清单、完整 175 题清单及源文件全部保留。

## 数据与计算分层

1. 参考程序生成只看到题目、starter code 和公开示例，不看到隐藏测试。
   原始 API 请求/响应保存，正式 JSON 只取 content，不能以 thinking 兜底。
2. 在隔离的 CPU Slurm 作业中验证 Python 参考程序。stdin 与函数型测试各有
   独立适配。源码解析通过不等于运行通过；超时和设施错误不记为答案错误。
   不在登录节点或本机不受限环境执行生成代码。
3. 仅通过冻结测试的程序进入自然语言解释、拆步、依赖标注及审核。
   明确标为“测试通过的模型参考程序 + 模型审核解释”，不是官方/人工 gold。
   测试通过也不是数学正确性或 DAG 语义正确性的充分证明。
4. 冻结模型审核通过的 DAG 子集及所有失败分母，先验收 5 题再核定全量。
   当前 API 授权仅覆盖这 5 题：原代理 deepseek-v4-flash，最多 160 请求、
   800 万预留 token；参考代码和构图共享总账，不按阶段或目录重置额度。
5. DAG 冻结后，三个既有模型运行 E1/E2，分别记录独立 canary 与正式结果。

## 后续正式统计（预设，不表示已经运行）

### 当前验收记录

- B1 数据作业 `111417`，COMPLETED/0:0，27 秒，2 CPU/4GB，无 GPU。
  缓存文件与上述官方 SHA256 完全一致，完整导入 175 题，未下载重复数据。
- 固定五题：LeetCode 3709、AtCoder abc396_a、LeetCode 3759、
  AtCoder abc398_c、LeetCode 3781。隐藏测试独立保存在私有 `tests/`。
- 参考生成配置 `configs/livecodebench-v6-canary-reference.json`：5 workers，
  thinking enabled/high，32768 输出上限；本阶段最多 20 请求/100 万预留 token。
  后续构图最多使用剩余的 140 请求/700 万额度，两阶段合计不超过本次授权。
- 启动器 `scripts/run_private_pilot.py` 会冻结代码与提示。只生成候选；
  不执行程序、不自动构图，不把一次响应解析成功标为数据质量通过。
- 五题参考生成已完成：5 次 API 请求，报告总用量 57972 token；4 个完整候选，
  LeetCode 3781 在 32768 输出 token 处 length 截断，全部用于 thinking，
  正式 content 不可用。保留原响应，不修补、不换题、不补采样。
- 四个候选对应 162 个公开/隐藏测试；第五题的 43 个测试没有可执行候选，
  不把未执行标成通过。实际 CPU 验收状态以 completion 与逐用例记录为准。
- 本次 CPU 验收尚未取得作业 ID：上传未完成，提交前文件检查已停止提交。
  改用等内容 gzip 包后仍在 120 秒整组进程上限处超时；未执行参考测试、
  未调用 DAG 构建 API、未启动 LCB PALS 评分。SSH 主连接存活但新会话
  延迟，尚不能归因为 Slurm 或 DNS 故障；需恢复传输后先核对作业和哈希。
- 获得用户明确许可后重建 SSH，身份检查曾通过，确认同名验收作业不存在。
  持续终端上传可见部分进展，但最终仍超时；再次连接在 banner exchange
  阶段超时，未通过身份检查。已停止本次传输；远端部分文件未经完整哈希
  验收，没有提交 CPU 验收或新增 API 请求。恢复后续用原候选，不重新生成。
- 本地回归通过：310 项构图测试（含 28 项 LCB 专属测试）与 63 项 PALS
  测试。该结果证明软件回归检查通过，不代替真实参考程序与 DAG 质量验收。

### 参考执行与构图入口

`scripts/verify_livecodebench_reference.py --prepare <参考生成目录> --root <执行包目录>`
只校验输入、绑定代码/测试哈希和准备数据，不执行生成程序。
真正执行仅在 B1 的 Slurm 作业中调用 `--root`；默认拒绝 seccomp、内存/CPU/
时间/输出限制、最小环境、每用例新进程，以及正负控制均生效后才运行候选。
隐藏期望输出不进入候选进程，只在父进程比较结果。

当前质量门槛使用精确 JSON 相等、或逐行空白归一化后的文本相等，不做浮点
容差放宽。它是保守的参考程序筛选门槛，不冒充官方排行榜的完整评分器；
未来若需浮点题容差，必须新增明确的比较协议，不能静默改旧结果。

```bash
PYTHONPATH=src python -m dag_builder.livecodebench_dag \
  --source /absolute/private/reference-generation \
  --execution /absolute/private/verified-execution-snapshot \
  --expected-manifest /absolute/private/pre-submission/input-manifest.json \
  --root /absolute/private/dag-canary
```

只有 Slurm 执行完成、隔离正负控制通过、全部计划测试齐全、程序/源题/测试/
结果哈希与提交前本地 manifest 匹配的候选才被纳入。缺失任一条件立即停止。
执行入口会区分错误答案、超时、运行错误、设施/隔离错误，不把它们合并成
一个“模型答错”。构图实际使用 `configs/livecodebench-v6-canary-dag.json`，
仍是解释→审核→拆步→依赖→论证→DAG审核六阶段，保留 HumanEval v5 的
代码事实、自包含与不变量质量检查，但使用独立 LCB 提示词及来源标记。
原 HumanEval 提示词不变，不能把 LCB 的模型程序标成官方 canonical solution。

参考代码、测试及 API 响应始终留在私有产物目录；Git 只发布实现和合成测试。
LCB 的 PALS 评分适配与全量提交尚未验收，不应直接用 HumanEval benchmark 标签运行。

### 正式实验预设

- E1：同一拆树基线，合法完整分支交换 vs 相邻直接依赖逆置；主终点为
  共同题、共同目标上的 M(破坏)−M(合法)。所有适用题纳入，不人为造分支。
- E2：每题哈希选一个非答案锚点，其全部祖先作前缀；温度 .3/.7/1.2、
  各 8 次单步续写，评分温度 1；主终点 D(1.2)−D(.3)。保留 N 和平均 g。
- D 为 ddof=1 的样本标准差。主表仅三温度各 8 次均有效的共同题集，
  同时给计划/有效分母、截断与格式错误，不补采样、不填零。
- 题目等权，5000 次题级 bootstrap，seed=2026091703，95% 点式区间。
  g/M/N/D 不跨 tokenizer 做绝对排名；不因不利效应修改配置。
- 生成预算沿用 HumanEval v2 的 4096 新 token 候选设置；全部题目前缀需
  逐模型 token 化核算上下文，无法容纳时在正式运行前修订协议，不截断。
- 任一温度无效率 >=5% 提示覆盖风险；数值/哈希/分片失败停止对应任务。
  生成缺失、模型表现与基础设施失败分别报告。

## 目录与版本纪律

B1 唯一写入项目 `/work/projects/polyullm/xxj/PALS`：`src/` 固定源码，
`artifacts/` 存冻结接受数据，`runs/` 每次独立目录，日志与 scratch 属于 run。
源码先测后提交，推送 feature branch，不覆盖 main 或旧实验。
Git 不收题目原文、隐藏测试、API 原始响应或凭据；只收实现、合成测试、
版本化协议和不含题目的汇总结果。每次验收同步状态，不将计划写成完成。

## 官方题解优先路线：两题可行性试用

用户批准先试官方题解路线。使用原固定五题中的全部两道 AtCoder 题，
abc396_a 与 abc398_c，不另挑题；对应官方题解 12398、12531，来源身份与
任务索引已人工式核对（由助手完成，不冒充独立人工标注）。原文较短，
不能将其称为现成逐步 gold 轨迹，更不能称为官方 gold DAG。

`livecodebench-editorial-pilot-v1` 只做两次调用：带来源的步骤/依赖提议、
题解一致性审核。每个节点区分 source_supported 与 supplementary；
引用必须匹配源文本，模型补出的推导不得假装是题解原文或给定前提。
不为图复杂度凑步骤或分支，末节点保留原样官方代码，代码不作为 PALS
推理步骤。提示、请求、响应、来源快照与失败记录全部冻结。

本次来源为网页工具渲染文本的摘录转录，数学格式有归一化，并非原始
HTML 字节存档；普通 HTTP 抓取受到 403/429 限制。选取的 Python 代码
保持网页显示内容不变。来源快照留在私有目录，不默认取得转载许可。

两题合计最多 12 请求、60 万预留 token，仍计入原五题 160 请求/800 万
授权，不额外重置总账。构图试用只生成 editorial-candidate.json，明确
formal_eligible=false，不生成 dag.json，也不进入正式导出。
其官方代码另在 B1 CPU 隔离验收 87 个既有用例；测试通过与 DAG 语义
审核是独立条件，试用通过也不能据此推断全部 175 题均可采用该路线。

首轮两题在节点编号检查处停止：均从 0 而不是 1 编号，其中一题另有
reference_code 引用类型违规；不是测试通过的 DAG。两次请求报告用量
43873、预留 76145 token。修订提示显式列出编号起点和来源字段约束，
保留首轮原记录，用新目录重试同两题一次，不修改校验器来接收失败结果。
修订调用上限 10 次、50 万预留 token，准备清单记录前次消耗并检查共享
额度；正式内容仍不使用 reasoning_content 兜底。官方代码 CPU 作业
111437 已提交（2 CPU/4GB/20 分钟，无 GPU），结果尚待验收。
