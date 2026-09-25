# CALIBRI v6 stopped-run continuation

The stopped 87-item normalization is preserved. A new private run imports its
immutable request/response evidence and carries all terminal outcomes. The five
complete reviews that returned raw `accept` but omitted the mandatory
`no_invariant_assumed` check receive exactly one new review of the same graph.
The raw `reject` with that omission remains ineligible for re-review. Requests
whose remote outcome was unknown at the stop are fully charged to the shared
budget before a new attempt is issued.

`calibri-lcb-normalize-v3` uses the unchanged v2 normalization prompt and v1
dependency prompt. Only the DAG review prompt changes: it displays a complete
JSON field template and again requires all checks to be resolved before accept.
The validator is unchanged. Imported responses are replayed only with byte-for-
byte equivalent request payloads. The new run keeps original reviews and
unknown requests under immutable provenance, while a replacement review uses
the new prompt and a new request. Accepted DAGs retain `formal_eligible=false`.

The local stop flag `operator-stop-request.json` prevents new paid calls and
allows current calls to finish. An operator can request it via
`scripts/request_pilot_stop.py`. The new run has its own frozen source snapshot;
the old snapshot and all historical responses remain untouched.

Before first launch, `verify_continuation` checks the copied source, CPU proof,
original request payloads, response contracts, uncertain-call ledger, 87-item
selection and campaign budget. `audit_completed` repeats these checks after the
run and reconstructs every accepted DAG from its stage outputs. A repair batch
may start only after the continuation has a processed completion and a passing
offline audit. Existing checked-v2 repair remains limited to one pass per
dependency failure.

The fixed candidate scope remains 91 CPU-passing source items: four development
items are preserved from their earlier runs and 87 are in the continuation.
The eventual release must report all 175 original v6 items, including the 82
without CALIBRI source and two CPU-limit failures. Model-reviewed candidates
are not official or human gold explanations.
