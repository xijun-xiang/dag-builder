#!/bin/zsh

set -u

project="${DAG_BUILDER_ROOT:-${0:A:h:h}}"
key_file="$project/.secrets/judge_api_key"
log_root="$project/data/logs/full-dag-v2"
controller_log="$log_root/controller.log"
workers="${DAG_RUNTIME_WORKERS:-6}"

mkdir -p "$log_root"

record() {
  print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$controller_log"
}

clone_for_resume() {
  local source="$1"
  local destination="$2"
  if [[ -e "$destination/items.json" ]]; then
    return 0
  fi
  mkdir -p -m 700 "$destination"
  rsync -a \
    --exclude=.lock \
    --exclude=implementation.json \
    --exclude=run_config.json \
    --exclude=policies \
    --exclude=invocations \
    --exclude=status_events \
    "$source/" "$destination/"
  mkdir -p -m 700 "$destination/policies" "$destination/invocations"
}

run_one() {
  local name="$1"
  local root="$2"
  local config="$3"
  local log="$log_root/$name.log"
  local code=0
  record "START name=$name root=$root config=$config workers=$workers"
  "$project/.venv/bin/dag-builder" run \
    --root "$root" \
    --config "$config" \
    --key-file "$key_file" \
    --workers "$workers" \
    --resilient > "$log" 2>&1 || code=$?
  record "END name=$name exit_code=$code log=$log"
}

record "SEQUENTIAL_FULL_RUN_START"
clone_for_resume "$project/outputs/gsm8k-test-main-740312ad" "$project/outputs/dag-v2-sequential/gsm8k"
clone_for_resume "$project/outputs/mmlu-math-v1/abstract_algebra" "$project/outputs/dag-v2-sequential/mmlu-abstract-algebra"

run_one gsm8k-sequential "$project/outputs/dag-v2-sequential/gsm8k" "$project/configs/gsm8k-v2-full.json"
run_one mmlu-abstract-algebra-sequential "$project/outputs/dag-v2-sequential/mmlu-abstract-algebra" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-college-mathematics "$project/outputs/mmlu-math-v1/college_mathematics" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-elementary-mathematics "$project/outputs/mmlu-math-v1/elementary_mathematics" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-high-school-mathematics "$project/outputs/mmlu-math-v1/high_school_mathematics" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-high-school-statistics "$project/outputs/mmlu-math-v1/high_school_statistics" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-high-school-psychology "$project/outputs/mmlu-psych-social-v1/high_school_psychology" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-professional-psychology "$project/outputs/mmlu-psych-social-v1/professional_psychology" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-sociology "$project/outputs/mmlu-psych-social-v1/sociology" "$project/configs/mmlu-thinking-v2-full.json"
run_one mmlu-human-sexuality "$project/outputs/mmlu-psych-social-v1/human_sexuality" "$project/configs/mmlu-thinking-v2-full.json"
record "SEQUENTIAL_FULL_RUN_END"
