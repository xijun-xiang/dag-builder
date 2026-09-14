# Approved pilot protocol — v1

## Objective

Exercise an API-only, auditable source → worked-solution → reviewed-DAG pipeline.
All generation, solution review and DAG annotation use `deepseek-v4-flash` at
`https://proxy.infix-ai.xyz/v1/`. No local small-model generation, GPU, B1 access,
PALS calculation, destructive operators, or automatic data release in this phase.

## Fixed design

- Source: `cais/mmlu`, revision `c30699e8356da336a370243923dbaf21066bb9fe`.
- Subset/split: `high_school_physics` / `test`.
- Candidate sample: 30 without replacement; seed 20260909; no replenishment.
- All 30 are engineering/development data, not an independent confirmatory set.
- Unit of accounting: source question, not model call or individual step.
- Generation is blind to the reference option. An answer mismatch is recorded and
  not repaired. Matching an option still requires explicit reasoning review.
- Difficulty is a subjective model estimate; it does not determine acceptance.
- Structure checks include typed references, earlier parents and sink reachability.
  No edge is invented just to force closure. Insufficient traces are retained as
  unsuccessful outcomes rather than expanded into better synthetic solutions.
- Same-model semantic review is not independent evidence. Human approval is an
  explicit separate gate. Universal automatic physics verification is not claimed.

## Execution dependencies and stop rules

1. Implement/test locally with fake fixtures only; requires no credential or GPU.
2. Pin source and selection; preserve hashes and all pre-score exclusions.
3. Obtain the authorized API key locally; no script-triggered B1 connection.
4. Probe model ID, then generate only the first two selected items as a smoke test.
   Check actual model identifier, response/usage shape, latency, output cap and cost
   accounting before invoking the other 28.
5. Finish generation for the cohort; then review solutions; then annotate accepted
   traces. This order is implemented with explicit `--through` barriers.
6. Inspect all outcomes, generate human-review reports; do not publish data merely
   because the API requests completed.

Owner: this development task, with the user supplying credentials and human review.
Elapsed-time/cost estimates for real API work remain unavailable until step 4.
There is no deadline-based expansion or automatic resubmission. Limits are in the
versioned config. Source ambiguity, API incompatibility, quota limits and annotation
failures are reported separately; they must not be hidden by replacement questions.

## Required report

- Selected/generated/answer-matched/reviewed/model-accepted/released counts.
- Every rejected, incomplete or uncertain record and the exact stage/reason.
- Original CoT, all stage outputs and the full candidate DAG for inspection.
- Exact executed commands, code revision/hashes, model/config, start/end times,
  available token usage, raw artifacts and source provenance.
- No inferential statistics or claim that PALS is validated: no PALS experiment is
  part of this pilot. Later validation uses frozen data and a separately approved
  protocol, never construction feedback based on favorable PALS scores.

## Local layout

- Checkout: `/absolute/path/to/da-g-builder`
- Branch: `feat/mmlu-dag-builder`; base `64c032935bf1e5aef10e6a027897e4c5d8e4bb2b`.
- Virtual environment: `<checkout>/.venv`
- Pilot artifacts: `<checkout>/outputs/pilot-v1` (Git-ignored).
- Credentials are external to source and artifacts; use a private key file or
  the configured environment variable. No credential helper is distributed.

This standalone repository is not an AI Research OS registered project. Its local
manifests provide provenance; no Research OS audit pass or verified-claim status is
claimed. Scientific findings remain unavailable until actual data construction and
subsequent human review have occurred.
