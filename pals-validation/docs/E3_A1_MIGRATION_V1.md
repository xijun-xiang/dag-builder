# E3 全量协议迁移到 A1 pretrain

2026-10-07 用户批准将尚未运行的 B1 全量 E3 迁移至 A1。
B1 六项任务 114566—114571 经逐项确认仍在排队后取消，耗时均为零；历史产物不删除。

## 科学设置不变

沿用 `E3_GREEDY_FULL_V1.md`：三冻结模型、每模型 15898 题、MMLU 全57子集，
贪心一次生成、batch8、score T=1、相邻前步删除、题级 G/M 与同目标 NLL。
不导入旧 T=.7 轨迹，不额外采样或另行排 GPU canary。
完整 GPU allocation 内执行原生 masked-loss 门槛和首批40题，再继续全量。
迁移不改变提示词、解析、g/G/M 内核、判分规则或失败分母。

## 部署边界

`PALS_CLUSTER=a1` 只能写 `/work/projects/polyullm/xxj/pals`；默认 B1 仍只允许
`/work/projects/polyullm/xxj/PALS`。未知 cluster、符号链接逃逸、跨项目目录均拒绝。
部署信息和代码哈希进入新 manifest，产生新的运行协议 ID；不把它伪装成旧运行。
运行 `scripts/a1-e3-greedy.sbatch`，固定 `defq` / `pretrain`。Slurm 路径采用 A1 配置。
仍然三模型 GPU/CPU 串行 afterok，全程最多8GPU，不自动重试失败或不确定生成。

## 迁移与验收顺序

1. 原模型只读核对冻结逐文件 SHA256。仅模型及清单允许经用户批准的
   `s3:data/xxj/pals-migration-20261007/` 中转；不覆盖对象、不放题目/轨迹/凭据。
2. A1 接收到 `transfer-staging/`，逐文件数量、大小、SHA256 与冻结清单一致后
   才 rename 到 `models/hf/`。S3 中转保留，不自动清理。
3. 同镜像在纯 CPU allocation 上验收 torch/transformers/libseccomp，记录镜像
   哈希与宿主驱动；实际 GPU 数值门槛仍在正式 GPU allocation 内执行。
4. configs 只重定位 `model.path`、`model.files_manifest`；模型 revision、文件摘要
   及全部科学设置与 B1 一致。数据哈希保持原样，不重新构造题目。
5. A1 CPU init 执行安全隔离自检、三模型文件哈希和全量上下文预算检查，随后冻结。
   CPU init 提交记录单独保存在 `init-submissions/`，正式链使用 `submissions/`。
6. 只有 Slurm 主/batch/step 成功且应用 init=PASS 后，才调用原一次性提交器，
   提交 GPU24h→CPU16CPU64G4h 的三模型链。缺项/失败保留证据并停止。

跨硬件可能引入数值差异，故本次结果注明 A1 部署，不与 B1 旧分数混写为同一次实验。
不因迁移调整生成参数、模型、资源上限或统计定义；若环境不兼容，应先报告具体证据。
