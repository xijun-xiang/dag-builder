"""One diagnosed semantic repair, rooted in a first-pass HumanEval cohort.

Only a completed lossless-format recovery may be overlaid. This is deliberately
not a recursive retry-until-accepted service. Existing accepted items and source
quarantines remain in the complete disposition inventory, outside paid work.
"""
from collections import Counter
from contextlib import ExitStack
import hashlib
from importlib.resources import files
import json
from pathlib import Path

from .client import CallFailure
from .config import Config
from .humaneval_recovery import baseline, inspect_locked, source_lock, HumanEvalRecoveryPipeline
from .humaneval_quality import VERSION, CODE_FACT_VERSION
from .pipeline import Pipeline
from .schemas import InvalidOutput, public_question, require, text
from .stages import payload, request_controls, stage_input, validate
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

PROTOCOL = "humaneval-diagnosed-repair-v1"
CONTRACT_PROTOCOL = "humaneval-diagnosed-repair-v2"
PROTOCOL_VERSIONS = {PROTOCOL: VERSION, CONTRACT_PROTOCOL: CODE_FACT_VERSION}
ROUTES = ("graph_repair", "rationale_revision", "source_concern")


def diagnosis_prompt(protocol=PROTOCOL):
    require(protocol in PROTOCOL_VERSIONS, "unknown diagnosed repair protocol")
    return files("dag_builder").joinpath("prompts", protocol, "diagnose.md").read_text(encoding="utf-8")


def recovery_pipeline_type(root):
    protocol = read_json(Path(root) / "recovery_manifest.json")["protocol"]
    if protocol == "humaneval-final-local-recovery-v1":
        from .humaneval_final_recovery import HumanEvalFinalRecoveryPipeline
        return HumanEvalFinalRecoveryPipeline
    if protocol == "humaneval-contract-continuation-v1":
        from .humaneval_continuation import HumanEvalContinuationPipeline
        return HumanEvalContinuationPipeline
    if protocol in PROTOCOL_VERSIONS:
        return HumanEvalRepairPipeline
    if protocol == "humaneval-recovery-v1":
        return HumanEvalRecoveryPipeline
    raise ValueError("unknown HumanEval recovery protocol")


def diagnosis_input(item, seed, *, protocol=PROTOCOL):
    require(protocol in PROTOCOL_VERSIONS, "unknown diagnosed repair protocol")
    outputs = seed["stage_outputs"]
    nodes = outputs.get("atomize", seed.get("failed_completions", {}).get("atomize", {}).get("parsed", {}))
    # Explicit allowlist: benchmark tests and complete source records never enter API calls.
    data = {
        "question": public_question(item), "reference_code": item["canonical_solution"],
        "reference_execution": "not_executed",
        "evidence_sources": {
            "question": item["question"], "reference_code": item["canonical_solution"],
            "rationale": outputs.get("solve", {}).get("rationale", ""),
            "prior_nodes": json.dumps(nodes, ensure_ascii=False, sort_keys=True),
            "prior_reviews": json.dumps({k: outputs.get(k) for k in ("review_solution", "review_dag")},
                                        ensure_ascii=False, sort_keys=True),
            "failure": json.dumps(seed["latest_result"], ensure_ascii=False, sort_keys=True),
        },
        "prior_dependencies": outputs.get("dependencies"),
        "prior_justifications": outputs.get("justify"),
    }
    if protocol == CONTRACT_PROTOCOL:
        # Serialize the already supplied evidence; never invent/fuzzily match quotes.
        for name in ("prior_dependencies", "prior_justifications"):
            data["evidence_sources"][name] = json.dumps(data[name], ensure_ascii=False, sort_keys=True)
    return data


