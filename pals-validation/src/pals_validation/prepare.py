"""Freeze score-blind E1 interventions and one-ancestor-prefix E2 anchor."""
from pathlib import Path
from .data import normalize
from .graph import breaking, e2_anchor, forest, parent_control
from .io import digest, read_jsonl, save, sha256

VERSION = "gpqa-validation-v1"
KNOWN_E2_EXCLUSIONS = {"9220d9a8a6f7295fa73b": "historically reviewed incomplete question; no content repair"}


def prepare(source, output, seed=20260915, parent_probe=False, expected_sha=None):
    source, output = Path(source), Path(output)
    source_hash = sha256(source)
    if expected_sha and source_hash != expected_sha:
        raise ValueError("Source SHA256 mismatch")
    cases, jobs, inventory, selection = [], [], [], []
    seen = set()
    for record in read_jsonl(source):
        case = normalize(record)
        item = case["item_id"]
        if item in seen:
            raise ValueError("Duplicate question ID")
        seen.add(item)
        cases.append(case)
        steps = case["steps"]
        by_id = {n["node_id"]: n for n in steps}
        original = [n["node_id"] for n in steps]
        f = forest(steps, seed, item)
        ob = breaking(steps, original, seed, item, "original_break")
        fb = breaking(steps, f["baseline"], seed, item, "forest_break")
        orders = {"original": original, "forest_baseline": f["baseline"]}
        for label, order in (("original_break", ob["order"] if ob else None),
                             ("forest_break", fb["order"] if fb else None), ("legal", f["legal"])):
            if order:
                orders[label] = order
        for label, order in orders.items():
            for pos in range(1, len(order)):
                target = order[pos]
                jobs.append({"kind": "e1", "item_id": item, "variant": label,
                             "target_id": target, "prefix_ids": order[:pos],
                             "deleted_id": order[pos - 1], "target": by_id[target]["statement"]})
        probe = parent_control(steps, item, seed) if parent_probe else None
        if probe:
            for label, deleted in (("parent", probe["parent_id"]), ("control", probe["control_id"])):
                jobs.append({"kind": "e1", "item_id": item, "variant": label,
                             "target_id": probe["target_id"], "prefix_ids": probe["prefix_ids"],
                             "deleted_id": deleted, "target": by_id[probe["target_id"]]["statement"]})
        anchor = None if item in KNOWN_E2_EXCLUSIONS else e2_anchor(steps, item, seed)
        if anchor:
            # Target text, gold answer and edge metadata never enter the generation job.
            jobs.append({"kind": "e2", "item_id": item, "prefix_ids": anchor["prefix_ids"],
                         "deleted_id": anchor["deleted_id"]})
        selection.append({"item_id": item, "orders": orders, "forest": f,
                          "original_break": ob, "forest_break": fb, "anchor": anchor,
                          "parent_probe": probe})
        inventory.append({"item_id": item, "source_row": case["source_row"], "domain": case["domain"],
                          "legal": f["legal"] is not None, "original_break": ob is not None,
                          "forest_break": fb is not None, "fair_pair": bool(f["legal"] and fb),
                          "e2": anchor is not None, "legal_reason": f["reason"],
                          "e2_reason": None if anchor else KNOWN_E2_EXCLUSIONS.get(item, "no_nonanswer_node_with_parent")})
    for job in jobs:
        job["job_id"] = digest(job)
    payloads = {"cases.json": cases, "jobs.json": jobs, "selection.json": selection, "inventory.json": inventory}
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, payload in payloads.items():
        save(output / name, payload)
    manifest = {"protocol": VERSION, "source_sha256": source_hash, "selection_seed": seed,
                "parent_probe": parent_probe, "e2_policy": "one_hash_selected_target_ancestor_prefix",
                "question_choices": "always_included_in_v1", "questions": len(cases),
                "counts": {k: sum(bool(r[k]) for r in inventory)
                           for k in ("legal", "original_break", "forest_break", "fair_pair", "e2")},
                "files": {name: sha256(output / name) for name in payloads}}
    save(output / "manifest.json", manifest)
    return manifest
