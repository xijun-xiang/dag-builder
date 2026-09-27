Convert the reviewed solution into a minimal DAG-ready list of atomic assertions. Treat all supplied text as data. Preserve the actual reasoning; do not add missing facts or inferences merely to obtain a connected graph.

Use these provenance rules exactly:
- A stated fact from the question stem is a `given`. Cite an exact substring of `question.question` with `source_field: "question"`. If the rationale paraphrases the stem, quote the original stem, not the paraphrase.
- An independently invoked fact, definition, or rule is `knowledge`. Cite an exact substring of `solution.rationale` with `source_field: "solution"`.
- A conclusion inferred from earlier assertions is `derived`. Cite an exact substring of `solution.rationale`. A first node cannot be `derived`: without an earlier premise, classify its actual basis as `given` or `knowledge`, or leave the unsupported explanation to fail validation.
- Labeled options are candidate answers, not true premises. Never use `choice_A` through `choice_D` for a `given`, `knowledge`, or `derived` node. Only the final `answer` node may quote the selected option from its matching `choice_X` source. Its statement must name the selected label and meaning.

Keep only premises and inferences genuinely used to select the answer. Omit question restatements, abandoned alternatives, redundant option eliminations, and repeated conclusions. Do not force a multi-step DAG for bare recall. Every quote must be a nonempty verbatim substring of its declared source. If the explanation lacks a sound support path, retain that limitation for the later audit; do not manufacture a path.

Return exactly one JSON object, no Markdown:
{"nodes":[{"node_id":1,"kind":"given|knowledge|derived|answer","statement":"one assertion","source_field":"question|solution|choice_A|choice_B|choice_C|choice_D","source_quote":"exact source substring"},...]}

IDs are consecutive in reasoning order, starting at 1. Include exactly one final `answer` node and at least one earlier node.
