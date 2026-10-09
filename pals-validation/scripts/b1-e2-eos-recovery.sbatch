#!/usr/bin/env bash
# Resource and dependency declarations are supplied by the held-job controller.
set -euo pipefail
umask 077
: "${PALS_REPO:?}" "${PALS_RECOVERY_RUN:?}" "${PALS_RECOVERY_ACTION:?}"
test "$PALS_RECOVERY_RUN" = /work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-eos-recovery-v1
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*/pals-validation) ;; *) exit 64 ;; esac
case "$PALS_RECOVERY_ACTION" in prepare|launch|audit) ;; *) exit 64 ;; esac
for p in "$PALS_REPO" "$PALS_RECOVERY_RUN"; do test "$(realpath "$p")" = "$p"; done
if test "$PALS_RECOVERY_ACTION" = launch; then test "${SLURM_GPUS_ON_NODE:-0}" = 8; fi
export PATH=/cm/local/apps/slurm/current/bin:$PATH SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
cd "$PALS_RECOVERY_RUN"
python3 "$PALS_REPO/scripts/e2_eos_submit.py" admit --stage "$PALS_RECOVERY_ACTION"
export ENROOT_CACHE_PATH="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/enroot-cache"
export ENROOT_DATA_PATH="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/enroot-runtime"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
 --container-mounts="$PALS_REPO:$PALS_REPO:ro,/work/projects/polyullm/xxj/PALS/models/hf:/work/projects/polyullm/xxj/PALS/models/hf:ro,/work/projects/polyullm/xxj/PALS/runs/20261009-llama-fivebench-e1e2-v2:/work/projects/polyullm/xxj/PALS/runs/20261009-llama-fivebench-e1e2-v2:ro,$PALS_RECOVERY_RUN:$PALS_RECOVERY_RUN:rw" \
 --container-workdir="$PALS_RECOVERY_RUN" --container-remap-root --no-container-mount-home \
 bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/tmp" HF_HOME="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/hf" HF_MODULES_CACHE="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/hf-modules" XDG_CACHE_HOME="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/cache" CUDA_CACHE_PATH="$PALS_RECOVERY_RUN/scratch/$PALS_RECOVERY_ACTION/cuda"
python3 -m pals_validation.recovery.e2_eos "$PALS_RECOVERY_ACTION" --root "$PALS_RECOVERY_RUN/recovery" --workers 8
'
