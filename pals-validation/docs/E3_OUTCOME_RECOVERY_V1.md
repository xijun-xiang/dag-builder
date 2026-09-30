# E3 判分退出分类修复与只读来源恢复

2026-09-30，扩展首波 Phi 的 Slurm 113565 在一条 LCB 的第三个公开测试
约 6.25 秒处遇到 infrastructure_error。旧判分器没有保存该测试退出码，
因此不能追认其原始退出信号；6 秒 CPU 软限额只能提供待检验的解释。

## 修复

不改变 seccomp、允许导入集合、测试内容、6/7 秒 CPU 软/硬限额、12 秒墙钟
上限、内存或输出上限。隔离探针通过后、候选代码执行前输出独立 ready 记录。
父进程仅在该记录有效且退出信号为 SIGXCPU 时记为 CPU 超时；父进程触发
墙钟截止且已确认隔离才记为墙钟超时。缺少 ready、未知信号、SIGKILL 或不完整
最终记录仍为基础设施异常。不能依据运行时长直接猜测退出原因。

逐测试保存返回码、信号、ready、墙钟截止标记、输出摘要和耗时，不丢弃诊断。
纯单元测试覆盖分支；GPU-free Slurm 合成安全回归覆盖正常功能、错误答案、
拒绝导入、文件/网络隔离探针、真实死循环触发 CPU 超时。

## 本次恢复边界

`outcome_recovery` 是独立一次性 overlay，不修改旧 expansion manifest、raw、
score、outcome 或 worker。核对冻结代码：只允许 harness、自检脚本变化及新增
恢复模块，生成、似然、解析和数学实现必须原样。旧输入和全部相关证据哈希锁定。

- 38 条已完成 outcomes 原样复用（不按好坏挑选）；
- 1 条基础设施异常、1 条尚无结果，分别用新判分器执行一次；
- 全 40 条做答案身份、token 与 g/G/M/W 离线审计，不调用模型；
- 任何新基础设施失败停止，不自动再试；没有跨模型或扩量执行授权由此产生。

新记录标明原 expansion_id 和 recovery_id，原失败保留。旧判分 policy 文件
不覆盖；overlay 记录仅 harness 哈希更新的派生 policy。正常旧结果仍明确标为
旧判分器产生，不能声称所有测试已由新判分器重跑。

入口：`python -m pals_validation.e3.outcome_recovery --source OLD --output NEW
--selftest PASS_JSON --slot phi4mini --wave 0`，只能在受限 CPU Slurm 作业执行。
恢复验收不让旧控制脚本跳过失败作业；后续扩量需另行显式衔接冻结版本。
