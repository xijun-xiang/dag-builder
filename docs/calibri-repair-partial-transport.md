# Checked CALIBRI repair: partial transport continuation

`calibri-lcb-repair-partial-transport-v1` continues only an **operationally
paused** checked first repair. It is not another repair round. The original
completion must be `paused`; every selected item must have a current pause
event and no terminal `result.json`. Existing `model_accepted`, `rejected`, and
`needs_review` results remain terminal, including unfavorable results. Their
full source directory is frozen under `source-run-evidence/` and hash-checked.

For a paused item, the importer replays each returned stage against the exact
original request and raw `message.content`. Completed repair/dependency/review
stages are copied as immutable cached outputs. A stage with transport errors
but **no** returned response is the only stage eligible for a new call. An
incomplete/invalid/truncated returned response, semantic refusal, changed
source, or unknown request outcome fails preparation instead of being silently
resampled. No `reasoning_content` fallback, code mutation, hidden-test transfer,
or synthetic dependency edge is allowed.

Every historical request, including HTTP 500 or uncertain state, is charged
conservatively. Cached old requests are counted as active calls; the remaining
historical requests are included in `prior_calls`/`prior_reserved_tokens`.
Thus `prior + active + new = all historical + new`, with no refund or double
count. The operator must explicitly supply **three independent numbers** when
preparing a new directory: `--max-new-calls`, `--max-new-tokens`, and the total
`--campaign-token-limit`. The implementation enforces the 600-call campaign
ceiling and a 64-million-token emergency ceiling; that ceiling is **not an
authorization** to spend it. Actual spending still needs project approval.
The existing per-stage worker cap remains four lifetime transport attempts
inside this new run. A returned semantic response is never retried.

The new run contains only paused items. Terminal old outputs are retained in
the frozen source snapshot and merged into the offline audit report as a
separate historical stratum; they are not processed again. The audit reports
all source requests plus new requests and marks every accepted graph
`formal_eligible=false`, since same-model review and passing frozen tests do
not make an explanation human or official gold.

Preparation and tests never contact the API or B1. Before launch, inspect the
prepared selection, old/new budget ledger, frozen hashes, and source stage
actions. Use a new private directory; never rerun or edit the paused source.
