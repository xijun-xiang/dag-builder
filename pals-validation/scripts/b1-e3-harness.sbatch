#!/usr/bin/env bash
#SBATCH --job-name=pals-e3-harness
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=00:20:00
# Submit with explicit --output and --error inside PALS_HARNESS_RUN/logs.
set -euo pipefail
umask 077
: "${PALS_REPO:?}"
: "${PALS_HARNESS_RUN:?}"
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*) ;; *) exit 64 ;; esac
case "$PALS_HARNESS_RUN" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
test "$(realpath "$PALS_REPO")" = "$PALS_REPO"
test "$(realpath "$PALS_HARNESS_RUN")" = "$PALS_HARNESS_RUN"
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_HARNESS_RUN/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_HARNESS_RUN/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_HARNESS_RUN/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH"
cd "$PALS_HARNESS_RUN"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
  --container-mounts="$PALS_REPO:$PALS_REPO:ro,$PALS_HARNESS_RUN:$PALS_HARNESS_RUN:rw" \
  --container-workdir="$PALS_HARNESS_RUN" --container-remap-root --no-container-mount-home \
  bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$PALS_HARNESS_RUN/scratch/tmp"
mkdir -p "$TMPDIR"
python3 "$PALS_REPO/scripts/e3_harness_selftest.py"
'
