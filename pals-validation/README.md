# PALS：GPQA-Diamond E1 / E2

将散落的先导协议整理成独立、可审计的实验代码。不依赖 infi-evalscope；不自动连接集群、下载模型或提交任务。当前只支持 GPQA accepted DAG 导出，不包含 E3。

**状态：离线测试、真实 118 题预处理及 Qwen2.5-7B 的 B1 八卡 canary 已通过。其他模型尚未验收；配置示例不是已经验证过的五模型运行配置。** 详见 [canary 记录](docs/CANARY-20260918.md)。

## 两个实验

| 实验 | 固定什么 | 改变什么 | 报告什么 |
|---|---|---|---|
| E1 换序 | 题目、选项、步骤文字、评分模型 | 原始相邻破坏；拆树基线上的合法分支换序与相邻破坏 | 每步 g、轨迹平均负支持 M、同题同目标配对差值 |
| E2 续写 | 每题一个哈希选中的位置、其祖先前缀、评分温度 1 | 生成温度和随机续写 | 每次 g；每题每温度的 N、D；完整配对题的温度差值 |

E2 不是整条 CoT 生成，也不是固定 gold 步骤重新调温评分。每次只续写一个步骤；gold 目标内容不进入生成提示词。默认生成温度 0.3 / 0.7 / 1.2，每档 8 repeats。

详细定义见 [协议](docs/PROTOCOL.md)，历史差异见 [迁移说明](docs/MIGRATION.md)。

## 目录

```text
configs/                 模型配置模板、模型清单、合成测试配置
src/pals_validation/     数据校验、图算子、提示词、评分、运行、离线验收
tests/                   无需 GPU 的测试
scripts/                 B1 八卡启动模板
docs/                    协议、迁移记录、验收边界
outputs/                 本地预处理产物（忽略，不交付题目文本）
```

正式部署建议：代码在 `/work/projects/polyullm/xxj/PALS/src/pals-validation`；输入在 `PALS/artifacts/`，配置在 `PALS/configs/`，模型在 `PALS/models/`，每模型每实验独立运行目录在 `PALS/runs/`，日志在 `PALS/logs/`。不要写入其他人的目录。默认 umask 077；协作分享代码，不使用 chmod 777，也不自动放宽数据权限。

## 本地检查与准备

Python >=3.10。核心代码没有第三方运行依赖：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/pals-validation prepare --source /absolute/path/model_accepted.jsonl --output outputs/gpqa-v1 --expected-sha256 537403ba3190dd8e2f8b423554b8441cddd279a37f0f04068c0810fc20c129d8
```

可选 `--parent-probe` 添加父节点／非祖先删除诊断，不是默认主实验。源文件变更时使用新输出目录并记录新哈希，不能沿用这里的哈希冒充同一数据版本。

查看 `manifest.json`、`inventory.json`、`selection.json`：每题算子和不适用原因已固定。全量 118 题不是每个比较都强行有 118 个样本。

## 真实模型运行

在已有合规推理环境安装 `pip install -e '.[inference]'`。依赖范围用于安装兼容性，不代替正式实验的环境锁定；首个模型验收后保存 `pip freeze`。所有权重必须已经在本地，后端禁用网络下载与 remote code。

复制 `configs/gpqa-hf.example.json` 到项目配置目录，填写真实快照目录、权重 revision、模型支持的 context 上限和 chat template 参数。`model-matrix.json` 仅列出候选标识；不能据此宣称每个模型都已兼容。特别检查 InternLM 的原生 Transformers 支持及 Qwen3 thinking 模式。

```bash
pals-validation init --prepared /absolute/path/gpqa-v1 --config /absolute/path/model.json --output /work/projects/polyullm/xxj/PALS/runs/gpqa-v1-model-e1 --experiment e1 --shards 8
pals-validation init --prepared /absolute/path/gpqa-v1 --config /absolute/path/model.json --output /work/projects/polyullm/xxj/PALS/runs/gpqa-v1-model-e2 --experiment e2 --shards 8
```

初始化会冻结输入、配置、代码与模型文件哈希。不得初始化后改代码继续跑同一 run。模型哈希校验会产生启动 I/O，不能将其误认为卡死。

八卡是 **8 个独立单卡 worker**，不是张量并行：每个模型必须单卡装得下。E1 评分 batch=1；E2 同一前缀的 8 个 repeats 一批生成，随后逐条评分。OOM 时不要静默减少 batch；修改配置后创建新 run。

在 B1 项目目录创建日志目录后，按集群要求补充 partition/account，分别提交 E1/E2：

```bash
umask 077
mkdir -p /work/projects/polyullm/xxj/PALS/logs
cd /work/projects/polyullm/xxj/PALS
export PALS_REPO=/work/projects/polyullm/xxj/PALS/src/pals-validation
export PALS_RUN=/work/projects/polyullm/xxj/PALS/runs/gpqa-v1-model-e1
export PALS_PYTHON=/absolute/path/to/approved/environment/bin/python
sbatch "$PALS_REPO/scripts/b1-eight-gpu.sbatch"
```

CUDA worker 拒绝在 Slurm allocation 之外运行；运行目录必须在获准 PALS 根目录内。本仓库不自动申请资源。

## 续跑与验收

同一 run 重新提交同样的分片：已落盘结果不重算；E2 已保存的生成不重新采样。进程被强杀可能遗留 `workers/<shard>/ACTIVE.lock`，必须先确认旧 Slurm 任务及进程已结束，再人工移走该特定锁；禁止对活跃任务解除锁。未落盘的生成批次只能重跑，不声称恢复了那次未保存的抽样。

```bash
pals-validation analyze --run /absolute/path/run --output /absolute/path/new-analysis-directory
```

只有所有分片和结果齐全、哈希与逐 token 算术复核通过才验收。输出 `summary.json`、`steps.csv`、`cells.csv`、`报告.md`。原始输出、token ID、两侧 logprob 和无效输出原因留在 run 中。格式失败不是 g=0；E2 主比较仅使用所有温度均完成全部 repeats 的共同题集，同时报告覆盖率。

`configs/mock.json` 只用于测试管线，其数值永远标为非科学证据。

## 正式启动前还差什么

在 Slurm 内用每个实际模型做少量真实端到端验收：chat template/停止边界正确、生成可解析、评分位置与 token 数对应、重复评分误差可接受、八分片能续跑。Qwen2.5-7B 已通过两题 canary；其他模型不能跳过。代码中的重复评分检查只能排查部分实现问题，不能证明整个后端正确。

给同事移植其他 benchmark，请先读 [扩展边界](docs/EXTENDING.md)。
