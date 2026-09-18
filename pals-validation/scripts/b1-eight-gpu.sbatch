#!/usr/bin/env bash
#SBATCH --job-name=pals-validation
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --gpus-per-task=1
#SBATCH --cpus-per-task=4
#SBATCH --time=04:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
set -euo pipefail
umask 077
: "${PALS_REPO:?Set absolute deployed repository path}"
: "${PALS_RUN:?Set absolute initialized run path}"
: "${PALS_PYTHON:?Set absolute inference-environment python path}"
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*) ;; *) exit 64 ;; esac
case "$PALS_RUN" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
cd "$PALS_REPO"
export PYTHONPATH="$PALS_REPO/src"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
# Slurm binds one GPU to each process. No shell interpolation of task data.
srun --kill-on-bad-exit=1 "$PALS_PYTHON" scripts/slurm_worker.py
