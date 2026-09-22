#!/bin/bash
#SBATCH --job-name=pals-calibri-ref4
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:20:00
#SBATCH --output=/work/projects/polyullm/xxj/PALS/runs/20260922-calibri-lcb-reference4-v1/logs/%j.out
#SBATCH --error=/work/projects/polyullm/xxj/PALS/runs/20260922-calibri-lcb-reference4-v1/logs/%j.err
set -euo pipefail
umask 077
CALIBRI_RUN=/work/projects/polyullm/xxj/PALS/runs/20260922-calibri-lcb-reference4-v1
test "$(realpath "$CALIBRI_RUN")" = "$CALIBRI_RUN"
cd "$CALIBRI_RUN"
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$CALIBRI_RUN/scratch"
test ! -e execution-input.json
gzip -dk execution-input.json.gz
/usr/bin/python3 -B verify_livecodebench_reference.py --root "$CALIBRI_RUN"
