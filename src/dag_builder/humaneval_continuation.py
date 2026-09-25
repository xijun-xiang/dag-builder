"""Complete contract-blocked HumanEval stages without another semantic round.

The failed source run remains immutable. Reused outputs are bound to original
responses and recorded as seeded stages, never fabricated new API responses.
"""
from collections import Counter
import hashlib
from pathlib import Path

from .config import Config
from .humaneval_quality import CODE_FACT_VERSION, normalize_sources
from .humaneval_recheck import FAILURES, _files, _response
from .humaneval_recovery import source_lock
from .humaneval_repair import (
    CONTRACT_PROTOCOL, PROTOCOL as PARENT_PROTOCOL, HumanEvalRepairPipeline,
    diagnosis_input, validate_diagnosis,
)
from .pipeline import Pipeline
from .schemas import require
from .stages import payload, stage_input, validate
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .validation import assemble

PROTOCOL = "humaneval-contract-continuation-v1"
PROFILE_FIELDS = ("base_url", "model", "task_type", "solution_source", "thinking",
                  "reasoning_effort", "response_format", "max_tokens", "temperature",
                  "strict_response_contract", "content_gated_response", "transport", "tls_max_version")


def _saved_output(directory, stage, config):
    data, request, parsed, _ = _response(directory / stage, config)
    require(request == payload(stage, data, config), "preserved stage request changed")
    require(parsed == read_json(directory / stage / "output.json"), "preserved output differs from content")
    validate(stage, parsed, data, prompt_version=config.prompt_version)
    return parsed, data


