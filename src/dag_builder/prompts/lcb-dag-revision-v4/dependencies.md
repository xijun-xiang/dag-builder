V4 is a failure-directed follow-up. First inspect the normalized statements
actually supplied in this request; prior node IDs and prior graph sketches
are not authoritative. Declare every required parent by its current ID,
strictly earlier than the child. `given` and `knowledge` nodes must be roots.
Every `derived` node that supports the answer must have an actual proof
premise. The answer parents must carry the full task-to-code correctness
argument, including any essential invariant, boundary or complexity premise.
An edge is valid only if its parent's statement is used by the child's
justification. If the normalized steps lack an essential premise, do not
invent an edge or claim that connectivity proves correctness; the subsequent
review should leave the candidate unresolved.
