#!/usr/bin/env bash
#SBATCH --job-name=pals-e3-derived-score
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=32
#SBATCH --mem=256G
#SBATCH --time=01:00:00
# Only existing raw likelihood scoring. No generation or candidate code execution.
set -euo pipefail
umask 077
: "${PALS_REPO:?}"
: "${PALS_RUN:?}"
: "${PALS_SOURCE_RUN:?}"
: "${PALS_REPLAY:?}"
: "${PALS_RUN_LABEL:?}"
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*) ;; *) exit 64 ;; esac
case "$PALS_RUN" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
case "$PALS_SOURCE_RUN" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
case "$PALS_REPLAY" in /work/projects/polyullm/xxj/PALS/artifacts/*) ;; *) exit 64 ;; esac
for selected_path in "$PALS_REPO" "$PALS_RUN" "$PALS_SOURCE_RUN" "$PALS_REPLAY"; do
  test "$(realpath "$selected_path")" = "$selected_path"
done
test "$PALS_RUN" != "$PALS_SOURCE_RUN"
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_RUN/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_RUN/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_RUN/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH" "$PALS_RUN/logs"
cd "$PALS_RUN"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
  --container-mounts="$PALS_REPO:$PALS_REPO:ro,/work/projects/polyullm/xxj/PALS/models/hf:/work/projects/polyullm/xxj/PALS/models/hf:ro,/work/projects/polyullm/xxj/PALS/configs/e3:/work/projects/polyullm/xxj/PALS/configs/e3:ro,$PALS_SOURCE_RUN:$PALS_SOURCE_RUN:ro,$PALS_REPLAY:$PALS_REPLAY:ro,$PALS_RUN:$PALS_RUN:rw" \
  --container-workdir="$PALS_RUN" --container-remap-root --no-container-mount-home \
  bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src"
export PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_RUN/scratch/tmp" HF_HOME="$PALS_RUN/scratch/hf" HF_MODULES_CACHE="$PALS_RUN/scratch/hf-modules" XDG_CACHE_HOME="$PALS_RUN/scratch/cache" CUDA_CACHE_PATH="$PALS_RUN/scratch/cuda"
mkdir -p "$TMPDIR" "$HF_HOME" "$HF_MODULES_CACHE" "$XDG_CACHE_HOME" "$CUDA_CACHE_PATH"
python3 "$PALS_REPO/scripts/e3_launch_derived.py"
'
