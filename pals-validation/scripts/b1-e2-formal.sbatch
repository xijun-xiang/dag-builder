#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${PALS_REPO:?}" "${PALS_FORMAL_INDEX:?}"
export PALS_FORMAL_ROOT=/work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-formal-eos-v2
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*/pals-validation) ;; *) exit 64 ;; esac
case "$PALS_FORMAL_INDEX" in 0|1|2|3|4|5|6|7|8) ;; *) exit 64 ;; esac
actions=(prepare launch audit launch audit launch audit launch audit)
benches=(none humaneval humaneval gsm8k gsm8k livecodebench livecodebench mmlu mmlu)
export PALS_FORMAL_ACTION="${actions[$PALS_FORMAL_INDEX]}" PALS_FORMAL_BENCH="${benches[$PALS_FORMAL_INDEX]}"
for p in "$PALS_REPO" "$PALS_FORMAL_ROOT"; do test "$(realpath "$p")" = "$p"; done
if test "$PALS_FORMAL_ACTION" = launch; then test "${SLURM_GPUS_ON_NODE:-0}" = 8; fi
export PATH=/cm/local/apps/slurm/current/bin:$PATH SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
cd "$PALS_FORMAL_ROOT"
python3 "$PALS_REPO/scripts/e2_formal_submit.py" admit --index "$PALS_FORMAL_INDEX"
export ENROOT_CACHE_PATH="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/enroot-cache"
export ENROOT_DATA_PATH="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/enroot-runtime"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
 --container-mounts="$PALS_REPO:$PALS_REPO:ro,/work/projects/polyullm/xxj/PALS/models/hf:/work/projects/polyullm/xxj/PALS/models/hf:ro,/work/projects/polyullm/xxj/PALS/runs/20261009-llama-fivebench-e1e2-v2:/work/projects/polyullm/xxj/PALS/runs/20261009-llama-fivebench-e1e2-v2:ro,/work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-eos-recovery-v1:/work/projects/polyullm/xxj/PALS/runs/20261009-llama-e2-eos-recovery-v1:ro,$PALS_FORMAL_ROOT:$PALS_FORMAL_ROOT:rw" \
 --container-workdir="$PALS_FORMAL_ROOT" --container-remap-root --no-container-mount-home \
 bash -c '
set -euo pipefail
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/tmp" HF_HOME="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/hf" HF_MODULES_CACHE="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/hf-modules" XDG_CACHE_HOME="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/cache" CUDA_CACHE_PATH="$PALS_FORMAL_ROOT/scratch/$PALS_FORMAL_INDEX/cuda"
args=("$PALS_FORMAL_ACTION" --root "$PALS_FORMAL_ROOT")
if test "$PALS_FORMAL_BENCH" != none; then args+=(--benchmark "$PALS_FORMAL_BENCH"); fi
python3 -m pals_validation.recovery.e2_formal "${args[@]}"
'
