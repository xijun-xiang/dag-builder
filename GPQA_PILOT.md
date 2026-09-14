# GPQA-Diamond official-reference DAG pilot

## Protocol

The input is the official expert **Explanation**, not freshly generated CoT or
the model's hidden reasoning. The API transforms and audits this fixed reference:

`review_solution → atomize → dependencies → justify → review_dag`

There is no solve/structure_solution request and no answer-accuracy measurement.
DeepSeek v4 Flash handles all five stages through the existing HTTPS proxy. This
is **same-model construction and review**, not independent gold validation.
Official expert provenance does not transfer automatically to synthetic edges.

The pinned upstream ZIP and its SHA256 are defined in `gpqa_source.py`. Keep the
archive, all raw CSV fields, normalized records, selected IDs, field mapping and
checksums. Use a complete Extra Revised bundle when present (9/198 in this
snapshot), otherwise use the complete base bundle. Never mix partial revisions.
Thus this is explicitly a revision-aware reference set, not a claim of byte-level
identity with another GPQA evaluator's default fields. Options are deterministically
permuted; explanations are unchanged. Ambiguous original option-letter references
must be flagged, not silently remapped.

The fixed 30-item development cohort contains 10 Physics, 10 Chemistry and 10
Biology items, selected by seeded hash before API calls. This is domain coverage,
not a representative estimate of the Diamond distribution. Possible missing visual
references, duplicate questions and duplicate choice texts are documented before selection. Failed items are never
replaced. No new difficulty labels are generated; original writer metadata is kept.

## Scientific and engineering gates

- Source suitability review does not repair missing explanations. Missing premises,
  insufficient derivation or unresolved scientific claims remain visible failures.
- Atomic statements retain exact source quotes. The official answer field may
  identify the terminal target only; it cannot be a reasoning premise. Distractors
  are alternatives, not asserted truths. Quote presence is provenance, not proof.
- Parents must be valid earlier nodes. The answer must have every node as an
  ancestor. Disconnected graphs are flagged; no artificial edges or automatic
  pruning are introduced to improve yield.
- Final audit checks faithfulness, sufficiency, minimality and no added facts.
  General scientific/calculation correctness has no independent automated checker.
- Model-accepted DAGs are candidates pending explicit human review, not gold data.
  No PALS scoring or validation claim is made by this construction pilot.

Use the 32,768-token output cap, two concurrent workers and 600-second timeout.
The full clean path costs 150 requests. Budget caps are 240 attempts and 14M
conservative reserved tokens (not actual usage or a currency quote). Resilient
mode permits up to four lifetime attempts per stage for transient transport
failures only. Unknown calls remain billable-unknown. Semantic failures are never
resampled. Authentication, budget and integrity failures safely stop the run.

## Commands

Run from an installed checkout; credentials are read from a private file, never
passed literally or committed. The artifact root must be private and separate
from historical MMLU/GSM8K runs.

```sh
dag-builder prepare-gpqa --root /absolute/private/pilot --count 30 --seed 20260910
dag-builder run --root /absolute/private/pilot --config configs/gpqa-diamond-reference-30.json --key-file /absolute/private/key-file --resilient
dag-builder report --root /absolute/private/pilot
```

Archive source, prompts, code hashes/commit, config, raw requests/responses,
parsed stage outputs, failures, usage and candidate DAGs. Review reports display
the unchanged official Explanation alongside the derived structure. Reports and
benchmark content remain private/local; do not upload public examples. Only code,
synthetic test fixtures and non-content documentation belong in Git.

Report all 30 outcomes: suitability, atomization, dependency closure, justification,
final model audit, failure categories, per-domain yield and known/unknown API usage.
Do not relabel review rejection as a GPQA solving error. Do not compare these yields
directly to historical generated-CoT MMLU batches as a controlled experiment.

## Non-overlapping 50-item extension

Use `prepare-gpqa --count 50 --exclude-root /absolute/private/prior-30` to
exclude **all** prior items, regardless of their outcome. The option ordering,
source snapshot and prompt protocol remain unchanged. Source mismatches fail
preparation. Allocation uses the largest-remainder method, proportional to the
eligible remaining domain populations; within-domain sampling uses the same
seeded hash. This is not the equal allocation used by the initial 30-item pilot.
Report batches and domain composition separately; do not interpret the pooled
acceptance rate as a representative population estimate.

The extension config allows 400 total attempts and 24M conservative reserved
tokens, scaling the initial budget to 50 items. The clean five-stage path needs
250 calls. The four-attempt stage limit and all semantic gates are unchanged.
Keep the extension in a new private root, with its own code snapshot and report.

For optional one-round reassessment after a batch finishes, see [REPAIR.md](REPAIR.md).
It preserves the first-pass run and records repaired candidates separately.
