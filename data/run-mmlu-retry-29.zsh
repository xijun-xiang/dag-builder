#!/bin/zsh
set -u
project="${DAG_BUILDER_ROOT:-${0:A:h:h}}"
root="$project/outputs/dag-v2-resume-20260922/mmlu-retry-29-20260923"
config="$project/configs/mmlu-thinking-v2-full.json"
key_file="$project/.secrets/judge_api_key"
log_root="$project/data/logs/mmlu-retry-29"
mkdir -p "$log_root"
run_one() {
  local name="$1" code=0
  print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] START name=$name workers=12" >> "$log_root/controller.log"
  "$project/.venv/bin/dag-builder" run --root "$root/$name" --config "$config" --key-file "$key_file" --workers 12 --resilient --isolate-uncertain-failures --retry-safe-failures > "$log_root/$name.log" 2>&1 || code=$?
  local results=$(find "$root/$name/items" -name result.json -type f | wc -l | tr -d ' ')
  print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] END name=$name exit_code=$code results=$results" >> "$log_root/controller.log"
}
print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] RETRY_29_START" >> "$log_root/controller.log"
run_one elementary_mathematics
run_one high_school_psychology
print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] RETRY_29_END" >> "$log_root/controller.log"