def validate_diagnosis(value, data):
    require(isinstance(value, dict) and value.get("route") in ROUTES, "invalid repair route")
    require(type(value.get("rationale_reusable")) is bool and text(value.get("reason")), "invalid diagnosis fields")
    require("source_consistent" in value and (value["source_consistent"] is None or type(value["source_consistent"]) is bool),
            "invalid source-consistency flag")
    issues = value.get("issues")
    require(isinstance(issues, list) and issues, "diagnosis needs cited evidence")
    for issue in issues:
        require(isinstance(issue, dict) and issue.get("evidence_source") in data["evidence_sources"],
                "unknown diagnosis evidence source")
        quote = issue.get("quote")
        require(text(quote) and quote in data["evidence_sources"][issue["evidence_source"]],
                "diagnosis quote must match supplied evidence")
        require(text(issue.get("problem")) and text(issue.get("action")), "diagnosis needs problem and action")
    if value["route"] == "source_concern":
        require(value.get("source_consistent") is not True and value["rationale_reusable"] is False,
                "source concern must stay quarantined")
    else:
        require(value.get("source_consistent") is True, "repair cannot waive source concerns")
        require(value["rationale_reusable"] == (value["route"] == "graph_repair"), "route/reuse mismatch")
        if value["route"] == "graph_repair":
            require(text(data["evidence_sources"]["rationale"]), "graph repair needs a complete original rationale")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_diagnosed_repair(root, source_root, format_root=None, quarantines=None, *, protocol=PROTOCOL):
    """Offline full-cohort triage, immutable ancestry, and exactly one repair round."""
    require(protocol in PROTOCOL_VERSIONS, "unknown diagnosed repair protocol")
    root = Path(root).absolute()
    with ExitStack() as stack:
        source = stack.enter_context(source_lock(source_root))
        items, _, source_inventory = inspect_locked(source)
        parents = {"first_pass": source}
        overlay_items = {}
        if format_root is not None:
            overlay = stack.enter_context(source_lock(format_root))
            require(overlay != source, "duplicate parent root")
            manifest = read_json(overlay / "recovery_manifest.json")
            require(manifest["protocol"] == "humaneval-recovery-v1" and Path(manifest["source_root"]) == source,
                    "only a direct lossless-format overlay is allowed")
            verifier = HumanEvalRecoveryPipeline(overlay, Config.load(overlay / "run_config.json"), object())
            overlay_items = {i["item_id"]: i for i in read_json(overlay / "items.json")}
            originals = {i["item_id"]: i for i in items}
            require(all(originals.get(k) == v for k, v in overlay_items.items()), "overlay source identity mismatch")
            require(all(verifier.seed(i)["route"] == "lossless_format" for i in overlay_items.values()),
                    "semantic repair cannot be recursively overlaid")
            parents["format_recovery"] = overlay
        require(root.resolve() == root and all(not root.is_relative_to(p) and not p.is_relative_to(root)
                                              for p in parents.values()), "repair root must be separate")
        quarantines = quarantines or []
        require(isinstance(quarantines, list), "quarantine list required")
        quarantine_by_task = {}
        for entry in quarantines:
            require(isinstance(entry, dict) and entry.get("task_id") not in quarantine_by_task
                    and text(entry.get("reason")) and text(entry.get("evidence_result_sha256")),
                    "invalid or duplicate quarantine")
            quarantine_by_task[entry["task_id"]] = entry
        require(set(quarantine_by_task) <= {i["task_id"] for i in items}, "unknown quarantine task")
        rows, chosen, seeds = [], [], {}
        first_rows = {r["item_id"]: r for r in source_inventory["inventory"]}
        for item in items:
            item_id = item["item_id"]
            first = source / "items" / item_id
            latest = parents["format_recovery"] / "items" / item_id if item_id in overlay_items else first
            result = read_json(latest / "result.json") if (latest / "result.json").exists() else {
                "item_id": item_id, "status": "unfinished", "stage": "transport", "reason": "no terminal result"}
            require(result["item_id"] == item_id, "result identity mismatch")
            row = {"item_id": item_id, "task_id": item["task_id"], "latest_result": result,
                   "latest_parent": "format_recovery" if item_id in overlay_items else "first_pass"}
            explicit = quarantine_by_task.get(item["task_id"])
            if explicit:
                require(digest(result) == explicit["evidence_result_sha256"], "quarantine evidence changed")
            if explicit or first_rows[item_id].get("quality_veto"):
                row.update(disposition="source_quarantine", evidence=explicit or first_rows[item_id]["quality_veto"])
            elif result["status"] == "model_accepted":
                dag = read_json(latest / "dag.json")
                require(digest(dag) == result["dag_sha256"] and dag["source"] == item, "accepted ancestry changed")
                row["disposition"] = "retain_accepted_without_regeneration"
            else:
                row["disposition"] = "diagnose_once_then_repair_or_quarantine"
                seed = baseline(first)
                if item_id in overlay_items:
                    prior_seed = read_json(latest / "recovery_seed.json")
                    seed["stage_outputs"].update(prior_seed["stage_outputs"])
                    seed["stage_outputs"]["atomize"] = prior_seed["normalized_atomize"]
                    refreshed = baseline(latest)
                    seed["stage_outputs"].update(refreshed["stage_outputs"])
                    seed["failed_completions"].update(refreshed["failed_completions"])
                seed.update(latest_result=result, source_parent=row["latest_parent"], semantic_round=1)
                chosen.append(item)
                seeds[item_id] = seed
            rows.append(row)
        require(chosen, "no unresolved repair candidates")
        private_dir(root)
        hashes = {}
        for label, parent in parents.items():
            paths = [parent / name for name in ("items.json", "selection.json", "run_config.json",
                                               "inputs_manifest.json", "completion.json")]
            for name in ("recovery_manifest.json", "quality_exclusions.json"):
                if (parent / name).exists():
                    paths.append(parent / name)
            for item in chosen:
                paths.extend(sorted((parent / "items" / item["item_id"]).rglob("*.json")))
            for path in paths:
                require(path.resolve() == path, "symlinked parent evidence")
                relative = label + "/" + str(path.relative_to(parent))
                raw = path.read_bytes()
                hashes[relative] = hashlib.sha256(raw).hexdigest()
                write_bytes_once(root / "originals" / relative, raw)
        for item_id, seed in seeds.items():
            write_once(root / "items" / item_id / "repair_seed.json", seed)
        selection = {"protocol": protocol, "selected_ids": [i["item_id"] for i in chosen],
                     "selected_count": len(chosen), "candidate_count": source_inventory["source_candidates"],
                     "excluded": source_inventory["source_exclusions"], "semantic_round_limit": 1,
                     "selection_rule": "all unresolved items except evidence-bound source quarantines; score-blind",
                     "disposition_counts": dict(Counter(r["disposition"] for r in rows))}
        inventory = {**source_inventory, "disposition_inventory": rows, "disposition_counts": selection["disposition_counts"]}
        requests = [p for parent in parents.values() for p in parent.glob("items/*/*/attempt-*/request.json")]
        manifest = {"protocol": protocol, "round_limit": 1, "source_root": str(source),
                    "diagnosis_prompt_sha256": digest(diagnosis_prompt(protocol)),
                    "parent_roots": {label: str(p) for label, p in parents.items()},
                    "items_sha256": digest(chosen), "selection_sha256": digest(selection),
                    "source_inventory": inventory, "original_file_sha256": hashes,
                    "seed_sha256": {k: digest(v) for k, v in seeds.items()},
                    "historical_cost": {"request_attempts": len(requests), "reserved_tokens": sum(
                        read_json(p)["reserved_tokens"] for p in requests), "note": "Original and format-recovery costs retained; not a bill"}}
        write_once(root / "items.json", chosen)
        write_once(root / "selection.json", selection)
        write_once(root / "recovery_manifest.json", manifest)
        return {"selected": len(chosen), "dispositions": selection["disposition_counts"],
                "manifest_sha256": digest(manifest), "api_calls": 0}