def prepare_continuation(root, source_root, recheck_root):
    """Offline preparation from all passing, evidence-bound contract rechecks."""
    root, audit = Path(root).absolute(), Path(recheck_root).absolute()
    with source_lock(source_root) as source:
        require(root.resolve() == root and audit.resolve() == audit, "symlinked continuation paths")
        require(all(not root.is_relative_to(p) and not p.is_relative_to(root) for p in (source, audit)),
                "continuation root must be separate from source and recheck")
        report = read_json(audit / "report.json")
        require(report["protocol"] == "humaneval-contract-recheck-v1"
                and report["source_protocol"] == PARENT_PROTOCOL and Path(report["source_root"]) == source
                and report["target_construction_protocol"] == CODE_FACT_VERSION
                and report["target_repair_protocol"] == CONTRACT_PROTOCOL,
                "unsupported recheck lineage")
        before = _files(source)
        require(before == read_json(audit / "source_file_hashes.json")
                and digest(before) == report["source_file_hashes_sha256"], "parent changed since recheck")
        config = Config.load(source / "run_config.json")
        parent = HumanEvalRepairPipeline(source, config, object())
        require(parent.protocol == PARENT_PROTOCOL, "only a direct diagnosed-v1 continuation is allowed")
        rows = {row["item_id"]: row for row in report["items"]}
        require(len(rows) == report["selected"] == len(report["items"]), "duplicate recheck IDs")
        chosen, seeds, inventory = [], {}, []
        expected_rechecks = set()
        for item in read_json(source / "items.json"):
            item_id = item["item_id"]
            directory = source / "items" / item_id
            result = read_json(directory / "result.json")
            require(result["item_id"] == item_id, "parent result identity mismatch")
            known = result["status"] == "needs_review" and (result["stage"], result["reason"]) in FAILURES
            if known:
                expected_rechecks.add(item_id)
            row = rows.get(item_id)
            inventory.append({"item_id": item_id, "task_id": item["task_id"], "parent_result": result,
                              "selected": bool(row and row["status"] == "contract_recheck_passed")})
            if not row or row["status"] != "contract_recheck_passed":
                continue
            require(known and row["task_id"] == item["task_id"] and row["original_result"] == result
                    and row["original_result_sha256"] == digest(result), "recheck/result binding mismatch")
            prior_seed = parent.seed(item)
            data, _, parsed, response = _response(directory / result["stage"], config)
            candidate = normalize_sources(parsed)[0] if result["stage"] == "atomize" else parsed
            require(str(response.relative_to(source)) == row["response_path"]
                    and before[str(response.relative_to(source))] == row["response_file_sha256"]
                    and digest(parsed) == row["parsed_content_sha256"]
                    and digest(candidate) == row["candidate_sha256"]
                    and digest(data) == row["original_input_sha256"]
                    and candidate == read_json(audit / "items" / item_id / "candidate.json"),
                    "recheck candidate differs from original response")
            reused = {}
            if result["stage"] == "atomize":
                diagnosis = _response(directory / "diagnose", config)[2]
                require(diagnosis == read_json(directory / "diagnose/output.json"), "cached diagnosis changed")
                validate_diagnosis(diagnosis, diagnosis_input(item, prior_seed))
                if diagnosis["route"] == "graph_repair":
                    reused["solve"] = prior_seed["stage_outputs"]["solve"]
                else:
                    reused["solve"] = _saved_output(directory, "solve", config)[0]
                reused["review_solution"], reviewed_data = _saved_output(directory, "review_solution", config)
                require(reviewed_data == stage_input("review_solution", item, reused),
                        "reused review input differs from frozen explanation")
                require(reused["review_solution"]["decision"] == "accept", "cannot reuse a failed review")
                require(data["solution"] == stage_input("atomize", item, reused)["solution"],
                        "reused nodes refer to another explanation")
                reused["atomize"] = candidate
            else:
                diagnosis = candidate
                require(data == diagnosis_input(item, prior_seed), "original diagnosis input changed")
                if diagnosis["route"] == "graph_repair":
                    reused["solve"] = prior_seed["stage_outputs"]["solve"]
            validate_diagnosis(diagnosis, diagnosis_input(item, prior_seed, protocol=CONTRACT_PROTOCOL))
            completed = {}
            for stage, value in reused.items():
                validate(stage, value, stage_input(stage, item, completed), prompt_version=CODE_FACT_VERSION)
                completed[stage] = value
            seed = {"parent_repair_seed": prior_seed, "parent_result": result,
                    "diagnosis": diagnosis, "reused_outputs": reused, "recheck": row,
                    "semantic_round": 1, "contract_continuation_round": 1}
            chosen.append(item)
            seeds[item_id] = seed
        require(set(rows) == expected_rechecks, "recheck does not cover the exact known failure cohort")
        require(chosen, "no passing contract rechecks")
        private_dir(root)
        evidence = {}
        paths = [p for p in source.glob("*.json")]
        paths += list((source / "originals").rglob("*.json"))
        for item in chosen:
            paths += list((source / "items" / item["item_id"]).rglob("*.json"))
        for path in sorted(set(paths)):
            relative = str(path.relative_to(source))
            evidence[relative] = before[relative]
            write_bytes_once(root / "originals" / relative, path.read_bytes())
        write_once(root / "recheck_report.json", report)
        for item_id, seed in seeds.items():
            write_once(root / "items" / item_id / "continuation_seed.json", seed)
        selection = {"protocol": PROTOCOL, "selected_ids": [item["item_id"] for item in chosen],
                     "selected_count": len(chosen), "candidate_count": parent.manifest["source_inventory"]["source_candidates"],
                     "excluded": parent.manifest["source_inventory"].get("source_exclusions", []),
                     "rule": "all passing rechecks of two exact interface failures; no second semantic candidate"}
        requests = [read_json(path) for path in source.glob("items/*/*/attempt-*/request.json")]
        historical = parent.manifest["historical_cost"]
        manifest = {"protocol": PROTOCOL, "round_limit": 1, "source_root": str(source),
                    "items_sha256": digest(chosen), "selection_sha256": digest(selection),
                    "seed_sha256": {key: digest(value) for key, value in seeds.items()},
                    "original_file_sha256": evidence, "recheck_report_sha256": digest(report),
                    "source_profile": {key: getattr(config, key) for key in PROFILE_FIELDS},
                    "source_inventory": {"parent": parent.manifest["source_inventory"], "continuation": inventory},
                    "historical_cost": {"request_attempts": historical["request_attempts"] + len(requests),
                        "reserved_tokens": historical["reserved_tokens"] + sum(r["reserved_tokens"] for r in requests),
                        "note": "Prior requests remain paid/unknown history; new batch allowance is additional, not a refund"},
                    "reused_stage_counts": dict(Counter(stage for seed in seeds.values() for stage in seed["reused_outputs"])),
                    "maximum_new_semantic_requests": sum(6 - len(seed["reused_outputs"]) for seed in seeds.values()
                                                         if seed["diagnosis"]["route"] != "source_concern")}
        require(_files(source) == before, "parent changed during preparation")
        write_once(root / "items.json", chosen)
        write_once(root / "selection.json", selection)
        write_once(root / "recovery_manifest.json", manifest)
        return {"selected": len(chosen), "reused_stage_counts": manifest["reused_stage_counts"],
                "maximum_new_semantic_requests": manifest["maximum_new_semantic_requests"], "api_calls": 0}


