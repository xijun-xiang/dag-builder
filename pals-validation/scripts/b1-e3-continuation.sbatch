#!/usr/bin/env bash
#SBATCH --job-name=e3-continuation-import
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=01:00:00
set -euo pipefail
umask 077
: "${PALS_REPO:?}" "${PALS_PREP_RUN:?}" "${PALS_PARENT:?}" "${PALS_RECOVERY_PARENT:?}" "${PALS_SOURCE_ROOT:?}"
test -z "${CUDA_VISIBLE_DEVICES:-}"
test "${SLURM_GPUS_ON_NODE:-0}" = 0
for path in "$PALS_REPO" "$PALS_PREP_RUN" "$PALS_PARENT" "$PALS_RECOVERY_PARENT" "$PALS_SOURCE_ROOT"; do
  case "$path" in /work/projects/polyullm/xxj/PALS/*) ;; *) exit 64 ;; esac
  test "$(realpath "$path")" = "$path"
done
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_PREP_RUN/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_PREP_RUN/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_PREP_RUN/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH"
cd "$PALS_PREP_RUN"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
  --container-mounts="$PALS_REPO:$PALS_REPO:ro,$PALS_PREP_RUN:$PALS_PREP_RUN:rw,$PALS_PARENT:$PALS_PARENT:ro,$PALS_RECOVERY_PARENT:$PALS_RECOVERY_PARENT:ro,$PALS_SOURCE_ROOT:$PALS_SOURCE_ROOT:ro,/work/projects/polyullm/xxj/PALS/configs/e3:/work/projects/polyullm/xxj/PALS/configs/e3:ro,/work/projects/polyullm/xxj/PALS/models/hf:/work/projects/polyullm/xxj/PALS/models/hf:ro" \
  --container-workdir="$PALS_PREP_RUN" --container-remap-root --no-container-mount-home \
  bash -c '
set -euo pipefail
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=2
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export TMPDIR="$PALS_PREP_RUN/scratch/tmp" HF_HOME="$PALS_PREP_RUN/scratch/hf"
export HF_MODULES_CACHE="$PALS_PREP_RUN/scratch/hf-modules" XDG_CACHE_HOME="$PALS_PREP_RUN/scratch/cache"
mkdir -p "$TMPDIR" "$HF_HOME" "$HF_MODULES_CACHE" "$XDG_CACHE_HOME"
python3 -m pals_validation.e3.continuation --parent "$PALS_PARENT" \
  --recovery "$PALS_RECOVERY_PARENT/phi4mini-wave000" --output "$PALS_PREP_RUN/experiment"
'
