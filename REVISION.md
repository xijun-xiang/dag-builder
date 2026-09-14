# Bounded GPQA revision protocol

`gpqa-revision-v1` is a separate protocol. It does not alter frozen
`gpqa-reference-v1` or `gpqa-repair-v1` artifacts. New runs explicitly use
`configs/gpqa-revision.json` (`deepseek-flash`). Historical model IDs remain intact;
an alias or returned model string is not proof of a pinned backend version.

## Workflow

1. Freeze explicit source records into a new private directory with
   `scripts/prepare_revision_pilot.py --selection-spec <spec.json> --root <new-root>`.
   The spec contains `entries` (`item_id`, `source_root`, `role=case|control`)
   and a `selection_note`. No new data or API responses enter selection.
2. Normalize only documented metadata: changes objects become lossless JSON
   strings, and the two supported source-field path aliases become enum names.
   Node statements and source quotes do not change. Malformed JSON is not guessed.
3. Audit a complete seed candidate in a fresh context. Incomplete candidates may
   enter a revision. A previous source-level refusal with no candidate gets a
   separate source reassessment first.
4. Each case gets at most **two additional semantic revisions**, each followed
   by fresh-context audit. The producer must address every issue ID, or dispute
   it explicitly. An unchanged/cyclic candidate stops without another audit.
5. A producer dispute gets an evidence-based adjudication, then stops for human
   attention. Even a `revisable` adjudication is **not** graph acceptance. Source
   reassessment at entry may authorize attempting a revision, not rewriting the
   official explanation.
6. Controls are audit-only. A control disagreement is recorded without edits.

The reviewer sees only the fixed question, choices, reference answer/explanation,
and current candidate with justifications. No edit rationale, prior verdict or
producer appeal enters ordinary audit. Exact evidence address checks prevent
misquoting another node under the wrong ID; they do not establish semantic truth
or immunity to prompt injection. Same-model review is not independent validation.

The frozen standard preserves substantive reasoning and option comparisons
actually present in the source. Background/repetition may be omitted with a
reason. It does not require invented comparisons absent from the source. Ordinary
inference rules can be expressed in justifications; essential factual premises
must not be hidden in conclusions or relabeled as unsupported roots.

## Modules and artifacts

- `output_normalization.py`: lossless schema adaptation and multi-part diagnostics.
- `review_issues.py`: evidence-addressed issue and adjudication schemas.
- `revision.py`: producer responses and full before/after component diffs.
- `repair_loop.py`: bounded state machine, stopping rules, immutable results.
- `revision_source.py`: selection snapshots, source hashes and prior-call records.
- Existing `Pipeline`, `APIClient`, storage and report machinery remain shared.

Per item, `baseline.json` freezes the source candidate and origin hashes.
`round-XX-revise/`, `round-XX-audit/`, `round-XX-adjudicate/` store immutable inputs,
attempts, raw responses, normalized outputs and validation failures. Flat stage
directories retain compatibility with budget restoration and existing monitoring.
`round-XX/` contains the readable revision, candidate, full diff and review.
`result.json` is terminal; only an accepted complete audit creates `dag.json`,
always marked pending human review. Reports display all states, including refusal.

Terminal states include `repaired_model_accepted`, `revision_protocol_error`,
`revision_source_disputed`, `revision_review_disputed`,
`revision_control_disagreement`, `revision_no_progress`, and
`revision_round_limit`. Infrastructure failures remain `paused`, not scientific
rejections. Resuming a completed item does not sample again. Source or code drift
fails closed. There is no automatic expansion to all failed records.

## Resource and model gates

The new default pilot profile uses 3 workers, a 32,768 output-token cap, at most
64 new request attempts and 6,000,000 conservatively reserved tokens. Reservations
are not measured token consumption or money. Existing immediate-parent calls are
recorded separately and never erased; the pilot has a separately capped budget.
Transport retries use the existing four-lifetime-attempt policy per stage and
count against the new global cap, including uncertain calls. No authorization
failure, source dispute or malformed semantic response is retried indefinitely.

