"""CPU-gated t2ance explanation DAGs using the existing strict step review.

This source has its own provenance and budget. It deliberately reuses the
CALIBRI structural validators, not CALIBRI's source or acceptance labels.
"""

import json
from pathlib import Path

from .calibri_pipeline import CALIBRIPipeline
from .calibri_normalize import public_input
from .client import CallFailure
from .config import Config
from .livecodebench_dag import sha256, verify_execution
from .livecodebench_source import select_canary
from .pipeline import Pipeline
from .schemas import InvalidOutput, require
from .stages import payload
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .t2ance_source import inspect_candidate
from .t2ance_v3 import PROTOCOL as PROTOCOL_V3, assemble_graph_v3, normalize_v3, validate_audit_v3
from .t2ance_v4 import PROTOCOL as PROTOCOL_V4, assemble_graph_v4, normalize_v4, validate_audit_v4

PROTOCOL = "t2ance-lcb-normalize-v1"
PROTOCOL_V2 = "t2ance-lcb-normalize-v2"
PROTOCOLS = (PROTOCOL, PROTOCOL_V2, PROTOCOL_V3, PROTOCOL_V4)
# The original 240 / 16M campaign allocation was exhausted by conservative
# byte-based accounting during the full CPU-verified cohort.  A new,
# explicitly versioned continuation retains every old request and response;
# these are upper bounds for additional allocations, not permission to retry
# semantic failures or overwrite an earlier run.
CALL_LIMIT = 600
TOKEN_LIMIT = 60000000


