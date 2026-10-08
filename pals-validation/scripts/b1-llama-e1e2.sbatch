#!/usr/bin/env bash
# Resources, reservation, exclusion, logs and serial dependencies supplied by controller.
set -euo pipefail
umask 077
: "${PALS_REPO:?}" "${PALS_CAMPAIGN_ROOT:?}" "${PALS_BENCHMARK:?}" "${PALS_EXPERIMENT:?}"
test "$PALS_CAMPAIGN_ROOT" = /work/projects/polyullm/xxj/PALS/runs/20261008-llama-fivebench-e1e2-v1
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*/pals-validation) ;; *) exit 64 ;; esac
case "$PALS_BENCHMARK" in gpqa|humaneval|gsm8k|livecodebench|mmlu) ;; *) exit 64 ;; esac
case "$PALS_EXPERIMENT" in e1|e2) ;; *) exit 64 ;; esac
test "${SLURM_GPUS_ON_NODE:-0}" = 8
export PALS_TARGET="$PALS_CAMPAIGN_ROOT/$PALS_BENCHMARK/$PALS_EXPERIMENT/llama3-8b"
for p in "$PALS_REPO" "$PALS_CAMPAIGN_ROOT" "$PALS_TARGET"; do test "$(realpath "$p")" = "$p"; done
export ENROOT_CACHE_PATH="$PALS_TARGET/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_TARGET/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_TARGET/scratch/enroot-runtime"
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
cd "$PALS_TARGET"
srun --container-image=/lustre/projects/polyullm/container/lmsysorg+sglang+v0.5.12+infix+v6.sqsh \
 --container-mounts="$PALS_REPO:$PALS_REPO:ro,/work/projects/polyullm/xxj/PALS/models/hf:/work/projects/polyullm/xxj/PALS/models/hf:ro,/work/projects/polyullm/xxj/PALS/runs/20261003-internlm-fivebench-e1e2-v1/spec.json:/work/projects/polyullm/xxj/PALS/runs/20261003-internlm-fivebench-e1e2-v1/spec.json:ro,$PALS_CAMPAIGN_ROOT:$PALS_CAMPAIGN_ROOT:rw" \
 --container-workdir="$PALS_TARGET" --container-remap-root --no-container-mount-home \
 bash -c '
set -euo pipefail
umask 077
export PYTHONPATH="$PALS_REPO/src" PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4
export TMPDIR="$PALS_TARGET/scratch/tmp" HF_HOME="$PALS_TARGET/scratch/hf" HF_MODULES_CACHE="$PALS_TARGET/scratch/hf-modules" XDG_CACHE_HOME="$PALS_TARGET/scratch/cache" CUDA_CACHE_PATH="$PALS_TARGET/scratch/cuda"
python3 "$PALS_REPO/scripts/e1e2_llama_campaign.py" runtime --benchmark "$PALS_BENCHMARK" --experiment "$PALS_EXPERIMENT"
'
