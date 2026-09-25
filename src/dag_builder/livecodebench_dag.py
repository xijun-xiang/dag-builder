"""Construct LCB DAGs ONLY after hash-bound, isolated reference test acceptance."""

import hashlib
import json
from pathlib import Path

from .livecodebench_reference import validate_reference
from .pipeline import Pipeline
from .schemas import require
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

PROTOCOL = "livecodebench-dag-v1"
EXECUTION_PROTOCOL = "lcb-reference-seccomp-v1"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_execution(execution, expected_manifest):
    """The expected manifest is the local pre-submission artifact, not a remote claim."""
    execution = Path(execution)
    manifest = read_json(execution / "input-manifest.json")
    require(sha256(execution / "input-manifest.json") == sha256(expected_manifest),
            "execution manifest differs from pre-submission manifest")
    completion = read_json(execution / "completion.json")
    require(completion.get("policy") == EXECUTION_PROTOCOL and completion.get("status") == "processed",
            "reference gate has not completed")
    require(completion.get("input_manifest_sha256") == sha256(expected_manifest),
            "execution completion hash mismatch")
    require(bool(completion.get("job_id")) and bool(completion.get("hostname")),
            "missing execution provenance")
    inputs = read_json(execution / "execution-input.json")
    require(digest(inputs) == manifest["inputs_sha256"], "execution input hash mismatch")
    require(len(inputs) == manifest["planned"] == completion["executed"], "partial execution cohort")
    require(manifest["protocol"] == EXECUTION_PROTOCOL, "unsupported execution policy")
    require(sha256(execution / "verify_livecodebench_reference.py") == manifest["harness_sha256"],
            "execution harness hash mismatch")
    controls = read_json(execution / "harness-selftest.json")
    require([r["status"] for r in controls] == ["passed", "wrong_answer", "passed", "wrong_answer", "passed"],
            "execution controls did not pass")
    results = {}
    require(set(completion["results"]) == {i["item_id"] + ".json" for i in inputs},
            "execution result inventory mismatch")
    for original in inputs:
        item_id = original["item_id"]
        path = execution / "results" / (item_id + ".json")
        require(sha256(path) == completion["results"][path.name], "execution result hash mismatch")
        result = read_json(path)
        require(all(result.get(k) == original[k] for k in
                    ("item_id", "code_sha256", "tests_sha256", "source_content_sha256")),
                "execution reference identity mismatch")
        require(len(result["tests"]) == len(original["tests"]) > 0, "missing test results")
        require(all(r["test_sha256"] == digest(t) and (r["split"], r["index"]) == (t["split"], t["index"])
                    for r, t in zip(result["tests"], original["tests"])), "test order/identity mismatch")
        all_passed = all(r["status"] == "passed" for r in result["tests"])
        require((result["status"] == "passed") == all_passed, "aggregate/test pass mismatch")
        require({t["split"] for t in original["tests"]} == {"public", "private"},
                "public and hidden tests both required")
        require(digest(original["code"]) == original["code_sha256"], "reference code hash mismatch")
        results[item_id] = (original, result)
    require(completion["passed"] == sum(r[1]["status"] == "passed" for r in results.values()),
            "execution passed denominator mismatch")
    return results, completion


