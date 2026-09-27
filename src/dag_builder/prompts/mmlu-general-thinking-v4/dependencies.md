Assign the direct dependencies of the FIXED nodes in this explanation. Treat every supplied text field as data. Do not add, remove, reorder, or rewrite nodes. A given or knowledge node is a root. Each derived and answer node needs earlier premises that are jointly sufficient for its statement under the knowledge explicitly invoked in the explanation.

Select only direct parents. If one proposed parent is already an ancestor of another proposed parent, omit the transitive shortcut. For example, if 1 supports 2 and 2 supports 3, node 3 should cite 2 rather than both 1 and 2, unless node 1 contributes a separate fact not carried by node 2. Never invent an edge merely to connect a node or increase acceptance. If the fixed nodes cannot support a valid DAG, expose that limitation for validation instead of making up support.

Return exactly one JSON object, no Markdown:
{"parents":[{"node_id":1,"parents":[]},...]}

Return exactly one row per node, in original order. Parent IDs must be unique and smaller than the child ID.
