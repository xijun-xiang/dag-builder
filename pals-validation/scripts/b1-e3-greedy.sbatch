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
export PALS_COHORT="${PALS_COHORT:-original}"
case "$PALS_COHORT" in original|extension|extension-llama3|extension-internlm3) ;; *) exit 64 ;; esac
export PALS_CONFIGS="${PALS_CONFIGS:-$PALS_ROOT/configs/e3/20260930-native-v2-format}"
case "$PALS_CONFIGS" in "$PALS_ROOT"/configs/e3/*) ;; *) exit 64 ;; esac
test "$(realpath "$PALS_CONFIGS")" = "$PALS_CONFIGS"
compat_mount=""
if [ "$PALS_COHORT" = extension-internlm3 ]; then
  export PALS_COMPAT_RUNTIME="$PALS_ROOT/src/pals-runtime-internlm3471-20260921-v2"
  export PALS_COMPAT_SPEC="$PALS_ROOT/runs/20261003-internlm-fivebench-e1e2-v1/spec.json"
  test "$(realpath "$PALS_COMPAT_RUNTIME")" = "$PALS_COMPAT_RUNTIME"
  test "$(realpath "$PALS_COMPAT_SPEC")" = "$PALS_COMPAT_SPEC"
  compat_mount=",$PALS_COMPAT_RUNTIME:$PALS_COMPAT_RUNTIME:ro,$PALS_COMPAT_SPEC:$PALS_COMPAT_SPEC:ro"
else
  unset PALS_COMPAT_RUNTIME PALS_COMPAT_SPEC
fi
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
continuation_mount=""
if [ -n "${PALS_FULL_CONTINUATION_SOURCE:-}" ]; then
  case "$PALS_FULL_CONTINUATION_SOURCE" in "$PALS_ROOT"/runs/*/experiment) ;; *) exit 64 ;; esac
  test "$(realpath "$PALS_FULL_CONTINUATION_SOURCE")" = "$PALS_FULL_CONTINUATION_SOURCE"
  test "$PALS_FULL_CONTINUATION_SOURCE" != "$PALS_EXPERIMENT"
  continuation_mount=",$PALS_FULL_CONTINUATION_SOURCE:$PALS_FULL_CONTINUATION_SOURCE:ro"
fi
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
 --container-mounts="$PALS_REPO:$PALS_REPO:ro,$PALS_ROOT/models/hf:$PALS_ROOT/models/hf:ro,$PALS_ROOT/configs/e3:$PALS_ROOT/configs/e3:ro,$PALS_PREPARED:$PALS_PREPARED:ro,$PALS_JOB_ROOT:$PALS_JOB_ROOT:rw$compat_mount$continuation_mount" \
 --container-workdir="$PALS_JOB_ROOT" --container-remap-root --no-container-mount-home \
 bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
if [ -n "${PALS_COMPAT_RUNTIME:-}" ]; then
  # Verify before adding the compatibility directory to Python import paths.
  python3 -m pals_validation.e3.compat_runtime --spec "$PALS_COMPAT_SPEC" --runtime "$PALS_COMPAT_RUNTIME"
  export PYTHONPATH="$PALS_COMPAT_RUNTIME:$PALS_REPO/src"
fi
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_JOB_ROOT/scratch/tmp" HF_HOME="$PALS_JOB_ROOT/scratch/hf" HF_MODULES_CACHE="$PALS_JOB_ROOT/scratch/hf-modules" XDG_CACHE_HOME="$PALS_JOB_ROOT/scratch/cache" CUDA_CACHE_PATH="$PALS_JOB_ROOT/scratch/cuda"
mkdir -p "$TMPDIR" "$HF_HOME" "$HF_MODULES_CACHE" "$XDG_CACHE_HOME" "$CUDA_CACHE_PATH"
if [ "$PALS_STAGE" = init ]; then
  export PALS_HARNESS_RUN="$PALS_JOB_ROOT/preflight"
  mkdir -p "$PALS_HARNESS_RUN"
  python3 "$PALS_REPO/scripts/e3_harness_selftest.py"
  continuation_args=()
  if [ -n "${PALS_FULL_CONTINUATION_SOURCE:-}" ]; then
    continuation_args=(--continue-full-from "$PALS_FULL_CONTINUATION_SOURCE")
  fi
  python3 -m pals_validation.e3.greedy_cli init --root "$PALS_EXPERIMENT" --prepared "$PALS_PREPARED" \
   --configs "$PALS_CONFIGS" --cohort "$PALS_COHORT" --selftest "$PALS_HARNESS_RUN/selftest.json" "${continuation_args[@]}"
else
  test "$(realpath "$PALS_EXPERIMENT")" = "$PALS_EXPERIMENT"
  python3 "$PALS_REPO/scripts/e3_greedy_launch.py"
fi
'
