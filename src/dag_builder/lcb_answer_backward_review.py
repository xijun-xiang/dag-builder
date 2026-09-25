"""One-pass, answer-backward review of disconnected CALIBRI revision graphs.

This deterministic edit removes only nodes outside the proposed answer
ancestry. It does not add edges or repair incorrect claims. The unchanged
source and every discarded statement remain visible to fresh semantic review.
"""

import argparse
import json
import os
from pathlib import Path

from .calibri_normalize import output_source_field, public_input
from .client import CallFailure
from .config import Config
from .livecodebench_dag_revision_audit import audit as audit_previous
from .pipeline import Pipeline
from .schemas import InvalidOutput, parse_object, require, validate_nodes
from .stages import payload
from .storage import digest, private_dir, read_json, write_once
from .t2ance_v3 import _ancestors, _checked_rows
from .t2ance_v4 import validate_audit_v4
from .validation import validate_justifications, validate_parents

PROTOCOL = "lcb-answer-backward-review-v1"
PREVIOUS_REASON = "nodes not connected to the answer; do not invent edges to close them"


def canonicalize(normalized, dependencies, item):
    require(item["reference_origin"] == "calibri_model_output"
            and normalized["protocol"] == "calibri-lcb-normalize-v1",
            "answer-backward review requires frozen CALIBRI normalization")
    rows, mapping = _checked_rows(dependencies, normalized)
    terminal = len(normalized["nodes"]) + 1
    selected = sorted(_ancestors(mapping, terminal, {}))
    require(bool(selected) and len(selected) < terminal - 1,
            "expected a nonempty proper answer-ancestor subset")
    renumber = {old: new for new, old in enumerate(selected, 1)}
    renumber[terminal] = len(selected) + 1
    nodes = []
    for old in selected:
        node = normalized["nodes"][old - 1]
        nodes.append({**node, "node_id": renumber[old], "original_node_id": old,
                      "parents": [renumber[p] for p in mapping[old]],
                      "justification": rows[old - 1]["justification"]})
    nodes.append({"node_id": renumber[terminal], "original_node_id": terminal,
                  "kind": "answer", "statement": item["reference_code"],
                  "source_field": "reference_code", "source_quote": item["reference_code"],
                  "support_type": "source_supported",
                  "parents": [renumber[p] for p in mapping[terminal]],
                  "justification": dependencies["answer_justification"],
                  "excluded_from_pals": True})
    validate_nodes({"nodes": nodes}, item["question"], "", extra_sources={
        output_source_field(item): item["raw_output"],
        "reference_code": item["reference_code"]}, allow_reference_code_facts=True)
    validate_parents({"parents": [{"node_id": n["node_id"], "parents": n["parents"]}
                                  for n in nodes]}, nodes)
    validate_justifications({"justifications": [
        {"node_id": n["node_id"], "text": n["justification"]} for n in nodes]}, nodes)
    discarded = [{"original_node_id": old,
                  "statement": normalized["nodes"][old - 1]["statement"],
                  "source_refs": normalized["nodes"][old - 1]["source_refs"],
                  "model_justification": rows[old - 1]["justification"],
                  "reason": "outside proposed answer ancestry; necessity unverified"}
                 for old in range(1, terminal) if old not in selected]
    return {"nodes": nodes, "transformation": {
        "protocol": PROTOCOL, "raw_dependencies_sha256": digest(dependencies),
        "original_node_count": len(normalized["nodes"]),
        "selected_original_ids": selected, "discarded_nodes": discarded,
        "new_edges": 0,
        "claim": "structural pruning only; fresh semantic review required"}}


def _raw_dependencies(stage_dir):
    """Read the raw, prior-audited content when validation withheld output.json."""
    responses = sorted(stage_dir.glob("attempt-*/response.json"))
    require(len(responses) == 1, "expected one immutable dependency response")
    body = read_json(responses[0])["body"]
    choices = body.get("choices", [])
    require(len(choices) == 1 and choices[0].get("finish_reason") == "stop",
            "cannot structurally salvage truncated dependency content")
    return parse_object(choices[0]["message"].get("content"))


