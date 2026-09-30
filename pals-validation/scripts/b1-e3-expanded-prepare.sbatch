#!/usr/bin/env bash
#SBATCH --job-name=pals-e3-expand-freeze
#SBATCH --partition=defq
#SBATCH --reservation=evaluation
#SBATCH --no-requeue
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=48G
#SBATCH --time=01:00:00
set -euo pipefail
umask 077
: "${PALS_REPO:?}"
: "${PALS_EXPANSION:?}"
: "${PALS_SOURCE_ROOT:?}"
: "${PALS_PREP_RUN:?}"
case "$PALS_REPO" in /work/projects/polyullm/xxj/PALS/src/*/pals-validation) ;; *) exit 64 ;; esac
for path in "$PALS_SOURCE_ROOT" "$PALS_PREP_RUN"; do
  case "$path" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
  test "$(realpath "$path")" = "$path"
done
case "$PALS_EXPANSION" in /work/projects/polyullm/xxj/PALS/runs/*) ;; *) exit 64 ;; esac
test "$(realpath "$PALS_REPO")" = "$PALS_REPO"
test ! -e "$PALS_EXPANSION"
test ! -L "$PALS_EXPANSION"
test "$(dirname "$PALS_EXPANSION")" = /work/projects/polyullm/xxj/PALS/runs
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
export PATH=/cm/local/apps/slurm/current/bin:$PATH
export ENROOT_CACHE_PATH="$PALS_PREP_RUN/scratch/enroot-cache"
export ENROOT_DATA_PATH="$PALS_PREP_RUN/scratch/enroot-data"
export ENROOT_RUNTIME_PATH="$PALS_PREP_RUN/scratch/enroot-runtime"
mkdir -p "$ENROOT_CACHE_PATH" "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH"
cd "$PALS_PREP_RUN"
# No model/candidate execution: only source checks, hashes, selection and raw copy.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PALS_REPO/src" python3 -m pals_validation.e3.expanded prepare \
  --source-root "$PALS_SOURCE_ROOT" --root "$PALS_EXPANSION" --mmlu-count 100
