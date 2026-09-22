#!/bin/bash
#SBATCH --job-name=pals-lcb-ref5
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:40:00
#SBATCH --output=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-reference-canary-v1/logs/%j.out
#SBATCH --error=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-reference-canary-v1/logs/%j.err
set -euo pipefail
umask 077
LCB_RUN=/work/projects/polyullm/xxj/PALS/runs/20260922-livecodebench-v6-reference-canary-v1
test "$(realpath "$LCB_RUN")" = "$LCB_RUN"
cd "$LCB_RUN"
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$LCB_RUN/scratch"
if test ! -e execution-input.json; then
  # Restore the exact frozen JSON bytes on the compute node, never overwrite.
  test -f execution-input.json.gz
  gzip -dk execution-input.json.gz
fi
/usr/bin/python3 -B verify_livecodebench_reference.py --root "$LCB_RUN"
