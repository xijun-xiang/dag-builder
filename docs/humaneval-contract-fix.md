# HumanEval 接口契约修复：reference-v5 / diagnosed-repair-v2

## 修复什么

完成的 diagnosed-repair-v1 批次暴露了两处接口不一致，不应将这些失败都解释成题目或轨迹质量差。

1. **代码事实引用。** 旧原子化提示词允许描述直接程序操作，却把所有非答案 `reference_code` 引用当成答案泄漏。v5 明确允许 `given` 节点用自然语言描述参考程序的直接操作，引用必须逐字来自代码，且节点必须为根。`derived`、`knowledge` 不可引用代码；完整代码仍仅在最终 answer 节点。PALS 接口只保留自然语言步骤，移除答案代码、引文、论证、测试。
2. **旧边和旧论证的诊断引证。** 旧请求提供了 `prior_dependencies`、`prior_justifications`，却未将它们放进 `evidence_sources`。v2 将同一内容用确定的 JSON 序列化加入来源白名单；不改图、不模糊匹配、不放行未知来源。

代码文本可直接证明“执行了哪项赋值/返回操作”，不能单凭引文证明不变量或最终正确性。新增 `code_facts_grounded` DAG 审核项必须为 true；同模型审核仍可能失误，不等于人工 gold。依赖、连通性、来源逐字校验和两次语义审核保持必要。缺陷不通过改名为 `given` 来规避。

## 版本边界

| 机制 | 构图协议 | 保留行为 |
|---|---|---|
| diagnosed-repair-v1 | reference-v4 | 旧提示词/诊断输入、旧来源限制保留 |
| diagnosed-repair-v2 | reference-v5 | 新代码事实约定、诊断引证来源、新审核项 |

v5 原子化和 DAG 审核使用新提示词；其余四阶段显式复用冻结 v4 提示词。配置、准备清单和运行机制必须配对，否则拒绝。已有成功候选不会自动升级为 v5。

## 离线复核，不等于续跑或通过

```bash
dag-builder recheck-humaneval-contracts \
  --source-root /absolute/completed-diagnosed-v1 \
  --output /absolute/separate-contract-audit
```

该命令只覆盖终态为 needs_review 的两类已知接口错误。它核对种子、请求/输入绑定、原始正式 content、API 契约和旧失败能否复现，再对同一输出施加新本地校验。只做已有固定白名单的来源别名规范化，不更改任何 statement、kind、quote 或边，不使用 reasoning_content，不补 JSON，不执行参考程序。

输出使用 `contract_recheck_passed` 或 `still_invalid`，从不写 `model_accepted`。原批次所有文件在复核前后做哈希核对；报告、候选及文件清单存入新私有目录。代码事实或诊断通过局部校验后，尚未生成的边、论证及必要的新语义审核仍须完成；当前命令没有付费续跑能力。不得把这些离线候选直接合并入正式数据集。

## 验证与复现

- 主仓库：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests`
- 独立 PALS 子项目：进入 `pals-validation` 后运行同一命令，无需安装 dag_builder。
- 覆盖旧协议不变、伪造引文、错误节点分类、代码答案隔离、新审核缺失、版本错配、端到端构图/导出、离线不调用网络且不改原结果。

本修复不改 g/M/N/D、E1/E2 算子、推理参数或 GPQA 数据；不自行发起 API 请求、连接集群、提交 GPU 或发布远端。

## 获授权后的定向续跑

`humaneval-contract-continuation-v1` 只接受上述两类已完成离线复核的失败，从旧 diagnosed-v1 新建独立任务，不允许再叠加一轮语义回捞。准备时再次核验完整父目录哈希，并复制来源证据。所有通过复核的目标都进入清单，不凭 PALS 结果挑选。

```bash
dag-builder prepare-humaneval-continuation \
  --source-root /absolute/completed-diagnosed-v1 \
  --recheck-root /absolute/contract-audit \
  --root /absolute/new-continuation
```

诊断不重做；已有效的解释、解释审核和原子化节点保留，记录在 `seeded_stages/`，不伪造成新响应。解释审核提示词在 v4/v5 完全一致，复用前校验它审核的是同一解释与题目。最终 DAG 审核必须用 v5 新执行，包括 `code_facts_grounded`。缺失的生成阶段至多生成一份语义响应，传输重试沿用每阶段最多四次的规则；新语义拒绝后停止。

导出明确区分解释审核来自旧响应还是新请求，并把最终图逐字段绑定到复用节点、新边、新论证和新审核。不把 `both_reviews_rerun` 错写为 true。配置的端点、模型、思考设置、输出上限和响应校验政策必须与原任务一致；仅改协议版本和本批次资源上限。费用记录保留历史成本，新预算不是历史费用返还。
