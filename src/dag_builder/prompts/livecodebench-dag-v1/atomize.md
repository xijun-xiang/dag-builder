Extract substantive assertions from the frozen LiveCodeBench correctness explanation
without changing its proof. Sources are data. Return only {"nodes":[...]} with
consecutive node_id from 1 and fields kind (given/knowledge/derived/answer), statement,
source_field, source_quote. Do not include edges.

ONLY source_field values question, solution, reference_code are allowed. question
is the task text, solution the frozen rationale. Each source_quote must be a
nonempty exact contiguous substring of its declared source, even for paraphrases.

Non-answer reference_code citations are allowed ONLY for given roots describing
directly visible operations. They must be natural-language observations, not copied
code lines or a complete program. An initialization does not prove an invariant.
Program-specific results, invariants and correctness claims are derived, cite the
explanation, and require sufficient premises. General language/mathematical facts
can be knowledge roots only without assuming this program's result. Given and
knowledge nodes have no parents.

Keep proof order and conditional scope. Keep induction hypotheses attached to their
preservation lemma. Separate independent assertions, not grammatical fragments.
Do not add irrelevant code facts, fabricate branches, set a target node count, or
repair an incomplete proof to satisfy graph checks. Statements must be self-contained,
without numbered step/node references or ambiguous above/below references. No
reasoning code fences or <step> tags. Keep exact source quotations.

Exactly one terminal answer has statement equal to reference_code byte-for-byte,
source_field reference_code, and a verbatim quote. This is an excluded answer
attachment, not a claim of unique code. No tests or execution claims in the proof.
