"""One-round HumanEval recovery with immutable ancestry and explicit routes.

Preparation is offline. Format recovery reuses explanation and normalized nodes,
but re-runs BOTH semantic reviews; other repairs regenerate once from a diagnosis.
Original failures are never overwritten or relabelled as successful responses.
"""
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re

from .humaneval_quality import VERSION, normalize_sources, text_findings
from .pipeline import Pipeline
from .schemas import InvalidOutput, parse_object, require
from .stages import STAGES, payload, stage_input, validate
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

PROTOCOL = "humaneval-recovery-v1"


@contextmanager
def source_lock(root):
    root = Path(root).absolute()
    require(root.resolve() == root, "symlinked source root")
    require((root / "completion.json").is_file(), "source batch must stop before recovery")
    fd = os.open(root / ".lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield root
    finally:
        os.close(fd)


def baseline(directory):
    outputs, failed = {}, {}
    for stage in STAGES:
        sd = directory / stage
        responses = sorted(sd.glob("attempt-*/response.json"))
        require(len(responses) <= 1, "ambiguous semantic response history")
        if (sd / "output.json").exists():
            require(len(responses) == 1, "stage output missing original response")
            raw = read_json(responses[0])["body"]["choices"]
            require(len(raw) == 1 and raw[0]["finish_reason"] == "stop", "incomplete cached stage")
            parsed = parse_object(raw[0]["message"]["content"])
            for contract_path in responses[0].parent.glob("contract_check-v*.json"):
                require(not read_json(contract_path)["violations"], "cached response failed its API contract")
            output = read_json(sd / "output.json")
            require(parsed == output, "cached stage differs from original content")
            data = read_json(sd / "input.json")["input"]
            validate(stage, output, data, "reference_code_explanation")
            outputs[stage] = output
        elif responses:
            choices = read_json(responses[0])["body"].get("choices", [])
            failed[stage] = {"response_sha256": hashlib.sha256(responses[0].read_bytes()).hexdigest()}
            if len(choices) == 1 and choices[0].get("finish_reason") == "stop":
                for contract_path in responses[0].parent.glob("contract_check-v*.json"):
                    require(not read_json(contract_path)["violations"], "failed response violated its API contract")
                try:
                    failed[stage]["parsed"] = parse_object(choices[0]["message"].get("content"))
                except InvalidOutput:
                    failed[stage]["parse_error"] = "invalid complete JSON; no syntax repair"
    return {"stage_outputs": outputs, "failed_completions": failed}


def inspect_locked(root):
    config = read_json(root / "run_config.json")
    require(config["task_type"] == "humaneval" and config["prompt_version"] in (
        "humaneval-reference-v1", "humaneval-reference-v2", "humaneval-reference-v3"),
        "recovery currently accepts first-pass HumanEval v1/v2/v3 runs only")
    require(not (root / "recovery_manifest.json").exists(), "nested repair rounds are not allowed")
    items, selection = read_json(root / "items.json"), read_json(root / "selection.json")
    frozen = read_json(root / "inputs_manifest.json")
    require(digest(items) == frozen["items_sha256"] and digest(selection) == frozen["selection_sha256"],
            "source inputs changed")
    require([i["item_id"] for i in items] == selection["selected_ids"], "source selection mismatch")
    require(len({i["item_id"] for i in items}) == len(items) and all(
        re.fullmatch(r"[0-9a-f]{20}", i["item_id"]) and i["task_type"] == "humaneval" for i in items),
        "invalid or duplicate source IDs")
    vetoes = read_json(root / "quality_exclusions.json") if (root / "quality_exclusions.json").exists() else []
    require(isinstance(vetoes, list), "invalid quality exclusion list")
    veto_by_id = {}
    source_by_id = {i["item_id"]: i for i in items}
    for veto in vetoes:
        item_id = veto.get("item_id")
        require(item_id in source_by_id and item_id not in veto_by_id and veto.get("decision") == "reject",
                "invalid quality exclusion identity")
        require(veto.get("task_id") == source_by_id[item_id]["task_id"] and veto.get("reason")
                and veto.get("evidence") and veto.get("solve_output_sha256") == digest(read_json(
                    root / "items" / item_id / "solve/output.json")), "unbound quality exclusion")
        veto_by_id[item_id] = veto
    rows, seeds = [], {}
    for item in items:
        directory = root / "items" / item["item_id"]
        result_path = directory / "result.json"
        if not result_path.exists():
            attempted = any(directory.glob("*/attempt-*/request.json"))
            rows.append({"item_id": item["item_id"], "task_id": item["task_id"],
                         "status": "unfinished" if attempted else "not_started",
                         "route": "transport_review" if attempted else "not_started",
                         "reason": "No terminal scientific result; do not convert to rejection"})
            continue
        result = read_json(result_path)
        require(result["item_id"] == item["item_id"], "source result identity mismatch")
        row = {"item_id": item["item_id"], "task_id": item["task_id"],
               "status": result["status"], "stage": result["stage"], "reason": result["reason"]}
        if item["item_id"] in veto_by_id:
            row.update(route="manual_source_review", quality_veto=veto_by_id[item["item_id"]])
            rows.append(row)
            continue
        if result["status"] == "model_accepted":
            dag = read_json(directory / "dag.json")
            require(digest(dag) == result["dag_sha256"] and dag["source"] == item, "source DAG changed")
            row.update(route="retain_candidate_with_audit", text_findings=text_findings(dag["nodes"]))
        elif result["status"] in ("needs_review", "rejected"):
            seed = baseline(directory)
            seed["original_result"] = result
            row["route"] = "manual_source_review" if result["stage"] == "review_solution" else "semantic_revision"
            failed = seed["failed_completions"].get("atomize", {})
            if result["stage"] == "atomize" and "parsed" in failed:
                candidate, changes = normalize_sources(failed["parsed"])
                if changes:
                    row["alias_changes"] = len(changes)
                    try:
                        data = read_json(directory / "atomize/input.json")["input"]
                        validate("atomize", candidate, data, config["solution_source"])
                        row["structural_pass_after_alias"] = True
                        row["text_findings"] = text_findings(candidate["nodes"])
                        if not row["text_findings"]:
                            row["route"] = "lossless_format"
                            seed["normalized_atomize"] = candidate
                            seed["normalization_changes"] = changes
                    except (InvalidOutput, TypeError, KeyError):
                        row["structural_pass_after_alias"] = False
            seeds[item["item_id"]] = seed
        else:
            raise ValueError("unknown terminal status")
        rows.append(row)
    report = {"protocol": PROTOCOL, "source_root": str(root), "source_items_sha256": digest(items),
              "source_selection_sha256": digest(selection), "source_candidates": selection.get("candidate_count", len(items)),
              "source_selected": len(items), "source_exclusions": selection.get("excluded", []),
              "counts": dict(Counter(r["route"] for r in rows)), "inventory": rows,
              "policy": "score-blind triage, not acceptance; semantic repair is opt-in; source/transport problems separate"}
    return items, seeds, report


def audit_quality(source_root, output):
    with source_lock(source_root) as source:
        _, _, report = inspect_locked(source)
        output = Path(output).absolute()
        require(not output.is_relative_to(source) and not source.is_relative_to(output), "audit must be separate")
        write_once(private_dir(output) / "quality-audit.json", report)
    return report


def prepare_recovery(root, source_root, include_semantic=False):
    destination = Path(root).absolute()
    with source_lock(source_root) as source:
        require(destination.resolve() == destination and not destination.is_relative_to(source)
                and not source.is_relative_to(destination), "source and recovery must be separate")
        items, seeds, report = inspect_locked(source)
        routes = {"lossless_format", "semantic_revision"} if include_semantic else {"lossless_format"}
        selected = {r["item_id"]: r for r in report["inventory"] if r["route"] in routes}
        chosen = [item for item in items if item["item_id"] in selected]
        require(bool(chosen), "no eligible recovery candidates")
        private_dir(destination)
        hashes = {}
        paths = [source / name for name in ("items.json", "selection.json", "run_config.json",
                                            "inputs_manifest.json", "completion.json")]
        if (source / "quality_exclusions.json").exists():
            paths.append(source / "quality_exclusions.json")
        for item in chosen:
            paths.extend(sorted((source / "items" / item["item_id"]).rglob("*.json")))
        for path in paths:
            require(path.resolve() == path, "symlink in source evidence")
            relative, data = str(path.relative_to(source)), path.read_bytes()
            hashes[relative] = hashlib.sha256(data).hexdigest()
            write_bytes_once(destination / "originals" / relative, data)
        for item in chosen:
            seed = {**seeds[item["item_id"]], "route": selected[item["item_id"]]["route"]}
            if seed["route"] == "semantic_revision":
                seed["diagnosis"] = selected[item["item_id"]]
            write_once(destination / "items" / item["item_id"] / "recovery_seed.json", seed)
        selection = {"protocol": PROTOCOL, "selected_ids": [i["item_id"] for i in chosen],
                     "selected_count": len(chosen), "candidate_count": report["source_candidates"],
                     "source_selected_count": len(items), "excluded": report["source_exclusions"],
                     "semantic_round_limit": 1, "source_root": str(source),
                     "selection_rule": "all chosen routes; no PALS scores, branching or result-based replacement"}
        # Old spend remains visible, separate from the explicit NEW run's budget.
        requests = list(source.glob("items/*/*/attempt-*/request.json"))
        historical = {"request_attempts": len(requests), "reserved_tokens": sum(
            read_json(p)["reserved_tokens"] for p in requests),
            "note": "Historical allowance, not verified billing; not reset by recovery; new calls need an explicit budget"}
        manifest = {"protocol": PROTOCOL, "source_root": str(source), "round_limit": 1,
                    "items_sha256": digest(chosen), "selection_sha256": digest(selection),
                    "original_file_sha256": hashes, "source_inventory": report,
                    "historical_cost": historical, "seed_sha256": {i["item_id"]: digest(read_json(
                        destination / "items" / i["item_id"] / "recovery_seed.json")) for i in chosen}}
        write_once(destination / "items.json", chosen)
        write_once(destination / "selection.json", selection)
        write_once(destination / "recovery_manifest.json", manifest)
    return {"selected": len(chosen), "routes": dict(Counter(selected[i["item_id"]]["route"] for i in chosen)),
            "manifest_sha256": digest(manifest), "api_calls": 0, "accepted": 0}


class HumanEvalRecoveryPipeline(Pipeline):
    def __init__(self, root, config, client, **kwargs):
        require(config.task_type == "humaneval" and config.prompt_version == VERSION,
                "HumanEval recovery requires the v4 quality protocol")
        manifest = read_json(Path(root) / "recovery_manifest.json")
        require(manifest["protocol"] == PROTOCOL and manifest["round_limit"] == 1, "invalid recovery manifest")
        require(digest(read_json(Path(root) / "items.json")) == manifest["items_sha256"]
                and digest(read_json(Path(root) / "selection.json")) == manifest["selection_sha256"],
                "recovery cohort changed")
        for relative, expected in manifest["original_file_sha256"].items():
            path = Path(root) / "originals" / relative
            require(path.resolve() == path.absolute() and path.is_relative_to(Path(root) / "originals"),
                    "unsafe recovery evidence path")
            require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, "recovery evidence changed")
        self.manifest = manifest
        super().__init__(root, config, client, **kwargs)

    def seed(self, item):
        seed = read_json(self.root / "items" / item["item_id"] / "recovery_seed.json")
        require(digest(seed) == self.manifest["seed_sha256"][item["item_id"]], "recovery seed changed")
        return seed

    def stage(self, stage, item, results):
        seed = self.seed(item)
        data = stage_input(stage, item, results, self.config.solution_source)
        if seed["route"] == "lossless_format" and stage in ("solve", "atomize"):
            value = seed["stage_outputs"]["solve"] if stage == "solve" else seed["normalized_atomize"]
            validate(stage, value, data, self.config.solution_source, prompt_version=VERSION)
            write_once(self.root / "items" / item["item_id"] / "seeded_stages" / (stage + ".json"), {
                "origin": "reused_original_explanation" if stage == "solve" else "lossless_source_alias_normalization",
                "output_sha256": digest(value), "seed_sha256": digest(seed),
                "new_api_call": False, "not_a_new_model_response": True,
            })
            return value
        if seed["route"] == "semantic_revision" and stage == "solve":
            data["revision_context"] = {
                "round": 1, "original_result": seed["original_result"],
                "offline_diagnosis": seed.get("diagnosis", {}),
                "prior_rationale": seed["stage_outputs"].get("solve", {}).get("rationale"),
                "prior_nodes": seed["stage_outputs"].get("atomize", seed["failed_completions"].get("atomize", {}).get("parsed")),
                "prior_dependencies": seed["stage_outputs"].get("dependencies", seed["failed_completions"].get("dependencies", {}).get("parsed")),
                "prior_dag_review": seed["stage_outputs"].get("review_dag"),
                "policy": "Diagnosed proof revision only; never repair reference code or force acceptance",
            }
            return self.request_stage(stage, item, data, payload(stage, data, self.config),
                lambda value: validate(stage, value, data, self.config.solution_source, prompt_version=VERSION))
        return super().stage(stage, item, results)

    def recovery_provenance(self, item):
        seed = self.seed(item)
        return {"protocol": PROTOCOL, "round": 1, "route": seed["route"],
                "source_root": self.manifest["source_root"], "seed_sha256": digest(seed),
                "manifest_sha256": digest(self.manifest), "original_status": seed["original_result"]["status"],
                "normalization_changes": seed.get("normalization_changes", []),
                "both_reviews_rerun": True, "human_approved": False}
