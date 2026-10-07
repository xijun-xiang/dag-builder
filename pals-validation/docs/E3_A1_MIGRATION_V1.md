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
6. 只有 Slurm 主/batch/step 成功且应用 init=PASS 后，才调用一次性提交器，
   提交 GPU24h→CPU16CPU64G4h 的三模型链。全部先HOLD，经实际参数核验再释放。
   缺项/失败保留证据并停止。

跨硬件可能引入数值差异，故本次结果注明 A1 部署，不与 B1 旧分数混写为同一次实验。
不因迁移调整生成参数、模型、资源上限或统计定义；若环境不兼容，应先报告具体证据。

## 2026-10-07 迁移验收记录

- 三个模型的53份文件在A1逐文件SHA256、大小和revision与B1冻结清单一致。
- 同名容器镜像实际SHA256相同；A1 CPU环境验收通过：torch2.11.0+cu130、
  transformers5.6.0、libseccomp.so.2，宿主驱动580.65.06。
- 本地150项测试通过；184条旧输出中的633个已评分步骤重放，算术差异0。
  这是工程回归，不是新贪心协议的实验结果。
- CPU检查不要在`set -u`下加载未经适配的系统`/etc/profile`；采用已核实的Slurm路径。
  零GPU allocation不能枚举GPU是正常的隔离结果，不应把`nvidia-smi`成功设为CPU门槛。
  两次早期检查失败均保留并经用户批准后在新目录重提；没有运行题目或重采样。
- 当前实验执行快照固定为`246bf4d05e9d278f059305f76dad7c34d303fbec`；
  本节是后补验收记录，不修改集群运行快照。GPU数值门槛、上下文预算和正式结果须分别验收。

### 容器存储故障与获批重提

首轮CPU init通过；首个GPU作业在另一计算节点解压容器时返回ENOSPC，
容器内模型代码没有执行，三个模型的生成尝试/原始输出/分数目录均为空。
后续五项作业因afterok依赖失败取消。失败运行和调度账本保留，不清理节点共享缓存。

用户批准后，新运行目录再次执行相同CPU init，显式`--exclude`生效，CPU验收通过。
但是本次私有包装脚本试图通过`SBATCH_EXCLUDE`向未修改的GPU提交器传递排除约束，
实际没有生效。提交后回读得到`ExcNodeList=(null)`，首GPU作业再次落在故障节点，
在容器启动前退出，其余五项依赖取消。这是调度包装实现错误，不是新的模型/数据问题。
此前本节称该环境变量是有效传递方式不正确；旧版本留在Git历史，当前明确纠正。
全部原始回执保留；三个模型仍无任何生成尝试或轨迹。停止新增提交，等待重新授权。

下一次应将`--exclude=<node>`作为显式sbatch命令行参数，同时用`--hold`暂缓调度，
逐项核验实际排除节点、reservation、资源、路径和依赖，再释放正确的任务链。
不得继续使用该环境变量方案，也不得重启旧包装脚本或覆盖失败账本。
冻结科学代码仍为246bf4d；调度变化及旧失败来源独立记录，不伪装为实验参数的变更。

### 修正提交器：explicit-held-v1

用户再次批准后，修正仓库内`e3_greedy_submit.py`，不再依赖私有环境变量包装：

- `--exclude-node`写入每条sbatch的显式`--exclude`，同时写入不可变调度账本。
- 所有六项任务都加`--hold`；清除继承的`SBATCH_*`选项，不允许暗中改变申请。
- `--action verify`逐项查询实际Slurm字段：用户、HOLD、节点排除、pretrain、
  分区、8GPU或0GPU、CPU/内存/时限、源码/日志路径、afterok和失败依赖取消。
- `--action release`在锁内再次核验全部六项，先释放下游、最后释放首个GPU任务；
  释放命令有独占尝试记录，中断或不确定状态不自动重放。
- 原CPU安全与预算门槛、科学输入及数值门槛不改变。部署为新冻结提交和新运行目录，
  不覆盖246bf4d旧快照。最终执行提交与作业号记录在对应运行回执。

调用顺序（各路径和CPU init ID必须使用新批次的真实值）：

```bash
python scripts/e3_greedy_submit.py --root RUN --prepared INPUT --init-job ID --exclude-node NODE
python scripts/e3_greedy_submit.py --root RUN --prepared INPUT --init-job ID --action verify
python scripts/e3_greedy_submit.py --root RUN --prepared INPUT --init-job ID --action release
```

本地纯CPU回归包含：显式参数、继承环境污染、14种调度字段不匹配、串行释放顺序、
部分释放失败后首GPU仍保持HOLD、重复提交/释放拒绝。测试模拟调度器，不能替代真实回读。

实际Slurm 23.02在HOLD时会把单节点请求打印成`NumNodes=1-1`，校验同时接受`1`
与`1-1`，仍拒绝`1-2`或其他范围。该兼容修正发生在全部任务仍被HOLD时，没有计算作业失败。
独立调度控制器可用`--runtime-repo`核验/释放已经冻结的运行；必须将PYTHONPATH指向
该运行源码，不能用此选项提交新任务。它不修改旧源码/manifest，审计回执同时保留
控制器自身SHA256、冻结runtime路径与未改写的Slurm原字段。不要为了显示格式重跑CPU或GPU。

新旧运行须逐项比较题目、模型配置、批次、实现哈希和判分规则。
每次重新执行安全自检会产生新的作业ID/时间记录，故自检摘要及派生protocol_id
可能不同；不得覆盖旧回执或强行令两个ID相同。资源上限、数值门槛和失败保留规则不变。
CPU门槛在一台节点通过不证明其他节点的本地容器存储同样可用；
调度排除只是规避已知故障，不保证其他节点绝不会发生同类故障。
