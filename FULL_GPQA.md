# Full GPQA-Diamond coverage

This is a historical campaign-specific orchestration interface, not a fresh-install
quick start. `prepare_campaign(root, base)` expects the exact external pilot and
repair directories listed in `campaign.HISTORY`, including the original 30/50
source batches. These private data artifacts are not distributed in this repository.
New users should start with `dag-builder prepare-gpqa` and the README workflow.

campaign.prepare_campaign verifies the pinned official archive, all198 source rows and exact source equality of the prior80. Explicitly frozen historical outcomes are reused; all remaining eligible rows are selected. Entry exceptions remain in the all-outcome export. This is cumulative coverage, not a homogeneous confirmatory experiment.

Launch a prepared root with scripts/run_gpqa_campaign.py --root <private-root> --key-file <private-key-file>. Code/prompts and runner are frozen before detaching. Construction runs the five-stage official-reference pipeline. Every transport-complete new item, including first-pass acceptances, then receives the current final audit and at most two content revisions. Transport-exhausted items remain visible without resetting retry budgets.

campaign_export creates immutable snapshots with all_results.jsonl (198 unique rows), model_accepted.jsonl, dag_viewer.html and manifest.json. Later nonacceptance is not hidden by earlier acceptance. Diagnostic acceptance retains a separate label and candidate evidence. No human release gate is bypassed. HTML uses escaped embedded JSON and textContent, no remote assets.

First-pass partial nodes, edges and justifications are preserved for revision. Source-review failures without a candidate require adjudication. Existing repair inputs remain supported. Prelaunch tests:178 passed; semantic correctness is not proved by these tests.
