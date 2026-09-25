# 本地 DAG 数据构建与交付 Pipeline

本文记录当前 `dag_fix` 分支用于 GSM8K 和 MMLU 的可复现数据构建流程。目标是把固定版本的原始数据转换为经过模型审核、可恢复、可追溯的 DAG，并导出为 `pals_dag_unified_v1`。

## 1. 数据范围

当前交付分为三组：

| 分组 | 子集 | 当前 `model_accepted` 条数 |
|---|---|---:|
| GSM8K | `main/test` | 530 |
| MMLU 数学 | `abstract_algebra`, `college_mathematics`, `elementary_mathematics`, `high_school_mathematics`, `high_school_statistics` | 415 |
| MMLU 心理与社会 | `high_school_psychology`, `professional_psychology`, `sociology`, `human_sexuality` | 594 |

合计 1,539 条。它们是模型接受的 synthetic reference DAG，尚未经过逐题人工批准，不能标记为 human-approved gold。

## 2. 总体流程

```text
固定数据集 revision
        |
        v
prepare: 下载/读取 Parquet、标准化、确定性选样、冻结来源哈希
        |
        v
solve: 独立求解并与数据集答案核对
        |
        v
structure_solution -> review_solution
        |
        v
atomize -> dependencies -> justify -> review_dag
        |
        v
model_accepted / rejected / paused
        |
        +--> 对安全的瞬态失败使用原请求断点重试
        |
        v
冻结 accepted_source.jsonl
        |
        v
转换并校验 pals_dag_unified_v1.jsonl
        |
        v
manifest.json + HTML 查看器 + 独立交付包
```

每个运行目录都是不可变审计目录：请求、响应、失败分类、配置、实现版本、DAG 和结果均保留在本地；再次运行时只复用完全匹配的缓存请求，不覆盖历史响应。

## 3. 环境与凭据

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[source]'
mkdir -p -m 700 .secrets
```

API key 只应保存在权限为 `0600` 的 `.secrets/judge_api_key`，或通过配置中的环境变量提供。命令行只传 `--key-file` 路径，不传明文 key；`.secrets/`、`.env*`、`data/` 和 `outputs/` 均不提交。

## 4. 固定并准备来源数据

GSM8K 使用固定 revision：

```bash
.venv/bin/dag-builder prepare \
  --dataset gsm8k \
  --revision 740312add88f781978c0658806c59bc2815b9866 \
  --subset main \
  --split test \
  --count 1319 \
  --root outputs/source/gsm8k-main-test
```

MMLU 使用固定 revision `c30699e8356da336a370243923dbaf21066bb9fe`。每个子集使用独立目录；`--count` 设置为该子集需要冻结的题数。例如：

```bash
.venv/bin/dag-builder prepare \
  --dataset mmlu \
  --revision c30699e8356da336a370243923dbaf21066bb9fe \
  --subset abstract_algebra \
  --split test \
  --count 100 \
  --root outputs/source/abstract_algebra
```

若数据已通过 Hugging Face mirror 下载，可增加 `--source-parquet /absolute/path/to/file.parquet`。构建器会保留原始字节、SHA256、revision、subset、split 和确定性选样记录。

## 5. Pilot 与正式构建

先使用 smoke 或 pilot 配置验证服务、schema 和 prompt，再运行 full 配置：

```bash
.venv/bin/dag-builder probe \
  --config configs/gsm8k-v2-smoke.json \
  --key-file .secrets/judge_api_key

.venv/bin/dag-builder run \
  --root outputs/source/gsm8k-main-test \
  --config configs/gsm8k-v2-pilot-10.json \
  --key-file .secrets/judge_api_key \
  --workers 4 \
  --resilient
