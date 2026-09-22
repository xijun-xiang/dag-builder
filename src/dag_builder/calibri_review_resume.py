"""One review-only continuation for an empty, output-budget-exhausted audit.

Never retry a semantic rejection or resample the candidate graph. The preceding
repair, dependencies, failed request and raw response are copied and hash-bound.
Only the review output allowance changes; all scientific acceptance checks stay.
"""

from hashlib import sha256
from pathlib import Path

from .calibri_normalize import public_input
from .calibri_pipeline import CAMPAIGN_CALL_LIMIT, CAMPAIGN_TOKEN_LIMIT, verify_prepared
from .calibri_repair import (
    CHECKED_PROTOCOL, _used_budget, apply_versioned_repair, assemble_repaired_graph,
    validate_repair_audit, verify_repair,
)
from .client import CallFailure
from .config import Config
from .pipeline import Pipeline
from .response_contract import check_response
from .schemas import InvalidOutput, parse_object, require
from .stages import payload
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

PROTOCOL = "calibri-empty-review-resume-v1"


def candidate_input(directory, item):
    seed = read_json(directory / "seed.json")
    proposal = read_json(directory / "repair/output.json")
    normalized, record = apply_versioned_repair(proposal, seed["previous_normalized"], item, CHECKED_PROTOCOL)
    require(normalized == read_json(directory / "normalization.json")
            and record == read_json(directory / "repair-record.json"), "candidate repair changed")
    graph = assemble_repaired_graph(read_json(directory / "dependencies/output.json"), normalized, item, record)
    data = {**public_input(item), "normalized": normalized, "candidate": graph,
            "previous_normalized": seed["previous_normalized"], "repair_record": record}
    require(read_json(directory / "review_dag/input.json")["input"] == data, "candidate review input changed")
    responses = list((directory / "review_dag").glob("attempt-*/response.json"))
    require(len(responses) == 1, "ambiguous failed review")
    choices = read_json(responses[0])["body"].get("choices", [])
    require(len(choices) == 1 and choices[0].get("finish_reason") == "length"
            and choices[0].get("message", {}).get("content") in (None, ""),
            "resume only an empty length-limited review, never a semantic judgment")
    return data


