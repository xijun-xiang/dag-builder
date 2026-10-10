#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${PALS_REPO:?}" "${PALS_DYNAMIC_ACTION:?}"
export PALS_DYNAMIC_ROOT=/work/projects/polyullm/xxj/PALS/runs/20261010-llama-e2-mmlu-dynamic-v1
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*/pals-validation) ;; *) exit 64 ;; esac
case "$PALS_DYNAMIC_ACTION" in prepare|launch|audit) ;; *) exit 64 ;; esac
for p in "$PALS_REPO" "$PALS_DYNAMIC_ROOT"; do test "$(realpath "$p")" = "$p"; done
if test "$PALS_DYNAMIC_ACTION" = launch; then test "${SLURM_GPUS_ON_NODE:-0}" = 8; fi
export PATH=/cm/local/apps/slurm/current/bin:$PATH SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
cd "$PALS_DYNAMIC_ROOT"
python3 "$PALS_REPO/scripts/e2_dynamic_submit.py" admit --stage "$PALS_DYNAMIC_ACTION"
export ENROOT_CACHE_PATH="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/enroot-cache"
export ENROOT_DATA_PATH="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/enroot-runtime"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
 --container-mounts="$PALS_REPO:$PALS_REPO:ro,/work/projects/polyullm/xxj/PALS/models/hf:/work/projects/polyullm/xxj/PALS/models/hf:ro,/work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-formal-eos-v2:/work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-formal-eos-v2:ro,/work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-eos-recovery-v1:/work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-eos-recovery-v1:ro,$PALS_DYNAMIC_ROOT:$PALS_DYNAMIC_ROOT:rw" \
 --container-workdir="$PALS_DYNAMIC_ROOT" --container-remap-root --no-container-mount-home \
 bash -c '
set -euo pipefail
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/tmp" HF_HOME="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/hf" HF_MODULES_CACHE="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/hf-modules" XDG_CACHE_HOME="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/cache" CUDA_CACHE_PATH="$PALS_DYNAMIC_ROOT/scratch/$PALS_DYNAMIC_ACTION/cuda"
python3 -m pals_validation.recovery.e2_dynamic "$PALS_DYNAMIC_ACTION" --root "$PALS_DYNAMIC_ROOT"
'
