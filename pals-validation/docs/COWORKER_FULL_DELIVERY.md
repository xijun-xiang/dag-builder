# Coworker DAG full-delivery PALS protocol

This release scores the complete **delivered** GSM8K/MMLU DAG cohort, not the
complete underlying benchmark and not a human-reviewed gold dataset. The
original ZIP and every record in it remain unchanged. The freezer verifies the
ZIP, each embedded archive/manifest, and the independent 1,539-row audit, then
publishes all rows in a flow ledger. Rows with only one scored non-answer step
are retained in that ledger but have no defined local contrast.

| Package | Delivered | Scoreable (E1 baseline) | E2 anchor |
| --- | ---: | ---: | ---: |
| GSM8K | 530 | 530 | 530 |
| MMLU mathematics | 415 | 415 | 413 |
| MMLU psychology/social | 594 | 586 | 520 |
| Total | 1,539 | 1,531 | 1,463 |

The local freezer is `scripts/freeze_coworker_full_delivery.py`. Its protocol
is `coworker-pals-full-delivery-v1`. Each unchanged `scoreable-*.jsonl` is
bound by source SHA-256 and may prepare both E1 and E2. The resulting prepared
manifest records `compatible_experiments=["e1","e2"]`; the run initializer
rejects an incompatible arm. All selection remains deterministic with seed
`20260915`. No semantic audit flag silently removes a delivered row.

E1 has different, explicitly reported denominators: all 1,531 scoreable
questions provide an original and a forest baseline; each break operator is
reported on its own matched original/forest target set; only questions with
both legal and forest-break variants support the three-way common-target
comparison (169 GSM8K, 57 mathematics, 27 psychology/social). Do not subtract
means from different target sets. Some default broken edges invert a
question/given ancestor, so broad-coverage effects are conditional-support
diagnostics, not uniformly verified reasoning-dependency violations. The
previous strict psychology/social E1 cohort also used six reviewed break-edge
overrides; that old result is not an exact nested rerun of this default broad
protocol.

E2 fixes one deterministic non-answer ancestor prefix per eligible question,
generates at temperatures 0.3/0.7/1.2 with eight repeats each, and scores at
temperature 1. Invalid draws, missing anchors, and the eight one-step records
remain separate from numeric zero. The full 1,463-anchor cohort includes
potential question-given redundancy and targets without downstream non-answer
children; those flags are preserved for stratified interpretation. E2 is a
fixed-prefix next-step experiment, not a whole-CoT stability claim.

Use the same three frozen B1 models and the same scoring/generation protocol
as the preceding smaller formal batch. Report ordinary NLL alongside g, M, N,
and D; retain per-token evidence, native-loss checks, all invalid generations,
and all unhelpful effects. Statistics are question-level, not step- or
repeat-level; duplicated question text needs a clustered/deduplicated
sensitivity check. No effect-size threshold controls whether the run proceeds.
