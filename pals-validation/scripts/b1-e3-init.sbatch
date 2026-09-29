#!/usr/bin/env bash
#SBATCH --job-name=pals-e3-init
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=48G
#SBATCH --time=02:00:00
# Submit with explicit --output and --error below PALS_RUN_ROOT/logs.
set -euo pipefail
umask 077
: "${PALS_REPO:?}"
: "${PALS_PREPARED:?}"
: "${PALS_MODEL_MATRIX:?}"
: "${PALS_CONFIGS:?}"
: "${PALS_RUN_ROOT:?}"
: "${PALS_HARNESS_SELFTEST:?}"
for path in "$PALS_REPO" "$PALS_PREPARED" "$PALS_MODEL_MATRIX" "$PALS_CONFIGS" "$PALS_RUN_ROOT" "$PALS_HARNESS_SELFTEST"; do
  case "$path" in /work/projects/polyullm/xxj/PALS/*) ;; *) exit 64 ;; esac
  test "$(realpath "$path")" = "$path"
done
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_RUN_ROOT/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_RUN_ROOT/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_RUN_ROOT/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH"
cd "$PALS_RUN_ROOT"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
  --container-mounts="/work/projects/polyullm/xxj/PALS:/work/projects/polyullm/xxj/PALS:rw" \
  --container-workdir="$PALS_RUN_ROOT" --container-remap-root --no-container-mount-home \
  bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src"
export PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_RUN_ROOT/scratch/tmp" HF_HOME="$PALS_RUN_ROOT/scratch/hf" HF_MODULES_CACHE="$PALS_RUN_ROOT/scratch/hf-modules" XDG_CACHE_HOME="$PALS_RUN_ROOT/scratch/cache"
mkdir -p "$TMPDIR" "$HF_HOME" "$HF_MODULES_CACHE" "$XDG_CACHE_HOME"
python3 "$PALS_REPO/scripts/e3_b1_init.py" --prepared "$PALS_PREPARED" \
  --model-matrix "$PALS_MODEL_MATRIX" --configs "$PALS_CONFIGS" --runs "$PALS_RUN_ROOT" \
  --harness-selftest "$PALS_HARNESS_SELFTEST"
'
