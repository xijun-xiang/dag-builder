"""Strict adapter for dag-builder's accepted GPQA export; never repair content."""
from copy import deepcopy


def topological(nodes):
    by_id = {n["node_id"]: n for n in nodes}
    if len(by_id) != len(nodes) or any(type(i) is not int for i in by_id):
        raise ValueError("Node IDs must be unique integers")
    for node in nodes:
        parents = node["parents"]
        if (len(parents) != len(set(parents)) or
                any(p not in by_id or p == node["node_id"] for p in parents)):
            raise ValueError("Invalid parent IDs")
    order = []
    while len(order) < len(nodes):
        ready = sorted(i for i, n in by_id.items()
                       if i not in order and set(n["parents"]) <= set(order))
        if not ready:
            raise ValueError("Cycle in DAG")
        order.append(ready[0])
    return order


def ancestors(nodes, target):
    by_id = {n["node_id"]: n for n in nodes}
    result = set()
    pending = list(by_id[target]["parents"])
    while pending:
        node = pending.pop()
        if node not in result:
            result.add(node)
            pending.extend(by_id[node]["parents"])
    return result


def normalize(record):
    if record.get("model_accepted") is not True:
        raise ValueError("Input must be an accepted export, not an unreviewed candidate")
    source = record["source"]
    if source["subset"] != "gpqa_diamond":
        raise ValueError("This version supports GPQA-Diamond only")
    if not isinstance(source["question"], str) or not source["question"].strip():
        raise ValueError("Empty question")
    if len(source["choices"]) != 4 or not all(isinstance(s, str) and s.strip() for s in source["choices"]):
        raise ValueError("Four nonempty choices required")
    if record.get("dag") is not None:
        if record["dag"]["source"] != source:
            raise ValueError("DAG/source mismatch")
        nodes = deepcopy(record["dag"]["nodes"])
        origin = "dag.nodes"
    else:
        if record.get("status") != "model_accepted_diagnostic":
            raise ValueError("Unrecognized accepted fallback")
        candidate = record["candidate"]
        nodes = deepcopy(candidate["nodes"])
        parents = {n["node_id"]: n["parents"] for n in candidate["parents"]}
        if len(parents) != len(candidate["parents"]) or set(parents) != {n["node_id"] for n in nodes}:
            raise ValueError("Candidate parent join is not one-to-one")
        for node in nodes:
            node["parents"] = parents[node["node_id"]]
        origin = "candidate.nodes+candidate.parents"
    topological(nodes)
    seen = set()
    for node in nodes:
        if node["kind"] not in ("given", "knowledge", "derived", "answer"):
            raise ValueError("Unknown step kind")
        if not isinstance(node["statement"], str) or not node["statement"].strip():
            raise ValueError("Empty statement")
        if not set(node["parents"]) <= seen:
            raise ValueError("Original node list is not topological; do not silently reorder")
        seen.add(node["node_id"])
    steps = [n for n in nodes if n["kind"] != "answer"]
    if len(nodes) - len(steps) != 1 or len(steps) < 2:
        raise ValueError("Exactly one answer and at least two reasoning steps required")
    if any(not set(n["parents"]) <= {s["node_id"] for s in steps} for n in steps):
        raise ValueError("Reasoning step depends on excluded answer")
    return {"item_id": record["item_id"], "source_row": source["row"],
            "domain": source["domain"], "question": source["question"],
            "choices": source["choices"], "steps": steps, "adapter": origin,
            "human_approved": record.get("human_approved", False)}
