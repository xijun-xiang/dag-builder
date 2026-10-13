# 当前工程与复现口径（2026-10-13）

本页更新入口，不重写历史结果或替代私有原始证据。原始仓库和各次失败、修复、冻结
运行的证据均保留。真实模型轨迹、逐 token 概率、答案判分、日志和模型权重不加入这次提交。

## 最终工程入口

| 部分 | 核心代码 | 最终边界 |
|---|---|---|
| E1 | `pals-validation/src/pals_validation/{prepare,graph,protocol,backend,analyze}.py` | 冻结 DAG、原序/合法/破坏干预、共同目标配对 |
| E2 | 同上；`recovery/e2_eos_v2.py` | 三温各八次；Llama EOS v2 与 strict-only 双口径；三温共同完整题 |
| E3 | `e3/{protocol,backend,greedy_parse,metrics,greedy_extension}.py` | 一次 greedy、Tscore=1、显式 v2 原文 span；Llama 8K 容量适配 |
| 最后调度更新 | `scripts/e2_dynamic_submit.py`、`recovery/e2_dynamic.py` | MMLU 八 worker 动态领取完整 temperature cell；不拆分或改写种子 |

以源码文件实际存在和冻结协议为准。研究探索脚本与最终正式执行脚本不混用；E3
自然编号 regex 回捞未进入此次匿名复现口径。AS=G，NS=M；负 g 不等于推理错误。

## 匿名复现整理

本地单独构建新的代码候选树，不复制本仓库的 Git 历史、部署说明、私有目录、集群
配置和实验输出。本仓库继续作为原工程追溯来源，不能直接拿来做匿名发布。

候选树使用统一的 scored-reasoning DAG JSONL，并与正式工作项/锚点逐项核对；E3
原题通过来源指纹和适配器重建。参考答案、代码测试与生成提示分离。模型配置保留
真实 runtime、预算、提示和解析例外，不为“统一”而隐去模型容量差异。

本地验收覆盖：五基准 E1/E2 工作项与选择哈希、五模型配置来源哈希、评分/解析
内核、旧证据离线回放、合成 CPU 端到端与无效分母检查。它不代表本轮重新执行了
GPU 实验，不承诺跨硬件位级复现。需要模型概率的重新推理仍由复现者在明确配置下运行。

发布另设门槛：代码许可、基准/派生 DAG 再分发、投稿匿名规范、最终文件与网页
预览检查。候选版目前仅本地保留，不因本页更新而获得对外发布授权。
