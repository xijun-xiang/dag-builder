# E3：过程与答案解耦，已有轨迹派生分析 v1

## 为什么修改

原 `strict-tag-v1` 把推理步骤、答案封装和整段输出合规性共同记为
`process_valid`。因此，步骤完整但最终代码使用围栏、或正常 EOS 前没有
闭合 `</answer>`，也会丢失过程分数。这不是改变 g 定义的理由。

新增 `process-answer-separated-v1`，**不修改旧解析器、旧 raw、旧配置或旧得分**。
它只解决结构可识别性，不认证逻辑正确或模型完成了充分推理。

## 三个独立判断

1. 过程可评分：从输出开头（容许空白）连续出现非空、闭合且无嵌套的
   `<step>` 块，随后有明确答案边界，且生成正常结束。
2. 答案可提取：存在唯一可提取的原文区间。这不等于程序能运行或答案正确。
3. 答案正确性：仍交给原有受限评测器。本次似然派生作业不执行候选代码，
   正确性保持未评测；不能把未评测或不可提取填成错误或零分。

统一的确定性边界规则：

- `<answer>` 明确标记推理结束；一个闭合答案块按原文提取。
- 连续步骤之后只有一个 `<answer>` 起始标签且正常 EOS，允许答案延伸到
  原文 EOF，标记 `answer_open_to_eos`，不追加结束标签，不认证语义完整性。
- 仅代码任务允许连续步骤后直接出现单个完整的 Python/py/无语言围栏；
  围栏前只允许可选的单行 `answer:` 或 `final answer:`（大小写不敏感）。
  围栏后不得有非空内容。
- 答案块内部恰好一个完整围栏时，只取围栏内部的连续原文区间；不去缩进、
  改函数名、补代码或执行代码。多重/不完整围栏使答案不可提取，但已有明确
  `<answer>` 边界的完整步骤仍可评分。
- 未标注的开头或步骤间推理、嵌套/缺失步骤标签、尾部新的步骤、多个答案块、
  含额外文字且边界不明确的尾部、`length`/异常结束均不自动恢复。
- 非代码任务不新增自由文本答案的猜测规则。不按模型、题号、正确性或 g 值
  选择是否恢复。未恢复样本仍保留完整分母。

目标步骤的 token、原始提示和上下文都不变。对原先有效的样本逐字核对
`full_context / deleted_context / target` 与旧规则相同；最终答案不参与 g。
G/M/W、K=0/1 的 N/A 规则不变，W 仍是轨迹内部步骤间总体标准差。

## 入口和证据

离线结构重放（不加载模型、不生成、不执行代码）：

```bash
python -m pals_validation.e3.reparse --source-manifest INPUT.json --out NEW_REPLAY
```

输入是完整 canary run 清单，不能只列失败题：

```json
{"runs": [{"label": "source-v1", "path": "/absolute/source/run", "remote_source": null}]}
```

重放校验旧协议、配置、公开题目输入、五组批次、全部 attempts/raw、八个生成
worker 终态和旧解析结果。它是 **generation-only 投影审计**，不冒充完整运行审计；
不需要复制隐藏测试。保存每条新旧状态、原文区间、原始提示下的完整评分请求、
源文件与派生产物 SHA256。`items.json` 中尚未计算的 G/M/W 均为 null。

模型似然重放（固定八 shard，仅 score，无 generate/evaluate 接口）：

```bash
python -m pals_validation.e3.derived_score score \
  --replay NEW_REPLAY --source-run ORIGINAL_RUN --run-label source-v1 \
  --out NEW_SCORES --shard 0
python -m pals_validation.e3.derived_score audit \
  --replay NEW_REPLAY --source-run ORIGINAL_RUN --run-label source-v1 --out NEW_SCORES
```

真实评分只能在 PALS 内的 Slurm allocation 运行。脚本
`b1-e3-derived-score.sbatch` 固定八卡、32 CPU、256 GiB、1 小时上限；
源 run、代码、模型和派生请求只读挂载，新目录独立写入。
`e3_launch_derived.py` 等八个评分 worker 完成后，重分词、逐 token 重算 g
和 G/M/W。任何失败保留产物，不自动重试。已有旧分数的样本同时做差值核对。

似然内核与原 run 必须哈希相同。唯一已审阅的兼容例外是 b384ae4 → f60618c
的 `e3/backend.py`：只有 `base_prompt` 显式传入旧配置中的提示版本，
`score_pair` 完全未变；两份精确文件 SHA256 在代码中限定，且实际提示逐条
与生成时保存的原始提示比较。不接受泛化的“忽略代码哈希”开关。

## 2026-09-30 开发性结构复核

三批全部 120 条旧输出统一重放，没有重新生成：

| 批次 | 旧严格有效 | 新过程结构有效 | 新增恢复 | G/M/W 可定义候选 |
|---|---:|---:|---:|---:|
| Qwen2.5 V1 | 40/40 | 40/40 | 0 | 39/40 |
| Phi V1 | 18/40 | 24/40 | 6 | 24/40 |
| Phi V2 | 33/40 | 40/40 | 7 | 40/40 |

104 条过程有效、103 条可计算 G/M/W，共 496 个相邻删除配对；Qwen2.5 一条
只有一个步骤，K=0。Phi V1 的 16 条仍无效，不强行恢复。
这些数字是格式/覆盖率结果，不是似然实验的新分数或准确率。
V1/V2 提示及题目不同，分开报告，不合并当作相同协议的独立正式样本。
