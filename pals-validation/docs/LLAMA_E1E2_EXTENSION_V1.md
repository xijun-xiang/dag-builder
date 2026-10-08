# Llama 五基准 E1/E2 补全：执行合同

## 2026-10-09 排队衔接更新（科学协议不变）

用户要求安排 Llama E1/E2。旧准备目录和失败的旧 E3 全部保留，不调用旧提交器。
当前入口的新 run 为 `runs/20261009-llama-fivebench-e1e2-v2`。
prepare 会验证旧 `20261008-llama-fivebench-e1e2-v1` 没有任何提交记录，
并逐项对照其10份配置、全部输入哈希、模型和引擎身份；不复用或覆盖旧运行目录。

现在可提前提交10项HOLD任务，第一项绑定 `afterok:115001`，即正在运行的
InternLM E3 115000 GPU → 115001 CPU 链；其余逐项 afterok，失败依赖自动取消。
submit/release 只确认精确的前置链及协议身份、状态未失败，**不宣称E3已经验收**。
release 仍审查全部实际资源/路径/节点/依赖后倒序释放，每站最多同时8GPU。

每个E1/E2作业获得分配后，在加载模型前先执行宿主机 `admit-runtime`：核对前置E3
主/batch/.0全部COMPLETED0:0、15898题应用audit PASS、含判分与科学证据、协议与提交时
摘要一致，并保存本job专属准入回执。E3的report_invalid覆盖率政策按新授权保留，
不要求覆盖率高或效果正向。115000.1 是已记录的无GPU遥测诊断失败，不能当成推理.0失败，
也不得隐去；科学计算.0的任何失败仍阻止准入。容器runtime没有匹配回执不能启动。

本次仅更改调度与准入衔接，不修改以下科学合同、E1/E2引擎或运行时。
原生masked-loss、重复数值、E2 canary最少2条有效的原门槛不变。
下文原先“114963/114964先完成才提交”的流程是历史设计，由本节替代。

本轮只新增部署与调度代码，不修改 E1/E2 评分、生成、解析、算子或分析引擎。
沿用历史 InternLM 五基准 spec（SHA256
`54419278650427638ce329c502c4de40d5736d17cfbfd6d4ef0224cdbe63d01f`）中的
全部 full、canary-e1、canary-e2 文件，逐文件验证。17 个历史同名引擎模块保持原 SHA。
现有 InternLM E1/E2 不重生成；A1 Llama E3 与本任务不相互等待。

五基准 GPQA / HumanEval / GSM8K / LiveCodeBench / MMLU，分别运行 E1、E2，共10项。
正式 DAG 共5500题，E1共78823配对工作项；E2共5109锚点，每锚点三个温度、各8次，
共122616次正式生成。原 canary 为工程门槛，其额外生成单列，不冒充正式重复或独立验证题。
T=0.3/0.7/1.2、8 repeats、seed=2026091507、bf16/sdpa不变。
GPQA输出预算2048，其余4096；HumanEval使用原single-step-v2，其余v1。

Llama固定ModelScope revision `e9f7e7d3fa08b550ea228e38bb5501d35be92c0d`，
原生8192上下文、无自定义模型代码、容器Transformers5.6.0。原题不截断、不改RoPE、
不将E3的动态输出预算套入E2。全部输入的CPU容量检查已经通过，最大5897/8192。
模型、运行时和容量差异明确披露，不称全部配置与InternLM完全相同。

## 操作入口

`scripts/e1e2_llama_campaign.py` 提供 prepare/check/submit/release/runtime，
只允许B1的 `/work/projects/polyullm/xxj/PALS`。新run固定为
`runs/20261008-llama-fivebench-e1e2-v1`，必须使用新的干净冻结源码目录。
已有/部分部署或不确定提交一律停下，不重试、不覆盖、不使用 --resume。

1. **prepare**：核查114951容量审计和114954权重准备均成功、CPU凭据/固定来源哈希一致；
   复制冻结输入，保存10项配置、源代码/控制器/启动器哈希、模型哈希和真实分母。
   不加载模型，不提交Slurm，不生成轨迹。
2. **check**：重新核对部署身份、输入哈希、配置和仅指向当前run内的prepared链接。
3. **submit**：除自身check外，必须114963/114964主、batch、.0均COMPLETED0:0，
   B1 InternLM E3全量audit的协议、15898题、coverage_gate、include_outcomes及科学证据标志通过。
   操作员仍须按E3完整验收合同核验并留档；摘要门槛不替代逐token应用审计。
   检查B1没有本任务其他活跃链后，一次提交10项HOLD作业，每项8GPU/32CPU/256G/8h、
   code-agent、排除dgx06，逐项afterok且依赖失败取消。A1不在等待条件内。
4. **release**：再次检查前置E3审计哈希、全部提交回执、实际HOLD资源/路径/依赖/用户，
   然后倒序释放；不确定提交或释放记录不可重试。
5. **runtime**：仅在分配内重算完整模型哈希；调用原campaign，在同一次8GPU分配中
   先做原生masked-loss和canary，再进入formal。无额外丢弃式GPU canary排队。

启动器 `scripts/b1-llama-e1e2.sbatch` 不挂载InternLM兼容依赖；模型/代码/历史spec只读，
输出和cache只写新run。此任务不执行模型生成的候选代码。

## 验收

原生masked-loss误差≤0.005、逐worker重复评分误差≤1e-5；E2 canary采用原
report_invalid规则，每cell至少2条有效用于数值验收。异常/截断不修补、不重抽样。
formal不以效果方向或完整率挑选结果；保留全部无效分母，并同时报告三温共同完整题。
每项终态要求Slurm主/batch/.0成功、8worker完成、canary回执、formal分析与逐token
算术验证通过；仅COMPLETED或仅应用completion都不能单独判定成功。

本地186项CPU测试通过（含8项新控制器测试），不等同于真实模型或集群验收。
