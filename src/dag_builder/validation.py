"""Structural checks are not a certificate of semantic correctness."""

from .schemas import require, text


def validate_parents(value, nodes):
    require(isinstance(value, dict), "dependencies must be an object")
    rows = value.get("parents")
    ids = [n["node_id"] for n in nodes]
    require(
        isinstance(rows, list) and len(rows) == len(ids), "dependency coverage mismatch"
    )
    require(all(isinstance(r, dict) for r in rows), "invalid dependency row")
    require(
        all(type(r.get("node_id")) is int for r in rows),
        "dependency IDs must be integers",
    )
    require([r.get("node_id") for r in rows] == ids, "dependency order/IDs mismatch")
    mapping = {}
    for node, row in zip(nodes, rows):
        parents = row.get("parents")
        require(
            isinstance(parents, list) and all(type(p) is int for p in parents),
            "invalid parents",
        )
        require(len(set(parents)) == len(parents), "duplicate parents")
        require(
            all(p in ids and p < node["node_id"] for p in parents),
            "unknown, self or future dependency",
        )
        if node["kind"] in ("given", "knowledge"):
            require(not parents, "given/knowledge nodes must be declared roots")
        else:
            require(bool(parents), "derived/answer node has no declared premise")
        mapping[node["node_id"]] = parents
    ancestor_cache = {}

    def ancestors(node_id):
        if node_id not in ancestor_cache:
            found, pending = set(), list(mapping[node_id])
            while pending:
                current = pending.pop()
                if current not in found:
                    found.add(current)
                    pending.extend(mapping[current])
            ancestor_cache[node_id] = found
        return ancestor_cache[node_id]

    for node_id, parents in mapping.items():
        for parent in parents:
            require(
                not any(
                    parent in ancestors(other)
                    for other in parents
                    if other != parent
                ),
                "transitively redundant direct dependency",
            )
    ancestors, pending = set(), [ids[-1]]
    while pending:
        current = pending.pop()
        if current not in ancestors:
            ancestors.add(current)
            pending.extend(mapping[current])
    require(
        ancestors == set(ids),
        "nodes not connected to the answer; do not invent edges to close them",
    )


def validate_justifications(value, nodes):
    require(isinstance(value, dict), "justifications must be an object")
    rows = value.get("justifications")
    require(
        isinstance(rows, list) and all(isinstance(r, dict) for r in rows),
        "invalid justifications",
    )
    require(
        all(type(r.get("node_id")) is int for r in rows),
        "justification IDs must be integers",
    )
    require(
        [r.get("node_id") for r in rows] == [n["node_id"] for n in nodes],
        "justification coverage mismatch",
    )
    require(all(text(r.get("text")) for r in rows), "empty justification")


def assemble(nodes, parents, justifications):
    validate_parents(parents, nodes)
    validate_justifications(justifications, nodes)
    return [
        dict(n, parents=p["parents"], justification=j["text"])
        for n, p, j in zip(nodes, parents["parents"], justifications["justifications"])
    ]


def calculation_status():
    # Arbitrary model-generated code is never evaluated. General physics claims
    # require semantic/human review; future named checkers must be tested first.
    return {
        "status": "not_checked",
        "reason": "no independently specified calculation checker for this item",
    }