def prepare(source, execution, expected_manifest, root, *, limit=None,
            development_run=None, max_calls=24, max_reserved_tokens=2000000,
            prior_calls=0, prior_reserved_tokens=0, prompt_version=PROTOCOL,
            semantic_quarantine=None):
    """Freeze reviewed input only after the same independent CPU gate passes."""
    source, execution, root = Path(source), Path(execution), private_dir(root)
    require(prompt_version in PROTOCOLS, "unknown t2ance normalization protocol")
    require(type(max_calls) is int and type(prior_calls) is int and prior_calls >= 0
            and 0 < max_calls <= CALL_LIMIT - prior_calls, "t2ance call allocation exceeded")
    require(type(max_reserved_tokens) is int and type(prior_reserved_tokens) is int
            and prior_reserved_tokens >= 0
            and 0 < max_reserved_tokens <= TOKEN_LIMIT - prior_reserved_tokens,
            "t2ance token allocation exceeded")
    manifest = read_json(source / "t2ance-manifest.json")
    source_items = read_json(source / "items.json")
    require(manifest["protocol"] == "t2ance-lcb-source-v1"
            and digest(source_items) == manifest["items_sha256"], "source selection changed")
    quarantine = None
    quarantine_ids = set()
    if semantic_quarantine is not None:
        require(prompt_version in (PROTOCOL_V3, PROTOCOL_V4),
                "semantic quarantine requires answer-backward protocol")
        quarantine = read_json(semantic_quarantine)
        require(isinstance(quarantine, dict) and set(quarantine) == {"protocol", "records"}
                and quarantine["protocol"] == "t2ance-semantic-quarantine-v1"
                and isinstance(quarantine["records"], list)
                and bool(quarantine["records"]), "invalid semantic quarantine manifest")
        source_by_id = {i["item_id"]: i for i in source_items}
        for record in quarantine["records"]:
            require(isinstance(record, dict) and set(record) == {
                "item_id", "reference_code_sha256", "counterexample", "reason"},
                "invalid semantic quarantine record")
            item_id = record["item_id"]
            case = record["counterexample"]
            require(isinstance(item_id, str) and item_id in source_by_id
                    and item_id not in quarantine_ids
                    and record["reference_code_sha256"] == digest(source_by_id[item_id]["reference_code"])
                    and isinstance(case, dict) and set(case) == {"input", "expected", "observed"}
                    and all(isinstance(case[k], str) and case[k].strip()
                            for k in ("input", "expected", "observed"))
                    and isinstance(record["reason"], str) and record["reason"].strip(),
                    "semantic quarantine is not bound to the frozen code and counterexample")
            quarantine_ids.add(item_id)
    cpu, completion = verify_execution(execution, expected_manifest)
    require(all(item_id in cpu and cpu[item_id][1]["status"] == "passed"
                for item_id in quarantine_ids),
            "semantic quarantine must refer to independently CPU-passing candidates")
    cpu_manifest = read_json(expected_manifest)
    source_ids = [item["item_id"] for item in source_items]
    executed_ids = list(cpu)
    require(cpu_manifest["t2ance_manifest_sha256"] == digest(manifest)
            and len(set(source_ids)) == len(source_ids)
            and cpu_manifest["source_candidates"] == len(source_items)
            and cpu_manifest["sample_ids"] == executed_ids
            and cpu_manifest["held_without_cpu"] == [item_id for item_id in source_ids
                                                      if item_id not in cpu]
            and set(executed_ids) <= set(source_ids),
            "execution is not bound to the t2ance source")
    passed, exclusions, cpu_passed_count = [], [], 0
    for item in source_items:
        if item["item_id"] not in cpu:
            exclusions.append({"item_id": item["item_id"],
                               "reason": "not_independently_cpu_tested"})
            continue
        original, result = cpu[item["item_id"]]
        require(original["code"] == item["reference_code"]
                and all(original[k] == item[k] for k in ("tests_sha256", "source_content_sha256")),
                "source differs from executed program")
        raw = read_json(source / "source-rows" / (item["item_id"] + ".json"))
        require(digest(raw) == item["origin"]["selected_columns_sha256"]
                and raw["solution_code"] == item["reference_code"]
                and raw["full_response"] == item["raw_output"]
                # The CPU proof below already requires the harness's exact
                # static policy; source recheck here is non-executing.
                and inspect_candidate(raw, item, lambda code: None)["candidate"],
                "frozen upstream response changed")
        if result["status"] != "passed":
            exclusions.append({"item_id": item["item_id"],
                               "reason": "reference_execution_not_passed"})
            continue
        cpu_passed_count += 1
        if item["item_id"] in quarantine_ids:
            exclusions.append({"item_id": item["item_id"],
                               "reason": "independent_semantic_counterexample"})
            continue
        evidence = {"status": "passed", "job_id": completion["job_id"],
                    "completion_sha256": sha256(execution / "completion.json"),
                    "manifest_sha256": sha256(expected_manifest),
                    "result_sha256": digest(result),
                    "code_sha256": original["code_sha256"],
                    "test_count": len(result["tests"])}
        passed.append({**item, "reference_execution": "passed_frozen_tests_not_exhaustive_proof",
                       "execution_evidence": evidence})
    require(passed, "no independent CPU-passing references")
    prior_ids = set()
    if development_run is not None:
        development_run = Path(development_run)
        config = Config.load(development_run / "run_config.json")
        require(read_json(development_run / "completion.json")["status"] == "processed",
                "development run must be completed before exclusion")
        prior_items = verify_prepared(development_run, config)
        prior_ids = {i["item_id"] for i in prior_items}
        require(not quarantine_ids.intersection(prior_ids),
                "development run and semantic quarantine overlap")
        require(prior_ids <= {i["item_id"] for i in passed}
                and read_json(development_run / "t2ance-normalization-manifest.json")[
                    "source_manifest_sha256"] == digest(manifest),
                "development run source differs")
    remaining = [i for i in passed if i["item_id"] not in prior_ids]
    if limit is not None:
        require(type(limit) is int and 1 <= limit <= len(remaining), "invalid bounded cohort")
        items = select_canary(remaining, limit, 20260923)
    else:
        items = remaining
    require(items, "no unprocessed CPU-passing candidates")
    exclusions.extend({"item_id": item_id, "reason": "development_run_already_processed"}
                      for item_id in sorted(prior_ids))
    selected_ids = {i["item_id"] for i in items}
    exclusions.extend({"item_id": i["item_id"], "reason": "held_for_later_batch"}
                      for i in remaining if i["item_id"] not in selected_ids)
    selection = {"source_candidates": len(source_items), "cpu_passed": cpu_passed_count,
                 "eligible_after_semantic_quarantine": len(passed),
                 "selected_ids": [i["item_id"] for i in items], "excluded": exclusions,
                 "selection": "stratified fixed-hash canary" if limit is not None else
                              "all remaining independent-CPU-passing candidates",
                 "development_run": str(development_run) if development_run else None,
                 "no_score_selection": True}
    proof = {"protocol": prompt_version, "prompt_version": prompt_version,
             "source_manifest_sha256": digest(manifest),
             "items_sha256": digest(items), "selection_sha256": digest(selection),
             "execution_completion_sha256": sha256(execution / "completion.json"),
             "execution_manifest_sha256": sha256(expected_manifest),
             "max_calls": max_calls, "max_reserved_tokens": max_reserved_tokens,
             "prior_calls": prior_calls, "prior_reserved_tokens": prior_reserved_tokens,
             "campaign_call_limit": CALL_LIMIT, "campaign_token_limit": TOKEN_LIMIT,
             "formal_eligible": False}
    if quarantine is not None:
        proof["semantic_quarantine_sha256"] = digest(quarantine)
        proof["quarantined_source_items_sha256"] = digest(source_items)
        write_once(root / "evidence/semantic-quarantine.json", quarantine)
        write_once(root / "evidence/quarantined-source-items.json", source_items)
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    write_once(root / "t2ance-normalization-manifest.json", proof)
    write_bytes_once(root / "evidence/source-manifest.json",
                     (source / "t2ance-manifest.json").read_bytes())
    for name in ("input-manifest.json", "harness-selftest.json", "completion.json"):
        write_bytes_once(root / "evidence" / name, (execution / name).read_bytes())
    for item in items:
        name = item["item_id"] + ".json"
        write_bytes_once(root / "evidence/results" / name,
                         (execution / "results" / name).read_bytes())
    for item_id in sorted(quarantine_ids):
        name = item_id + ".json"
        write_bytes_once(root / "evidence/quarantine-results" / name,
                         (execution / "results" / name).read_bytes())
    return selection


