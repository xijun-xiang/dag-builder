# LiveCodeBench t2ance 答案反向最小图 v3：代码与小样本方案

## 开发证据与目的

独立 CPU 回归的固定七题在 v2 构图中为 0/7 模型接受：三份依赖图有传递冗余直边（其中两份还有未通向答案的节点），三份缺少已声明的推导前提，一份引用了不存在的来源单元。原始 v1/v2 响应、失败和预算账本不能覆盖或改成“修复成功”。本版本只测试能否改善构图流程，沿用同题是**开发诊断**，不是独立 validation。

`main` 的最小答案链和来源绑定是有用的启发，但不能直接追溯性施加到旧结果：当前 interim 38 份旧协议模型接受图中，按新的“直父无祖先冗余”规则有 37 份含冗余边（CALIBRI 36/37、t2ance 1/1）。这说明旧图与新规则不在同一协议下，不能据此自动判旧结果无效，也不能给旧图静默约简后宣称语义等价。

## v3 的有界修改

1. 规范化提示要求从最终算法与必要正确性条件向后选择节点，先列题面给定的操作性前提，再列推导；来源单位必须逐字绑定，不能凭程序本身证明程序正确。
2. 依赖响应仍覆盖所有原节点。程序只保留**模型声明的答案祖先**，保留被排除节点的原文、来源引用及原解释。程序仅删除“父节点已是另一直接父节点的祖先”这种可证明传递冗余的直边，记录每条删除及其经由节点；不新造节点或依赖边，不改写任何论断或来源。
3. 机械门槛要求保留图有题面 `given` 根、每个保留的推导节点有前提、答案非空且全图连通。语义门槛由新的 `review_dag` 请求重新检查所有排除节点、被约简边、题面根、代码反例和边界条件。三个新增审核项必须全为真才可记为“模型接受候选”。结构修整**不保证语义正确**；同模型审核也不是人工/官方 gold。
4. 已知 `714ad0b7be6566b3aec4` 参考程序存在独立反例，必须用私有的 `t2ance-semantic-quarantine-v1` 清单排除出新 canary；清单记录冻结代码哈希、反例输入、预期与程序实际输出，作为 manifest 的哈希证据。固定测试 43/43 通过不解除隔离。

代码仍将 `formal_eligible=false`。私有题目、原始回复、测试和反例不进入 Git。任何失败仍留在流转分母；不得把 v3 选择或约简当成一次“人工修补”。

## 若执行下一轮小 canary

只对原七题中**排除已知错误参考后最多六题**开展新版本开发回归，不扩大到未验的 53 题。先在私有目录准备包含反例的隔离清单，再以新目录调用 `dag_builder.t2ance_pipeline` 的 `prepare`，指定 `--prompt-version t2ance-lcb-normalize-v3 --semantic-quarantine <私有清单路径> --limit 6` 和历史 t2ance 已用预算。随后冻结代码与配置、做离线审计。六题理论上最多 18 个正常阶段调用；应为服务传输预留有界余量，但不得超出已批准的总调用/token 上限。若出现系统性格式失败、程序语义反例或新增审核无法支持，停在小样本，不扩到剩余候选。

固定配置文件为 `configs/livecodebench-t2ance-v3-canary6.json`，只把 v2 七题 canary 的提示协议切换到 v3；`workers=2`、24 次请求和 200 万 token 等控制不变。配置不含 API 密钥。

本设计阶段未启动 API，也没有连接 B1；后续执行结果另记如下。执行前须核对历史累计请求与 token、隔离清单、私有目录权限、全链路回放和新版代码快照。即使六题出现少量接受，也只是在反复查看的开发样本上产生的新候选；后续扩大 CPU 复验和独立样本审核之后，才可以估计可用覆盖率。

## 2026-09-25 开发 canary 结果

按上述冻结方案运行六题，得到三条模型审核接受、两条规范化格式失败、一条模型审核拒绝；共 14 次请求、1,114,066 保守预留 token。`t2ance_audit` 从原始响应离线重放通过，但这只说明协议与产物一致。两条格式失败都漏写必需的 `normalization_note`，旧校验错误文字未准确指出“字段缺失”；拒绝案的审核文字混淆了重编号 ID 与原图 ID，不能把全部批评视为事实。三条模型接受中，独立只读审阅发现一条明确表述错误、一条必要证明缺口；第三条适合作为人工复核候选，但有成本函数提前返回的文字精度问题。完整逐题证据保存在私有运行目录，不进入 Git。

结论：v3 相比 v2 的 0/7 在同一开发样本上改善了**模型审核候选数**，但尚不能证明高质量图覆盖率，更不能据此对未 CPU 复验的 53 题放大。同模型 review 的接受不等于独立语义通过。下一版若继续探索，需先修复规范化字段提示/错误定位和审核的原始 ID—重编号映射说明；旧运行不得改写，且应在未见过的 CPU 复验样本上验证收益。

已审计的 v1/v2 累计为 10+13=23 次请求、782,498+912,951=1,695,449 个保守计账 token。若历史账本在执行时仍是这两批，v3 可预分配最多 **24 次新请求、200 万 token**（六题正常流程需要 18 次），总计上限变为 47 次、3,695,449 token，低于 t2ance 项目既有 240 次/1600 万上限。代码准备命令如下；清单路径必须在私有目录中，示例并不执行：

```sh
PYTHONPATH=src python -m dag_builder.t2ance_pipeline \
  --source /Users/xxj/Desktop/infi-evalscope/artifacts/pals-livecodebench-v6-20260922/t2ance-source-v1/selected \
  --execution /Users/xxj/Desktop/infi-evalscope/artifacts/pals-livecodebench-v6-20260922/t2ance-cpu-canary7-b1-111733-v1/execution \
  --expected-manifest /Users/xxj/Desktop/infi-evalscope/artifacts/pals-livecodebench-v6-20260922/t2ance-cpu-canary7-b1-111733-v1/execution/input-manifest.json \
  --root /Users/xxj/Desktop/infi-evalscope/artifacts/pals-livecodebench-v6-20260922/t2ance-answer-backward-v3-canary6 \
  --prompt-version t2ance-lcb-normalize-v3 --semantic-quarantine <私有清单路径> \
  --limit 6 --prior-calls 23 --prior-reserved-tokens 1695449 \
  --max-calls 24 --max-reserved-tokens 2000000
```

隔离清单格式为 `{"protocol":"t2ance-semantic-quarantine-v1","records":[{"item_id":"...","reference_code_sha256":"...","counterexample":{"input":"...","expected":"...","observed":"..."},"reason":"..."}]}`。程序校验题目在冻结来源内、代码哈希吻合且独立 CPU 通过，再排除该题；不接受没有反例记录的静默人工删题。准备时若六题之外没有可用题，或历史额度变动，先重新核对而非修改旧 run。
