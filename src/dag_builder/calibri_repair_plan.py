"""Checked repair plans: explicit premises, deletion impact and root placement.

Inventory coverage and graph ancestry are mechanically checkable. Completeness
of the inventory and semantic necessity are not; a fresh-context audit is still
required. No question, code or retained statement is rewritten here.
"""

from .calibri_normalize import normalize, source_units
from .schemas import require, text
from .storage import digest

PROTOCOL = "calibri-lcb-repair-v2"
AUDIT_CHECKS = ("premise_inventory_complete", "deletion_impact_checked", "root_relocations_safe")


def id_list(value, allowed, message):
    require(isinstance(value, list) and all(type(v) is int and v in allowed for v in value)
            and len(value) == len(set(value)), message)


def apply_checked_repair(value, previous, item):
    from .calibri_repair import STEP_FIELDS, apply_repair
    require(isinstance(value, dict) and set(value) == {
        "additions", "removals", "moves", "premise_checks", "deletion_checks", "reason"},
        "checked repair needs edits, premise_checks and deletion_checks")
    base = {k: value[k] for k in ("additions", "removals", "reason")}
    normalized, record = apply_repair(base, previous, item)
    old_nodes = {n["node_id"]: n for n in previous["nodes"]}
    base_mapping = {r["old_node_id"]: r["new_node_id"] for r in record["node_mapping"]}
    retained = {k for k, v in base_mapping.items() if v is not None}
    removed = set(old_nodes) - retained
    derived = {k for k in retained if old_nodes[k]["kind"] == "derived"}
    moves = value["moves"]
    require(isinstance(moves, list), "moves must be a list")
    moved_ids = []
    for move in moves:
        require(isinstance(move, dict) and set(move) == {"node_id", "before_node_id", "reason"}
                and text(move["reason"]), "invalid root move")
        source, target = move["node_id"], move["before_node_id"]
        require(type(source) is int and source in retained and source not in moved_ids
                and old_nodes[source]["kind"] in ("given", "knowledge"), "only retained roots may move once")
        require(type(target) is int and target in retained and target < source,
                "root can only move before an earlier retained node")
        moved_ids.append(source)
    require(all(m["before_node_id"] not in moved_ids for m in moves), "move target must remain stationary")
    require(all(a["before_node_id"] not in moved_ids for a in value["additions"]),
            "add a premise before a stationary node")
    order = [n["node_id"] for n in normalized["nodes"]]
    for move in sorted(moves, key=lambda m: m["node_id"]):
        source, target = base_mapping[move["node_id"]], base_mapping[move["before_node_id"]]
        order.remove(source)
        order.insert(order.index(target), source)
    renumber = {old: new for new, old in enumerate(order, 1)}
    by_id = {n["node_id"]: n for n in normalized["nodes"]}
    repaired = normalize({"steps": [{k: by_id[n][k] for k in STEP_FIELDS} for n in order],
                          "omissions": normalized["omissions"]}, item)
    mapping = {old: renumber[new] for old, new in base_mapping.items() if new is not None}
    # Source order can differ from proposal order, so resolve additions by their
    # insertion target, statement and refs, rather than zipping the two lists.
    added_ids = []
    for addition in value["additions"]:
        matches = [c["new_node_id"] for c in record["changes"] if c["operation"] == "add_question_premise"
                   and all(c[k] == addition[k] for k in addition)]
        require(len(matches) == 1, "ambiguous duplicate addition")
        added_ids.append(renumber[matches[0]])
    checks = value["premise_checks"]
    require(isinstance(checks, list) and len(checks) == len(derived), "premise inventory coverage mismatch")
    question_units = {u["unit_id"] for u in source_units(item) if u["source_field"] == "question"}
    seen, mapped_checks = set(), []
    for check in checks:
        require(isinstance(check, dict) and set(check) == {
            "node_id", "required_node_ids", "required_additions", "question_refs_used", "background_assumptions", "reason"},
            "invalid premise inventory row")
        target = check["node_id"]
        require(type(target) is int and target in derived and target not in seen and text(check["reason"]),
                "invalid/duplicate premise target")
        seen.add(target)
        required, added = check["required_node_ids"], check["required_additions"]
        id_list(required, retained - {target}, "required premise missing, deleted or self-referential")
        id_list(added, set(range(len(added_ids))), "unknown/duplicate required addition index")
        new_required = [mapping[k] for k in required] + [added_ids[k] for k in added]
        require(new_required and all(n < mapping[target] for n in new_required),
                "necessary premise must precede its conclusion; move only an existing root")
        refs, background = check["question_refs_used"], check["background_assumptions"]
        require(isinstance(refs, list) and all(isinstance(r, str) and r in question_units for r in refs)
                and len(refs) == len(set(refs)), "invalid used question refs")
        require(isinstance(background, list) and all(text(r) for r in background), "invalid background assumptions")
        declared = {r for n in repaired["nodes"] if n["node_id"] in new_required and n["kind"] == "given"
                    for r in n["source_refs"]}
        require(set(refs) <= declared, "used question condition needs an explicitly inventoried given node")
        mapped_checks.append({"node_id": mapping[target], "required_ancestor_ids": new_required,
                              "question_refs_used": refs, "background_assumptions": background,
                              "reason": check["reason"]})
    deletion_checks = value["deletion_checks"]
    require(isinstance(deletion_checks, list) and len(deletion_checks) == len(removed),
            "every deletion needs an impact check")
    seen = set()
    for check in deletion_checks:
        require(isinstance(check, dict) and set(check) == {
            "node_id", "retained_conclusions_checked", "required_by_node_ids", "reason"}, "invalid deletion impact check")
        target = check["node_id"]
        require(type(target) is int and target in removed and target not in seen and text(check["reason"]),
                "invalid/duplicate deletion impact target")
        seen.add(target)
        id_list(check["retained_conclusions_checked"], derived, "invalid deletion coverage")
        require(set(check["retained_conclusions_checked"]) == derived,
                "deletion check must cover every retained conclusion")
        require(check["required_by_node_ids"] == [], "cannot remove a fact needed by a retained conclusion")
    for change in record["changes"]:
        if "new_node_id" in change:
            change["new_node_id"] = renumber[change["new_node_id"]]
    record.update(protocol=PROTOCOL, repaired_normalization_sha256=digest(repaired),
                  node_mapping=[{"old_node_id": old, "new_node_id": mapping.get(old)} for old in old_nodes],
                  premise_checks=checks, premise_inventory=mapped_checks, deletion_checks=deletion_checks)
    record["changes"].extend({"operation": "move_existing_root", "old_node_id": m["node_id"],
        "new_node_id": mapping[m["node_id"]], "before_old_node_id": m["before_node_id"],
        "reason": m["reason"]} for m in moves)
    return repaired, record


def validate_premise_paths(graph, record):
    """An inventoried fact must actually support the conclusion in the graph."""
    nodes = {n["node_id"]: n for n in graph["nodes"]}
    for check in record["premise_inventory"]:
        ancestors, pending = set(), list(nodes[check["node_id"]]["parents"])
        while pending:
            n = pending.pop()
            if n not in ancestors:
                ancestors.add(n)
                pending.extend(nodes[n]["parents"])
        require(set(check["required_ancestor_ids"]) <= ancestors,
                "dependency graph drops an inventoried necessary premise")
