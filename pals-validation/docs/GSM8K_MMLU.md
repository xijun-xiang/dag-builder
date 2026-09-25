# GSM8K / MMLU unified adapter contract

This adapter accepts `pals_dag_unified_v1` records only. It does not repair nodes, infer
missing edges, change source answers, or declare model-reviewed DAGs to be human gold.

- `--benchmark gsm8k`: `openai/gsm8k`, `main`, `test`, open-answer text, no choices.
  The question alone enters the prompt; the answer node and answer value do not.
- `--benchmark mmlu`: `cais/mmlu`, `test`, one of the 57 official subject
  subsets named in `unified.py`, four labeled choices. The
  `answer.value` field and terminal answer node do not enter the prompt. Some
  source DAGs nevertheless mention the correct option in nonterminal steps;
  cohort review must flag this separately rather than claiming answer-blind CoT.
  The 57-subject construction/export protocol is documented in
  `../../docs/mmlu-all-subjects.md`; accepting all IDs in this reader does not
  mean that their DAGs have been constructed or semantically reviewed.
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

## Narrow MMLU psychology E1 break-edge override

The preceding primary rule describes the *default* freezer/operator. A separate
score-blind review found six psychology/social records whose default broken
edge was weak or already present in the question/choices but another **existing
adjacent direct solution edge** in the same forest baseline was usable. The
optional `prepare --e1-break-overrides FILE.json` protocol is restricted to a
manifest-pinned MMLU psychology/social cohort; it is not a general mechanism
for selecting the largest PALS effect.

The JSON schema `pals_mmlu_psych_e1_break_overrides_v1` has exactly
`schema_version`, `benchmark` (`mmlu`), `selection_seed`, `cohort`, and
`overrides`. Every `cohort` entry has `item_id`, `source_id`, and the original
`source_record_sha256`; every override has `item_id`, `parent_id`, and
`target_id`. The cohort must exactly match the prepared source, with unique
identities. The six overrides are a subset of the cohort and may only replace
`forest_break` by swapping an adjacent parent→target pair after the fixed first
node. The pair must be an existing source-DAG direct edge, the parent must be
solution-derived or knowledge, and the swap must invert exactly that edge.
Source DAG, baseline, legal order, original break, and E2 anchor remain
unchanged. A hash mismatch or unused override fails preparation.

Pass the **same JSON** first to `freeze_coworker_cohort.py
--e1-break-overrides` and then to `pals_validation.cli prepare
--e1-break-overrides` for the frozen `e1-mmlu_psych_social.jsonl`. With this
option, freeze emits `coworker-pals-frozen-synthetic-cohort-v3-psych-e1-overrides`:
only manifest-listed psychology/social E1 questions can enter the primary
cohort; all other psychology/social fair pairs remain secondary or excluded.
GSM8K, MMLU math, and E2 retain their usual selection rules. Preparation
checks the frozen release file hash and selection seed for every E1/E2 cohort,
records the release manifest hash and source experiment, and refuses an E1 run
from an E2 cohort or vice versa. It also checks the override hash and refuses
the psychology E1 file if the override JSON is missing or different. This
protection applies to the release file in its original directory alongside
`manifest.json`; do not detach or rename it before preparing.

Prepared `selection.json` preserves the default breaking choice in
`forest_break_before_override` for each changed question. The prepared
manifest marks `+mmlu-psych-e1-break-overrides-v1` and records hashes of the
source cohort, override manifest, outputs, and selection-related code. Omitting
the flag leaves all existing behavior and protocol names unchanged. The
reviewed 11-question cohort and its six override decisions are recorded in
the private experiment artifacts; the Git repository does not include private
questions or model-reviewed source rows. This small cohort is a diagnostic E1
subset, not a claim that all 594 psychology/social source questions passed.
