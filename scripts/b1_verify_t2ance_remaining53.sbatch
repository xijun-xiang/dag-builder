#!/bin/bash
#SBATCH --job-name=pals-t2ance-53
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=01:30:00
#SBATCH --output=/work/projects/polyullm/xxj/PALS/runs/20260925-t2ance-lcb-reference53-v1/logs/%A_%a.out
#SBATCH --error=/work/projects/polyullm/xxj/PALS/runs/20260925-t2ance-lcb-reference53-v1/logs/%A_%a.err
set -euo pipefail
umask 077

PALS_RUN_BASE=/work/projects/polyullm/xxj/PALS/runs/20260925-t2ance-lcb-reference53-v1
PALS_SOURCE=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-source-canary-v1/prepared/source/test6.jsonl
case "${SLURM_ARRAY_TASK_ID:-}" in
  0|1|2|3) ;;
  *) exit 2 ;;
esac
test "$(realpath "$PALS_RUN_BASE")" = "$PALS_RUN_BASE"
test "$(realpath "$PALS_SOURCE")" = "$PALS_SOURCE"
PALS_BATCH="$PALS_RUN_BASE/batch-$SLURM_ARRAY_TASK_ID"
mkdir -m 700 "$PALS_BATCH"
test "$(realpath "$PALS_BATCH")" = "$PALS_BATCH"
cd "$PALS_RUN_BASE"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PALS_BATCH/scratch" PYTHONPATH="$PALS_RUN_BASE/code"
/usr/bin/python3 -B scripts/prepare_t2ance_full_execution.py \
  --source "$PALS_RUN_BASE/source" --source-file "$PALS_SOURCE" \
  --output "$PALS_BATCH" --remaining-batch "$SLURM_ARRAY_TASK_ID"
/usr/bin/python3 -B "$PALS_BATCH/verify_livecodebench_reference.py" --root "$PALS_BATCH"
