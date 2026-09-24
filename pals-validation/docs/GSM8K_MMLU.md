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

For the 2026-09-24 coworker delivery, `scripts/freeze_coworker_cohort.py`
consumes the immutable mechanical audit and a **pre-score**, experiment-specific
decision file:

```json
{
  "protocol": "score_blind_experiment_exclusions_v2",
  "excluded_source_ids": {
    "e1": {"openai/gsm8k:main:test:9": "reviewed dependency is incomplete"},
    "e2": {}
  }
}
```

An E1-only defect must not silently exclude an otherwise eligible E2 anchor.
The legacy `score_blind_semantic_exclusions_v1` flat map remains readable and
applies each named exclusion to **both** arms, exactly as before. Unknown IDs,
missing arms, blank reasons, duplicate JSON keys, mismatched audit hashes, and
incomplete certified candidate sets fail before an output directory is created.
Create the output's parent directory in the private artifact area first; the
freezer creates a new `0700` release directory with `0600` files and refuses
to overwrite an existing release.

The freezer replays the existing seeded forest and `forest_break` operator on
validated **non-answer** steps. The E1 *primary* file `e1-<package>.jsonl`
retains a mechanically fair, semantically unexcluded case only when the
selected inverted edge's parent has `kind=derived` or `kind=knowledge` and
`source_field=solution`. A selected parent marked `question` or `choice_*` is
already visible in the scoring prompt, so its inversion is not a clean test of
lost process information. No edge or operator is replaced to rescue that case.
`e1-mechanical-secondary-<package>.jsonl` separately preserves all E1
mechanical candidates remaining after E1 semantic exclusions for descriptive
sensitivity analysis; it is **not** an independent confirmatory cohort.
`e2-<package>.jsonl` uses only the E2-specific exclusions and its existing
anchor rule. An empty file is recorded with count zero and a SHA-256; it is
not silently substituted with another case and cannot be passed to `prepare`
as a nonempty formal cohort.

The immutable-row outputs are accompanied by `flow_1539.jsonl`, with each
delivered ID's mechanical flags, selected E1 parent provenance, primary-rule
result, per-arm exclusion reason, and final E1/E2 decision. The manifest pins
the decision file and audit hashes and labels the new frozen protocol v2. This
is a score-blind *structural proxy*: it does not prove semantic validity or
human approval, and a solution-derived sentence may still repeat the question.
All frozen files remain model-reviewed *synthetic reference* DAGs, not human
or official gold. The original benchmark-to-delivery selection flow is absent
and must be reported as unavailable unless obtained independently.
