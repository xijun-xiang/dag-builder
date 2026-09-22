"""Execution-gated CALIBRI normalization, with no hidden tests in API inputs.

Three immutable calls propose steps, dependencies and a fresh-context audit.
Mechanical failures and negative audits are terminal evidence, not retry targets.
Accepted artifacts are model-reviewed derived explanations, never native/gold CoT.
"""

from pathlib import Path

from .calibri_normalize import PROTOCOL, assemble_graph, normalize, public_input, validate_audit
from .calibri_source import inspect_row
from .client import CallFailure
from .livecodebench_dag import sha256, verify_execution
from .pipeline import Pipeline
from .schemas import InvalidOutput, require
from .stages import payload
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

CAMPAIGN_CALL_LIMIT = 600
CAMPAIGN_TOKEN_LIMIT = 25000000
PROMPT_VERSIONS = (PROTOCOL, "calibri-lcb-normalize-v2")


def prepare(source, execution, expected_manifest, root, *, max_calls=12,
            max_reserved_tokens=1200000, prior_calls=0, prior_reserved_tokens=0,
            prompt_version=PROTOCOL):
    """Bind source, actual execution and bounded allocation before paid calls."""
    source, execution, root = Path(source), Path(execution), private_dir(root)
    require(prompt_version in PROMPT_VERSIONS, "unknown normalization prompt")
    require(type(max_calls) is int and type(prior_calls) is int
            and 0 < max_calls <= CAMPAIGN_CALL_LIMIT - prior_calls and prior_calls >= 0,
            "campaign call allocation exceeded")
    require(type(max_reserved_tokens) is int and type(prior_reserved_tokens) is int
            and 0 < max_reserved_tokens <= CAMPAIGN_TOKEN_LIMIT - prior_reserved_tokens
            and prior_reserved_tokens >= 0, "campaign token allocation exceeded")
    manifest = read_json(source / "calibri-manifest.json")
    original_items = read_json(source / "items.json")
    old_selection = read_json(source / "selection.json")
    require(manifest["protocol"] in ("calibri-lcb-source-v2", "calibri-lcb-source-full-v1")
            and digest(original_items) == manifest["items_sha256"]
            and digest(old_selection) == manifest["selection_sha256"], "source selection changed")
    results, completion = verify_execution(execution, expected_manifest)
    execution_manifest = read_json(expected_manifest)
    require(execution_manifest["calibri_manifest_sha256"] == digest(manifest),
            "execution is not bound to this CALIBRI source")
    require(set(results) == {i["item_id"] for i in original_items}, "partial candidate execution")
    items, exclusions = [], []
    for item in original_items:
        original, result = results[item["item_id"]]
        require(original["code"] == item["reference_code"]
                and all(original[k] == item[k] for k in ("tests_sha256", "source_content_sha256")),
                "source/code differs from executed reference")
        origin = item["origin"]
        require(origin["config"] in ("livecodebench_qwen3", "livecodebench_gpt-oss"), "unknown source config")
        raw = read_json(source / "source-rows" / origin["config"] / (item["item_id"] + ".json"))
        n = origin["sample_index"]
        require(type(n) is int and 0 <= n < 10 and digest(raw) == origin["selected_columns_sha256"],
                "raw CALIBRI row changed")
        require(inspect_row(raw, item)[n]["candidate"]
                and raw["program"][n] == item["reference_code"] and raw["output"][n] == item["raw_output"],
                "CALIBRI selected output changed")
        if result["status"] != "passed":
            exclusions.append({"item_id": item["item_id"], "reason": "reference_execution_not_passed"})
            continue
        evidence = {"status": "passed", "job_id": completion["job_id"],
                    "completion_sha256": sha256(execution / "completion.json"),
                    "manifest_sha256": sha256(expected_manifest), "result_sha256": digest(result),
                    "code_sha256": original["code_sha256"], "test_count": len(result["tests"])}
        items.append({**item, "reference_execution": "passed_frozen_tests_not_exhaustive_proof",
                      "execution_evidence": evidence})
    require(bool(items), "no tested reference; no paid normalization allowed")
    selection = {"selected_ids": [i["item_id"] for i in items], "selected_count": len(items),
                 "candidate_count": len(original_items), "excluded": exclusions,
                 "source_selection": old_selection,
                 "sampling": "all frozen candidates passing isolated tests; no score-based selection"}
    proof = {"protocol": PROTOCOL, "prompt_version": prompt_version, "source_manifest_sha256": digest(manifest),
             "items_sha256": digest(items), "selection_sha256": digest(selection),
             "execution_completion_sha256": sha256(execution / "completion.json"),
             "execution_manifest_sha256": sha256(expected_manifest),
             "max_calls": max_calls, "max_reserved_tokens": max_reserved_tokens,
             "prior_calls": prior_calls, "prior_reserved_tokens": prior_reserved_tokens,
             "formal_eligible": False}
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    write_once(root / "calibri-normalization-manifest.json", proof)
    write_bytes_once(root / "evidence/source-manifest.json", (source / "calibri-manifest.json").read_bytes())
    for name in ("input-manifest.json", "harness-selftest.json", "completion.json"):
        write_bytes_once(root / "evidence" / name, (execution / name).read_bytes())
    for item in items:
        name = item["item_id"] + ".json"
        write_bytes_once(root / "evidence/results" / name, (execution / "results" / name).read_bytes())
    return selection