def prepare(parent, root):
    parent, root = Path(parent).resolve(), private_dir(root)
    require(parent != root and not root.is_relative_to(parent), "new sibling review run required")
    require(not (parent / "calibri-review-resume-manifest.json").exists(), "one review continuation only")
    previous = Config.load(parent / "run_config.json")
    require(previous.prompt_version == CHECKED_PROTOCOL and previous.max_tokens == 32768,
            "unexpected source review protocol or allowance")
    require(read_json(parent / "completion.json")["status"] == "processed", "parent is unfinished")
    original = verify_repair(parent, previous)
    prior_calls, prior_reserved = _used_budget(parent, previous)
    require(prior_calls + 1 <= CAMPAIGN_CALL_LIMIT and prior_reserved + 200000 <= CAMPAIGN_TOKEN_LIMIT,
            "campaign allocation exceeded")
    items, exclusions, evidence_files = [], [], {}
    for item in original:
        directory = parent / "items" / item["item_id"]
        result = read_json(directory / "result.json")
        if result["status"] != "needs_review" or result["stage"] != "review_dag":
            exclusions.append({"item_id": item["item_id"], "status": result["status"],
                               "reason": "not an empty incomplete review; unchanged"})
            continue
        responses = list((directory / "review_dag").glob("attempt-*/response.json"))
        if len(responses) != 1:
            raise InvalidOutput("ambiguous review history")
        choices = read_json(responses[0])["body"].get("choices", [])
        require(len(choices) == 1 and choices[0].get("finish_reason") == "length"
                and choices[0].get("message", {}).get("content") in (None, ""),
                "cannot resume a returned review judgment")
        require(not (directory / "review_dag/output.json").exists(), "a review judgment already exists")
        items.append(item)
        prefix = item["item_id"] + "/"
        evidence_files[prefix + "seed.json"] = (parent / "repair-seeds" / (item["item_id"] + ".json")).read_bytes()
        for name in ("normalization.json", "repair-record.json", "repair/output.json",
                     "dependencies/output.json", "result.json"):
            evidence_files[prefix + name] = (directory / name).read_bytes()
        for path in (directory / "review_dag").rglob("*.json"):
            evidence_files[prefix + str(path.relative_to(directory))] = path.read_bytes()
    require(len(items) == 1, "this bounded continuation expects exactly one eligible review")
    config = Config(**{**previous.to_dict(), "max_tokens": 65536, "max_calls": 1,
                       "max_reserved_tokens": 200000, "workers": 1})
    selection = {"selected_ids": [i["item_id"] for i in items], "selected_count": 1,
                 "source_count": len(original), "excluded": exclusions,
                 "sampling": "all empty length-limited final reviews; no graph/semantic resampling"}
    proof = {**read_json(parent / "calibri-normalization-manifest.json"),
             "items_sha256": digest(items), "selection_sha256": digest(selection),
             "prior_calls": prior_calls, "prior_reserved_tokens": prior_reserved,
             "max_calls": 1, "max_reserved_tokens": 200000}
    for path in (parent / "evidence").rglob("*"):
        if path.is_file():
            write_bytes_once(root / path.relative_to(parent), path.read_bytes())
    for name, content in evidence_files.items():
        write_bytes_once(root / "candidate-evidence" / name, content)
    for item in items:
        candidate_input(root / "candidate-evidence" / item["item_id"], item)
    manifest = {"protocol": PROTOCOL, "parent_run": str(parent),
                "parent_completion_sha256": digest(read_json(parent / "completion.json")),
                "candidate_files": {k: sha256(v).hexdigest() for k, v in evidence_files.items()},
                "normalization_manifest_sha256": digest(proof), "config_sha256": digest(config.to_dict()),
                "review_only": True, "old_max_tokens": 32768, "new_max_tokens": 65536,
                "reason": "prior final review used all output tokens for reasoning and returned no content"}
    for name, value in (("items.json", items), ("selection.json", selection), ("config.json", config.to_dict()),
                        ("calibri-normalization-manifest.json", proof), ("calibri-review-resume-manifest.json", manifest)):
        write_once(root / name, value)
    return selection


def verify(root, config):
    items = verify_prepared(root, config)
    manifest = read_json(root / "calibri-review-resume-manifest.json")
    require(manifest["protocol"] == PROTOCOL and manifest["review_only"] is True
            and digest(config.to_dict()) == manifest["config_sha256"]
            and digest(read_json(root / "calibri-normalization-manifest.json")) == manifest["normalization_manifest_sha256"],
            "review continuation config or allocation changed")
    for name, expected in manifest["candidate_files"].items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts, "unsafe candidate path")
        require(sha256((root / "candidate-evidence" / name).read_bytes()).hexdigest() == expected,
                "fixed candidate evidence changed")
    for item in items:
        candidate_input(root / "candidate-evidence" / item["item_id"], item)
    return items


