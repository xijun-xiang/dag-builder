"""One final, explicitly enumerated HumanEval patch-and-review campaign.

No recursive repair, new explanations, relaxed validators, or altered source
programs. Local edits and reused stages are evidence, never fabricated API calls.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from .config import Config
from .humaneval_continuation import PROFILE_FIELDS
from .humaneval_quality import CODE_FACT_VERSION, normalize_sources
from .humaneval_recheck import _files, _response
from .humaneval_recovery import source_lock
from .pipeline import Pipeline
from .schemas import require
from .stages import STAGES, payload, stage_input, validate
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .validation import assemble, validate_parents

PROTOCOL = "humaneval-final-local-recovery-v1"
APPROVED_TASKS = (4, 5, 38, 83, 85, 87, 106, 138, 163)
NODE_COUNTS = {4: 12, 5: 14, 38: 14, 83: 13, 85: 15, 87: 13, 106: 14, 138: 10, 163: 15}
PARENT_NAMES = ("humaneval-diagnosed-repair-v1", "humaneval-contract-continuation21-v1")
REVIEW_SCOPE = (
    "Apply every existing quality check without lowering acceptance thresholds. "
    "Self-contained statements means the reasoning nodes' statement fields. "
    "As specified by the justify protocol, audit-only justification fields may "
    "refer to node IDs and quote code; this alone is not a statement defect. "
    "Do not treat a proposed loop-invariant definition as its proof: verify "
    "the base case, conditional preservation and the induction conclusion. "
    "Reject unresolved substantive defects; do not repair this frozen candidate."
)


def revise_outputs(number, original):
    """Replay only the nine approved recipes; return explicit, reproducible edits."""
    require(number in APPROVED_TASKS, "task outside approved final shortlist")
    original = deepcopy(original)
    nodes = original["atomize"]["nodes"]
    require(len(nodes) == NODE_COUNTS[number]
            and [n["node_id"] for n in nodes] == list(range(1, len(nodes) + 1)),
            "unexpected frozen node identity")
    revised = {s: deepcopy(original[s]) for s in ("solve", "review_solution", "atomize")}
    nodes = revised["atomize"]["nodes"]
    edits = []
    mapping = {n["node_id"]: n["node_id"] for n in nodes}
    if number == 85:
        matches = [n["node_id"] for n in nodes if "with step 2" in n["statement"]]
        require(matches == [3, 4, 10], "stride paraphrase target changed")
        for n in nodes:
            if n["node_id"] in matches:
                old = n["statement"]
                n["statement"] = old.replace("with step 2", "with stride two")
                edits.append({"operation": "equivalent_wording", "old_node_id": n["node_id"],
                              "before": old, "after": n["statement"]})
    else:
        parents = deepcopy(original["dependencies"])
        pm = {r["node_id"]: r["parents"] for r in parents["parents"]}
        require(set(pm) == set(mapping), "parent identity mismatch")
        additions = {4: [(1, 8)], 38: [(3, 12)]}.get(number, [])
        removals = {38: [(7, 11)], 138: [(7, 9)], 163: [(8, 13)]}.get(number, [])
        for parent, child in additions:
            require(parent not in pm[child], "approved missing edge already present")
            pm[child].append(parent)
            edits.append({"operation": "add_edge", "old_parent": parent, "old_child": child})
        for parent, child in removals:
            require(parent in pm[child], "approved redundant edge missing")
            pm[child].remove(parent)
            edits.append({"operation": "remove_edge", "old_parent": parent, "old_child": child})
        deleted = {83: 6, 87: 6, 138: 7}.get(number)
        if deleted is not None:
            require(not any(deleted in ps for ps in pm.values()), "cannot prune a still-used node")
            nodes[:] = [n for n in nodes if n["node_id"] != deleted]
            edits.append({"operation": "prune_unused_node", "old_node_id": deleted})
        if number == 106:
            require(nodes[11]["kind"] == "given" and not pm[12], "moved premise must be a root")
            nodes[:] = nodes[:10] + [nodes[11], nodes[10]] + nodes[12:]
            edits.append({"operation": "move_existing_root", "old_node_id": 12, "before_old_node_id": 11})
        mapping = {n["node_id"]: i for i, n in enumerate(nodes, 1)}
        parents = {"parents": [{"node_id": mapping[n["node_id"]],
                                 "parents": sorted(mapping[p] for p in pm[n["node_id"]])} for n in nodes]}
        for n in nodes:
            n["node_id"] = mapping[n["node_id"]]
        validate_parents(parents, nodes)
        revised["dependencies"] = parents
        if number == 5:
            revised["justify"] = deepcopy(original["justify"])
    return revised, {"edits": edits, "old_to_new_node_id": {str(k): v for k, v in mapping.items()}}


def parent_outputs(parent, item):
    """Read complete formal content and bound parent seeds; no field fallback."""
    directory = parent / "items" / item["item_id"]
    config = Config.load(parent / "run_config.json")
    manifest = read_json(parent / "recovery_manifest.json")
    result = read_json(directory / "result.json")
    require(result["status"] in ("rejected", "needs_review"), "only failed sources may enter recovery")
    seed_name = "continuation_seed.json" if parent.name == PARENT_NAMES[1] else "repair_seed.json"
    seed = read_json(directory / seed_name)
    require(digest(seed) == manifest["seed_sha256"][item["item_id"]], "parent seed changed")
    inherited = (seed["reused_outputs"] if seed_name == "continuation_seed.json"
                 else {"solve": seed["stage_outputs"]["solve"]})
    needed = ["solve", "review_solution", "atomize"]
    if item["row"] != 85:
        needed.append("dependencies")
    if item["row"] == 5:
        needed.append("justify")
    outputs = {}
    for stage in needed:
        if stage in inherited:
            value = inherited[stage]
        else:
            data, request, parsed, _ = _response(directory / stage, config)
            require(request == payload(stage, data, config), "parent request/prompt changed")
            value = normalize_sources(parsed)[0] if stage == "atomize" else parsed
            cached = directory / stage / "output.json"
            if cached.exists():
                require(read_json(cached) == value, "parent output differs from formal response")
            else:
                require(result["stage"] == stage, "uncached stage is not the recorded failure")
            if stage != "solve":
                require(data["solution"]["rationale"] == outputs["solve"]["rationale"],
                        "parent stages use different explanations")
                if stage in ("dependencies", "justify"):
                    require(data["nodes"] == outputs["atomize"]["nodes"], "parent nodes changed")
        outputs[stage] = value
    require(outputs["review_solution"]["decision"] == "accept", "explanation review must already pass")
    return outputs


def prepare_final_recovery(root, base):
    """Prepare exactly the approved nine, retaining original files and patch logs."""
    root, base = Path(root).absolute(), Path(base).absolute()
    require(root.resolve() == root and base.resolve() == base, "symlinked campaign path")
    parents = [base / name for name in PARENT_NAMES]
    require(all(not root.is_relative_to(p) and not p.is_relative_to(root) for p in parents),
            "new campaign must be separate from parents")
    require(not (root / "launch.json").exists(), "already launched")
    with ExitStack() as stack:
        for parent in parents:
            stack.enter_context(source_lock(parent))
        snapshots = {p.name: _files(p) for p in parents}
        latest = {}
        for parent in parents:
            for item in read_json(parent / "items.json"):
                if item["row"] in APPROVED_TASKS:
                    latest[item["row"]] = (parent, item)
        require(set(latest) == set(APPROVED_TASKS), "incomplete final shortlist")
        config = replace(Config.load(parents[-1] / "run_config.json"), workers=9,
                         max_calls=72, max_reserved_tokens=8_000_000)
        items, seeds, evidence = [], {}, {}
        private_dir(root)
        for number in APPROVED_TASKS:
            parent, item = latest[number]
            require(item["task_id"] == f"HumanEval/{number}", "task/row mismatch")
            parent_config = Config.load(parent / "run_config.json")
            require(all(getattr(parent_config, k) == getattr(config, k) for k in PROFILE_FIELDS),
                    "source API/model profile mismatch")
            original = parent_outputs(parent, item)
            revised, changes = revise_outputs(number, original)
            done = {}
            for stage, value in revised.items():
                validate(stage, value, stage_input(stage, item, done), prompt_version=CODE_FACT_VERSION)
                done[stage] = value
            require(revised["solve"] == original["solve"] and revised["review_solution"] == original["review_solution"],
                    "explanation or its review changed")
            seed = {"task_id": item["task_id"], "source_root": str(parent), "item": item,
                    "original_result": read_json(parent / "items" / item["item_id"] / "result.json"),
                    "original_outputs": original, "reused_outputs": revised, "change_log": changes,
                    "round": 1, "new_stages": [s for s in STAGES if s not in revised]}
            seeds[item["item_id"]] = seed
            items.append(item)
            paths = list((parent / "items" / item["item_id"]).rglob("*.json")) + list(parent.glob("*.json"))
            for path in paths:
                rel = f"{parent.name}/{path.relative_to(parent)}"
                evidence[rel] = snapshots[parent.name][str(path.relative_to(parent))]
                write_bytes_once(root / "originals" / rel, path.read_bytes())
            write_once(root / "items" / item["item_id"] / "final_recovery_seed.json", seed)
        selection = {"selected_ids": [i["item_id"] for i in items], "selected_count": 9,
                     "candidate_count": 164, "rule": "approved final nine; no PALS-score-based selection"}
        manifest = {"protocol": PROTOCOL, "round_limit": 1, "config_sha256": digest(config.to_dict()),
                    "items_sha256": digest(items), "selection_sha256": digest(selection),
                    "seed_sha256": {k: digest(v) for k, v in seeds.items()},
                    "original_file_sha256": evidence, "review_scope": REVIEW_SCOPE,
                    "maximum_new_semantic_requests": sum(len(s["new_stages"]) for s in seeds.values()),
                    "source_inventory": {"historical_accepted": 75, "known_hold": ["HumanEval/65"],
                                         "prior_candidates_minus_hold": 74, "approved_tasks": list(APPROVED_TASKS)},
                    "historical_cost": {"note": "All earlier paid/unknown calls remain in source records; no refund or reset"}}
        require(manifest["maximum_new_semantic_requests"] == 18, "request plan changed")
        require(all(_files(p) == snapshots[p.name] for p in parents), "source changed during preparation")
        write_once(root / "source_file_hashes.json", snapshots)
        write_once(root / "items.json", items)
        write_once(root / "selection.json", selection)
        write_once(root / "recovery_manifest.json", manifest)
        write_once(root / "prepared_config.json", config.to_dict())
        return {"selected": 9, "new_semantic_requests_max": 18, "api_calls": 0}


class HumanEvalFinalRecoveryPipeline(Pipeline):
    def __init__(self, root, config, client, **kwargs):
        root = Path(root).absolute()
        require(root.resolve() == root, "symlinked recovery root")
        manifest = read_json(root / "recovery_manifest.json")
        require(manifest["protocol"] == PROTOCOL and manifest["round_limit"] == 1,
                "invalid final recovery protocol")
        require(config.prompt_version == CODE_FACT_VERSION and config.task_type == "humaneval"
                and digest(config.to_dict()) == manifest["config_sha256"], "final recovery config changed")
        items = read_json(root / "items.json")
        require(tuple(i["row"] for i in items) == APPROVED_TASKS
                and digest(items) == manifest["items_sha256"]
                and digest(read_json(root / "selection.json")) == manifest["selection_sha256"]
                and manifest["review_scope"] == REVIEW_SCOPE, "final recovery inputs changed")
        for relative, expected in manifest["original_file_sha256"].items():
            path = root / "originals" / relative
            require(path.resolve() == path and path.is_relative_to(root / "originals")
                    and hashlib.sha256(path.read_bytes()).hexdigest() == expected, "source evidence changed")
        self.manifest = manifest
        super().__init__(root, config, client, **kwargs)
        for item in items:
            self.seed(item)

    def seed(self, item):
        seed = read_json(self.root / "items" / item["item_id"] / "final_recovery_seed.json")
        require(digest(seed) == self.manifest["seed_sha256"][item["item_id"]]
                and seed["item"] == item and seed["round"] == 1, "final recovery seed changed")
        values, changes = revise_outputs(item["row"], seed["original_outputs"])
        require(seed["reused_outputs"] == values and seed["change_log"] == changes
                and seed["new_stages"] == [s for s in STAGES if s not in values], "unapproved revision")
        return seed

    def request(self, stage, data):
        request = payload(stage, data, self.config)
        if stage == "review_dag":
            request["messages"][0]["content"] += "\n\n" + REVIEW_SCOPE
        return request

    def stage(self, stage, item, results):
        seed = self.seed(item)
        data = stage_input(stage, item, results, self.config.solution_source)
        if stage in seed["reused_outputs"]:
            value = seed["reused_outputs"][stage]
            validate(stage, value, data, prompt_version=CODE_FACT_VERSION)
            write_once(self.root / "items" / item["item_id"] / "seeded_stages" / (stage + ".json"), {
                "origin": "registered_local_revision" if value != seed["original_outputs"].get(stage)
                          else "preserved_parent_stage", "output_sha256": digest(value),
                "seed_sha256": digest(seed), "new_api_call": False})
            return value
        require(stage in seed["new_stages"], "unplanned paid stage")
        return self.request_stage(stage, item, data, self.request(stage, data),
            lambda value: validate(stage, value, data, prompt_version=CODE_FACT_VERSION))

    def process(self, item, through="review_dag"):
        path = self.root / "items" / item["item_id"] / "result.json"
        if path.exists():
            result = read_json(path)
            require(result["item_id"] == item["item_id"], "cached result identity mismatch")
            return result
        return super().process(item, through)

    def recovery_provenance(self, item):
        seed = self.seed(item)
        return {"protocol": PROTOCOL, "round": 1, "manifest_sha256": digest(self.manifest),
                "seed_sha256": digest(seed), "change_log_sha256": digest(seed["change_log"]),
                "source_root": seed["source_root"], "solution_review_origin": "preserved_parent_response",
                "dag_review_origin": "new_response", "local_edits": seed["change_log"],
                "reused_stages": list(seed["reused_outputs"]), "human_approved": False}

    def validate_export(self, item, dag):
        seed = self.seed(item)
        require(dag.get("recovery_provenance") == self.recovery_provenance(item), "final provenance mismatch")
        results = {}
        for stage in STAGES:
            data = stage_input(stage, item, results)
            if stage in seed["reused_outputs"]:
                value = seed["reused_outputs"][stage]
            else:
                directory = self.root / "items" / item["item_id"] / stage
                observed, request, value, _ = _response(directory, self.config)
                require(observed == data and request == self.request(stage, data)
                        and value == read_json(directory / "output.json"), "new stage evidence mismatch")
            validate(stage, value, data, prompt_version=CODE_FACT_VERSION)
            results[stage] = value
        require(results["review_dag"]["decision"] == results["review_solution"]["decision"] == "accept",
                "unaccepted final reviews")
        require(dag["reference_solution"]["rationale"] == results["solve"]["rationale"]
                and dag["solution_review"] == results["review_solution"]
                and dag["dag_review"] == results["review_dag"]
                and dag["nodes"] == assemble(results["atomize"]["nodes"], results["dependencies"], results["justify"]),
                "export differs from evidence-bound stages")


if __name__ == "__main__":
    import argparse
    import os
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_final_recovery(args.root, args.base)))
