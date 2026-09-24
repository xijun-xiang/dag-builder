# GSM8K / MMLU unified adapter contract

This adapter accepts `pals_dag_unified_v1` records only. It does not repair nodes, infer
missing edges, change source answers, or declare model-reviewed DAGs to be human gold.

- `--benchmark gsm8k`: `openai/gsm8k`, `main`, `test`, open-answer text, no choices.
  The question alone enters the prompt; the answer node and answer value do not.
- `--benchmark mmlu`: `cais/mmlu`, `test`, one of the five math or four
  psychology/social subsets named in `unified.py`, four labeled choices. The
  `answer.value` field and terminal answer node do not enter the prompt. Some
  source DAGs nevertheless mention the correct option in nonterminal steps;
  cohort review must flag this separately rather than claiming answer-blind CoT.
- Both require a model-accepted record, a matching source ID/row, a valid DAG
  node hash, one excluded answer node at the end, and at least two non-answer
  steps in original topological order. No semantic edge certification is implied.
- E1 and E2 use the existing deterministic graph operators and fixed scoring
  prompt. `gsm8k-unified-validation-v1` and `mmlu-unified-validation-v1` keep
  their results separate from GPQA, HumanEval, and LiveCodeBench protocols.

Prepare each benchmark from a **frozen reviewed cohort JSONL** and pass its
expected SHA-256. Do not pass the coworker archive directly to `prepare`:
ineligible rows, semantic review decisions, and the original denominator must
be recorded separately before any GPU run. The prepared inventory reports E1
fair-pair and E2 eligibility; cases with no applicable intervention are not
positive or negative evidence. Run artifacts remain scientific evidence only
with the `hf` backend; the mock backend is an interface test.
