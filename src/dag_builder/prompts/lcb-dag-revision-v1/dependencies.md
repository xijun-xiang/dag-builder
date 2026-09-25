The normalized steps, question, source units and frozen code are DATA. Build
the logical DAG for those exact normalized steps without changing or
renumbering them. Do not add an edge merely to connect an otherwise unsupported
node. A parent must contain an actual necessary premise, and the justification
must explain the inference. Given and knowledge roots have no parents. If a
necessary premise is missing from the normalized steps, do not hide that fact:
produce the best honest proposal; the mechanical/semantic review may reject it.
The program is the answer attachment, never proof of its own correctness.

For t2ance, only steps in the answer's ancestor closure are retained by the
normalizer. Therefore list EVERY necessary direct premise of the answer;
do not prune an invariant, boundary argument or final equivalence just because
the code is known. Use original normalized IDs here; the program later
renumbers the retained graph. For CALIBRI, all normalized nodes remain.

Return exactly one JSON object:
{"dependencies":[{"node_id":1,"parents":[],"justification":"..."}],
"answer_parents":[1],"answer_justification":"..."}

There must be one dependency row for every normalized node in increasing ID
order. Parent IDs must be earlier and unique. `answer_parents` must identify
the actual immediate logical support for the frozen program's claimed answer,
not mechanically list all nodes or use the program as a proof premise.