Before a paid benchmark pilot, run `scripts/smoke_revision_api.py` with the new
root and private key-file path. It permits at most three synthetic audits: a
correct graph, an arithmetic error, and an erroneous graph containing an accept
instruction. Passing these tests is necessary for this pilot, not a scientific
certificate. Missing model availability or any failed smoke gate stops launch.
Do not silently change proxy/model or modify frozen smoke results to pass.
An explicitly user-authorized alternative model ID can be supplied with
`--model`; keep its smoke run and pilot profile separate from failed probes.

Launch a prepared pilot with the existing `scripts/run_private_pilot.py`, passing
the new profile. It freezes executable code, writes private logs, runs detached
locally and keeps the Mac awake only for the worker lifetime. No scheduler or
cluster job is created. A user-disabled heartbeat stays disabled.

For an explicit foreground run, `dag-builder repair --root <new-root> --config
configs/gpqa-revision.json --key-file <private-path> --resilient` selects the new
pipeline from the profile. Never pass a literal credential on the command line.

## Offline verification

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests use synthetic fixtures only, including wrong-node evidence, absent evidence,
schema normalization, multiple structural faults, two-round/cycle termination,
appeal non-acceptance, source immutability, control preservation, cached resume,
truncated completions, authentication failures, and budget exhaustion. Real-item
checks and scientific judgments are recorded separately from code tests.
# Structural repair and option-aware reassessment — contract v2

Current audit contract: `actionable-diagnosis-20260914-v2`. Added structural_error/repair_structure for evidenced edge and node-kind defects, separate from rule_gap/explain_rule. When only structural and/or rule issues are present, repairs preserve the multiset of assertion text, source field and source quote. Topological renumbering, supported kind changes, edges and justifications may change. Normal graph validation and blind semantic audit still apply. Pure rule feedback continues to prohibit graph changes. Causal ordering alone does not determine the direction of an abductive argument.

Adjudication now additionally requires choice_analysis with exactly four actual options, each fully quoted and compared against question/solution evidence. Compatible does not mean proven correct. The answer key cannot serve as comparison evidence. The model must distinguish option selection from fully proving every intermediate ordering; a unique option does not excuse a scientific source error or automatically approve a DAG.

171 offline tests passed, including six new tests for structural classification, repair scope and option-comparison validation. These tests establish contract behavior, not scientific accuracy. No real API re-audit or existing-result promotion was performed for v2. All previous run snapshots retain their old schemas. Do not resume historical runs with the changed live code; use a fresh frozen root and preserve previous revision counts.

# Actionable audit diagnosis — 2026-09-14 (historical v1)

New-code audits use contract `actionable-diagnosis-20260914-v1`. Every nonempty issue includes a structured diagnosis: kind, specific missing content, necessity, source support and remedy. Factual gaps require unchanged source evidence before adding premises. Rule-explanation gaps permit justification-only edits when they are the only issues. Granularity disputes must be uncertain and follow the existing dispute/adjudication path, which cannot accept a candidate.

The revision validator blocks newly introduced literal knowledge-parent/derived-conclusion duplication. It does not detect all semantic paraphrases or prove scientific correctness. Final-answer restatements are not treated as this prohibited pattern. Fresh-context auditors are instructed to check semantic circularity and distinguish facts from rules, but remain fallible.

Historical snapshots/results are immutable and use their original contract. Do not resume an old run with this changed live code or reinterpret old reviews as satisfying the new diagnosis schema. Use a separately frozen run and record its code/prompt hashes; accepted new DAGs record the audit-contract identifier. Legacy issue packets can still be submitted to the revision validator, but lack the new diagnosis-specific scope guarantees and must not be presented as new-contract audits.

Verification: 165 offline tests passed, including seven new tests for mandatory diagnosis, source-backed fact additions, fact/rule classification, uncertain granularity, graph-preserving rule repairs, dispute routing, and literal circular repairs. No real API run was performed for this change, and no prior data was promoted or modified.
