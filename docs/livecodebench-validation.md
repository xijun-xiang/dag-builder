# LiveCodeBench v6：执行协议与进度

2026-09-22 用户确认：仅第六批新增题 `v6`（test6.jsonl，175 题），
不是累计 1055 题的 `release_v6`，也不使用可变 `release_latest`。

## 当前实现范围

已实现严格离线导入、5 题固定选样和单候选参考代码生成；参考代码验证、DAG 构建和 PALS 正式
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
