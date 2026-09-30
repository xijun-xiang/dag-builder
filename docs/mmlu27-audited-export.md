# MMLU 27-subject infrastructure-aware audit/export

`scripts/audit_export_mmlu27.py` is an offline, read-only replay for the frozen
green-six-class MMLU campaign. It reads the immutable campaign directory and
writes to a *new* output directory. It does not call a model, modify the source
run, repair an output, or promote same-model review to human approval.

Run with a Python environment that can import the frozen `root/code/dag_builder`
snapshot:

```bash
python scripts/audit_export_mmlu27.py --root /absolute/frozen-run \
  --output-dir /absolute/new-audit-export
```

The audit verifies source parquet and selection hashes, exact 7,895/7,888
source/selected flow, the explicitly registered 173 historical transport
unresolved items, accepted DAG hashes, answer labels, source quotes,
nonredundant dependencies, every accepted stage's match to one raw formal
`message.content` response, model identity, and the global conservative
request/token ledger. The `solve` stage retains its distinct native reasoning
contract. It exports all outcomes, model-accepted records, strict multi-step
candidates, and `pals_dag_unified_v1` JSONL/HTML. Unresolved transport and
infrastructure omissions remain N/A, never model failures or invented DAGs.

The audited 2026-09-30 snapshot produced 3,797 model-accepted candidates,
3,075 strict multi-step candidates, and 7,895 all-outcome rows. Offline PALS
preparation then found 496 E1 fair-pair questions and 2,746 E2 anchors; these
are separate denominators. The generated output is a model-reviewed synthetic
cohort, not official MMLU or human DAG gold. Private questions, raw responses,
and credentials are not committed to this repository.
