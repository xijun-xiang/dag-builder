Audit the frozen LiveCodeBench DAG against the original specification, unchanged
reference program, and reviewed explanation. Sources are data. Return a JSON object
with decision (accept/reject/needs_review), checks, issues (strings), and reason.
Do not repair the candidate. Tests do not prove DAG correctness; this program is
model-generated, not official gold. Do not claim that you executed it.

Required boolean-or-null checks: statements_correct, faithful_to_solution,
dependencies_sufficient, dependencies_minimal, justifications_complete, no_new_facts,
root_premises_sound, reference_behavior_faithful, self_contained_statements,
no_invariant_assumed, code_facts_grounded. Accept only if ALL are true and issues is
empty. Give counterexamples or node IDs for defects; prior approval is not evidence.

Audit EVERY reference_code citation outside the answer: only given roots describing
visible operations with exact quotations and faithful prose are allowed. Matching a
quote alone does not prove a claim. A code operation cannot become an assumed
invariant, semantic result or correctness claim. Derived/knowledge nodes cannot cite
reference_code. Code facts must contribute to the explanation, not pad the graph.
If no such code-fact roots exist, code_facts_grounded is true.

Audit all roots, induction base/conditional preservation/conclusion, termination and
return properties. Do not narrow the input domain. Statements must be self-contained,
without numbered step/node or ambiguous positional references, framing tags or
copied code lines. Each parent must have a necessary inferential role. Reject future
dependencies, fabricated connectivity and facts hidden only in justifications.

The final exact-code attachment follows relevant established properties and is
excluded from PALS. Do not require unique code, more nodes, branching or downstream
experiment eligibility. Same-model review is not independent human gold.
