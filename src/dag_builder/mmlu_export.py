"""Immutable, all-outcome MMLU export; model acceptance is not human gold."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from .mmlu_catalog import MMLU_SUBJECTS
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .unified import convert_file


def export_subject(root, output_dir):
    root, output_dir = Path(root), Path(output_dir)
    if output_dir.exists():
        raise ValueError("refusing to overwrite an existing export directory")
    items = read_json(root / "items.json")
    selection = read_json(root / "selection.json")
    normalized = read_json(root / "source" / "normalized.json")
    if not items or [item["item_id"] for item in items] != selection["selected_ids"]:
        raise ValueError("incomplete or mismatched source selection")
    run_config = read_json(root / "run_config.json")
    implementation = read_json(root / "implementation.json")
    inputs_manifest = read_json(root / "inputs_manifest.json")
    if (run_config.get("task_type") != "mmlu"
            or not isinstance(run_config.get("prompt_version"), str)
            or not isinstance(run_config.get("model"), str)
            or inputs_manifest.get("items_sha256") != digest(items)
            or inputs_manifest.get("selection_sha256") != digest(selection)
            or not isinstance(implementation.get("code_sha256"), str)):
        raise ValueError("missing or mismatched frozen run provenance")
    excluded = {row["item_id"]: row["reason"] for row in selection["excluded"]}
    source_by_id = {item["item_id"]: item for item in normalized}
    selected_ids = {item["item_id"] for item in items}
    if (len(source_by_id) != len(normalized) or len(excluded) != len(selection["excluded"])
            or len(normalized) != selection["candidate_count"]
            or len(items) != selection["selected_count"]
            or selected_ids & set(excluded)
            or not (selected_ids | set(excluded)) <= set(source_by_id)
            or any(source_by_id[item["item_id"]] != item for item in items)
            or selection["eligible_count"] != len(normalized) - len(excluded)):
        raise ValueError("incomplete source inventory or exclusion flow")
    unselected = set(source_by_id) - selected_ids - set(excluded)
    subjects = {item.get("subset") for item in items}
    if len(subjects) != 1 or next(iter(subjects)) not in MMLU_SUBJECTS:
        raise ValueError("one recognized MMLU subject is required per root")
    subset = next(iter(subjects))
    provenance = read_json(root / "source" / "provenance.json")
    raw = (root / "source" / "original.parquet").read_bytes()
    if (provenance.get("dataset") != "cais/mmlu" or provenance.get("config") != subset
            or provenance.get("split") != "test"
            or hashlib.sha256(raw).hexdigest() != provenance.get("sha256")):
        raise ValueError("unverified MMLU source")
    rows, accepted, pals_eligible = [
        {"item_id": item_id, "subset": subset, "source_row": source_by_id[item_id]["row"],
         "status": "source_excluded", "stage": "source_selection", "reason": reason,
         "pals_structural_eligible": False}
        for item_id, reason in excluded.items()
    ] + [
        {"item_id": item_id, "subset": subset, "source_row": source_by_id[item_id]["row"],
         "status": "not_selected", "stage": "source_selection",
         "reason": "outside_frozen_selection", "pals_structural_eligible": False}
        for item_id in unselected
    ], [], []
    for item in items:
        result = read_json(root / "items" / item["item_id"] / "result.json")
        status = result.get("status")
        if result.get("item_id") != item["item_id"] or status not in (
                "model_accepted", "needs_review", "rejected"):
            raise ValueError("nonterminal or mismatched result")
        row = {"item_id": item["item_id"], "subset": subset,
               "source_row": item["row"], "status": status,
               "stage": result.get("stage"), "reason": result.get("reason"),
               "pals_structural_eligible": False}
        rows.append(row)
        if status != "model_accepted":
            continue
        dag = read_json(root / "items" / item["item_id"] / "dag.json")
        if (dag.get("source") != item or dag.get("item_id") != item["item_id"]
                or dag.get("construction_protocol") != run_config["prompt_version"]
                or digest(dag) != result.get("dag_sha256")):
            raise ValueError("accepted DAG does not match frozen source/result")
        candidate = {"schema_version": "mmlu_model_candidates_v1",
                     "item_id": item["item_id"], "source": item, "dag": dag,
                     "dag_sha256": result["dag_sha256"],
                     "status": "model_accepted", "model_accepted": True,
                     "human_approved": False}
        accepted.append(candidate)
        if sum(node["kind"] != "answer" for node in dag["nodes"]) >= 2:
            row["pals_structural_eligible"] = True
            pals_eligible.append(candidate)
        else:
            row["pals_ineligibility_reason"] = "fewer_than_two_nonanswer_steps"
    rows.sort(key=lambda row: row["source_row"])
    if len(rows) != len(normalized) or len({row["source_row"] for row in rows}) != len(rows):
        raise ValueError("MMLU all-outcome denominator mismatch")
    private_dir(output_dir)
    encoded = ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                   allow_nan=False) + "\n" for row in accepted)).encode()
    eligible_encoded = ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                            allow_nan=False) + "\n"
                                for row in pals_eligible)).encode()
    write_bytes_once(output_dir / "model_accepted.jsonl", encoded)
    write_bytes_once(output_dir / "pals_eligible_candidates.jsonl", eligible_encoded)
    write_bytes_once(output_dir / "all_outcomes.jsonl", ("".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for row in rows)).encode())
    unified = None
    if pals_eligible:
        unified = convert_file(output_dir / "pals_eligible_candidates.jsonl", "mmlu",
                               hashlib.sha256(eligible_encoded).hexdigest(), output_dir / "unified")
    manifest = {"schema_version": "mmlu_model_candidates_v1", "subset": subset,
                "source_revision": provenance["revision"],
                "source_parquet_sha256": provenance["sha256"],
                "source_selection_sha256": digest(selection),
                "run_config_sha256": digest(run_config),
                "prompt_version": run_config["prompt_version"],
                "model": run_config["model"],
                "implementation_sha256": implementation["code_sha256"],
                "inputs_manifest_sha256": digest(inputs_manifest),
                "candidate_count": len(normalized),
                "eligible_count": selection["eligible_count"],
                "source_excluded": len(excluded),
                "not_selected": len(unselected),
                "selected": len(items), "status_counts": dict(Counter(row["status"] for row in rows)),
                "model_accepted": len(accepted),
                "pals_structural_eligible": len(pals_eligible), "human_approved": 0,
                "all_outcomes_sha256": hashlib.sha256((output_dir / "all_outcomes.jsonl").read_bytes()).hexdigest(),
                "model_accepted_sha256": hashlib.sha256(encoded).hexdigest(),
                "pals_eligible_sha256": hashlib.sha256(eligible_encoded).hexdigest(),
                "unified": unified}
    write_once(output_dir / "manifest.json", manifest)
    return manifest


def export_campaign(campaign_root, output_dir):
    """Require all 57 terminal cohorts, then combine without hiding failures."""
    campaign_root, output_dir = Path(campaign_root), Path(output_dir)
    if output_dir.exists():
        raise ValueError("refusing to overwrite an existing campaign export")
    campaign = read_json(campaign_root / "campaign_manifest.json")
    if (campaign.get("protocol") != "mmlu-57-source-selection-v1"
            or set(campaign.get("subjects", {})) != set(MMLU_SUBJECTS)):
        raise ValueError("incomplete MMLU source campaign")
    if campaign.get("split") != "test":
        raise ValueError("only the frozen test split is exportable for PALS")
    # Preflight all terminal states before creating any output directory.
    first_root = campaign_root / "subjects" / MMLU_SUBJECTS[0]
    expected_run_config = read_json(first_root / "run_config.json")
    expected_code_sha = read_json(
        first_root / "implementation.json"
    ).get("code_sha256")
    for subset in MMLU_SUBJECTS:
        source_root = campaign_root / "subjects" / subset
        if read_json(source_root / "selection.json") != campaign["subjects"][subset]:
            raise ValueError("campaign and subject selections differ")
        provenance = read_json(source_root / "source" / "provenance.json")
        if (provenance.get("revision") != campaign["revision"]
                or provenance.get("config") != subset):
            raise ValueError("campaign and subject source provenance differ")
        for item in read_json(source_root / "items.json"):
            result_path = source_root / "items" / item["item_id"] / "result.json"
            if not result_path.is_file():
                raise ValueError("unfinished subject: " + subset)
            if read_json(result_path).get("status") not in (
                    "model_accepted", "needs_review", "rejected"):
                raise ValueError("nonterminal subject: " + subset)
        run_config = read_json(source_root / "run_config.json")
        implementation = read_json(source_root / "implementation.json")
        if (run_config != expected_run_config
                or run_config.get("prompt_version") != "mmlu-general-thinking-v1"
                or not isinstance(expected_code_sha, str)
                or implementation.get("code_sha256") != expected_code_sha):
            raise ValueError("mixed MMLU construction protocol or code revision")
    private_dir(output_dir)
    accepted_bytes, eligible_bytes, outcome_bytes, subject_manifests = [], [], [], {}
    for subset in MMLU_SUBJECTS:
        source_root = campaign_root / "subjects" / subset
        subject_export = output_dir / "subjects" / subset
        subject_manifests[subset] = export_subject(source_root, subject_export)
        accepted_bytes.append((subject_export / "model_accepted.jsonl").read_bytes())
        eligible_bytes.append((subject_export / "pals_eligible_candidates.jsonl").read_bytes())
        outcome_bytes.append((subject_export / "all_outcomes.jsonl").read_bytes())
    accepted, eligible, outcomes = (b"".join(accepted_bytes), b"".join(eligible_bytes),
                                    b"".join(outcome_bytes))
    write_bytes_once(output_dir / "model_accepted.jsonl", accepted)
    write_bytes_once(output_dir / "pals_eligible_candidates.jsonl", eligible)
    write_bytes_once(output_dir / "all_outcomes.jsonl", outcomes)
    unified = (convert_file(output_dir / "pals_eligible_candidates.jsonl", "mmlu",
                            hashlib.sha256(eligible).hexdigest(), output_dir / "unified")
               if eligible else None)
    manifest = {"schema_version": "mmlu-57-model-campaign-export-v1",
                "source_revision": campaign["revision"], "split": campaign["split"],
                "subject_count": len(MMLU_SUBJECTS),
                "candidate_count": sum(row["candidate_count"] for row in subject_manifests.values()),
                "eligible_count": sum(row["eligible_count"] for row in subject_manifests.values()),
                "source_excluded": sum(row["source_excluded"] for row in subject_manifests.values()),
                "not_selected": sum(row["not_selected"] for row in subject_manifests.values()),
                "selected": sum(row["selected"] for row in subject_manifests.values()),
                "model_accepted": sum(row["model_accepted"] for row in subject_manifests.values()),
                "pals_structural_eligible": sum(row["pals_structural_eligible"]
                                                 for row in subject_manifests.values()),
                "human_approved": 0,
                "all_outcomes_sha256": hashlib.sha256(outcomes).hexdigest(),
                "model_accepted_sha256": hashlib.sha256(accepted).hexdigest(),
                "pals_eligible_sha256": hashlib.sha256(eligible).hexdigest(),
                "subjects": subject_manifests, "unified": unified}
    write_once(output_dir / "manifest.json", manifest)
    return manifest