def verify_prepared(root, config):
    """Offline gate repeated before every request and by the release audit."""
    proof = read_json(root / "t2ance-normalization-manifest.json")
    items, selection = read_json(root / "items.json"), read_json(root / "selection.json")
    require(proof["protocol"] in PROTOCOLS and digest(items) == proof["items_sha256"]
            and digest(selection) == proof["selection_sha256"], "prepared t2ance input changed")
    if "semantic_quarantine_sha256" in proof:
        quarantine = read_json(root / "evidence/semantic-quarantine.json")
        original_source = read_json(root / "evidence/quarantined-source-items.json")
        source_manifest = read_json(root / "evidence/source-manifest.json")
        original_by_id = {i["item_id"]: i for i in original_source}
        excluded = {r["item_id"] for r in selection["excluded"]
                    if r["reason"] == "independent_semantic_counterexample"}
        require(proof["protocol"] in (PROTOCOL_V3, PROTOCOL_V4)
                and digest(quarantine) == proof["semantic_quarantine_sha256"]
                and digest(original_source) == proof["quarantined_source_items_sha256"]
                == source_manifest["items_sha256"]
                and excluded == {r["item_id"] for r in quarantine["records"]}
                and not excluded.intersection(i["item_id"] for i in items),
                "semantic quarantine evidence changed")
        completion = read_json(root / "evidence/completion.json")
        for record in quarantine["records"]:
            item_id = record["item_id"]
            path = root / "evidence/quarantine-results" / (item_id + ".json")
            result = read_json(path)
            require(item_id in original_by_id
                    and record["reference_code_sha256"] == digest(
                        original_by_id[item_id]["reference_code"])
                    and sha256(path) == completion["results"][path.name]
                    and result["status"] == "passed"
                    and result["code_sha256"] == record["reference_code_sha256"],
                    "quarantined code or CPU result differs from frozen evidence")
    else:
        require(not any(r["reason"] == "independent_semantic_counterexample"
                        for r in selection["excluded"]), "unbound semantic exclusion")
    require(config.task_type == "livecodebench" and config.prompt_version == proof["prompt_version"]
            and config.solution_source == "t2ance_reference_normalization",
            "wrong t2ance protocol")
    require(config.max_calls <= proof["max_calls"] <= CALL_LIMIT - proof["prior_calls"]
            and config.max_reserved_tokens <= proof["max_reserved_tokens"]
            <= TOKEN_LIMIT - proof["prior_reserved_tokens"], "shared t2ance budget exceeded")
    evidence = root / "evidence"
    require(digest(read_json(evidence / "source-manifest.json")) == proof["source_manifest_sha256"]
            and sha256(evidence / "completion.json") == proof["execution_completion_sha256"]
            and sha256(evidence / "input-manifest.json") == proof["execution_manifest_sha256"],
            "source or CPU evidence changed")
    completion = read_json(evidence / "completion.json")
    require(completion["status"] == "processed"
            and completion["input_manifest_sha256"] == proof["execution_manifest_sha256"],
            "unbound CPU completion")
    require([r["status"] for r in read_json(evidence / "harness-selftest.json")]
            == ["passed", "wrong_answer", "passed", "wrong_answer", "passed"],
            "CPU isolation controls failed")
    for item in items:
        path = evidence / "results" / (item["item_id"] + ".json")
        result = read_json(path)
        record = item["execution_evidence"]
        require(sha256(path) == completion["results"][path.name]
                and digest(result) == record["result_sha256"]
                and result["status"] == record["status"] == "passed"
                and record["job_id"] == completion["job_id"], "CPU result changed")
        require(record["completion_sha256"] == proof["execution_completion_sha256"]
                and record["manifest_sha256"] == proof["execution_manifest_sha256"]
                and result["code_sha256"] == record["code_sha256"] == digest(item["reference_code"])
                and result["tests_sha256"] == item["tests_sha256"]
                and result["source_content_sha256"] == item["source_content_sha256"],
                "executed source/code mismatch")
        require(len(result["tests"]) == record["test_count"] > 0
                and all(t["status"] == "passed" for t in result["tests"])
                and {t["split"] for t in result["tests"]} == {"public", "private"},
                "partial CPU test proof")
    return items


