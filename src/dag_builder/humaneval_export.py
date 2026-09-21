"""Export model-reviewed HumanEval candidates, never label them human gold."""

import json
from pathlib import Path

from .schemas import require, validate_review
from .storage import digest, read_json, write_bytes_once, write_once


def export_validation(root):
    root = Path(root)
    items, selection = read_json(root / "items.json"), read_json(root / "selection.json")
    require(bool(items) and all(i.get("task_type") == "humaneval" for i in items), "HumanEval run required")
    require([i["item_id"] for i in items] == selection["selected_ids"], "selection mismatch")
    frozen = read_json(root / "inputs_manifest.json")
    require(frozen["items_sha256"] == digest(items) and frozen["selection_sha256"] == digest(selection),
            "construction inputs changed")
    accepted, inventory = [], []
    for item in items:
        directory = root / "items" / item["item_id"]
        require((directory / "result.json").is_file(), "construction is incomplete; do not silently export a partial cohort")
        result = read_json(directory / "result.json")
        require(result["item_id"] == item["item_id"], "result identity mismatch")
        require(result["status"] in ("model_accepted", "rejected", "needs_review"), "nonterminal result")
        inventory.append({"item_id": item["item_id"], "task_id": item["task_id"],
                          "status": result["status"], "reason": result["reason"]})
        if result["status"] != "model_accepted":
            continue
        dag = read_json(directory / "dag.json")
        require(digest(dag) == result["dag_sha256"] and dag["source"] == item and dag["item_id"] == item["item_id"],
                "DAG identity/hash mismatch")
        require(dag.get("construction_protocol") == "humaneval-reference-v1", "wrong construction protocol")
        for key, stage in (("solution_review", "review_solution"), ("dag_review", "review_dag")):
            validate_review(dag[key], stage)
            require(dag[key]["decision"] == "accept", "rejected review")
        accepted.append({"schema_version": "humaneval_validation_export_v1", "item_id": item["item_id"],
                         "source": item, "dag": dag, "dag_sha256": digest(dag),
                         "model_accepted": True, "human_approved": False, "status": "model_accepted"})
    require(bool(accepted), "no accepted DAGs")
    accepted.sort(key=lambda r: r["item_id"])
    manifest = {"protocol": "humaneval_validation_export_v1", "selected": len(items), "accepted": len(accepted),
                "records_sha256": digest(accepted), "inventory": inventory,
                "construction_config_sha256": digest(read_json(root / "run_config.json")),
                "construction_implementation_sha256": digest(read_json(root / "implementation.json")),
                "quality": "model-reviewed candidates; NOT human-approved or official gold"}
    destination = root / "exports" / digest(manifest)[:16]
    write_once(destination / "manifest.json", manifest)
    write_bytes_once(destination / "model_accepted.jsonl", "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in accepted).encode())
    return destination
