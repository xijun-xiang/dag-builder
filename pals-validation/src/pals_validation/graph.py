"""Lossless forest serialization and deterministic dependency interventions."""
from .data import ancestors, topological
from .io import rank


def violations(steps, order):
    pos = {n: i for i, n in enumerate(order)}
    return [(p, n["node_id"]) for n in steps for p in n["parents"]
            if pos[p] >= pos[n["node_id"]]]


def breaking(steps, order, seed, item_id, label):
    """One adjacent direct-edge inversion after a fixed first node."""
    candidates = []
    for i in range(1, len(order) - 1):
        changed = order[:]
        changed[i], changed[i + 1] = changed[i + 1], changed[i]
        edges = violations(steps, changed)
        if edges == [(order[i], order[i + 1])]:
            candidates.append({"order": changed, "swapped": order[i:i + 2],
                               "violated_edges": edges})
    return min(candidates, key=lambda c: rank(seed, item_id, label, c["swapped"])) if candidates else None


def forest(steps, seed, item_id):
    """Shared nodes+ancestors fixed; remaining converging subtrees kept intact.

    Candidate policy preserves the historical rule: first two nontrivial children
    per sibling group, then choose one group by a fixed hash. Not all pairs.
    """
    ids = [n["node_id"] for n in steps]
    parents = {n["node_id"]: n["parents"] for n in steps}
    consumers = {i: [] for i in ids}
    for n in steps:
        for p in n["parents"]:
            consumers[p].append(n["node_id"])
    shared = {n for n in ids if len(consumers[n]) > 1}
    fixed = set(shared)
    for node in shared:
        fixed.update(ancestors(steps, node))
    remaining = set(ids) - fixed
    children = {n: [p for p in parents[n] if p in remaining] for n in remaining}
    subtree = {}
    for n in ids:
        if n in remaining:
            subtree[n] = {n}.union(*(subtree[p] for p in children[n]))
    positions = {n: i for i, n in enumerate(ids)}
    def ordered(values):
        return sorted(values, key=lambda n: min(positions[a] for a in subtree[n]))
    roots = ordered([n for n in ids if n in remaining and not consumers[n]])
    children = {n: ordered(values) for n, values in children.items()}
    prefix = [n for n in ids if n in fixed]

    def serialize(operation=None):
        def arranged(parent, values):
            result = list(values)
            if operation and operation["parent"] == parent:
                a, b = operation["swapped_roots"]
                i, j = result.index(a), result.index(b)
                result[i], result[j] = result[j], result[i]
            return result
        def visit(n):
            return [v for p in arranged(n, children[n]) for v in visit(p)] + [n]
        return prefix + [v for root in arranged(None, roots) for v in visit(root)]

    def validate(order):
        if len(order) != len(ids) or set(order) != set(ids) or violations(steps, order):
            raise ValueError("Forest serialization lost nodes or inverted edges")
        if order[:len(prefix)] != prefix:
            raise ValueError("Shared prefix moved")
        pos = {n: i for i, n in enumerate(order)}
        for ns in subtree.values():
            ps = sorted(pos[n] for n in ns)
            if ps != list(range(ps[0], ps[-1] + 1)):
                raise ValueError("Subtree interleaved")

    baseline = serialize()
    validate(baseline)
    candidates = []
    for parent, values in [(None, roots)] + [(n, children[n]) for n in ids if n in children]:
        large = [n for n in values if len(subtree[n]) >= 2]
        if len(large) >= 2:
            candidates.append({"parent": parent, "swapped_roots": large[:2],
                               "type": "independent_roots" if parent is None else "siblings"})
    operation = min(candidates, key=lambda c: rank(seed, item_id, "legal", c)) if candidates else None
    legal = serialize(operation) if operation else None
    if legal:
        validate(legal)
    return {"baseline": baseline, "legal": legal, "fixed_prefix": prefix,
            "operation": operation, "candidate_count": len(candidates),
            "reason": None if legal else "no_two_nontrivial_sibling_subtrees"}


def e2_anchor(steps, item_id, seed):
    by_id = {n["node_id"]: n for n in steps}
    order = topological(steps)
    targets = [i for i in order if by_id[i]["parents"]]
    if not targets:
        return None
    target = min(targets, key=lambda i: rank(seed, "target", item_id, i))
    selected = ancestors(steps, target)
    prefix = [i for i in order if i in selected]
    if not prefix or prefix[-1] not in by_id[target]["parents"]:
        raise ValueError("Deleted ancestor must be a direct parent of reference target")
    return {"target_id": target, "prefix_ids": prefix, "deleted_id": prefix[-1],
            "candidate_target_ids": targets, "target_parents": by_id[target]["parents"]}


def parent_control(steps, item_id, seed):
    ids = [n["node_id"] for n in steps]
    by_id = {n["node_id"]: n for n in steps}
    choices = []
    for index, node in enumerate(steps):
        anc = ancestors(steps, node["node_id"])
        for parent in node["parents"]:
            for control in ids[:index]:
                if control in anc:
                    continue
                lp, lc = (len(by_id[i]["statement"].split()) for i in (parent, control))
                ratio = max(lp, lc) / min(lp, lc)
                gap = abs(ids.index(parent) - ids.index(control))
                if ratio <= 2 and gap <= 2:
                    choices.append({"target_id": node["node_id"], "parent_id": parent,
                                    "control_id": control, "prefix_ids": ids[:index],
                                    "position_gap": gap, "length_ratio": ratio})
    return min(choices, key=lambda c: (c["position_gap"], c["length_ratio"], rank(seed, item_id, c))) if choices else None
