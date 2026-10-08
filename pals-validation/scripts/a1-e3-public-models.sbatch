#!/usr/bin/env bash
#SBATCH --partition=defq
#SBATCH --reservation=pretrain
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --gres=none
#SBATCH --time=04:00:00
#SBATCH --no-requeue
set -euo pipefail
umask 077
: "${PALS_DOWNLOAD_RUN:?}"
case "$PALS_DOWNLOAD_RUN" in /work/projects/polyullm/xxj/pals/runs/*) ;; *) exit 64 ;; esac
test "$(realpath "$PALS_DOWNLOAD_RUN")" = "$PALS_DOWNLOAD_RUN"
test -z "${CUDA_VISIBLE_DEVICES:-}"
test "${SLURM_GPUS_ON_NODE:-0}" = 0
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$PALS_DOWNLOAD_RUN/tmp"
mkdir -p "$TMPDIR"
cd "$PALS_DOWNLOAD_RUN"
python3 "$PALS_DOWNLOAD_RUN/e3_public_models.py" download "$PALS_DOWNLOAD_RUN"
