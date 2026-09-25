# LiveCodeBench v6: audited split-repair export

This is a **new, versioned export path**. It does not change the historical
`livecodebench_interim_export` or `calibri_full_export` behavior.

The first checked CALIBRI repair selected 35 questions and stopped for transport
reasons after recording 11 terminal results. `calibri-lcb-repair-partial-transport-v1`
may continue only its 24 evidenced operational pauses in a new directory. The
11 old terminal decisions, including rejections and needs-review decisions,
are never resampled. The two cohorts are distinct; a pause is not acceptance.

After the new run has `completion.json.status=processed`, independently run
`scripts/audit_calibri_repair.py --root <new-run>`. This audit replays the old
11 terminal results from raw request/response `message.content`, the 24 new
terminal statuses, stages, reasons and accepted DAGs from their own raw
`message.content`, and the cumulative budget. It rejects even joint edits to
a result label and validation file when raw content does not support them.
Do not use a prior audit file as proof.

From the repository root, with `PYTHONPATH=src:scripts`, export to a **new
private directory**. If it is inside the artifact root, it must be a named
`releases/<name>` child; an input run or its descendant is refused before any
file is created:

```sh
python -m dag_builder.livecodebench_repair_continuation_export \
  --base <private-livecodebench-artifact-root> \
  --continuation-run <completed-partial-transport-run> \
  --output <new-private-export-directory>
```

Before writing a final export, the module recomputes the raw audit and requires
byte-for-byte equality with `offline-audit.json`; checks that all 35 items
partition into exactly 11 old terminals and 24 new terminals; verifies the
old source directory matches its frozen snapshot; and requires every new
item to have a terminal result. It then creates a named immutable interim
baseline for the unaffected 140 questions, including the independently
verified t2ance stratum. Only the 35 `repair_pending_audit` rows are replaced.
The result has 175 unique question rows, a separate accepting-candidate JSONL,
the stable `pals_dag_unified_v1` JSONL/HTML, SHA-256 provenance in
`manifest.json`, and a compact flow HTML. All nonaccepting results remain in
the 175-row flow; only audited `model_accepted` DAGs enter the unified file.

The output protocol is `lcb-v6-source-stratified-candidates-split-repair-v1`.
It still records `human_approved=false` and `formal_eligible=false`. This is a
model-reviewed source-derived candidate export, **not** official or human gold
and not by itself proof of 50% validated benchmark coverage or solution
correctness beyond the frozen CPU tests.