class CALIBRIReviewResume(Pipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(through == "review_dag", "review-only continuation")
        verify(self.root, self.config)
        return super().run(limit, progress, through)

    def process(self, item, through="review_dag"):
        directory = self.root / "items" / item["item_id"]
        if (directory / "result.json").exists():
            return read_json(directory / "result.json")
        data = candidate_input(self.root / "candidate-evidence" / item["item_id"], item)
        try:
            review = self.request_stage("review_dag", item, data, payload("review_dag", data, self.config),
                                        lambda v: validate_repair_audit(v, CHECKED_PROTOCOL))
            if review["decision"] != "accept":
                return self._finish(item, "rejected" if review["decision"] == "reject" else "needs_review",
                                    "review_dag", review["reason"])
            dag = {"schema_version": "reference_dag_v1", "construction_protocol": CHECKED_PROTOCOL,
                   "item_id": item["item_id"], "source": item, "nodes": data["candidate"]["nodes"],
                   "normalization": data["normalized"], "repair_record": data["repair_record"],
                   "dag_review": review, "execution_evidence": item["execution_evidence"],
                   "calculation_check": {"status": "reference_tests_passed"}, "formal_eligible": False,
                   "quality_status": "model_reviewed_pending_release_audit",
                   "recovery_provenance": {"development_iteration": 2, "review_resume_protocol": PROTOCOL,
                                            "fixed_review_input_sha256": digest(data)},
                   "limitation": "Same fixed repaired graph; one higher-budget review after empty truncation; same-model review, not human/official gold"}
            write_once(directory / "dag.json", dag)
            return self._finish(item, "model_accepted", "review_dag", "pending release audit", digest(dag))
        except InvalidOutput as error:
            return self._finish(item, "needs_review", "review_dag", str(error))
        except CallFailure as error:
            self._stop.set()
            return {"item_id": item["item_id"], "status": "paused", "stage": "review_dag", "reason": error.category}


def audit_completed(root):
    """Offline replay of the sole review; no interpretation of reasoning_content."""
    config = Config.load(root / "run_config.json")
    items = verify(root, config)
    require(read_json(root / "completion.json")["status"] == "processed", "review run unfinished")
    item = items[0]
    directory = root / "items" / item["item_id"]
    data = candidate_input(root / "candidate-evidence" / item["item_id"], item)
    expected = payload("review_dag", data, config)
    requests = list(root.glob("items/*/*/attempt-*/request.json"))
    require(len(requests) == 1, "review continuation must contain exactly one request")
    require(read_json(directory / "review_dag/input.json") == {"input": data, "payload_sha256": digest(expected)},
            "review inputs differ from fixed candidate")
    request = read_json(requests[0])
    require(request["payload"] == expected, "review request changed")
    body = read_json(requests[0].parent / "response.json")["body"]
    contract = check_response(expected, body, request["reserved_tokens"],
                             strict=config.strict_response_contract, content_gated=config.content_gated_response)
    require(not contract["violations"], "response contract failed")
    result = read_json(directory / "result.json")
    if (directory / "review_dag/output.json").exists():
        choices = body["choices"]
        require(len(choices) == 1 and choices[0]["finish_reason"] == "stop", "incomplete final review")
        review = parse_object(choices[0]["message"].get("content"))
        validate_repair_audit(review, CHECKED_PROTOCOL)
        require(read_json(directory / "review_dag/output.json") == review, "review output changed")
        if review["decision"] == "accept":
            dag = read_json(directory / "dag.json")
            require(result["status"] == "model_accepted" and result["dag_sha256"] == digest(dag)
                    and dag["nodes"] == data["candidate"]["nodes"] and dag["source"] == item
                    and dag["normalization"] == data["normalized"] and dag["repair_record"] == data["repair_record"]
                    and dag["dag_review"] == review and dag["formal_eligible"] is False, "accepted DAG mismatch")
        else:
            require(result["status"] in ("rejected", "needs_review") and not (directory / "dag.json").exists(),
                    "failed review published a DAG")
    else:
        require(result["status"] == "needs_review" and not (directory / "dag.json").exists(), "incomplete review published")
    calls, reserved = _used_budget(root, config)
    report = {"protocol": PROTOCOL, "mechanical_pass": True, "semantic_certification": False,
              "item_id": item["item_id"], "question_id": item["question_id"], "status": result["status"],
              "fixed_candidate_unchanged": True, "requests": 1, "reserved_tokens": request["reserved_tokens"],
              "reported_tokens": contract["reported_tokens"], "cumulative_requests": calls,
              "cumulative_reserved_tokens": reserved, "review_input_sha256": digest(data),
              "result_sha256": digest(result)}
    write_once(root / "offline-review-audit.json", report)
    return report


def main():
    import argparse
    import json
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        if args.audit:
            print(json.dumps(audit_completed(args.root.resolve()), ensure_ascii=False, indent=2))
        else:
            require(args.parent is not None, "--parent required for preparation")
            print(json.dumps(prepare(args.parent, args.root)))


if __name__ == "__main__":
    main()
