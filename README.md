# DAG Builder

将题目与解答转化为**可追溯、可审核的推理 DAG**。

独立 Python 工具，使用模型 API 完成原子步骤拆分、依赖标注、推理解释和审核。无需 EvalScope、GPU 或 Slurm。支持 MMLU、GSM8K 与 GPQA-Diamond；不是任意 JSONL 的通用清洗器。

新增 [HumanEval 参考代码解释与 PALS E1/E2](docs/humaneval-validation.md)：本地官方数据导入、模型解释/审核、候选 DAG 导出；不执行代码。另见 [v4 质量门槛与分流回捞](docs/humaneval-recovery.md)：来源别名无损归一化、自包含步骤、根前提审查，以及新目录中的单轮修订。[诊断式全量回捞](docs/humaneval-diagnosed-repair.md)区分重建图、修订解释和源题隔离，不重复生成已接受候选。真实构图产出不等于 PALS canary 通过。

[v5/v2 接口修复](docs/humaneval-contract-fix.md)明确代码事实的可引用范围、补齐旧边/论证的诊断来源，并提供不改原结果的离线复核命令；旧协议保留。

[API 控制与预算保护](docs/api-response-contract.md)：失败的长输出探针保留；后续内容门槛例外需要显式授权，逐响应校验和预算保护持续生效。不能将批量构图成功解释为服务端上限契约已通过。

[2026-09-22 正式实验结果索引](docs/PALS_RESULTS_20260922.md)记录 GPQA/HumanEval
三模型的已验收主结果、有效样本分母、协议差异及私有产物定位；保留负向和不确定结果。

[PALS 最终 DAG 数据统一格式 v1](docs/pals-dag-unified-v1.md)固定今后各 benchmark 的交付字段、节点/边和来源哈希，并提供 JSON Schema 与导出器。[GPQA/HumanEval 历史冻结格式](docs/gpqa-humaneval-frozen-data-schema.md)说明已经运行的两套 JSONL 和 HTML 如何读取。

[LiveCodeBench v6 接入进度与协议](docs/livecodebench-validation.md)：固定第六批
175 题、私有测试隔离、5 题 score-blind canary；未验收前不标为正式实验完成。

## PALS 验证实验（独立子项目）

[pals-validation/](pals-validation/README.md) 将 GPQA-Diamond DAG 用于 E1 换序、E2 固定祖先前缀续写验证，单独安装、单独运行，不改变 DAG Builder 的依赖和 API。只需要 PALS 时可以单独复制这个子目录；真实实验另需数据文件、模型权重及 GPU 推理环境。数学协议、逐 token 证据、续跑与验收说明均在子目录中。

## 工作流

```text
固定数据来源与样本
  → 生成解答 / 读取官方解答
  → 解答审核 → 原子节点 → 依赖关系 → justification → DAG 审核
  → 可选的有限回捞与重新审核
  → 保存全部状态、候选图、证据与报告
```

| 数据集 | 解答来源 | 使用说明 |
| --- | --- | --- |
| GPQA-Diamond | 官方 Explanation，不重新生成初始 CoT | [GPQA 构图](GPQA_PILOT.md) |
| MMLU | 模型生成；支持保存原生 `reasoning_content` 后整理 | [原生推理协议](docs/mmlu-thinking-pilot.md) |
| GSM8K | 独立生成、答案条件生成或官方解答等显式模式 | `configs/gsm8k-*.json` |

MMLU 全部 57 个独立学科的跨学科构图、预算化批量入口和 PALS 格式导出见
[MMLU 全学科协议](docs/mmlu-all-subjects.md)。原物理题提示词与旧结果保持不变。

## 安装