```

正式运行使用：

- GSM8K：`configs/gsm8k-v2-full.json`
- MMLU：`configs/mmlu-thinking-v2-full.json`
- `--workers`：仅覆盖运行时并发，不改变冻结配置和请求 payload，允许范围为 1–32。
- `--resilient`：只对传输、限流等瞬态失败做有界恢复。
- `--isolate-uncertain-failures`：隔离不确定项，避免单题阻塞全队列。
- `--retry-safe-failures`：显式允许重试已经修复凭据后的 authentication failure。

实际批量脚本：

- `data/run-dag-v2-sequential.zsh`：GSM8K 和各 MMLU 子集顺序运行。
- `data/run-dag-v2-three-lanes.zsh`：GSM8K、MMLU 数学、MMLU 心理与社会三条 lane 并行。
- `data/run-mmlu-math-then-psych-12.zsh`：MMLU 九个子集按顺序、每个子集内部 12 并发。
- `data/run-mmlu-retry-29.zsh`：对最终 29 条瞬态失败建立独立重试快照。

所有控制日志写入 `data/logs/`，数据与日志不进入 Git。

## 6. 运行状态与失败处理

```bash
.venv/bin/dag-builder status --root <run-root>
.venv/bin/dag-builder report --root <run-root>
```

处理规则：

1. `model_accepted` 才能进入 unified 导出。
2. 答案不一致、语义审核失败和 schema 失败不自动重采样为通过。
3. 已返回但结果不确定的请求不假设为免费调用，也不直接重复发送。
4. resume 会恢复历史请求的实际 token 使用量，并继续执行全局预算约束。
5. 重试批次使用独立目录，最终导出时同一 `item_id` 只能来自一个快照。

## 7. DAG 验收门槛

每条接受记录至少满足：

- 节点 ID 从 1 连续递增。
- `parents` 只引用更早节点，无重复、未知、自环或未来节点。
- 不允许传递冗余的直接依赖边。
- 恰有一个 `answer` 节点，并且位于最后。
- 所有节点都位于通向最终答案的祖先路径上。
- `source_quote` 必须是声明来源的逐字子串。
- DAG SHA256 与 `result.json` 一致。
- MMLU 标准答案和数据集 gold label 一致。

MMLU v2 prompt 进一步要求终端 answer 节点引用对应的 `choice_A`–`choice_D`。早期运行中有部分已接受记录仍引用 `solution`；它们能通过当前 unified v1 结构校验，但如需严格执行该 prompt 约束，应先做确定性 choice-source 修复并重算 DAG 哈希。

## 8. 导出为 PALS unified v1

导出器只读取 `model_accepted` 且 DAG 哈希一致的记录，并生成：

- `accepted_source.jsonl`
- `pals_dag_unified_v1.jsonl`
- `pals_dag_unified_v1.html`
- `manifest.json`

GSM8K 示例：

```bash
PYTHONPATH=src python3 scripts/export_local_pals_dag_unified_v1.py \
  --benchmark gsm8k \
  --root outputs/dag-v2-resume/gsm8k \
  --output-dir outputs/pals-dag-unified-v1/gsm8k-final
```

MMLU 可通过重复 `--root` 合并目标子集：

```bash
PYTHONPATH=src python3 scripts/export_local_pals_dag_unified_v1.py \
  --benchmark mmlu \
  --root outputs/dag-v2-resume/abstract_algebra \
  --root outputs/dag-v2-resume/college_mathematics \
  --output-dir outputs/pals-dag-unified-v1/mmlu-math-final
```

输出目录必须不存在，导出器不会覆盖既有冻结交付。`manifest.json` 记录条数、子集分布、源文件 SHA256、unified JSONL SHA256 和 HTML SHA256。

## 9. 交付前验证

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

交付文件还应逐行调用 `validate_row()`，并核对：

- JSONL 行数等于 manifest `records`。
- `item_id` 全局唯一。
- 子集计数等于 `records_by_subset`。
- JSONL 和 HTML 的 SHA256 等于 manifest。
- HTML 中的 rows 与 JSONL 逐对象一致。

本分支提交前的离线测试基线为 195/195 通过。

## 10. Git 保存边界

提交：

- `src/dag_builder/` 中的构建、验证和 unified 转换代码。
- `configs/` 中的 v2 配置。
- `src/dag_builder/prompts/` 中的版本化 prompt。
- `scripts/` 和 `data/run-*.zsh` 中的可复现运行脚本。
- `schemas/`、`tests/` 和 `docs/`。

不提交：

- `.secrets/`、`.env*` 和任何 API key。
- `data/gsm8k/`、`data/mmlu/`、`data/logs/`。
- `outputs/`、模型响应、缓存和交付压缩包。
- 本地第三方工作目录 `cp-tools/`。
