V2 graph-validity preflight:

- For CALIBRI, every retained normalized node must have a genuine path to the
  terminal answer. If an exploratory node is not needed, it should have been
  omitted during revision; do not invent an edge now just to satisfy this test.
- For t2ance, every `derived` node selected into the answer-ancestor closure
  must have at least one valid earlier parent. Given and knowledge roots have
  no parents. The answer's direct parents must collectively carry every
  indispensable invariant, boundary condition, recurrence and final bridge.
- A proof about the program's result needs task premises and a reasoning
  bridge; observed code behavior alone is not sufficient.
- If these conditions cannot honestly be met, return a proposal exposing the
  gap and let the validator reject it. Never change node text or IDs here.
