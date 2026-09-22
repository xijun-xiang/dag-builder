# HumanEval：诊断式全量回捞

适用问题：格式修正后，仍可能存在缺失前提、错误依赖、错误不变量或无依据的输入域限制。不能把这些都判成源题无效，也不能不加区别地重写所有解释。

## 固定机制

新建任务推荐使用 `humaneval-diagnosed-repair-v2` 搭配 `humaneval-reference-v5`。
旧 v1/v4 的提示词与校验行为保留，不允许用新配置原地续写旧批次。
Python/CLI 的默认 prepare 协议仍是 v1，以避免悄悄改变既有脚本；新任务必须显式传入 `--protocol humaneval-diagnosed-repair-v2`。
接口修复范围、离线复核及语义放行边界见 [接口契约修复](humaneval-contract-fix.md)。

每个未通过候选先接受一次带原文引证的诊断，输出三选一：

| 路线 | 条件 | 动作 |
|---|---|---|
| graph_repair | 原解释充分正确，问题在原子化、引用、节点分类或边 | 原解释逐字保留；重新原子化、构边、论证；两轮审核重做 |
| rationale_revision | 题目与代码可兼容，错误在解释本身 | 根据具体诊断只生成一次新解释；重新完成六阶段 |
| source_concern | 参考代码有实质反例，或题意存在无法消解的重大歧义 | 隔离，不编造证明、不修改源题或参考代码 |

若“缺少的前提”在原解释中根本没有，不能仅补一条边来掩盖，应该走解释修订。graph_repair 可以重建节点，不能补入无法从冻结解释引用的证明事实。不得为增加合法重排数量而制造分叉、指定节点数，或使用 PALS 得分决定保留哪些题。

解释审核和最终 DAG 审核仅看到当前候选与题目/参考代码，不接收历史通过意见、修复理由或“应该接受”的指示。诊断也是同模型判断，可能出错；两轮审核、结构检查和逐字引文校验都不能省略。输出仍然是模型审核候选，不是官方 CoT 或人工 gold。

## 全量的含义

从原始完整构图清单逐题分流：已接受候选原样保留；有绑定证据的源题争议隔离；其余未通过/传输未完成题全部进入诊断。不按结果挑选几个容易成功的样本。原分母、既有来源排除、旧失败、新失败均进入 manifest。

可以叠加一份已结束的直接无损格式回捞目录，让新诊断看到其最新审核结论，并跳过其中已通过的题。拒绝接收已有语义修订目录或第二层修订；这一轮失败后停止，不自动再生成候选。

```bash
# 离线准备，不调用 API。输出必须使用新私有目录。
dag-builder prepare-humaneval-repair \
  --protocol humaneval-diagnosed-repair-v2 \
  --source-root /absolute/first-pass \
  --format-root /absolute/completed-lossless-recovery \
  --quarantines /absolute/source-quarantines.json \
  --root /absolute/diagnosed-repair

# 获授权后调用 API；同命令恢复会读取已有结果，不生成第二个语义候选。
dag-builder recover-humaneval \
  --root /absolute/diagnosed-repair \
  --config configs/humaneval-diagnosed-repair-v2.json \
  --key-file /absolute/private-key-file --resilient
```

`--format-root` 和 `--quarantines` 均可省略。隔离文件是一组包含 task_id、reason、evidence_result_sha256 的对象；哈希绑定该题最新的 result.json，未知题、重复题或不匹配证据均拒绝。

graph_repair 无故障时每题 6 次请求（含诊断），rationale_revision 为 7 次，source_concern 为 1 次。每阶段传输尝试最多 4 次；任何已返回的语义响应不会为了提高通过率重采样。代理把错误文本放进 content 时仍会保留原文并隔离，不修补 JSON，不用 reasoning_content 兜底。

示例配置使用 32 workers、thinking/high、32768 输出上限；3200 次请求、2 亿保守预留 token 只是覆盖全量和有限传输重试的防失控上限，不是预计用量或价格。历史请求成本与本轮成本分开保存但不能清零。启动前应按实际题数检查预算、端点和凭据。

## 证据和导出

- originals/first_pass 和 originals/format_recovery 保存来源文件及哈希；不改父批次。
- repair_seed.json 保存最后失败结果、旧解释/图/审核；diagnose 保存新诊断及逐字证据。
- graph 路线的解释复用写 seeded_stages，不伪装成新的 solve 响应。
- DAG 绑定修订轮次、诊断哈希、种子和 manifest；导出再次核对解释与两次新审核。
- 全队列达到终态才导出。source_concern 的终态为 rejected、stage=diagnose，与生成/图审核拒绝和传输暂停区分。
- 原先通过的旧协议候选不会因被保留就自动获得新协议认证；正式合并与统一质量放行需要独立记录。

此机制不改 PALS 数学定义、E1/E2 算子或评分逻辑，不连接集群、不提交 GPU、不自动发布仓库或数据。