class T2ancePipeline(CALIBRIPipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(self.config.prompt_version in PROTOCOLS and through == "review_dag",
                "wrong t2ance run protocol")
        verify_prepared(self.root, self.config)
        return Pipeline.run(self, limit, progress, through)

    def process(self, item, through="review_dag"):
        if self.config.prompt_version not in (PROTOCOL_V3, PROTOCOL_V4):
            return super().process(item, through)
        normalizer, assembler, reviewer = (
            (normalize_v4, assemble_graph_v4, validate_audit_v4)
            if self.config.prompt_version == PROTOCOL_V4 else
            (normalize_v3, assemble_graph_v3, validate_audit_v3))
        directory = self.root / "items" / item["item_id"]
        if (directory / "result.json").exists():
            return read_json(directory / "result.json")
        data, stage = public_input(item), "normalize"
        try:
            proposal = self.request_stage(stage, item, data, payload(stage, data, self.config),
                                          lambda value: normalizer(value, item))
            normalized = normalizer(proposal, item)
            write_once(directory / "normalization.json", normalized)
            stage = "dependencies"
            dependency_input = {**data, "normalized": normalized}
            dependencies = self.request_stage(stage, item, dependency_input,
                payload(stage, dependency_input, self.config),
                lambda value: assembler(value, normalized, item))
            graph = assembler(dependencies, normalized, item)
            stage = "review_dag"
            audit_input = {**data, "normalized": normalized, "candidate": graph}
            audit = self.request_stage(stage, item, audit_input,
                                       payload(stage, audit_input, self.config),
                                       reviewer)
            if audit["decision"] != "accept":
                return self._finish(item,
                    "rejected" if audit["decision"] == "reject" else "needs_review",
                    stage, audit["reason"])
            dag = {"schema_version": "reference_dag_v1",
                   "construction_protocol": self.config.prompt_version,
                   "item_id": item["item_id"], "source": item, "nodes": graph["nodes"],
                   "normalization": normalized, "graph_transformation": graph["transformation"],
                   "dag_review": audit, "execution_evidence": item["execution_evidence"],
                   "calculation_check": {"status": "reference_tests_passed"},
                   "formal_eligible": False,
                   "quality_status": "model_reviewed_pending_release_audit",
                   "limitation": (
                       "t2ance-derived and answer-backward canonicalized explanation; "
                       "frozen code passed tests; same-model semantic review, "
                       "not native CoT or official/human gold")}
            write_once(directory / "dag.json", dag)
            return self._finish(item, "model_accepted", stage,
                                "pending release audit", digest(dag))
        except InvalidOutput as error:
            return self._finish(item, "needs_review", stage, str(error))
        except CallFailure as error:
            if not (self.resilient and error.category == "transient_retries_exhausted"):
                self._stop.set()
            return {"item_id": item["item_id"], "status": "paused",
                    "stage": stage, "reason": error.category}


def main():
    import argparse
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "execution", "expected-manifest", "root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--development-run", type=Path)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--max-reserved-tokens", type=int, default=2000000)
    parser.add_argument("--prior-calls", type=int, default=0)
    parser.add_argument("--prior-reserved-tokens", type=int, default=0)
    parser.add_argument("--prompt-version", choices=PROTOCOLS, default=PROTOCOL)
    parser.add_argument("--semantic-quarantine", type=Path,
                        help="answer-backward frozen code-bound semantic counterexample manifest")
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        print(json.dumps(prepare(args.source, args.execution, args.expected_manifest,
            args.root, limit=args.limit, development_run=args.development_run,
            max_calls=args.max_calls, max_reserved_tokens=args.max_reserved_tokens,
            prior_calls=args.prior_calls, prior_reserved_tokens=args.prior_reserved_tokens,
            prompt_version=args.prompt_version,
            semantic_quarantine=args.semantic_quarantine),
            ensure_ascii=False))


if __name__ == "__main__":
    main()
