#!/bin/zsh

set -u

project="${DAG_BUILDER_ROOT:-${0:A:h:h}}"
key_file="$project/.secrets/judge_api_key"
log_root="$project/data/logs/full-dag-v2"
controller_log="$log_root/controller.log"

mkdir -p "$log_root"

record() {
  print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$controller_log"
}

run_one() {
  local name="$1"
  local root="$2"
  local config="$3"
  local log="$log_root/$name.log"

  record "START name=$name root=$root config=$config"
  local code=0
  "$project/.venv/bin/dag-builder" run \
      --root "$root" \
      --config "$config" \
      --key-file "$key_file" \
      --workers 6 \
      --resilient > "$log" 2>&1 || code=$?
  record "END name=$name exit_code=$code log=$log"
  return 0
}

record "FULL_RUN_CONTROLLER_START"

run_one gsm8k "$project/outputs/gsm8k-test-main-740312ad" "$project/configs/gsm8k-v2-full.json" || exit $?
run_one mmlu-abstract-algebra "$project/outputs/mmlu-math-v1/abstract_algebra" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-college-mathematics "$project/outputs/mmlu-math-v1/college_mathematics" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-elementary-mathematics "$project/outputs/mmlu-math-v1/elementary_mathematics" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-high-school-mathematics "$project/outputs/mmlu-math-v1/high_school_mathematics" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-high-school-statistics "$project/outputs/mmlu-math-v1/high_school_statistics" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-high-school-psychology "$project/outputs/mmlu-psych-social-v1/high_school_psychology" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-professional-psychology "$project/outputs/mmlu-psych-social-v1/professional_psychology" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-sociology "$project/outputs/mmlu-psych-social-v1/sociology" "$project/configs/mmlu-thinking-v2-full.json" || exit $?
run_one mmlu-human-sexuality "$project/outputs/mmlu-psych-social-v1/human_sexuality" "$project/configs/mmlu-thinking-v2-full.json" || exit $?

record "FULL_RUN_CONTROLLER_END"
