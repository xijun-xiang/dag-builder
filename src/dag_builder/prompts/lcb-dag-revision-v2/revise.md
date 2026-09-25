V2 mechanical lessons from the first fixed canary:

1. A step supported ONLY by `C` (program-code) units MUST be a `given` that
   describes an observable code operation, with `support_type=source_supported`.
   Never label a code-only step `derived` or use it to claim the algorithm is
   correct. An invariant or proof step must cite at least one relevant `Q`
   (question) or explanation unit, and its statement/note must contain the
   actual logical argument. Merely adding a Q reference is not a proof.
2. If the explanation and question do not support a necessary correctness
   bridge, disclose it in omissions. Do not turn the missing bridge into a
   plausible-sounding fact. CPU test success is not a premise for correctness.
3. Every `derived` step intended to support the answer must have a real earlier
   premise. Put that premise before the step. Do not put positional references
   such as `node 7`, `the previous step` or `above` inside a statement.
4. The terminal program is attached separately. Do not paste it into steps.
5. For CALIBRI, the graph validator retains every normalized step, so include
   only steps that are genuinely needed to support the terminal answer. Remove
   unrelated exploratory claims here; the dependency stage must not invent
   an edge merely to keep them connected.

Before emitting JSON, inspect every step's source_refs: if they are all C
units, apply rule 1 or remove the unsupported correctness claim.
