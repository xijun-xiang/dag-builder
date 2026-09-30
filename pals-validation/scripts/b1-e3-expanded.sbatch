#!/usr/bin/env bash
#SBATCH --job-name=pals-e3-expanded
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=256G
#SBATCH --time=04:00:00
# GPU count, resources, stdout/stderr explicitly supplied at submission.
set -euo pipefail
umask 077
: "${PALS_REPO:?}"
: "${PALS_EXPANSION:?}"
: "${PALS_SOURCE_ROOT:?}"
: "${PALS_SLOT:?}"
: "${PALS_WAVE:?}"
: "${PALS_STAGE:?}"
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*/pals-validation) ;; *) exit 64 ;; esac
case "$PALS_EXPANSION" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
case "$PALS_SOURCE_ROOT" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
case "$PALS_SLOT" in qwen25|phi4mini|qwen3) ;; *) exit 64 ;; esac
case "$PALS_STAGE" in generate|score|evaluate) ;; *) exit 64 ;; esac
[[ "$PALS_WAVE" =~ ^[0-9]+$ ]]
for path in "$PALS_REPO" "$PALS_EXPANSION" "$PALS_SOURCE_ROOT"; do
  test "$(realpath "$path")" = "$path"
done
test "$PALS_SOURCE_ROOT" != "$PALS_EXPANSION"
export PALS_RUN="$PALS_EXPANSION/$PALS_SLOT"
test "$(realpath "$PALS_RUN")" = "$PALS_RUN"
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_RUN/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_RUN/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_RUN/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH"
cd "$PALS_RUN"
if [ "$PALS_STAGE" = evaluate ]; then
  test -z "${CUDA_VISIBLE_DEVICES:-}"
  test "${SLURM_GPUS_ON_NODE:-0}" = 0
else
  test "${SLURM_GPUS_ON_NODE:-0}" = 8
fi
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
  --container-mounts="$PALS_REPO:$PALS_REPO:ro,/work/projects/polyullm/xxj/PALS/models/hf:/work/projects/polyullm/xxj/PALS/models/hf:ro,/work/projects/polyullm/xxj/PALS/configs/e3:/work/projects/polyullm/xxj/PALS/configs/e3:ro,$PALS_SOURCE_ROOT:$PALS_SOURCE_ROOT:ro,$PALS_EXPANSION:$PALS_EXPANSION:rw" \
  --container-workdir="$PALS_RUN" --container-remap-root --no-container-mount-home \
  bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src"
export PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_RUN/scratch/tmp" HF_HOME="$PALS_RUN/scratch/hf" HF_MODULES_CACHE="$PALS_RUN/scratch/hf-modules" XDG_CACHE_HOME="$PALS_RUN/scratch/cache" CUDA_CACHE_PATH="$PALS_RUN/scratch/cuda"
mkdir -p "$TMPDIR" "$HF_HOME" "$HF_MODULES_CACHE" "$XDG_CACHE_HOME" "$CUDA_CACHE_PATH"
python3 "$PALS_REPO/scripts/e3_expanded_launch.py"
'
