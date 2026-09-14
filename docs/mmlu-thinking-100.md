# Fixed 100-item candidate production, 32K output budget

User-approved expansion of the native-thinking MMLU construction pilot. This run
changes no generation, structuring, annotation, answer-gate, or review prompts.
It does not implement pruning, repair, another judge, automatic release, or PALS.

- Source: `cais/mmlu`, `high_school_physics/test`, revision
  `c30699e8356da336a370243923dbaf21066bb9fe`.
- Reuse the verified original Parquet with SHA256
  `48fc84060e4cd032a407da09164e99dfdf81f3ce345bf6c1871bb539ba4ead6c`.
- Select 100 of the same 150 eligible items by the existing SHA256 order and
  seed 20260909. The first 30 IDs must exactly match the prior cohort. This is
  100 total candidates (30 repeated + 70 additional), not 100 new held-out items.
- Regenerate all 100 under the new configuration; do not mix cached 8K outputs
  into this run. Stop each failed item under existing rules and retain it.
- Model/endpoint: `deepseek-v4-flash` via `https://proxy.infix-ai.xyz/v1/`;
  explicit thinking enabled, high effort, temperature omitted from requests.
- Per-call output cap 32,768, timeout 600 seconds, two workers. Increasing the
  cap reduces one known bottleneck but does not guarantee no truncation.
- At most seven stages per item, 700 calls total, no automatic retries.
  Reserve at most 35,000,000 tokens using the existing conservative byte-based
  guard. Reservations are not actual usage and not a guaranteed monetary budget.
  Save actual response usage; proxy price remains unverified.
- Execute all-item solve, then structure/review, then DAG annotation/review.
  No additional small-cohort gate is needed: explicit native thinking has already
  been verified in the prior 30-item run. Transport or budget errors still pause.
- Run from a fixed detached Git worktree with a task-specific PYTHONPATH so later
  development in the collaborative branch cannot change an active run's code.
- Persist raw responses, reasoning_content, final answers, structured solutions,
  nodes, dependencies, justifications, judgments, failures, config/code snapshots,
  selected IDs, exact commands, PID, start/end times and completion/stop records.
- Local artifacts only; private directories 700, files 600, credentials outside
  Git. No B1 connection or GPU. No key in command arguments or saved payloads.

Interpretation: report candidate coverage, first-attempt stage success, truncation,
failure categories and costs. Do not label candidate DAGs as independently verified
gold or claim causal improvement from an unpaired 30-versus-100 aggregate. Compare
the original 30 separately if studying the budget change. If future repair is
approved, keep it as a new version with separate outcomes and additional costs.
