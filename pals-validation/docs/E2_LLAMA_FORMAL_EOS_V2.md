# Llama E2 剩余四基准正式续跑：EOS v2

用户2026-10-09批准质量复查后最小修复并续跑。v1原文件、原始轨迹、原分数永久保留。
唯一解析变化：v1接纳的自然EOS正文若含step/think/answer/analysis/final/result标签前缀，
即使后接字母而无词边界，也按结构残片拒绝。新版本`llama-e2-natural-eos-v2`。
不增加语义/正确性过滤，不改正文，不修截断，不增抽；严格有效目标保持原样。

## 数据与统计

GPQA及旧HumanEval工程首批只读复用。CPU重放历史2592条，v2只改变GPQA正式两条误收：
2413→2411/2496，共同完整题仍55；HumanEval首批45/48且每cell至少6条，不重新生成。
其余正式使用原冻结DAG、锚点、前缀、删除节点、提示、种子2026091507、T0.3/0.7/1.2，
每cell8次，评分T1，原8192上下文与4096生成上限，不截题、不套用E3预算。
HumanEval/GSM8K/LCB/MMLU合计5005锚点、120120次正式生成；后三基准原工程首批单列，
具体首批数由原prepared清单核算。禁止重新生成GPQA或HumanEval旧首批。
原`backend.py`/`run.py`/`analyze.py`等顶层引擎不修改。

每次原生成同时输出严格版与EOS版：严格版可用原analyze重放，EOS版另外验证原始token、
源prompt、两侧评分上下文、目标token和逐token算术。旧严格分数两版完全相同。
主表分别采用各版三温共同完整题；无效/N/A不填零；回捞版是Llama解析适配，
不能称五模型共同解析协议。正确率、连贯性或效果方向不作通过门槛。

## 执行

新run固定`runs/20261009-llama-e2-formal-eos-v2`，旧所有目录只读。
一次CPU初始化后，四组GPU→CPU audit按HumanEval/GSM8K/LCB/MMLU串行afterok。
HOLD提交、逐项实际资源/路径/依赖/身份验收、倒序释放；有不确定回执或失败不重试。
CPU准备8CPU64G1h；GPU每项8GPU32CPU256G8h；CPU审计8CPU64G1h。
code-agent/defq，排除tko-b1-nv-dgx06，最多8GPU。不执行生成代码。
所有GPU申请依赖CPU准备PASS；分配后宿主机再查前置Slurm主/batch/.0及应用PASS。

每卡加载模型一次，原生masked-loss≤0.005、重复评分≤1e-5；HumanEval复用已验收首批，
其他三基准在同一分配内运行原canary并保留每cell至少2有效的门槛，然后进入正式。
首批门槛等待时模型不重载；正式仍按原8分片分配，不改变生成批次、随机种子或采样后端。
没有独立丢弃GPU canary，bootstrap/完整审计在GPU释放后CPU执行。
静态分片、首批六cell和长输出仍可能产生等待，不承诺持续100%利用率。
新增逐cell生成attempt在调用前排他落盘，任何中断不重抽；原8次批内随机数序列保持不变。

CPU初始化验证全部旧源/模型哈希、原prepared哈希、配置一致，并生成协议ID。
GPU正常退出不等于验收；终态需Slurm主/batch/.0、8worker、原始/严格/EOS双版本、
唯一attempt及每温完整分母、数值门槛和独立token重放均通过。失败保存，后续依赖取消。
