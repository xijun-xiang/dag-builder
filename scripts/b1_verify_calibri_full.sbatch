#!/bin/bash
#SBATCH --job-name=pals-calibri-full
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=01:00:00
#SBATCH --output=/work/projects/polyullm/xxj/PALS/runs/20260923-calibri-lcb-reference93-v1/logs/%j.out
#SBATCH --error=/work/projects/polyullm/xxj/PALS/runs/20260923-calibri-lcb-reference93-v1/logs/%j.err
set -euo pipefail
umask 077
CALIBRI_RUN=/work/projects/polyullm/xxj/PALS/runs/20260923-calibri-lcb-reference93-v1
CALIBRI_SOURCE=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-source-canary-v1/prepared/source/test6.jsonl
test "$(realpath "$CALIBRI_RUN")" = "$CALIBRI_RUN"
test "$(realpath "$CALIBRI_SOURCE")" = "$CALIBRI_SOURCE"
cd "$CALIBRI_RUN"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$CALIBRI_RUN/scratch" PYTHONPATH="$CALIBRI_RUN/code"
test ! -e "$CALIBRI_RUN/execution-input.json"
/usr/bin/python3 -B scripts/prepare_calibri_full_execution.py \
  --candidates candidates.json --manifest calibri-manifest.json \
  --source-file "$CALIBRI_SOURCE" --output "$CALIBRI_RUN"
/usr/bin/python3 -B verify_livecodebench_reference.py --root "$CALIBRI_RUN"
