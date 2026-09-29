#!/usr/bin/env bash
#SBATCH --job-name=pals-e3-evaluate
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=02:00:00
# Submit with explicit --output and --error below PALS_RUN/logs.
set -euo pipefail
umask 077
: "${PALS_REPO:?Absolute deployed pals-validation directory required}"
: "${PALS_RUN:?Absolute initialized E3 run required}"
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*) ;; *) exit 64 ;; esac
case "$PALS_RUN" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
test "$(realpath "$PALS_REPO")" = "$PALS_REPO"
test "$(realpath "$PALS_RUN")" = "$PALS_RUN"
export PALS_STAGE=evaluate
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_RUN/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_RUN/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_RUN/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH" "$PALS_RUN/logs"
cd "$PALS_RUN"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
  --container-mounts="$PALS_REPO:$PALS_REPO:ro,/work/projects/polyullm/xxj/PALS/configs/e3:/work/projects/polyullm/xxj/PALS/configs/e3:ro,$PALS_RUN:$PALS_RUN:rw" \
  --container-workdir="$PALS_RUN" --container-remap-root --no-container-mount-home \
  bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src"
export PYTHONDONTWRITEBYTECODE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2
export TMPDIR="$PALS_RUN/scratch/tmp" XDG_CACHE_HOME="$PALS_RUN/scratch/cache"
mkdir -p "$TMPDIR" "$XDG_CACHE_HOME" "$PALS_RUN/scratch/code-eval"
python3 "$PALS_REPO/scripts/e3_launch_workers.py"
'
