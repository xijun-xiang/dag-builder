#!/bin/bash
#SBATCH --job-name=pals-t2ance-7
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=00:20:00
#SBATCH --output=/work/projects/polyullm/xxj/PALS/runs/20260924-t2ance-lcb-reference7-clean-v1/logs/%j.out
#SBATCH --error=/work/projects/polyullm/xxj/PALS/runs/20260924-t2ance-lcb-reference7-clean-v1/logs/%j.err
set -euo pipefail
umask 077

PALS_RUN=/work/projects/polyullm/xxj/PALS/runs/20260924-t2ance-lcb-reference7-clean-v1
PALS_SOURCE=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-source-canary-v1/prepared/source/test6.jsonl
test "$(realpath "$PALS_RUN")" = "$PALS_RUN"
test "$(realpath "$PALS_SOURCE")" = "$PALS_SOURCE"
cd "$PALS_RUN"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PALS_RUN/scratch" PYTHONPATH="$PALS_RUN/code"
test ! -e "$PALS_RUN/execution-input.json"
/usr/bin/python3 -B scripts/prepare_t2ance_full_execution.py \
  --source "$PALS_RUN/source" --source-file "$PALS_SOURCE" \
  --output "$PALS_RUN" --canary-seven
/usr/bin/python3 -B verify_livecodebench_reference.py --root "$PALS_RUN"
