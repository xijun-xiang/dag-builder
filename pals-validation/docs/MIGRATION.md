# 与先导实验的关系

来源：用户《正式版pals实验启动方案.pdf》及本地既有 artifacts 中的 adjacent_rules.py、forest_rules.py、prepare_reviewed.py。本仓库整理实现的是冻结后的新协议，**不是旧结果逐位复刻工具**。

| 项目 | 固化方式与变化 |
|---|---|
| 数据 | 同一 118 题 accepted 导出，SHA256 537403ba3190dd8e2f8b423554b8441cddd279a37f0f04068c0810fc20c129d8 |
| E2 位置 | 保留单 hash 目标、祖先前缀；不采用后来讨论的 1/3、2/3 双锚点 |
| GPQA 选项 | 新版 E1/E2 统一带四选项；部分旧 E2 脚本未带选项，因此新旧数值不能合并 |
| 换序选择 | 保留算子结构，但改为显式确定性候选哈希；不声称与旧匹配位置规则选中同一交换 |
| 拆树 | 保留共享祖先固定、完整非平凡子树交换；改为统一 seed 的候选排名 |
| 对照 | 新增同题、同 forest baseline、同目标集合的合法／破坏比较 |
| 后端 | 独立 Hugging Face 实现，统一 chat prompt；不是历史 evalscope scorer 的数值复刻 |
| 模型 | 权重哈希、模板参数、库版本必须记录；thinking 与 non-thinking 是不同 profile |
| 结果 | 保留逐 token 证据；缺失不填零；完整共同题集与覆盖率分开报告 |

历史主要参考路径（不构成运行依赖）：

- artifacts/pals-gpqa-full118-v1/adjacent_rules.py
- artifacts/pals-gpqa-forest118-v1/forest_rules.py
- artifacts/pals-dual-axis-candidate-v1/prepare_reviewed.py

这次不导入旧数值为正式结果，不更改源数据，不运行任何集群任务。数据文本留在被忽略的 outputs 中，仓库不携带 GPQA 题面。