class HumanEvalRepairPipeline(Pipeline):
    def __init__(self, root, config, client, **kwargs):
        root = Path(root).absolute()
        manifest = read_json(root / "recovery_manifest.json")
        self.protocol = manifest["protocol"]
        require(self.protocol in PROTOCOL_VERSIONS and manifest["round_limit"] == 1, "invalid repair lineage")
        require(config.task_type == "humaneval" and config.prompt_version == PROTOCOL_VERSIONS[self.protocol],
                "repair protocol/config mismatch")
        require(manifest["diagnosis_prompt_sha256"] == digest(diagnosis_prompt(self.protocol)), "diagnosis prompt changed")
        require(digest(read_json(root / "items.json")) == manifest["items_sha256"]
                and digest(read_json(root / "selection.json")) == manifest["selection_sha256"], "repair selection changed")
        for relative, expected in manifest["original_file_sha256"].items():
            path = root / "originals" / relative
            require(path.resolve() == path and path.is_relative_to(root / "originals")
                    and _sha(path) == expected, "repair ancestry changed")
        self.manifest = manifest
        super().__init__(root, config, client, **kwargs)

    def seed(self, item):
        seed = read_json(self.root / "items" / item["item_id"] / "repair_seed.json")
        require(digest(seed) == self.manifest["seed_sha256"][item["item_id"]] and seed["semantic_round"] == 1,
                "repair seed changed")
        return seed

    def process(self, item, through="review_dag"):
        terminal = self.root / "items" / item["item_id"] / "result.json"
        if terminal.exists():
            result = read_json(terminal)
            require(result["item_id"] == item["item_id"], "cached result identity mismatch")
            return result
        try:
            if self._stop.is_set():
                return {"item_id": item["item_id"], "status": "paused", "stage": "diagnose"}
            data = diagnosis_input(item, self.seed(item), protocol=self.protocol)
            request = dict(request_controls(self.config), messages=[
                {"role": "system", "content": diagnosis_prompt(self.protocol)},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False, sort_keys=True)}])
            diagnosis = self.request_stage("diagnose", item, data, request, lambda v: validate_diagnosis(v, data))
            if diagnosis["route"] == "source_concern":
                return self._finish(item, "rejected", "diagnose", "source_concern: " + diagnosis["reason"])
        except InvalidOutput as error:
            return self._finish(item, "needs_review", "diagnose", str(error))
        except CallFailure as error:
            if not (self.resilient and error.category == "transient_retries_exhausted"):
                self._stop.set()
            return {"item_id": item["item_id"], "status": "paused", "stage": "diagnose", "reason": error.category}
        return super().process(item, through)

    def stage(self, stage, item, results):
        seed = self.seed(item)
        diagnosis = read_json(self.root / "items" / item["item_id"] / "diagnose/output.json")
        data = stage_input(stage, item, results, self.config.solution_source)
        if stage == "solve" and diagnosis["route"] == "graph_repair":
            value = seed["stage_outputs"]["solve"]
            validate(stage, value, data, self.config.solution_source, prompt_version=self.config.prompt_version)
            write_once(self.root / "items" / item["item_id"] / "seeded_stages/solve.json", {
                "origin": "unchanged_original_explanation", "output_sha256": digest(value),
                "seed_sha256": digest(seed), "new_api_call": False})
            return value
        if stage in ("solve", "atomize", "dependencies"):
            data["revision_context"] = {
                "protocol": self.protocol, "semantic_round": 1, "diagnosis": diagnosis,
                "prior_rationale": seed["stage_outputs"].get("solve", {}).get("rationale"),
                "prior_nodes": seed["stage_outputs"].get("atomize"),
                "prior_dependencies": seed["stage_outputs"].get("dependencies"),
                "prior_review": seed["stage_outputs"].get("review_dag"),
                "policy": "Repair only diagnosed proof/representation defects. Never change source task/code or optimize graph branching. No second semantic candidate.",
            }
            return self.request_stage(stage, item, data, payload(stage, data, self.config),
                lambda v: validate(stage, v, data, self.config.solution_source, prompt_version=self.config.prompt_version))
        # Reviews deliberately receive only the current candidate, not old verdicts or repair encouragement.
        return super().stage(stage, item, results)

    def recovery_provenance(self, item):
        seed = self.seed(item)
        diagnosis = read_json(self.root / "items" / item["item_id"] / "diagnose/output.json")
        return {"protocol": self.protocol, "round": 1, "route": diagnosis["route"],
                "manifest_sha256": digest(self.manifest), "seed_sha256": digest(seed),
                "diagnosis_sha256": digest(diagnosis), "source_root": self.manifest["source_root"],
                "original_status": seed["latest_result"]["status"], "both_reviews_rerun": True,
                "rationale_reused": diagnosis["route"] == "graph_repair", "human_approved": False}