def verify_prepared(root, config):
    """Cheap repeatable proof check, before a single request or resumed call."""
    proof = read_json(root / "calibri-normalization-manifest.json")
    items, selection = read_json(root / "items.json"), read_json(root / "selection.json")
    require(proof["protocol"] == PROTOCOL and digest(items) == proof["items_sha256"]
            and digest(selection) == proof["selection_sha256"], "frozen normalization input changed")
    require(config.prompt_version == proof.get("prompt_version", PROTOCOL), "prepared prompt version changed")
    require(config.max_calls <= proof["max_calls"] <= CAMPAIGN_CALL_LIMIT - proof["prior_calls"]
            and config.max_reserved_tokens <= proof["max_reserved_tokens"]
            <= CAMPAIGN_TOKEN_LIMIT - proof["prior_reserved_tokens"], "shared allocation exceeded")
    evidence = root / "evidence"
    require(digest(read_json(evidence / "source-manifest.json")) == proof["source_manifest_sha256"]
            and sha256(evidence / "completion.json") == proof["execution_completion_sha256"]
            and sha256(evidence / "input-manifest.json") == proof["execution_manifest_sha256"],
            "execution proof changed")
    completion = read_json(evidence / "completion.json")
    require(completion["status"] == "processed"
            and completion["input_manifest_sha256"] == proof["execution_manifest_sha256"],
            "unbound completion")
    require([r["status"] for r in read_json(evidence / "harness-selftest.json")]
            == ["passed", "wrong_answer", "passed", "wrong_answer", "passed"], "execution controls failed")
    for item in items:
        record = item["execution_evidence"]
        path = evidence / "results" / (item["item_id"] + ".json")
        result = read_json(path)
        require(sha256(path) == completion["results"][path.name]
                and digest(result) == record["result_sha256"] and result["status"] == "passed"
                and record["status"] == "passed" and record["job_id"] == completion["job_id"],
                "unaccepted execution result")
        require(record["completion_sha256"] == proof["execution_completion_sha256"]
                and record["manifest_sha256"] == proof["execution_manifest_sha256"], "proof binding mismatch")
        require(result["code_sha256"] == record["code_sha256"] == digest(item["reference_code"])
                and all(result[k] == item[k] for k in ("item_id", "tests_sha256", "source_content_sha256")),
                "executed code/source mismatch")
        require(len(result["tests"]) == record["test_count"] > 0
                and all(t["status"] == "passed" for t in result["tests"])
                and {t["split"] for t in result["tests"]} == {"public", "private"}, "partial test proof")
    return items


class CALIBRIPipeline(Pipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(self.config.prompt_version in PROMPT_VERSIONS and self.config.task_type == "livecodebench"
                and through == "review_dag", "wrong CALIBRI protocol")
        verify_prepared(self.root, self.config)
        return super().run(limit, progress, through)

    def process(self, item, through="review_dag"):
        directory = self.root / "items" / item["item_id"]
        if (directory / "result.json").exists():
            return read_json(directory / "result.json")
        data, stage = public_input(item), "normalize"
        try:
            proposal = self.request_stage(stage, item, data, payload(stage, data, self.config),
                                          lambda v: normalize(v, item))
            normalized = normalize(proposal, item)
            write_once(directory / "normalization.json", normalized)
            stage = "dependencies"
            dependency_input = {**data, "normalized": normalized}
            dependencies = self.request_stage(stage, item, dependency_input,
                payload(stage, dependency_input, self.config), lambda v: assemble_graph(v, normalized, item))
            graph = assemble_graph(dependencies, normalized, item)
            stage = "review_dag"
            audit_input = {**data, "normalized": normalized, "candidate": graph}
            audit = self.request_stage(stage, item, audit_input,
                                       payload(stage, audit_input, self.config), validate_audit)
            if audit["decision"] != "accept":
                return self._finish(item, "rejected" if audit["decision"] == "reject" else "needs_review",
                                    stage, audit["reason"])
            dag = {"schema_version": "reference_dag_v1", "construction_protocol": self.config.prompt_version,
                   "item_id": item["item_id"], "source": item, "nodes": graph["nodes"],
                   "normalization": normalized, "dag_review": audit,
                   "execution_evidence": item["execution_evidence"],
                   "calculation_check": {"status": "reference_tests_passed"},
                   "formal_eligible": False, "quality_status": "model_reviewed_pending_release_audit",
                   "limitation": "CALIBRI-derived explanation; frozen code tested; same-model semantic review, not native CoT or official/human gold"}
            write_once(directory / "dag.json", dag)
            return self._finish(item, "model_accepted", stage, "pending release audit", digest(dag))
        except InvalidOutput as error:
            return self._finish(item, "needs_review", stage, str(error))
        except CallFailure as error:
            if not (self.resilient and error.category == "transient_retries_exhausted"):
                self._stop.set()
            return {"item_id": item["item_id"], "status": "paused", "stage": stage, "reason": error.category}


def main():
    import argparse
    import json
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "execution", "expected-manifest", "root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--max-reserved-tokens", type=int, default=1200000)
    parser.add_argument("--prior-calls", type=int, default=0)
    parser.add_argument("--prior-reserved-tokens", type=int, default=0)
    parser.add_argument("--prompt-version", choices=PROMPT_VERSIONS, default=PROTOCOL)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        print(json.dumps(prepare(args.source, args.execution, args.expected_manifest, args.root,
            max_calls=args.max_calls, max_reserved_tokens=args.max_reserved_tokens,
            prior_calls=args.prior_calls, prior_reserved_tokens=args.prior_reserved_tokens,
            prompt_version=args.prompt_version)))


if __name__ == "__main__":
    main()
