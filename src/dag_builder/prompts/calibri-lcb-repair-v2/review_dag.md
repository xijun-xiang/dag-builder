Additional checked-repair review:
Require three additional boolean-or-null checks in checks:
premise_inventory_complete, deletion_impact_checked, root_relocations_safe.
Acceptance also requires all three true. Evaluate all conclusions against the
original question and code, NOT only against the inventoried subset. Coverage of
IDs does not prove completeness of required premises. Check input/value bounds,
complexity assumptions, initialization/update and actual final output behavior.
The graph must actually carry the inventoried necessary facts to each conclusion.

Existing given/knowledge roots may have been moved earlier without changing their
text, sources or kind. This is an explicit reference-DAG normalization operation,
not a claim that the source model originally emitted that order. Derived statements
must retain relative order and wording. A code-fact root may describe actual output
operations; it may not assert the program is correct without a proof. Verify that
every removed statement is unnecessary to ALL retained claims, even if the old
dependency graph omitted its real consumers. Reject unsupported deletion rationales.
