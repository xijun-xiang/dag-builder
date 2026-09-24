#!/bin/zsh

set -u

project="${DAG_BUILDER_ROOT:-${0:A:h:h}}"
key_file="$project/.secrets/judge_api_key"
log_root="$project/data/logs/full-dag-v2"
controller_log="$log_root/three-lanes-controller.log"
workers=4

mkdir -p "$log_root"

record() {
  print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$controller_log"
}

run_one() {
  local name="$1"
  local root="$2"
  local config="$3"
  local log="$log_root/$name.log"
  local code=0
  record "START name=$name root=$root workers=$workers"
  "$project/.venv/bin/dag-builder" run \
    --root "$root" \
    --config "$config" \
    --key-file "$key_file" \
    --workers "$workers" \
    --resilient \
    --isolate-uncertain-failures \
    --retry-safe-failures > "$log" 2>&1 || code=$?
  record "END name=$name exit_code=$code log=$log"
}

lane_gsm8k() {
  run_one gsm8k-lane "$project/outputs/dag-v2-resume-20260922/gsm8k" "$project/configs/gsm8k-v2-full.json"
}

lane_mmlu_math() {
  run_one mmlu-abstract-algebra-lane "$project/outputs/dag-v2-resume-20260922/abstract_algebra" "$project/configs/mmlu-thinking-v2-full.json"
  run_one mmlu-college-mathematics-lane "$project/outputs/dag-v2-resume-20260922/college_mathematics" "$project/configs/mmlu-thinking-v2-full.json"
  run_one mmlu-elementary-mathematics-lane "$project/outputs/dag-v2-resume-20260922/elementary_mathematics" "$project/configs/mmlu-thinking-v2-full.json"
  run_one mmlu-high-school-mathematics-lane "$project/outputs/dag-v2-resume-20260922/high_school_mathematics" "$project/configs/mmlu-thinking-v2-full.json"
  run_one mmlu-high-school-statistics-lane "$project/outputs/dag-v2-resume-20260922/high_school_statistics" "$project/configs/mmlu-thinking-v2-full.json"
}

lane_mmlu_social() {
  run_one mmlu-high-school-psychology-lane "$project/outputs/dag-v2-resume-20260922/high_school_psychology" "$project/configs/mmlu-thinking-v2-full.json"
  run_one mmlu-professional-psychology-lane "$project/outputs/dag-v2-resume-20260922/professional_psychology" "$project/configs/mmlu-thinking-v2-full.json"
  run_one mmlu-sociology-lane "$project/outputs/dag-v2-resume-20260922/sociology" "$project/configs/mmlu-thinking-v2-full.json"
  run_one mmlu-human-sexuality-lane "$project/outputs/dag-v2-resume-20260922/human_sexuality" "$project/configs/mmlu-thinking-v2-full.json"
}

record "THREE_LANES_START total_workers=12"
lane_gsm8k &
gsm_pid=$!
lane_mmlu_math &
math_pid=$!
lane_mmlu_social &
social_pid=$!

wait "$gsm_pid"
wait "$math_pid"
wait "$social_pid"
record "THREE_LANES_END"
