# Native reasoning rerun: fixed 30 MMLU physics questions

## Protocol fixed before paid execution

This is an engineering rerun of the exact 30 IDs selected in `pilot-json-v2`,
not a new sample, a benchmark score, or a causal thinking-mode ablation. Source
Parquet SHA256 must remain `48fc84060e4cd032a407da09164e99dfdf81f3ce345bf6c1871bb539ba4ead6c`;
revision `c30699e8356da336a370243923dbaf21066bb9fe`, subset
`high_school_physics/test`, seed 20260909. No replacements, label corrections,
content repair or automatic retries. Known questionable source rows remain in scope.

Reuse the GSM8K contribution's task dispatch, answer gate, source snapshot import,
shared reports and release path. Existing MMLU v1 and GSM8K protocols are unchanged.
The native profile adds one stage:

`solve -> structure_solution -> review_solution -> atomize -> dependencies -> justify -> review_dag`

- All stages use explicit `thinking=enabled`, `reasoning_effort=high` with the
  exact configured model `deepseek-v4-flash` at the authorized HTTPS proxy.
  Temperature is omitted because official thinking mode ignores it.
- Solve receives explicit A/B/C/D labels, no gold answer, no JSON requirement,
  no difficulty task. Require nonempty `message.reasoning_content` and a unique
  terminal `Final answer: X` line in `message.content`. Never fall back to the
  final explanation when the native field is absent. Raw fields are preserved.
- Wrong final labels stop before structuring. Structuring only organizes the
  already-generated reasoning, estimates difficulty, and cannot change the answer.
  It receives no gold. It may remove explicitly abandoned/corrected branches, not
  add absent reasoning or fix unresolved errors. The semantic reviewers inspect
  both native text and the structured solution. Their judgments remain fallible.
- The common node, dependency and justification contracts are unchanged. Graphs
  describe the organized reference derivation, not every token in native thought.
- Budget: at most 220 attempts and 2,500,000 conservatively reserved tokens;
  at most 210 calls if all 30 items complete seven stages without retries. Two
  workers, output cap 8192, timeout 180 seconds. This is not a dollar cost guarantee.
- Execute two selected-item solve checks first, reuse their responses in the
  30-item solve phase, then continue the existing stage barriers. Missing native
  fields in the smoke check stops the cohort for interface investigation. Wrong
  answers are data outcomes, not an interface failure.
- Stop on authentication/uncertain transport errors or budget exhaustion. Preserve
  every selected item, failure, raw response, usage and code/config/prompt hash.
- Report native field availability, final-label agreement, each stage's pass/fail
  counts, model-reviewed DAG candidates, usage and missing accounting. No automatic
  human approval, dataset release, or PALS scoring.

Compared with the earlier run, thinking controls, option rendering, solve output
format and the structuring/review protocol all change. Any yield difference is a
whole-protocol observation, not evidence that thinking alone caused improvement.

## Commands

From the repository root, after installing into `.venv`:

```bash
.venv/bin/dag-builder prepare --root /absolute/private/new-run \
  --dataset mmlu --revision c30699e8356da336a370243923dbaf21066bb9fe \
  --source-parquet /absolute/private/old-run/source/original.parquet \
  --subset high_school_physics --split test --count 30 --seed 20260909
.venv/bin/dag-builder run --root /absolute/private/new-run \
  --config configs/mmlu-thinking-30.json --limit 2 --through solve
.venv/bin/dag-builder run --root /absolute/private/new-run \
  --config configs/mmlu-thinking-30.json --through solve
.venv/bin/dag-builder run --root /absolute/private/new-run \
  --config configs/mmlu-thinking-30.json --through review_solution
.venv/bin/dag-builder run --root /absolute/private/new-run \
  --config configs/mmlu-thinking-30.json --through review_dag
.venv/bin/dag-builder report --root /absolute/private/new-run
```

Provide credentials privately through `JUDGE_API_KEY` or `--key-file`. Compare
selected IDs and the exact question/choice/gold fields with the prior run before
the first API call. Adding `task_type=mmlu` metadata does not alter those fields.