Python **3.10+**。建议在仓库根目录创建独立环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
dag-builder --help
```

`requirements.txt` 安装本项目及读取 MMLU/GSM8K Parquet 所需的 PyArrow。仅使用 GPQA 时，可用 `python -m pip install -e .`：核心运行时只依赖 Python 标准库。依赖定义以 `pyproject.toml` 为准。

## 快速开始：GPQA

先检查 `configs/gpqa-diamond-reference-30.json` 中的 API 地址、模型、并发与预算。配置示例沿用原实验代理；模型 ID 是否可用取决于服务商，不能把别名当成固定权重版本。

密钥由私有环境变量 `JUDGE_API_KEY` 或 `--key-file` 提供。密钥文件必须属于当前用户、权限为 `600` 且不是软链接。不要把密钥写进配置、命令行参数或 Git。

```bash
# 准备固定的 30 题；不发起模型生成请求。
# 如自动下载不可用，可增加 --source-archive /absolute/path/to/official.zip。
dag-builder prepare-gpqa --root "$PWD/outputs/gpqa-pilot-v1" --count 30

# 以下命令会调用 API，产生费用。先只运行一题检查协议。
dag-builder run --root "$PWD/outputs/gpqa-pilot-v1" \
  --config configs/gpqa-diamond-reference-30.json --limit 1 --resilient

# 确认产物与预算后继续，复用已完成阶段。
dag-builder run --root "$PWD/outputs/gpqa-pilot-v1" \
  --config configs/gpqa-diamond-reference-30.json --resilient

dag-builder status --root "$PWD/outputs/gpqa-pilot-v1"
dag-builder report --root "$PWD/outputs/gpqa-pilot-v1"
```

完成 30 题处理不保证产出 30 张通过审核的图。数据下载须遵守源数据的许可与访问条件；本仓库不分发 benchmark 原文。

## 审核与回捞

[新版回捞协议](REVISION.md)使用固定输入、带引文的问题诊断及最多两轮内容修改。普通复审只看到来源与当前候选，不携带生产模型的辩解或先前通过意见。来源争议、修改无进展及轮次耗尽会停止，不无限尝试直到通过。

- GPQA 和 HumanEval 并发上限为 **32**，其他任务为 **6**；实际值由配置决定，不承诺并发与速度线性增长。
- `--resilient` 对可重试传输故障采用每阶段最多四次的生命周期尝试限制；请求失败与内容拒绝分开记录。
- 输入、配置、提示词和代码有快照与哈希；变更协议应使用新运行目录，不能覆盖旧结果来续跑。
- 请求与 token 预算是运行护栏，不是费用报价；失败请求也可能产生费用。
- `release` 是独立的人工审核导出入口，不能与模型通过状态混同。

## 工程结构

```text
src/dag_builder/
  source.py, gpqa_source.py       数据接入与固定选样
  pipeline.py, stages.py          首次构图
  schemas.py, validation.py       格式、引文与图结构校验
  revision*.py, review_issues.py  候选快照、修改与审核契约
  repair_loop.py                 有限回捞状态机
  client.py, storage.py           API、预算与不可覆盖的产物存储
  campaign*.py                   历史全量实验编排与聚合导出
  prompts/                       版本化提示词
configs/                         无密钥的运行配置
scripts/                         启动、诊断及数据导出脚本
tests/                           离线合成测试，不调用模型
```

`campaign.prepare_campaign` 是复现既有“80 题先导 + 全量扩展”实验的专用入口，依赖外部历史产物，**不是新用户开箱即用的全量构图命令**。详见 [全量实验说明](FULL_GPQA.md)。部分后台启动脚本使用 macOS 保持唤醒机制；跨平台使用前台 CLI。

## 测试与协作

```bash
python -m unittest discover -s tests -v
python scripts/verify_local.py --output "$PWD/outputs/offline-check"
```

修改校验或审核逻辑时补充离线测试；新增协议须保留历史提示词，不原地改变旧运行。只有真实 API 运行才能检验服务兼容性，离线测试不证明模型判断正确。

## 数据与科学边界

模型通过 ≠ 人工认证的 gold；同一模型在不同上下文生产与审核，仍不属于独立模型验证。图闭合、引文匹配和答案正确也不能单独证明推理正确。管线不执行模型生成的任意代码，没有通用的数学或物理语义验证器。

实验数据、API 原始响应、凭据及运行日志均留在本地，不随代码发布。历史协议记录见 `docs/`、[旧版修复](REPAIR.md)及 [新版修复](REVISION.md)；当前使用方式以本 README 和实际配置为准。
