#!/bin/zsh

set -u

project="${DAG_BUILDER_ROOT:-${0:A:h:h}}"
root="$project/outputs/dag-v2-resume-20260922"
config="$project/configs/mmlu-thinking-v2-full.json"
key_file="$project/.secrets/judge_api_key"
log_root="$project/data/logs/mmlu-math-then-psych-12"
controller_log="$log_root/controller.log"
workers=12

mkdir -p "$log_root"

record() {
  print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$controller_log"
}

run_one() {
  local name="$1"
  local dataset_root="$root/$name"
  local log="$log_root/$name.log"
  local total results code=0

  total=$(jq 'length' "$dataset_root/items.json")
  results=$(find "$dataset_root/items" -name result.json -type f | wc -l | tr -d ' ')
  if [[ "$results" -eq "$total" ]]; then
    record "SKIP_COMPLETE name=$name results=$results total=$total"
    return 0
  fi

  record "START name=$name results=$results total=$total workers=$workers"
  "$project/.venv/bin/dag-builder" run \
    --root "$dataset_root" \
    --config "$config" \
    --key-file "$key_file" \
    --workers "$workers" \
    --resilient \
    --isolate-uncertain-failures \
    --retry-safe-failures > "$log" 2>&1 || code=$?
  results=$(find "$dataset_root/items" -name result.json -type f | wc -l | tr -d ' ')
  record "END name=$name exit_code=$code results=$results total=$total log=$log"
}

record "RUN_START order=math_then_psych workers=$workers"

for name in \
  abstract_algebra \
  college_mathematics \
  elementary_mathematics \
  high_school_mathematics \
  high_school_statistics \
  high_school_psychology \
  professional_psychology \
  sociology \
  human_sexuality
do
  run_one "$name"
done

record "RUN_END"
