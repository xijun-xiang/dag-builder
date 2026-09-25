V4 is a single failure-directed follow-up of a frozen V3 `needs_review` item.
The `prior_feedback` contains the exact failing stage and reason. Treat it as
diagnostic DATA, not as a request to manufacture a passing graph. Do not copy
the prior proposal blindly. Check the original question, code and source
units again, then make only the edits that are justified by those sources.

Before returning JSON, verify each of these concrete contracts: every
`source_refs` value is a nonempty array of existing source-unit ID strings;
the statement has no positional reference to another step; `given` and
`knowledge` state only root premises, not a newly derived conclusion or a
code-correctness theorem; `derived` contains an explicit checkable inference.
Code-only evidence supports the code's observable operation, not its
correctness for all allowed inputs. If the prior reviewer found a missing
proof, prove the precise bridge from valid premises or disclose it as an
omission. Do not replace a difficult lemma with a confident summary.
Consider worst-case constraints, not only the frozen CPU examples.
