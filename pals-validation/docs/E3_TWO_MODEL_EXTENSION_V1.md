# E3 补齐 Llama-3 与 InternLM3

状态：本地工程实现与合成测试；真实 CPU 兼容性、全题上下文和 GPU 数值门槛尚待验收。不能把下载成功或 mock 测试当作正式实验成功。

## 不变部分

复用已冻结的五基准原题，共 15,898 题/模型：GPQA 198、GSM8K 1,319、HumanEval 164、LiveCodeBench v6 175、MMLU 57 子集 14,042。没有新 DAG，也不重跑原来的三个模型。

保持同一系统提示、一次 greedy 生成（T=0）、评分 T=1、显式步骤边界 v2、相邻前一步删除、相同目标 token 的配对 logprob。AS=G，NS=M；NLL 为辅助量，不恢复跨重复 D，也不把 W 作为主指标。正确率由同一隔离 CPU 判分流程提供，不能替代过程指标。

## 两个固定快照

| slot | 模型来源 | 固定 revision | 原生上下文 |
|---|---|---|---:|
| llama3 | ModelScope `LLM-Research/Meta-Llama-3-8B-Instruct` | `e9f7e7d3fa08b550ea228e38bb5501d35be92c0d` | 8192 |
| internlm3 | Hugging Face `internlm/internlm3-8b-instruct` | `28c99415adaf61767bd1c619f4f99f308fdfd223` | 32768 |

只接收 safetensors 权重，不接收重复的 original/pickle 权重。逐文件核对来源给出的大小与 SHA256 或 Git blob SHA1，同时产生本地完整 SHA256 清单；安全策略和模型身份验收完成前不加载权重。

下载支持显式的任务级 HTTP 代理；清理子进程继承的其他代理环境，仍使用 HTTPS、证书校验和固定源文件哈希。先核验两个来源的 config，再下载权重。失败保留，不隐式重试；改用代理须使用新目录及新提交记录，不覆盖旧批次。

## 唯一的生成预算适配

Llama-3 是原始 8K 模型，不换成 3.1，也不外推 RoPE。对已经固定的每个最多八题的 batch，令 L 为该 batch 最长提示的 token 数，实际输出上限为：

`min(原定输出上限, 8192 - L - 64)`。

64 token 为评分边界重新编码预留的余量，并不取消评分端的严格上下文检查。不会截断题面；若题面本身不满足条件，CPU 预检停止并报告，不静默删题。保持八题 batch 意味着同批共享上限，短提示也受最长提示限制；该事实须披露，不能声称全部模型具有完全相同的输出预算。

InternLM3 保持知识/数学 8192、代码 16384 的原输出上限。每批保存原上限、实际上限、padding 宽度、逐题提示长度；审计从原始 token 重算。报告实际上限分布、length 终止数量和排除 length 终止的敏感性分析。

## 验收与执行

1. 新建单独的两模型 cohort；`original` 仍严格限定旧三模型，`extension` 严格限定这两模型，不允许混用或导入旧模型输出。
2. 审核 InternLM 的固定版本自定义代码，记录三份 Python 源文件哈希。仅对这份本地清单开启 `trust_remote_code`，离线运行，不临时拉取代码。
3. 在同一 CPU init 中完成隔离自检、模型清单核验、微型随机权重架构检查（forward、原生 masked loss、cached greedy generation）和全部题目的 tokenizer/上下文检查。微型架构结果只用于兼容性，绝不是预训练模型数值证据。
4. CPU init 主/batch/step 均成功且应用 PASS 后，提交四项 HOLD 作业：Llama GPU → CPU 判分与审计 → InternLM GPU → CPU 判分与审计。核验资源、pretrain、节点排除和依赖后释放；最多同时八张 GPU。
5. 正式 GPU worker 仍先验真实权重的重复评分和原生 masked loss，再运行固定首批（属于全量，不丢弃）及覆盖率门槛。任一失败保留，不自动重生成，不跳过门槛。
6. 最终保留无效、一步 N/A、格式恢复、截断及不利结果；题目等权统计 AS/NS，另列两模型共同有效题、57 子集结果及敏感性。与旧三模型合表时保留各自协议 ID，披露 Llama 预算差异；不得冒充同一个运行。

旧三个模型的冻结源码和产物保持只读。新增实现不代表已有实验已经通过，也不代表 CoEvalChain 的分布式机制提高了单条轨迹的 PALS。
