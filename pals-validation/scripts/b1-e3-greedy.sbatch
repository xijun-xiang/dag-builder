#!/usr/bin/env bash
#SBATCH --partition=defq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --no-requeue
# Resources, wall time and log paths must be explicit at submission.
set -euo pipefail
umask 077
: "${PALS_REPO:?}" "${PALS_JOB_ROOT:?}" "${PALS_PREPARED:?}" "${PALS_STAGE:?}"
PALS_ROOT=/work/projects/polyullm/xxj/PALS
case "$PALS_REPO" in "$PALS_ROOT"/src/*/pals-validation) ;; *) exit 64 ;; esac
case "$PALS_JOB_ROOT" in "$PALS_ROOT"/runs/*) ;; *) exit 64 ;; esac
case "$PALS_PREPARED" in "$PALS_ROOT"/artifacts/*) ;; *) exit 64 ;; esac
for path in "$PALS_ROOT" "$PALS_REPO" "$PALS_JOB_ROOT" "$PALS_PREPARED"; do
  test "$(realpath "$path")" = "$path"
done
export PALS_EXPERIMENT="$PALS_JOB_ROOT/experiment"
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_JOB_ROOT/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_JOB_ROOT/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_JOB_ROOT/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH"
if [ "$PALS_STAGE" = gpu ]; then
  test "${SLURM_GPUS_ON_NODE:-0}" = 8
else
  test -z "${CUDA_VISIBLE_DEVICES:-}"
  test "${SLURM_GPUS_ON_NODE:-0}" = 0
fi
end_time=$(scontrol show job "$SLURM_JOB_ID" -o | tr ' ' '\n' | sed -n 's/^EndTime=//p')
export PALS_DEADLINE=$(( $(date -d "$end_time" +%s) - 3600 ))
cd "$PALS_JOB_ROOT"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
 --container-mounts="$PALS_REPO:$PALS_REPO:ro,$PALS_ROOT/models/hf:$PALS_ROOT/models/hf:ro,$PALS_ROOT/configs/e3:$PALS_ROOT/configs/e3:ro,$PALS_PREPARED:$PALS_PREPARED:ro,$PALS_JOB_ROOT:$PALS_JOB_ROOT:rw" \
 --container-workdir="$PALS_JOB_ROOT" --container-remap-root --no-container-mount-home \
 bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_JOB_ROOT/scratch/tmp" HF_HOME="$PALS_JOB_ROOT/scratch/hf" HF_MODULES_CACHE="$PALS_JOB_ROOT/scratch/hf-modules" XDG_CACHE_HOME="$PALS_JOB_ROOT/scratch/cache" CUDA_CACHE_PATH="$PALS_JOB_ROOT/scratch/cuda"
mkdir -p "$TMPDIR" "$HF_HOME" "$HF_MODULES_CACHE" "$XDG_CACHE_HOME" "$CUDA_CACHE_PATH"
if [ "$PALS_STAGE" = init ]; then
  export PALS_HARNESS_RUN="$PALS_JOB_ROOT/preflight"
  mkdir -p "$PALS_HARNESS_RUN"
  python3 "$PALS_REPO/scripts/e3_harness_selftest.py"
  python3 -m pals_validation.e3.greedy_cli init --root "$PALS_EXPERIMENT" --prepared "$PALS_PREPARED" \
   --configs /work/projects/polyullm/xxj/PALS/configs/e3/20260930-native-v2-format --selftest "$PALS_HARNESS_RUN/selftest.json"
else
  test "$(realpath "$PALS_EXPERIMENT")" = "$PALS_EXPERIMENT"
  python3 "$PALS_REPO/scripts/e3_greedy_launch.py"
fi
'
