# CALIBRI 回修 v2：前提清单与删除影响检查

## 开发依据

v1回修三题：一题接受，两题结构通过却语义拒绝。失败分别为：
复杂度可行性遗漏题面规模上界；删除代码行为节点后，实际打印结论失去前提。
原记录不覆盖。用户确认继续后，以 `calibri-lcb-repair-v2` 对原来两道语义拒绝题
做一次新的开发验收；已接受两题不重跑。不是独立验证数据，也不是把v1重试到成功。

## 实质改动

仍为三个调用阶段：回修提议、依赖、完整审核，没有额外多轮judge链。
题目、程序、原CALIBRI输出、保留节点原句和类型不变；derived节点相对顺序不变。

1. 每个保留derived节点列出必要旧前提、新增前提、使用的题面条件及基本背景假设。
   Python检查覆盖完整、编号有效、前提未被删除、前提出现在结论之前；
   所列题面条件必须有对应的显式given，不得只在derived来源引文里出现。
2. 每个删除节点必须给出覆盖所有保留结论的影响检查。发现还有结论需要它就不能删。
   原图没有边不代表该事实不被需要；“事实尚未连接”不是冗余的定义。
3. 允许将已有given/knowledge前提原文前移，不能移动derived结论。
   例如输出行为事实在后、打印结论在前时，仅保留原相对顺序无法建立早到晚依赖。
   这是来源约束参考图的归一化，不声称是模型原始生成顺序。
4. 重建图后检查清单中的每条必要前提真的位于对应结论的祖先路径。
   不要求全部成为直接父节点，避免为通过检查制造冗余直接边。
5. 新上下文审核继续独立核对完整原题和代码，不因为清单声称“完整”就接受。
   新增审核项：清单完整性、删除影响、根前提前移是否合理。

机械检查不能证明模型没有漏列前提，也不能证明给定引文在语义上支持所写命题；
这些仍需语义复核。普通背景假设不能用来掩盖原题条件。没有放宽旧版语义标准。

## 运行与留痕

v1提示与默认行为保持兼容；v2使用独立提示、配置、输出目录和开发轮次字段。
准备v2时需要原normalize-v2与已完成的repair-v1，自动选择仍被语义拒绝的全部题，
从原节点开始重做本次修改，旧轮审核作为可核验的开发反馈而非指令。
原/新编号、添加、删除、移动、逐结论前提清单、删除影响表、请求与响应全部保存。
v2不能再次作为自己的history启动无限修复链。

两题配置worker=2，最多6请求、120万保守预留token，输出上限32768；
历史33请求、2059510预留token计入全量600请求/2500万预留token上限。
不发送隐藏测试，不执行参考程序，不连接B1，不启动PALS GPU评测。

```bash
PYTHONPATH=src python -m dag_builder.calibri_repair \
  --parent /absolute/private/calibri-normalize4-v2 \
  --history /absolute/private/calibri-repair3-v1 \
  --root /absolute/private/calibri-repair2-v2 \
  --prompt-version calibri-lcb-repair-v2 --max-calls 6

PYTHONPATH=src python scripts/run_private_pilot.py \
  --root /absolute/private/calibri-repair2-v2 \
  --config configs/livecodebench-calibri-repair-checked-canary.json \
  --key-file /absolute/private/existing-api-key

PYTHONPATH=src python scripts/audit_calibri_repair.py \
  --root /absolute/private/calibri-repair2-v2
```

正式输出仍 `formal_eligible=false`；通过模型审核不自动发布。
两题用于判断这次工程改动是否解决已知问题，不能估计未见数据上的总体产出率。
下一次扩量前需以固定协议处理新候选；不能悄悄重复已接受题或把修复代数漏记。

## 本轮运行与输出预算补审

v2主体：5请求、216635报告token、517986预留token，运行完整结束，离线重放通过。
3759补齐已知前提，但仍保留未通向答案的边界/复杂度分支，结构拒收。
abc398_c前提、输出规则前移及依赖路径已通过；最终审核用完32768输出token且
全部用于reasoning，正式content为空、finish_reason=length，没有语义裁决。

`calibri_review_resume.py` 对后者支持一次固定图补审，输出预算65536，
最多1请求/20万预留token。与主体合计最多6请求，旧消耗计入总额。
只有empty content + length符合条件；语义reject或结构失败不通过此入口重试。
候选图逐字段重建核对后冻结，仅补最终审核；不解析reasoning作为结论、不修复截断JSON。
补审通过也不是独立验证，`formal_eligible=false`仍保持。

```bash
PYTHONPATH=src python -m dag_builder.calibri_review_resume \
  --parent /absolute/private/calibri-repair2-v2 \
  --root /absolute/private/calibri-repair2-v2-review64k

PYTHONPATH=src python scripts/run_private_pilot.py \
  --root /absolute/private/calibri-repair2-v2-review64k \
  --config /absolute/private/calibri-repair2-v2-review64k/config.json \
  --key-file /absolute/private/existing-api-key

PYTHONPATH=src python -m dag_builder.calibri_review_resume \
  --root /absolute/private/calibri-repair2-v2-review64k --audit
```

### 补审验收结果

固定图补审以stop完整结束，正式content返回accept，issues为空；只解析content。
1请求、54267报告token、157114预留token。主体与补审合计6请求、270902报告token、
675100预留token；历史累计39请求、2734610预留token，均在600请求/2500万总额内。
离线重放确认输入图未变，原响应、解析审核、结果与预算对应一致。

四题开发集按题号去重为3接受、1保留失败：abc396_a在normalize-v2接受，
3709在repair-v1接受，abc398_c在repair-v2固定图补审接受；3759仍结构未闭合。
两类I/O均有接受实例；该3/4不是未见数据上的首轮成功率。
不为将3759纳入而补虚假边；该问题可排除，辅助分支表示仍需记录为失败类型。
接受集仍为同模型审核的候选，非独立人工gold，不自动改变formal_eligible。

最新363项构图库测试、63项PALS测试通过。93份来源候选尚未完成全量CPU复验或构图。
本v2准备入口绑定旧v1开发历史；将其用于新题首次回修需要单独的生产入口和测试，
不能把本开发入口直接当作93题完整生产工作流。
