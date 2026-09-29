# E3 原生轨迹运行手册 v1

本模块是 E1/E2 之外的独立实验。每个模型对每道原题只生成一次；自身模型以 T=1 的原始 logits 对完整推理轨迹逐步计算 `g`，再得到 `G`、`M`、`W`。`W` 是同一条轨迹的步骤间总体标准差，**不是**重复生成之间的 `D`。题目答案与过程评分分开验收。

## 数据与冻结协议

`pals-e3 prepare` 只接受五份精确原题源：GPQA Diamond 198、GSM8K test 1319、HumanEval 164、LiveCodeBench v6 新增 175、MMLU 五领域唯一题 1876，共 3732 题。固定哈希抽取共同评分者子集 375 题。GPQA 2 道选项文本重复题保留过程数据，正确率为 N/A。

Qwen2.5-7B-Instruct、Phi-4-mini-instruct、Qwen3-14B 各生成 3732 次，T=0.7、top-p=1、top-k=0、8 道不同题目为一个 batch、每题只生成一次。知识/数学输出预算 8192 token，代码题 16384 token。batch 成员和种子在 `init` 固定。真实 canary 只运行每 benchmark 的首个 batch，每模型最多 40 次生成，不挑选结果或重采样。每步只删除紧邻前一步；目标正文的 token IDs 在 full/deleted 条件相同。

## 已实现的安全门槛

- 原题与答案/测试分离；生成提示只从公开字段构造，不读取 grading 文件。
- 输出保留整批 raw、逐行结束位置、格式错误与独立答案块；未闭合轨迹不进入 `G/M/W`。一次生成状态机禁止 started-without-raw 自动重发。
- 审计重新解析 raw、重分词核对 full/deleted/target IDs、逐 token 重算 g、重新聚合 `G/M/W`；改动任一哈希或数值必须失败。
- HumanEval 使用原题测试的 `check(candidate)`；LCB 解码公开与私有测试，并按已审阅的 LCB 字符行/Decimal 及 functional 规则比较。**这是边界明确的兼容判分，不宣称与官方排行榜完全等价。**
- 代码题只允许在 B1 GPU-free Slurm CPU allocation 中执行。每个待测程序在独立 Python 子进程中运行：环境无凭据、输入不含 LCB 期望输出，先安装 Linux 默认拒绝 seccomp，实测文件/网络探针均须拒绝，另有 CPU/内存/输出/墙钟上限。危险导入或动态操作记 `harness_unsupported`/正确率 N/A，不能当作答错或偷偷排除。安全基础设施故障也记 N/A。
- 安全判分须先通过仅用合成程序的 `e3_harness_selftest.py`；其哈希进入不可变 `evaluation-policy.json`，未通过时 `e3_b1_init.py` 拒绝初始化。不能在登录节点执行生成代码。

## 运行顺序

1. 本地 `python -m unittest discover -s pals-validation/tests -p 'test_*.py'`；核对原题 manifest 及 prepared 输出。mock 只能检验工程合同，`scientific_evidence=false`。
2. 在 B1 的 `/work/projects/polyullm/xxj/PALS` 内建立**全新** `src/` 代码快照、`artifacts/` prepared 副本、`runs/` 安全门槛目录。保存传输前后 SHA256；所有输出权限使用 `umask 077`。不得修改旧 E1/E2 运行。
3. 仅提交 `b1-e3-harness.sbatch` 合成安全自检作业。核对 Slurm 终态、应用 `selftest.json`、真实 seccomp 文件/网络探针和正/负对照。失败即停止。
4. 安全门槛通过后，在新的 `configs/e3/` 和 `runs/` 目录执行 `b1-e3-init.sbatch`：逐题核对三模型上下文预算、哈希全部模型文件、冻结版本/评测政策、初始化三个独立 run。失败即停止，不自动改协议。
5. 每模型先做真实 logits 与原生 masked-loss 门槛，再做 generate、score、evaluate 的固定首批 canary；stage 各自独立，禁止把 Slurm `COMPLETED` 当审计通过。`audit --stage canary` 必须核对 120 条上限内的所有 raw、过程和答案终态。任何系统性格式失败、显存/数值/隔离错误立即停报。
6. 根据 canary 的 GPU/CPU 实际时长与有效率核定全量资源，沿用相同协议与 raw，不重新生成 canary。正式完成后以 `audit --stage all`、`analyze`、`report` 产生三张表、逐题 CSV 和完整案例。共同 Qwen3 评分只复用原 raw，不增加生成。

作业脚本必须在提交时显式指定 `--output`、`--error` 至对应新 run 的 `logs/`，并显式传入 PALS 路径环境变量；不依赖家目录或隐式凭据。`PALS_REPO` 为含 `src/`、`scripts/` 的不可变 `pals-validation` 快照。GPU 脚本在一个八卡容器 allocation 内给 8 个固定 shard 各一张可见卡；CPU 脚本不分配 GPU。

## 目前不应声称的事

通过离线单元测试不等于 B1 安全自检或真实模型 canary 通过；本次 E3 的 `W` 不测重复生成稳定性；人造分步标签与官方判分不自动等价；同一模型不同题的 g 可以描述模型对前文的条件支持，但不能直接证明推理正确、分布式框架更稳定或 CoEvalChain 的因果收益。
