# One-round GPQA DAG reassessment and restricted repair

For the separately versioned two-round revision loop and model migration, see
[REVISION.md](REVISION.md). The one-round protocol below remains unchanged.

This is a separate post-processing run, not a change to the original construction
protocol. It repairs the **representation of a fixed official explanation**, not
the explanation itself. No production API call is implied by preparing a cohort.

## Flow and boundaries

1. `prepare-repair` reads a finished first-pass GPQA batch under a shared lock.
   It copies all terminal `needs_review` and `rejected` records to a separate
   private root, preserving original requests, responses, validation failures and
   parsed outputs with hashes. Accepted candidates and transport-incomplete items
   are excluded. The original root is not written. Running batches cannot be used.
2. A graph rejected **only for forward parent numbering** first receives a stable
   topological sort. This changes IDs/order only, preserving every statement and
   edge. Cycles, unknown/self references, bad quotes and disconnected graphs cannot
   pass this route; there is no automatic pruning or synthetic edge insertion.
3. Other cases receive one model reassessment. The model can uphold or overturn the
   old verdict, propose a source-grounded candidate, flag uncertainty, or identify
   an unrepairable source gap/error. Every proposed edit needs an explanation.
   Existing assertions can be faithfully split/merged, redundant background removed,
   real dependencies restored and quotes corrected. Essential content cannot be
   removed, missing scientific premises cannot be invented, and tentative claims
   cannot be strengthened to certainty. If the first-pass source audit stopped
   before nodes existed, the same one call may construct a candidate from the
   unchanged reference after explaining why the old rejection was mistaken.
4. The same strict node/quote/parent/closure checks apply to the candidate. An invalid
   semantic response stops that item; it does not trigger another repair request.
5. Justifications are generated anew for the candidate, never copied from a stale
   graph. A separate final call receives only the official source and candidate
   graph/justifications—not the prior rejection, repair rationale or verdict.
   All six original DAG checks plus source suitability, essential source reasoning
   preservation and epistemic-strength preservation must pass.

New context is **not an independent model**. Default construction and audit both
use DeepSeek v4 Flash. Semantic source faithfulness is model-checked, not formally
proved; quotes and acyclicity cannot guarantee scientific correctness. Human review
remains required for release. Repairs are not evidence of PALS validity.

## Statuses

| Status | Meaning |
|---|---|
| `repaired_model_accepted` | All candidate gates and fresh-context audit passed; pending human review |
| `repair_needs_review` | Uncertainty, malformed proposal, structural failure or uncertain final audit |
| `repair_rejected` | Final audit explicitly rejects the candidate |
| `repair_unrepairable` | Reassessment identifies a source issue that cannot be fixed within the unchanged reference |
| `paused` | Transport attempts exhausted, budget/authentication issue, or other infrastructure pause; not scientific rejection |

No task automatically turns these statuses into gold. First-pass and repaired
acceptance remain separate; do not hide initial failures or report only successful
repairs. Same-model "unrepairable" is also an audit opinion, not expert ground truth.

## Commands

```sh
dag-builder prepare-repair --source-root /absolute/private/finished-first-pass --root /absolute/private/repair-round1
dag-builder repair --root /absolute/private/repair-round1 --config configs/gpqa-repair.json --key-file /absolute/private/key --resilient
dag-builder report --root /absolute/private/repair-round1
```

For a detached local process, the existing `scripts/run_private_pilot.py` launcher
selects RepairPipeline when its config has `prompt_version: gpqa-repair-v1`. It
freezes the code before launch. `prepare-repair` makes no network requests; `repair`
and the worker launcher make paid calls. Run the prepared cohort only when intended.

The nominal cost is at most three successful calls per item: proposal,
justification, audit. The pure topological route skips the proposal (two calls).
Unrepairable/uncertain proposals stop after one. Transport retries are separately
bounded at four lifetime attempts per stage in resilient mode; they may incur
duplicate billing. The config caps total attempts at 240 and conservative reserved
tokens at 14M. Actual consumption and unknown usage are tracked independently.

## Provenance and restart rules

- `originals/`: frozen copies of original records and run metadata.
- `items/<ID>/baseline.json`: original verdict and available/failed stage content.
- `topology.json`: exact ID mapping, or reason automatic sorting was inapplicable.
- `repair/`: raw model reassessment request, response and validation.
- `candidate.json`, `repair_audit.json`: candidate, complete before/after records,
  declared edits, route and hashes. The original Explanation is never overwritten.
- `justify/`, `review_repair/`: new explanation and final audit, with isolated inputs.
- `dag.json`: written only for repaired_model_accepted; explicitly marked repaired.
- `result.json`, `invocations/`, `reports/`: per-item outcomes, infrastructure pauses,
  bounded request accounting and private human-readable review.
- `status_events/<ID>/`: immutable operational pause events, recorded before live
  progress is rendered. These are not terminal scientific verdicts and do not
  prevent resuming. New stage artifacts invalidate the recorded pause; a final
  `result.json` takes precedence. Reports can also recover pauses from completed
  invocations in older runs, provided stored attempt timestamps show no newer
  work. Without sufficient evidence the report says `incomplete`, not `running`.
  Reporting never changes a verdict, creates a retry, or modifies old artifacts.

Resuming the same repair root reuses the first semantic responses and restores the
budget; it never launches a second semantic repair. A repair run cannot itself be
used as input to another prepare-repair call. Do not create repeated sibling roots
to cherry-pick passing repairs. Further protocol versions need an explicit research
decision and separate accounting, not an automatic retry loop.

For an explicitly approved concurrency change, stop and identify the old worker
before calling `continuation.prepare_continuation(source, destination, workers=6)`.
The source run lock must be free. A new sibling directory contains only unfinished,
non-paused items and byte-identical cached attempts. Terminal semantic failures are
not selected again. Unknown interrupted requests remain unknown and count against
both the stage's four-attempt limit and the inherited budget. The new run's budget
subtracts all old requests outside its selection, while its imported requests are
counted by the normal budget restore. This changes execution concurrency, not the
scientific protocol. Six workers is the explicit upper bound; the default is two.
Launch the prepared directory normally and merge results by item ID over its source
run, never by adding cohort sizes. `continuation.json` records the source, copied
hashes, imported attempts and budget deductions. Do not modify a frozen config or
reset exhausted retry windows to accelerate a run.

Local macOS launchers prevent idle sleep only; closing the lid can still suspend
requests. Long unattended work requires a continuously awake host. More workers
do not compensate for a sleeping machine.

Release still requires explicit human decisions tied to exact DAG hashes. Exported
repair trajectories carry `repaired:` variants and manifests count repaired DAGs
separately. The existing MMLU, GSM8K and first-pass GPQA routes are unchanged.
