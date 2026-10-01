# E3 修复后的规范续跑

本版本只衔接已验收的 CPU 超时分类修复，不改题目、批次成员、seed、生成配置、
提示词、解析规则、评分温度或 g/G/M/W。保持每模型 1956 题、三模型、最多同时
8 GPU。旧 run、旧失败终态、恢复 overlay 全部只读。

## 导入首批而非重跑

新 run 使用独立 expansion_id，保留旧 source protocol_id。先校验旧范围 manifest、
恢复 audit、原文件哈希、修复版 selftest 和未改变的科学内核。若发现来源中还有
其他已开始或部分完成的生成则停止，不能默默忽略后重新生成。

Phi wave 0 的 raw/attempt 按字节复制；40 份 scores 和修复后的 outcomes
保留原 payload，仅在新副本中更新外层 expansion_id 并增加 imported_from。
旧原文件完整复制到 imports/，记录 SHA256。没有改动原文件，也没有模型调用。
三个阶段的 worker 文件是明确带 imported=true 的导入凭据，不伪造新作业执行。
新目录重新做完整 token、g/G/M/W、答案及覆盖率审计。只有 PASS 才发布 ready。

38 条原判分沿用旧版本，2 条修复判分沿用 recovery_id 的版本，来源明确保留。
后续所有新判分统一使用 selftest 已通过的新 harness，资源与安全合同不变。

## 顺序与停止条件

controller 每次最多提交一组 generate→score→CPU evaluate/audit 的 afterok 链。
导入首批跳过提交，从 Qwen2.5 wave 0 继续，再 Qwen3 wave 0，之后每个 wave
按 Phi/Qwen2.5/Qwen3 顺序。32 waves/model，合计 96，其中首项已导入。

同一 wave 三作业主/batch/step 均需 COMPLETED 0:0，应用 audit PASS 且
coverage_gate=true，才提交下一 wave。独占锁＋提交前 attempt 文件防止重复；
作业失败、不确定提交、覆盖异常或哈希不符立即停止，不自动修复或重试。

使用 `b1-e3-continuation.sbatch` 在 CPU 上导入和离线审计；之后通过当天已确认
B1 helper 执行 `PYTHONPATH=... python3 scripts/e3_advance_expansion.py --run NEW`。
自动化不是权限绕过：跨日没有新的连接确认就不能 SSH。

本轮不额外执行共同评分者子集，不因出现 M=0、负 g、答错或正常代码超时筛题。
