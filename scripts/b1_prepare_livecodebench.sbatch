#!/bin/bash
# CPU-only source materialization, not model inference or code execution.
#SBATCH --job-name=pals-lcb-source5
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:15:00
#SBATCH --output=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-source-canary-v1/logs/%j.out
#SBATCH --error=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-source-canary-v1/logs/%j.err
set -euo pipefail
umask 077
LCB_RUN=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-source-canary-v1
LCB_SOURCE=/work/projects/polyullm/xxj/PALS/src/dag-builder-livecodebench-20260922-v1
test "$(realpath "$LCB_RUN")" = "$LCB_RUN"
test "$(realpath "$LCB_SOURCE")" = "$LCB_SOURCE"
cd "$LCB_RUN"
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$LCB_RUN/scratch"
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PYTHONPATH="$LCB_SOURCE/src"
export HF_HUB_OFFLINE=1
export HF_DATASETS_OFFLINE=1
/cm/local/apps/slurm/current/bin/srun \
  --container-image=/lustre/projects/polyullm/container/infi-evalscope+1.7.sqsh \
  --container-mounts="$LCB_RUN:$LCB_RUN,$LCB_SOURCE:$LCB_SOURCE:ro" \
  --container-workdir="$LCB_RUN" --no-container-mount-home \
  python3 -B -m dag_builder.livecodebench_source \
  --root "$LCB_RUN/prepared" \
  --source-file /app/data/pre-data/code_generation_lite/test6.jsonl --count 5
