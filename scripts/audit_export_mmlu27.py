"""Read-only replay and lossless, infrastructure-aware MMLU campaign export.

Never repairs a response or promotes a model judgement to human gold.  Writes
only into a fresh output directory, leaving the frozen campaign untouched.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


STAGES = ("solve", "structure_solution", "review_solution", "atomize",
          "dependencies", "justify", "review_dag")
PREHISTORY_MISSING = {"ae625427c20fc16ccd62", "d3e998612e060c4a093d",
                      "d3e6291ba7fa6f175676"}
MAX_REQUESTS = 66273
MAX_RESERVED = 3313650000


def jread(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def assert_true(value, message):
    if not value:
        raise ValueError(message)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        file.write("\n")


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        for value in values:
            file.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  allow_nan=False) + "\n")
    return sha_bytes(path.read_bytes())


def audit_stage(item_root, stage, config, parse_object, parse_native_solution):
    stage_root = item_root / stage
    output_path = stage_root / "output.json"
    assert_true(output_path.is_file(), f"missing accepted stage output: {item_root}/{stage}")
    output = jread(output_path)
    matches = 0
    for response_path in sorted(stage_root.glob("attempt-*/response.json")):
        response = jread(response_path)
        body = response.get("body", {})
        choices = body.get("choices", [])
        if body.get("model") != config["model"] or len(choices) != 1:
            continue
        if choices[0].get("finish_reason") != "stop":
            continue
        message = choices[0].get("message", {})
        try:
            parsed = (parse_native_solution(message) if stage == "solve" else
                      parse_object(message.get("content")))
        except (ValueError, TypeError):
            continue
        if parsed == output:
            matches += 1
    assert_true(matches == 1, f"accepted output has {matches} matching raw content: {item_root}/{stage}")


def audit_dag(item, item_root, result, config, lib):
    dag = jread(item_root / "dag.json")
    digest = lib["digest"]
    assert_true(dag.get("source") == item and dag.get("item_id") == item["item_id"],
                f"accepted DAG/source mismatch: {item_root}")
    assert_true(digest(dag) == result.get("dag_sha256"), f"accepted DAG hash mismatch: {item_root}")
    assert_true(dag.get("construction_protocol") == config["prompt_version"],
                f"protocol mismatch: {item_root}")
    assert_true(dag.get("reference_solution", {}).get("answer") == item["gold_answer"],
                f"gold answer mismatch: {item_root}")
    assert_true(dag.get("quality_status") == "model_reviewed_pending_human_review",
                f"quality status mismatch: {item_root}")
    nodes = dag["nodes"]
    choices = {f"choice_{letter}": item["choices"][index]
               for index, letter in enumerate("ABCD")}
    lib["validate_nodes"]({"nodes": nodes}, item["question"],
                          dag["reference_solution"]["rationale"], choices)
    lib["validate_parents"]({"parents": [{"node_id": n["node_id"],
                                             "parents": n["parents"]} for n in nodes]},
                            nodes, reject_transitive=True)
    lib["validate_justifications"]({"justifications": [
        {"node_id": n["node_id"], "text": n["justification"]} for n in nodes]}, nodes)
    for stage, key in (("review_solution", "solution_review"),
                       ("review_dag", "dag_review")):
        lib["validate_review"](dag[key], stage)
        assert_true(dag[key]["decision"] == "accept", f"review not accepted: {item_root}")
    for stage in STAGES:
        audit_stage(item_root, stage, config, lib["parse_object"],
                    lib["parse_native_solution"])
    return dag


def audit_budget(root, expected_model):
    requests = 0
    responses = 0
    errors = 0
    unknown = 0
    reserved = 0
    charged = 0
    over_reservation = 0
    models = Counter()
    request_paths = sorted(root.glob("subjects/*/items/*/*/attempt-*/request.json"))
    for path in request_paths:
        request = jread(path)
        assert_true(request.get("payload", {}).get("model") == expected_model,
                    f"request model changed: {path}")
        amount = request.get("reserved_tokens")
        assert_true(type(amount) is int and amount > 0, f"bad reservation: {path}")
        requests += 1
        reserved += amount
        response_path = path.with_name("response.json")
        error_path = path.with_name("error.json")
        if response_path.is_file():
            response = jread(response_path)
            body = response.get("body", {})
            models[body.get("model")] += 1
            usage = body.get("usage", {})
            actual = usage.get("total_tokens")
            assert_true(type(actual) is int and actual >= 0,
                        f"missing response token usage: {response_path}")
            responses += 1
            charged += max(amount, actual)
            over_reservation += actual > amount
        else:
            charged += amount
            if error_path.is_file():
                errors += 1
            else:
                unknown += 1
    assert_true(requests <= MAX_REQUESTS and charged <= MAX_RESERVED,
                "campaign exceeded authorized hard budget")
    assert_true(set(models) == {expected_model}, "response model identity changed")
    return {"requests": requests, "responses": responses, "errors": errors,
            "no_response_or_error": unknown, "reserved_tokens": reserved,
            "conservative_charged_tokens": charged,
            "response_above_reservation": over_reservation,
            "response_models": dict(models), "hard_request_limit": MAX_REQUESTS,
            "hard_conservative_token_limit": MAX_RESERVED}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output_dir.resolve()
    assert_true(root.is_dir() and not output.exists(), "root missing or export already exists")
    sys.path.insert(0, str(root / "code"))
    from dag_builder.schemas import (parse_native_solution, parse_object,
                                     validate_nodes, validate_review)
    from dag_builder.storage import digest
    from dag_builder.unified import convert_file
    from dag_builder.validation import validate_justifications, validate_parents
    lib = locals().copy()
    campaign = jread(root / "campaign_manifest.json")
    provenance = jread(root / "post503_v2_provenance.json")
    subjects = campaign["subjects"]
    assert_true(len(subjects) == campaign["subject_count"] == 27,
                "campaign subject count mismatch")
    assert_true(campaign["dataset"] == "cais/mmlu" and campaign["split"] == "test",
                "wrong source campaign")
    known_missing = {"prehistory": PREHISTORY_MISSING,
                     "public_relations": set(provenance["public_exhausted_transport"]),
                     "professional_law": set(provenance["law_exhausted_transport"])}
    assert_true(len(known_missing["public_relations"]) == 32 and
                len(known_missing["professional_law"]) == 138,
                "transport omission ledger changed")
    rows = []
    accepted = []
    eligible = []
    subject_stats = {}
    configs = {}
    code_hashes = {}
    science_configs = set()
    selected_total = 0
    candidate_total = 0
    for subset in subjects:
        sr = root / "subjects" / subset
        items = jread(sr / "items.json")
        selection = jread(sr / "selection.json")
        normalized = jread(sr / "source" / "normalized.json")
        inputs = jread(sr / "inputs_manifest.json")
        config = jread(sr / "run_config.json")
        impl = jread(sr / "implementation.json")
        source = jread(sr / "source" / "provenance.json")
        configs[subset] = digest(config)
        code_hashes[subset] = impl["code_sha256"]
        science_configs.add((config.get("task_type"), config.get("prompt_version"),
                             config.get("model"), config.get("thinking"),
                             config.get("reasoning_effort"),
                             config.get("response_format")))
        assert_true(selection == subjects[subset] and
                    [x["item_id"] for x in items] == selection["selected_ids"],
                    f"selection mismatch: {subset}")
        assert_true(inputs["items_sha256"] == digest(items) and
                    inputs["selection_sha256"] == digest(selection),
                    f"frozen input hash mismatch: {subset}")
        assert_true(source["dataset"] == "cais/mmlu" and source["config"] == subset and
                    source["split"] == "test" and source["revision"] == campaign["revision"] and
                    sha_bytes((sr / "source" / "original.parquet").read_bytes()) == source["sha256"],
                    f"source provenance mismatch: {subset}")
        assert_true(len(items) == selection["selected_count"] and
                    len(normalized) == selection["candidate_count"],
                    f"source denominator mismatch: {subset}")
        source_by_id = {x["item_id"]: x for x in normalized}
        assert_true(len(source_by_id) == len(normalized), f"duplicate source IDs: {subset}")
        selected = {x["item_id"] for x in items}
        excluded = {x["item_id"]: x["reason"] for x in selection["excluded"]}
        assert_true(len(selected) == len(items) and len(excluded) == len(selection["excluded"]) and
                    selected.isdisjoint(excluded) and selected | set(excluded) <= set(source_by_id) and
                    selection["eligible_count"] == len(normalized) - len(excluded),
                    f"source flow mismatch: {subset}")
        for item_id, reason in excluded.items():
            rows.append({"item_id": item_id, "subset": subset,
                         "source_row": source_by_id[item_id]["row"],
                         "status": "source_excluded", "stage": "source_selection",
                         "reason": reason, "pals_structural_eligible": False})
        for item_id in set(source_by_id) - selected - set(excluded):
            rows.append({"item_id": item_id, "subset": subset,
                         "source_row": source_by_id[item_id]["row"],
                         "status": "not_selected", "stage": "source_selection",
                         "reason": "outside_frozen_selection",
                         "pals_structural_eligible": False})
        counts = Counter()
        for item in items:
            item_id = item["item_id"]
            assert_true(item == source_by_id[item_id], f"selected source drift: {subset}/{item_id}")
            item_root = sr / "items" / item_id
            result_path = item_root / "result.json"
            if result_path.is_file():
                result = jread(result_path)
                status = result.get("status")
                assert_true(result.get("item_id") == item_id and status in
                            {"model_accepted", "needs_review", "rejected", "infrastructure_omitted"},
                            f"invalid terminal result: {subset}/{item_id}")
                stage = result.get("stage")
                reason = result.get("reason")
            else:
                assert_true(item_id in known_missing.get(subset, set()),
                            f"unaccounted nonterminal result: {subset}/{item_id}")
                status = "transport_unresolved"
                stage = (provenance["public_exhausted_transport"].get(item_id)
                         if subset == "public_relations" else
                         provenance["law_exhausted_transport"].get(item_id)
                         if subset == "professional_law" else "prehistory")
                reason = "historical_transport_attempts_exhausted; not_scored"
            counts[status] += 1
            row = {"item_id": item_id, "subset": subset, "source_row": item["row"],
                   "status": status, "stage": stage, "reason": reason,
                   "pals_structural_eligible": False}
            rows.append(row)
            if status != "model_accepted":
                continue
            dag = audit_dag(item, item_root, result, config, lib)
            candidate = {"schema_version": "mmlu_model_candidates_v1",
                         "item_id": item_id, "source": item, "dag": dag,
                         "dag_sha256": result["dag_sha256"],
                         "status": "model_accepted", "model_accepted": True,
                         "human_approved": False}
            accepted.append(candidate)
            if sum(node["kind"] != "answer" for node in dag["nodes"]) >= 2:
                eligible.append(candidate)
                row["pals_structural_eligible"] = True
            else:
                row["pals_ineligibility_reason"] = "fewer_than_two_nonanswer_steps"
        expected_missing = known_missing.get(subset, set())
        actual_missing = {x["item_id"] for x in items if not
                          (sr / "items" / x["item_id"] / "result.json").is_file()}
        assert_true(actual_missing == expected_missing,
                    f"transport missing set mismatch: {subset}")
        assert_true(sum(counts.values()) == len(items), f"selected denominator mismatch: {subset}")
        subject_stats[subset] = {"candidate_count": len(normalized),
                                 "selected_count": len(items),
                                 "source_excluded": len(excluded),
                                 "status_counts": dict(counts)}
        selected_total += len(items)
        candidate_total += len(normalized)
    assert_true(selected_total == 7888 and candidate_total == 7895 and
                len(rows) == 7895 and len(science_configs) == 1 and
                next(iter(science_configs))[:3] ==
                ("mmlu", "mmlu-general-thinking-v4", "deepseek-v4-flash"),
                f"campaign denominator or scientific protocol mismatch: "
                f"selected={selected_total} candidate={candidate_total} "
                f"rows={len(rows)} science_configs={science_configs}")
    rows.sort(key=lambda x: (list(subjects).index(x["subset"]), x["source_row"]))
    budget = audit_budget(root, "deepseek-v4-flash")
    output.mkdir(parents=True, exist_ok=False)
    outcome_hash = write_jsonl(output / "all_outcomes.jsonl", rows)
    accepted_hash = write_jsonl(output / "model_accepted.jsonl", accepted)
    eligible_hash = write_jsonl(output / "pals_eligible_candidates.jsonl", eligible)
    unified = convert_file(output / "pals_eligible_candidates.jsonl", "mmlu",
                           eligible_hash, output / "unified") if eligible else None
    manifest = {"schema_version": "mmlu27_lossless_audited_export_v1",
                "campaign_root": str(root), "source_revision": campaign["revision"],
                "subject_count": len(subjects), "candidate_count": candidate_total,
                "selected_count": selected_total, "frozen_config_sha256_by_subject": configs,
                "frozen_code_sha256_by_subject": code_hashes,
                "scientific_protocol": list(next(iter(science_configs))),
                "status_counts": dict(Counter(x["status"] for x in rows)),
                "model_accepted": len(accepted), "pals_structural_eligible": len(eligible),
                "human_approved": 0, "formal_eligible": False,
                "all_outcomes_sha256": outcome_hash,
                "model_accepted_sha256": accepted_hash,
                "pals_eligible_sha256": eligible_hash,
                "subjects": subject_stats, "budget": budget, "unified": unified,
                "note": "Model-reviewed candidates, not human or official DAG gold; transport rows are N/A."}
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"status": "audit_export_complete", "output_dir": str(output),
                      "status_counts": manifest["status_counts"],
                      "pals_structural_eligible": len(eligible),
                      "budget": budget}, ensure_ascii=False))


if __name__ == "__main__":
    main()
