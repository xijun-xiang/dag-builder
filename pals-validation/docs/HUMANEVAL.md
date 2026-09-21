# HumanEval validation adapter

本子项目仍可单独复制/安装，不导入父仓库 dag_builder。输入必须是其 `humaneval_validation_export_v1` 导出；官方 HumanEval 原始 JSONL 没有推理 DAG，不能直接评分。

```bash
pals-validation prepare --benchmark humaneval --source /absolute/path/model_accepted.jsonl --output /absolute/path/new-prepared
```

之后复用 README 中 `init / worker / analyze`，数学定义不变。GPQA 默认为 `--benchmark gpqa`，不跨基准混用源文件。构图教程位于完整仓库的 `docs/humaneval-validation.md`；PALS 本身不调用构图 API、不执行代码。

HumanEval prompt 保留原函数签名、缩进、docstring 和题面示例；不附带选项。只从已审核记录取自然语言步骤正文与 parents，去掉终端代码答案、引用、justification、测试及其他源字段。E2 只提供哈希选中目标的祖先，生成自然语言下一步，不把完整代码或每行代码当 step。

只有一个推理节点的题保留为不适用，不伪造更多步骤；所有 E1/E2 适用数都从 manifest/inventory 读取，不能预先写成 164。无效续写保留，不能填零；N/D 主比较仍用全部温度完整 repeats 的共同题集。

输入 DAG 的哈希、模型审核和结构检查不证明其语义正确。当前通过离线合成管线测试，**HumanEval 真实模型 canary 尚未执行**；GPQA 的通过记录不能替代新数据与新提示的验收。

升级此代码后，旧 run 的 source hash 将不匹配；旧 GPQA run 仍用原冻结代码验收/续跑，不修改旧 run 来绕过校验。新的 GPQA 预处理在相同输入下保持原 manifest 和内容哈希。