class HumanEvalContinuationPipeline(Pipeline):
    def __init__(self, root, config, client, **kwargs):
        root = Path(root).absolute()
        require(root.resolve() == root, "symlinked continuation root")
        manifest = read_json(root / "recovery_manifest.json")
        require(manifest["protocol"] == PROTOCOL and manifest["round_limit"] == 1,
                "invalid contract continuation lineage")
        require(config.task_type == "humaneval" and config.prompt_version == CODE_FACT_VERSION,
                "contract continuation requires HumanEval v5")
        require(all(getattr(config, key) == value for key, value in manifest["source_profile"].items()),
                "continuation model/API profile changed")
        require(digest(read_json(root / "items.json")) == manifest["items_sha256"]
                and digest(read_json(root / "selection.json")) == manifest["selection_sha256"]
                and digest(read_json(root / "recheck_report.json")) == manifest["recheck_report_sha256"],
                "continuation inputs changed")
        for relative, expected in manifest["original_file_sha256"].items():
            path = root / "originals" / relative
            require(path.resolve() == path and path.is_relative_to(root / "originals")
                    and hashlib.sha256(path.read_bytes()).hexdigest() == expected, "continuation evidence changed")
        self.manifest = manifest
        super().__init__(root, config, client, **kwargs)

    def seed(self, item):
        seed = read_json(self.root / "items" / item["item_id"] / "continuation_seed.json")
        require(digest(seed) == self.manifest["seed_sha256"][item["item_id"]]
                and seed["semantic_round"] == seed["contract_continuation_round"] == 1,
                "continuation seed changed")
        validate_diagnosis(seed["diagnosis"], diagnosis_input(item, seed["parent_repair_seed"], protocol=CONTRACT_PROTOCOL))
        return seed

    def _record_reuse(self, item, stage, value):
        write_once(self.root / "items" / item["item_id"] / "seeded_stages" / (stage + ".json"), {
            "origin": "preserved_parent_response_or_bound_seed", "output_sha256": digest(value),
            "seed_sha256": self.manifest["seed_sha256"][item["item_id"]], "new_api_call": False})
        return value

    def process(self, item, through="review_dag"):
        terminal = self.root / "items" / item["item_id"] / "result.json"
        if terminal.exists():
            result = read_json(terminal)
            require(result["item_id"] == item["item_id"], "cached continuation identity mismatch")
            return result
        diagnosis = self._record_reuse(item, "diagnose", self.seed(item)["diagnosis"])
        if diagnosis["route"] == "source_concern":
            return self._finish(item, "rejected", "diagnose", "preserved source concern; no regeneration")
        return super().process(item, through)

    def stage(self, stage, item, results):
        seed = self.seed(item)
        data = stage_input(stage, item, results, self.config.solution_source)
        if stage in seed["reused_outputs"]:
            value = seed["reused_outputs"][stage]
            validate(stage, value, data, prompt_version=CODE_FACT_VERSION)
            return self._record_reuse(item, stage, value)
        if stage in ("solve", "atomize", "dependencies"):
            prior = seed["parent_repair_seed"]["stage_outputs"]
            data["revision_context"] = {
                "protocol": PROTOCOL, "semantic_round": 1, "diagnosis": seed["diagnosis"],
                "prior_rationale": prior.get("solve", {}).get("rationale"),
                "prior_nodes": prior.get("atomize"), "prior_dependencies": prior.get("dependencies"),
                "policy": "Continue the same diagnosed repair after a local interface fix. Never change supplied nodes, task or code; no second semantic candidate or graph-branching target.",
            }
        return self.request_stage(stage, item, data, payload(stage, data, self.config),
            lambda value: validate(stage, value, data, prompt_version=CODE_FACT_VERSION))

    def recovery_provenance(self, item):
        seed = self.seed(item)
        return {"protocol": PROTOCOL, "round": 1, "manifest_sha256": digest(self.manifest),
                "seed_sha256": digest(seed), "source_root": self.manifest["source_root"],
                "route": seed["diagnosis"]["route"], "diagnosis_sha256": digest(seed["diagnosis"]),
                "reused_stages": ["diagnose", *seed["reused_outputs"]],
                "solution_review_origin": "preserved_parent_response" if "review_solution" in seed["reused_outputs"] else "new_response",
                "dag_review_origin": "new_response", "human_approved": False}

    def validate_export(self, item, dag):
        """Check mixed provenance without claiming reused reviews are new calls."""
        seed = self.seed(item)
        require(dag.get("recovery_provenance") == self.recovery_provenance(item), "continuation provenance mismatch")
        require(seed["diagnosis"]["route"] != "source_concern", "source concern cannot be exported")
        results = dict(seed["reused_outputs"])
        for stage in ("solve", "review_solution", "atomize", "dependencies", "justify", "review_dag"):
            if stage not in results:
                results[stage] = read_json(self.root / "items" / item["item_id"] / stage / "output.json")
        require(dag["reference_solution"]["rationale"] == results["solve"]["rationale"]
                and dag["solution_review"] == results["review_solution"]
                and dag["dag_review"] == results["review_dag"]
                and dag["nodes"] == assemble(results["atomize"]["nodes"], results["dependencies"], results["justify"]),
                "continuation DAG differs from preserved or newly completed stages")
