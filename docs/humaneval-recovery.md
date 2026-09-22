# HumanEval：提高质量与可追溯回捞

目标是减少错误接收和格式误杀，不是提高任意通过率，更不是制造适合 PALS 的图。v1/v2/v3 产物保留不动，新增 `humaneval-reference-v4`。PALS 公式、重排算子、选点、评分温度均不变。

## 1. 新构图的质量改进

- **来源别名无损归一化**：仅把 `solution.rationale` / `rationale` 转为 `solution`，`question.question` 转为 `question`；不改 statement、quote、节点种类、节点 ID 或边。归一化后依然逐字检查引用，保留 raw response 和 `normalization.json` 的前后哈希。GPQA 和旧 HumanEval 协议不受影响。
- **避免位置引用**：新正文使用具体事实、变量与条件，不用“step 6 / node 3 / 第 3 步”。代码会拦截数字式步骤/节点引用，审核模型同时检查其他含混指代。它不是自动把旧编号改成新编号，更不是删除逻辑内容。
- **知识根不能夹带待证结论**：精确整数加法是通用事实，“当前 balance 等于前缀和”则需要初始化和更新证明。新增解释与 DAG 审核字段分别检查根前提、代码语义、正文自包含和未偷用不变量。所有字段必须明确通过才能接受，不能靠额外布尔字段本身声称语义已获证明。
- **不制造图结构**：保留实质推导粒度和条件范围，不强制步数或分叉，不为合法重排的样本量补边。API 仍是同一模型生成和审核，输出仍是模型审核候选，不是人工认证 gold。

六阶段数量不增加。新构图可用 v4 配置调用普通 `run`；旧构图运行不能原地更换协议。

## 2. 先离线分流，不发 API 请求

```bash
dag-builder audit-humaneval-quality \
  --source-root /absolute/private/path/humaneval-construction-v4 \
  --output /absolute/private/path/humaneval-quality-audit
```

| 路线 | 处理方式 |
|---|---|
| `lossless_format` | 首次失败为原子化；别名转换后通过全部旧结构门槛，且无数字式位置引用。可以复用原解释与节点内容 |
| `semantic_revision` | 引用、JSON、图结构、正文指代或 DAG 语义存在问题；不能当作格式问题直接接受 |
| `manual_source_review` | 解释阶段已拒绝，或有绑定原产物的质量否决。先复核参考代码/题意，不自动重新包装成证明 |
| `transport_review` | 没有科学终态。单独处理请求状态/续跑，不算内容错误 |
| `not_started` | 没有任何请求记录，不把尚未执行的题目算成接口失败或已拒绝 |
| `retain_candidate_with_audit` | 保留原模型接受结论，同时输出正文引用风险；不擅自升级为人工放行 |

`quality-audit.json` 保留每题原状态、失败原因和路线。不以 PALS 分数、合法重排适用性或图大小筛选。当前离线工具针对已停止的首轮 HumanEval v1/v2/v3，拒绝正在写入的批次和嵌套修订。

## 3. 默认只准备无损格式回捞

```bash
dag-builder prepare-humaneval-recovery \
  --source-root /absolute/private/path/humaneval-construction-v4 \
  --root /absolute/private/path/humaneval-recovery-format-v1
```

此命令只做本地快照，API 调用数为 0；不是回捞成功。新目录保存：

- `originals/`：原批次清单、配置、completion 和选中题目的原始请求、响应、审核结果；文件哈希固定。
- `items/<id>/recovery_seed.json`：原解释、失败诊断和无损归一化候选。
- `recovery_manifest.json`：原批次分母、所有题目的分流记录、历史请求/预留用量、新队列与种子哈希。
- 新请求仅写新目录；不会把旧失败改成成功，也不伪造复用阶段的新 API 响应。

默认路线复用 solve 和 atomize 的内容，但 **重新进行解释审核、依赖构造、justification、DAG 审核**，共 4 次基础请求/题。原解释或节点若有真实问题，依然拒绝。复用记录在 `seeded_stages/`；它不是新的模型响应，因此普通阶段 output 数不能直接当总完成阶段数。

## 4. 付费执行与有界语义修订

以下命令会产生 API 费用，须确认端点、凭据和**新增**预算后执行：

```bash
dag-builder recover-humaneval \
  --root /absolute/private/path/humaneval-recovery-format-v1 \
  --config configs/humaneval-recovery-v4.json \
  --key-file /absolute/private/path/api-key \
  --resilient
```

配置示例是容量上限，不是费用预测：最多 256 次请求、1600 万保守预留 token；可按队列缩小。旧请求成本仍在 manifest 保留，新 run 不等于历史费用清零。缺失 usage 不是零费用；服务端实际计费不由客户端硬保证。

如批准处理语义/结构问题，使用**另一个新目录**并在 prepare 命令中增加 `--include-semantic`。它固定选入默认格式路线与全部可修订语义路线，不按结果挑题；原解释审核失败和质量否决仍需单独处理。语义路线把原诊断和原解释作为数据提供，最多做一次诊断驱动的新解释，随后走六阶段完整审核。不得修参考代码、补截断 JSON、伪造引用或循环生成直到通过。无论好坏都保留这一轮结果。嵌套回捞默认拒绝；新的研究协议需单独设计，而不是绕过轮数限制。

相同命令断点恢复复用已保存响应，不新增语义候选。传输重试至多四次阶段尝试，仍受新 run 的全局预算约束。已完成阶段不会再付费采样。

## 5. 导出与正式评分

```bash
dag-builder export-humaneval-validation \
  --root /absolute/private/path/humaneval-recovery-format-v1
```

新队列全部达到科学终态后才导出；输出明确只属于 recovery cohort，不替换原 163 题批次。manifest 记录原分母、排除原因、历史成本和修订来源；每条 DAG 绑定 recovery provenance。不要把原 55 道加上新候选后不记录版本就称为“一次性通过率”。合并正式数据集、人工放行及 B1 提交另行执行。

PALS 独立适配器支持 v4，并检查新增审核字段和正文位置引用。不导入 dag_builder 依赖，不把参考答案、测试、审核文字或修订诊断送进推理。模型概率计算与 GPQA 处理不变。

## 6. 已知边界

自动检查能确认结构、引用和来源一致性，不能证明自然语言逻辑正确；同模型重新审核也非独立验证。回捞提高的是找回合格样本的机会，实际新增接受数量必须等真实运行。新数据仍可能没有可合法交换的子树；这应如实进入实验适用性清单，而不是成为构图优化目标。

## 7. 后续：全量诊断式修订

首轮 15 题无损回捞结束后，针对仍未通过的可修订候选，使用新的 [诊断式全量回捞协议](humaneval-diagnosed-repair.md)。它允许把首轮构图与一次**无损格式**回捞叠加为来源，再做最多一次语义修订；不允许接收任何已有语义修订批次作为嵌套输入。旧命令行为保持不变。
