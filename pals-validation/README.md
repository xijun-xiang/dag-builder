# PALS：GPQA-Diamond / HumanEval / LiveCodeBench v6 E1 / E2

将散落的先导协议整理成独立、可审计的实验代码。不依赖 infi-evalscope 或 dag-builder 的 Python 包；不自动连接集群、下载模型或提交任务。支持 GPQA、HumanEval 及 LiveCodeBench v6 的统一格式 accepted DAG，不包含 E3。

HumanEval 使用 `prepare --benchmark humaneval`，原始函数说明取代四选项输入；其 g/N/D、图算子与选点规则不变，使用独立协议版本。[HumanEval 操作说明](docs/HUMANEVAL.md)。GPQA 仍为默认 adapter。

LiveCodeBench v6 使用 `prepare --benchmark livecodebench`，输入须是构图库完成来源、CPU 与语义审核后发布的 `unified/pals_dag_unified_v1.jsonl`，并用 `--expected-sha256` 固定文件。stdin 题 `entry_point=null`，functional 题保留函数入口；两者仅使用题面和推理步骤，参考代码与答案节点均不进入评分前缀。此入口已通过离线兼容性测试，**不是已完成的 B1 LiveCodeBench PALS 实验**。

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

在已有合规推理环境安装 `pip install -e '.[inference]'`。依赖范围用于安装兼容性，不代替正式实验的环境锁定；每次启动保存 `pip freeze`。所有权重必须已经在本地，后端禁用网络下载，默认禁用自定义模型代码。确需自定义实现的模型必须配置 `reviewed_local_code`：绑定实际 revision、全部 Python 文件的 SHA256 和审查记录；只有核验通过才允许加载本地实现，auto_map 不得指向外部仓库。权重只使用 safetensors。兼容环境用 `runtime_versions` 固定，不在任务内在线安装或替换其他模型的依赖。

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

同一 run 重新提交同样的分片：已落盘结果不重算；E2 已保存的生成不重新采样。锁改为 POSIX flock：进程死亡后由操作系统释放，锁文件本身保留，**不要删除它**。启动器在真实输出文件系统上检查互斥与释放；不支持时停止，不能关闭锁强行运行。只支持同一冻结代码的新版本 run，不把旧代码产物直接迁移到新版本继续混算。

完整 canary → E1/E2 的入口为 `python -m pals_validation.campaign --root <模型运行目录>`；明确续跑时添加 `--resume`，不自动重提任务。输入、配置、源代码、分片数必须与初始化时一致，否则拒绝续跑。初始化先在新暂存目录构建，完整后原子发布；中断的暂存目录保留备查，不作为有效 run。每次启动的日志、环境、验收分析与失败原因都保存在独立 `attempts/` 子目录，不覆盖旧记录。已完成分片重新检查产物哈希，再跳过计算。

从0.1.2起支持独立的 `--experiment e1` 与 `--experiment e2`，必须分别使用新运行目录，并在配置中固定对应的 `campaign_experiment`。E1只做评分canary和正式E1，不调用生成或E2门槛；E2独立做原生loss检查、生成canary与正式E2，不依赖E1完成。旧版不传参数的组合流程保留兼容。

HumanEval 79题重跑设置见 [解耦版协议](docs/HUMANEVAL_V2.md)。`canary_coverage_policy=report_invalid` 不要求所有抽样都符合格式，但每个题目-温度格必须完成全部预定抽样，且至少2条有效以检验重复评分；原始输出、数值算术和分片检查不放宽。默认旧配置仍要求完整生成覆盖。`generation_prompt_version=humaneval-single-step-v2` 只改变生成提示词，不改变计算g时的固定参考提示词；评分温度仍为1。

未落盘的生成批次仍只能重新执行同一个固定 seed，不能声称恢复了那次未保存的抽样。显式续跑是工程恢复，不允许为改善格式覆盖率或实验效果重新抽样。

```bash
pals-validation analyze --run /absolute/path/run --output /absolute/path/new-analysis-directory
```

只有所有分片和结果齐全、哈希与逐 token 算术复核通过才验收。输出 `summary.json`、`steps.csv`、`cells.csv`、`报告.md`。原始输出、token ID、两侧 logprob 和无效输出原因留在 run 中。格式失败不是 g=0；E2 主比较仅使用所有温度均完成全部 repeats 的共同题集，同时报告覆盖率。

`configs/mock.json` 只用于测试管线，其数值永远标为非科学证据。

## 正式启动前还差什么

在 Slurm 内用每个实际模型做少量真实端到端验收：chat template/停止边界正确、生成可解析、评分位置与 token 数对应、重复评分误差可接受、八分片能续跑。Qwen2.5-7B 已通过两题 canary；其他模型不能跳过。代码中的重复评分检查只能排查部分实现问题，不能证明整个后端正确。

给同事移植其他 benchmark，请先读 [扩展边界](docs/EXTENDING.md)。
