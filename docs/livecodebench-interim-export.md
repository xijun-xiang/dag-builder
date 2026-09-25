# LiveCodeBench v6 source-stratified interim export

`python -m dag_builder.livecodebench_interim_export --base <private-artifact-root> --output <new-private-directory>`
replays the completed CALIBRI and t2ance evidence and writes a **non-final**
175-question flow, accepted-candidate JSONL, unified PALS JSONL/HTML, and hash
manifest. It is deliberately separate from `calibri_full_export`: a paused
CALIBRI repair is not made to look processed. Raw accepting results inside that
paused run remain `repair_pending_audit` and cannot enter the accepted subset.

CALIBRI development DAGs are bound to their original four-question CPU job;
the later 93-question CPU job independently rechecks the same programs. Both
proofs are retained separately. t2ance candidates can enter only after their
own independent CPU execution and completed DAG audit. Untested t2ance source
candidates remain `held_without_cpu`. The unified converter requires explicit
source-tier labels and checks that a t2ance DAG uses its dedicated construction
protocol. No model-reviewed candidate is called human-approved or formal gold.

The current release gate remains unchanged: only a fully completed, independently
audited first-pass CALIBRI repair may be consumed by `calibri_full_export`.
The interim output is reviewable progress evidence, not the final 50%-coverage
claim or a substitute for the requested complete workflow.