def prepare(previous, root):
    previous, root = Path(previous).resolve(), Path(root).resolve()
    require(root.parent == previous.parent and root != previous,
            "new private sibling batch required")
    prior_audit = audit_previous(previous)
    require(read_json(previous / "offline-audit.json") == prior_audit
            and prior_audit["protocol"] == "lcb-dag-revision-v3"
            and prior_audit["selected"] == 50,
            "previous full revision audit missing or changed")
    selected, evidence, candidates = [], {}, {}
    for item in read_json(previous / "items.json"):
        item_id = item["item_id"]
        result = read_json(previous / "items" / item_id / "result.json")
        if not (result["status"] == "needs_review"
                and result["stage"] == "dependencies"
                and result["reason"] == PREVIOUS_REASON):
            continue
        normalized = read_json(previous / "items" / item_id / "normalization.json")
        dependencies = _raw_dependencies(previous / "items" / item_id / "dependencies")
        candidate = canonicalize(normalized, dependencies, item)
        selected.append(item)
        evidence[item_id] = {"previous_result": result, "normalized": normalized,
                             "dependencies": dependencies}
        candidates[item_id] = candidate
    require(len(selected) == 11 and all(item["reference_origin"] ==
            "calibri_model_output" for item in selected),
            "expected exactly 11 disconnected CALIBRI proposals")
    root = private_dir(root)
    require(not any(root.iterdir()), "review batch must start empty")
    selection = {"protocol": PROTOCOL,
                 "selected_ids": [item["item_id"] for item in selected],
                 "previous_run": str(previous),
                 "previous_audit_sha256": digest(prior_audit),
                 "semantic_edit_limit": "delete non-ancestors only; no new edges"}
    write_once(root / "items.json", selected)
    write_once(root / "selection.json", selection)
    for item_id in candidates:
        write_once(root / "evidence" / (item_id + ".json"), evidence[item_id])
        write_once(root / "candidates" / (item_id + ".json"), candidates[item_id])
    write_once(root / "answer-backward-manifest.json", {
        "protocol": PROTOCOL, "selected": len(selected),
        "items_sha256": digest(selected), "selection_sha256": digest(selection),
        "evidence_sha256": {key: digest(value) for key, value in evidence.items()},
        "candidates_sha256": {key: digest(value) for key, value in candidates.items()},
        "human_approved": 0, "formal_eligible": False})
    return {"selected": len(selected), "discarded_nodes": sum(len(value[
        "transformation"]["discarded_nodes"]) for value in candidates.values())}


def verify_prepared(root, config):
    root = Path(root)
    manifest = read_json(root / "answer-backward-manifest.json")
    items = read_json(root / "items.json")
    selection = read_json(root / "selection.json")
    require(config.task_type == "livecodebench"
            and config.prompt_version == manifest["protocol"] == selection["protocol"] == PROTOCOL
            and config.solution_source == "reference_dag_revision"
            and manifest["selected"] == len(items) == 11
            and manifest["items_sha256"] == digest(items)
            and manifest["selection_sha256"] == digest(selection)
            and selection["selected_ids"] == [item["item_id"] for item in items],
            "answer-backward selection changed")
    for item in items:
        item_id = item["item_id"]
        evidence = read_json(root / "evidence" / (item_id + ".json"))
        candidate = read_json(root / "candidates" / (item_id + ".json"))
        require(manifest["evidence_sha256"][item_id] == digest(evidence)
                and manifest["candidates_sha256"][item_id] == digest(candidate)
                and evidence["previous_result"]["reason"] == PREVIOUS_REASON
                and candidate == canonicalize(evidence["normalized"],
                                              evidence["dependencies"], item),
                "answer-backward candidate differs from frozen proposal")
    return items


class LCBAnswerBackwardReview(Pipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(through == "review_dag", "answer-backward batch must run semantic review")
        verify_prepared(self.root, self.config)
        return super().run(limit, progress, through)

    def process(self, item, through="review_dag"):
        directory = self.root / "items" / item["item_id"]
        if (directory / "result.json").exists():
            return read_json(directory / "result.json")
        evidence = read_json(self.root / "evidence" / (item["item_id"] + ".json"))
        candidate = read_json(self.root / "candidates" / (item["item_id"] + ".json"))
        review_input = {**public_input(item), "source_tier": "CALIBRI",
                        "normalized": evidence["normalized"], "candidate": candidate,
                        "previous_dependency_proposal": evidence["dependencies"]}
        try:
            review = self.request_stage("review_dag", item, review_input,
                payload("review_dag", review_input, self.config), validate_audit_v4)
            if review["decision"] != "accept":
                return self._finish(item, "rejected" if review["decision"] == "reject"
                                    else "needs_review", "review_dag", review["reason"])
            dag = {"schema_version": "reference_dag_v1",
                   "construction_protocol": PROTOCOL,
                   "item_id": item["item_id"], "source": item,
                   "nodes": candidate["nodes"],
                   "graph_transformation": candidate["transformation"],
                   "normalization": evidence["normalized"], "dag_review": review,
                   "execution_evidence": item["execution_evidence"],
                   "calculation_check": {"status": "reference_tests_passed"},
                   "formal_eligible": False,
                   "quality_status": "model_reviewed_pending_independent_semantic_audit",
                   "limitation": "Structural pruning and same-model review, not human or official gold"}
            write_once(directory / "dag.json", dag)
            return self._finish(item, "model_accepted", "review_dag",
                                "pending independent release audit", digest(dag))
        except InvalidOutput as error:
            return self._finish(item, "needs_review", "review_dag", str(error))
        except CallFailure as error:
            if not (self.resilient and error.category == "transient_retries_exhausted"):
                self._stop.set()
            return {"item_id": item["item_id"], "status": "paused",
                    "stage": "review_dag", "reason": error.category}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.previous, args.root), ensure_ascii=False))


if __name__ == "__main__":
    main()
