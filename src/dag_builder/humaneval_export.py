"""Export model-reviewed HumanEval candidates, never label them human gold."""

import json
from pathlib import Path

from .schemas import require, validate_review
from .pipeline import implementation
from .storage import digest, read_json, write_bytes_once, write_once


def export_validation(root):
    root = Path(root)
    items, selection = read_json(root / "items.json"), read_json(root / "selection.json")
    require(bool(items) and all(i.get("task_type") == "humaneval" for i in items), "HumanEval run required")
    require([i["item_id"] for i in items] == selection["selected_ids"], "selection mismatch")
    frozen = read_json(root / "inputs_manifest.json")
    require(frozen["items_sha256"] == digest(items) and frozen["selection_sha256"] == digest(selection),
            "construction inputs changed")
    # Post-construction quality vetoes cannot promote a rejected model output.
    # Keep original model decisions and source/response bytes unchanged.
    audit_path = root / "quality_exclusions.json"
    audits = read_json(audit_path) if audit_path.exists() else []
    require(isinstance(audits, list), "quality exclusions must be a list")
    by_id = {}
    sources = {item["item_id"]: item for item in items}
    for audit in audits:
        require(isinstance(audit, dict), "invalid quality exclusion")
        item_id = audit.get("item_id")
        require(item_id in sources and item_id not in by_id, "unknown or duplicate quality exclusion")
        require(audit.get("task_id") == sources[item_id]["task_id"], "quality exclusion identity mismatch")
        require(audit.get("decision") == "reject" and isinstance(audit.get("reason"), str) and audit["reason"].strip(),
                "quality audit may only reject with a reason")
        require(isinstance(audit.get("evidence"), str) and audit["evidence"].strip(), "quality evidence required")
        require(audit.get("solve_output_sha256") == digest(read_json(root / "items" / item_id / "solve/output.json")),
                "quality exclusion is not bound to this explanation")
        by_id[item_id] = audit
    continuation = None
    if (root / "recovery_manifest.json").exists():
        from .humaneval_continuation import PROTOCOL as CONTINUATION_PROTOCOL, HumanEvalContinuationPipeline
        recovery_protocol = read_json(root / "recovery_manifest.json")["protocol"]
        if recovery_protocol == "humaneval-final-local-recovery-v1":
            from .config import Config
            from .humaneval_final_recovery import HumanEvalFinalRecoveryPipeline
            continuation = HumanEvalFinalRecoveryPipeline(root, Config.load(root / "run_config.json"), object())
        elif recovery_protocol == CONTINUATION_PROTOCOL:
            from .config import Config
            continuation = HumanEvalContinuationPipeline(root, Config.load(root / "run_config.json"), object())
    accepted, inventory = [], []
    for item in items:
        directory = root / "items" / item["item_id"]
        require((directory / "result.json").is_file(), "construction is incomplete; do not silently export a partial cohort")
        result = read_json(directory / "result.json")
        require(result["item_id"] == item["item_id"], "result identity mismatch")
        require(result["status"] in ("model_accepted", "rejected", "needs_review"), "nonterminal result")
        inventory.append({"item_id": item["item_id"], "task_id": item["task_id"],
                          "status": result["status"], "reason": result["reason"]})
        if item["item_id"] in by_id:
            inventory[-1]["quality_audit"] = by_id[item["item_id"]]
            inventory[-1]["export_eligible"] = False
            continue
        if result["status"] != "model_accepted":
            continue
        dag = read_json(directory / "dag.json")
        require(digest(dag) == result["dag_sha256"] and dag["source"] == item and dag["item_id"] == item["item_id"],
                "DAG identity/hash mismatch")
        protocol = dag.get("construction_protocol")
        require(protocol in ("humaneval-reference-v1", "humaneval-reference-v2", "humaneval-reference-v3", "humaneval-reference-v4", "humaneval-reference-v5"), "wrong construction protocol")
        require(protocol == read_json(root / "run_config.json")["prompt_version"], "protocol/config mismatch")
        if protocol == "humaneval-reference-v5":
            from .schemas import public_question
            from .stages import validate
            data = {"question": public_question(item), "solution": dag["reference_solution"],
                    "reference_code": item["canonical_solution"],
                    "reference_sources": {"reference_code": item["canonical_solution"]}}
            validate("atomize", {"nodes": dag["nodes"]}, data, prompt_version=protocol)
            validate("dependencies", {"parents": [{"node_id": n["node_id"], "parents": n["parents"]}
                                                  for n in dag["nodes"]]}, {"nodes": dag["nodes"]})
        for key, stage in (("solution_review", "review_solution"), ("dag_review", "review_dag")):
            validate_review(dag[key], stage)
            require(dag[key]["decision"] == "accept", "rejected review")
            if protocol in ("humaneval-reference-v4", "humaneval-reference-v5"):
                from .humaneval_quality import validate_quality
                validate_quality(stage, dag[key], {"nodes": dag["nodes"]}, version=protocol)
        if (root / "recovery_manifest.json").exists():
            recovery = read_json(root / "recovery_manifest.json")
            proof = dag.get("recovery_provenance", {})
            require(proof.get("manifest_sha256") == digest(recovery)
                    and proof.get("seed_sha256") == recovery["seed_sha256"].get(item["item_id"]),
                    "missing or mismatched recovery provenance")
            if continuation is not None:
                continuation.validate_export(item, dag)
            else:
                require(proof.get("both_reviews_rerun") is True, "both repair reviews must be rerun")
            from .humaneval_repair import PROTOCOL_VERSIONS, diagnosis_input, validate_diagnosis
            if recovery["protocol"] in PROTOCOL_VERSIONS:
                require(PROTOCOL_VERSIONS[recovery["protocol"]] == protocol, "repair/construction protocol mismatch")
                seed = read_json(directory / "repair_seed.json")
                require(digest(seed) == proof["seed_sha256"], "repair seed hash mismatch")
                diagnosis = read_json(directory / "diagnose/output.json")
                validate_diagnosis(diagnosis, diagnosis_input(item, seed, protocol=recovery["protocol"]))
                require(diagnosis["route"] != "source_concern" and proof.get("diagnosis_sha256") == digest(diagnosis)
                        and proof.get("route") == diagnosis["route"], "unresolved repair diagnosis")
                rationale = (seed["stage_outputs"]["solve"] if diagnosis["route"] == "graph_repair"
                             else read_json(directory / "solve/output.json"))
                require(dag["reference_solution"]["rationale"] == rationale["rationale"], "repair changed frozen rationale")
                require(dag["solution_review"] == read_json(directory / "review_solution/output.json")
                        and dag["dag_review"] == read_json(directory / "review_dag/output.json"),
                        "repair reviews must be new, preserved outputs")
        accepted.append({"schema_version": "humaneval_validation_export_v1", "item_id": item["item_id"],
                         "source": item, "dag": dag, "dag_sha256": digest(dag),
                         "model_accepted": True, "human_approved": False, "status": "model_accepted"})
    require(bool(accepted), "no accepted DAGs")
    accepted.sort(key=lambda r: r["item_id"])
    manifest = {"protocol": "humaneval_validation_export_v1", "selected": len(items), "accepted": len(accepted),
                "candidate_count": selection.get("candidate_count", len(items)),
                "source_exclusions": selection.get("excluded", []),
                "quality_audit_exclusions": audits,
                "records_sha256": digest(accepted), "inventory": inventory,
                "construction_config_sha256": digest(read_json(root / "run_config.json")),
                "construction_implementation_sha256": digest(read_json(root / "implementation.json")),
                "export_implementation_sha256": digest(implementation()),
                "quality": "model-reviewed candidates; NOT human-approved or official gold"}
    if (root / "recovery_manifest.json").exists():
        recovery = read_json(root / "recovery_manifest.json")
        manifest["recovery"] = {"manifest_sha256": digest(recovery), "round_limit": recovery["round_limit"],
                                "source_inventory": recovery["source_inventory"],
                                "historical_cost": recovery["historical_cost"],
                                "note": "Recovery cohort only, not a replacement for the full original cohort"}
    destination = root / "exports" / digest(manifest)[:16]
    write_once(destination / "manifest.json", manifest)
    write_bytes_once(destination / "model_accepted.jsonl", "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in accepted).encode())
    return destination