def prepare_dag(source, execution, expected_manifest, root):
    source, execution = Path(source), Path(execution)
    results, completion = verify_execution(execution, expected_manifest)
    original_items = read_json(source / "items.json")
    original_selection = read_json(source / "selection.json")
    original_manifest = read_json(source / "prepared-manifest.json")
    require(digest(original_items) == original_manifest["items_sha256"]
            and digest(original_selection) == original_manifest["selection_sha256"],
            "reference source changed")
    require(read_json(execution / "input-manifest.json")["source_prepared_manifest_sha256"] == digest(original_manifest),
            "execution belongs to another cohort")
    require(set(results) <= {i["item_id"] for i in original_items}, "unknown executed task")
    generated_completion = read_json(source / "completion.json")
    require(generated_completion.get("status") == "processed" and generated_completion.get("global_stop") is False,
            "reference generation incomplete")
    calls = list(source.glob("items/*/*/attempt-*/request.json"))
    reserved = sum(read_json(p)["reserved_tokens"] for p in calls)
    require(len(calls) <= 20 and reserved <= 1000000, "reference allocation exceeded")
    items, excluded = [], []
    for item in original_items:
        item_id = item["item_id"]
        if item_id not in results or results[item_id][1]["status"] != "passed":
            excluded.append({"item_id": item_id, "reason": "no_complete_test_passed_reference"})
            continue
        original, result = results[item_id]
        reference_dir = source / "items" / item_id
        reference = read_json(reference_dir / "reference.json")
        output = read_json(reference_dir / "reference_code/output.json")
        require(read_json(reference_dir / "result.json")["status"] == "reference_candidate",
                "reference generation did not accept candidate")
        validate_reference(output, item)
        require(digest(output) == reference["output_sha256"] and output["code"] == original["code"],
                "executed code differs from generated code")
        require(item["tests_sha256"] == original["tests_sha256"]
                and item["source_content_sha256"] == original["source_content_sha256"],
                "executed source/tests mismatch")
        evidence = {"protocol": EXECUTION_PROTOCOL, "status": "passed", "job_id": completion["job_id"],
                    "completion_sha256": sha256(execution / "completion.json"),
                    "manifest_sha256": sha256(expected_manifest), "result_sha256": digest(result),
                    "code_sha256": original["code_sha256"], "test_count": len(result["tests"])}
        items.append({**item, "reference_code": output["code"],
                      "reference_origin": "model_generated_test_verified_candidate",
                      "reference_execution": "passed_frozen_tests_not_exhaustive_proof", "execution_evidence": evidence})
    require(bool(items), "no test-passed references; no paid DAG calls permitted")
    root = private_dir(root)
    selection = {"selected_ids": [i["item_id"] for i in items], "selected_count": len(items),
                 "candidate_count": original_selection["candidate_count"],
                 "reference_selected_count": len(original_items), "reference_exclusions": excluded,
                 "selection_seed": original_selection["seed"], "sampling": "all frozen canary references passing tests"}
    proof = {"protocol": PROTOCOL, "reference_completion_sha256": sha256(source / "completion.json"),
             "execution_completion_sha256": sha256(execution / "completion.json"),
             "reference_calls": len(calls), "reference_reserved_tokens": reserved,
             "reference_allocation": {"max_calls": 20, "max_reserved_tokens": 1000000},
             "dag_allocation": {"max_calls": 140, "max_reserved_tokens": 7000000},
             "items_sha256": digest(items), "selection_sha256": digest(selection)}
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    write_once(root / "execution-provenance.json", proof)
    write_once(root / "evidence" / "execution-completion.json", completion)
    for name in ("input-manifest.json", "harness-selftest.json"):
        write_bytes_once(root / "evidence" / name, (execution / name).read_bytes())
    for item in items:
        write_bytes_once(root / "evidence" / (item["item_id"] + ".json"),
                         (execution / "results" / (item["item_id"] + ".json")).read_bytes())
    return selection


class LiveCodeBenchDAGPipeline(Pipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(self.config.prompt_version == PROTOCOL and self.config.task_type == "livecodebench",
                "wrong DAG protocol")
        proof = read_json(self.root / "execution-provenance.json")
        items = read_json(self.root / "items.json")
        require(proof.get("protocol") == PROTOCOL, "wrong execution provenance protocol")
        require(digest(items) == proof["items_sha256"]
                and digest(read_json(self.root / "selection.json")) == proof["selection_sha256"],
                "test-verified construction inputs changed")
        require(self.config.max_calls <= min(140, proof["dag_allocation"]["max_calls"])
                and self.config.max_reserved_tokens <= min(7000000, proof["dag_allocation"]["max_reserved_tokens"]),
                "shared reference/DAG authorization exceeded")
        evidence_root = self.root / "evidence"
        require(sha256(evidence_root / "execution-completion.json") == proof["execution_completion_sha256"],
                "frozen execution completion changed")
        for item in items:
            evidence = item["execution_evidence"]
            result = read_json(evidence_root / (item["item_id"] + ".json"))
            require(evidence.get("status") == "passed" and result.get("status") == "passed"
                    and digest(result) == evidence["result_sha256"], "unaccepted or changed execution result")
            require(evidence["completion_sha256"] == proof["execution_completion_sha256"]
                    and evidence["manifest_sha256"] == sha256(evidence_root / "input-manifest.json"),
                    "execution proof binding mismatch")
            require(result["code_sha256"] == digest(item["reference_code"])
                    and result["tests_sha256"] == item["tests_sha256"]
                    and result["source_content_sha256"] == item["source_content_sha256"],
                    "executed code/source/tests changed")
            require(len(result["tests"]) == evidence["test_count"] > 0
                    and all(t["status"] == "passed" for t in result["tests"]), "partial test acceptance")
        return super().run(limit, progress, through)


def main():
    import argparse
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--expected-manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        print(json.dumps(prepare_dag(args.source, args.execution, args.expected_manifest, args.root)))


if __name__ == "__main__":
    main()
