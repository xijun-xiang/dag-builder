"""Bounded official-editorial feasibility pilot, NOT a test-passed DAG release.

Two calls per item: grounded step/edge proposal, then source-aware review. Existing
reference-execution gates and formal PALS protocols are deliberately untouched.
"""
import ast
import re
from pathlib import Path
from urllib.parse import urlparse

from .client import CallFailure
from .humaneval_quality import text_findings, validate_code_facts
from .pipeline import Pipeline
from .schemas import InvalidOutput, public_question, require, text, validate_nodes, validate_review
from .stages import payload
from .storage import digest, private_dir, read_json, write_once
from .validation import validate_justifications, validate_parents

PROTOCOL = "livecodebench-editorial-pilot-v1"
CHECKS = ("root_premises_sound", "reference_behavior_faithful", "self_contained_statements",
          "no_invariant_assumed", "code_facts_grounded", "editorial_faithful",
          "supplements_disclosed", "supplements_valid", "algorithm_consistent")


def validate_editorial(item):
    source = item["editorial_source"]
    require(item["task_type"] == "livecodebench" and item["platform"] == "atcoder",
            "this pilot supports AtCoder official editorials only")
    qid = item["question_id"]
    require(re.fullmatch(r"[a-z0-9]+_[a-z0-9]+", qid) is not None, "invalid AtCoder identity")
    contest = qid.split("_")[0]
    uri = urlparse(source["url"])
    require(uri.scheme == "https" and uri.netloc == "atcoder.jp"
            and re.fullmatch(r"/contests/" + re.escape(contest) + r"/editorial/[0-9]+", uri.path)
            and not uri.query and not uri.fragment, "editorial URL is not bound to the source contest")
    require(source["official_index_url"] == f"https://atcoder.jp/contests/{contest}/tasks/{qid}/editorial",
            "official task/editorial index mismatch")
    require(source["question_id"] == qid and source["question_title"] == item["question_title"],
            "editorial task identity mismatch")
    require(source["official_label_observed"] is True and source["task_match_reviewed"] is True,
            "official provenance and problem match need explicit review")
    require(all(text(source.get(k)) for k in ("capture_method", "capture_note", "editorial", "reference_code")),
            "incomplete source snapshot")
    require(source["editorial"] == item["official_editorial"]
            and source["reference_code"] == item["reference_code"], "source snapshot changed")
    ast.parse(item["reference_code"])
    require(item["reference_execution"] == "not_executed" and item["reference_origin"] == "official_editorial",
            "pilot must not imply test execution")


def validate_graph(value, item):
    require(isinstance(value, dict) and set(value) == {"nodes"}, "expected exactly nodes")
    validate_nodes(value, item["question"], "", extra_sources={
        "editorial": item["official_editorial"], "reference_code": item["reference_code"]},
        allow_reference_code_facts=True)
    nodes = value["nodes"]
    require(all(n["source_field"] in ("question", "editorial", "reference_code") for n in nodes),
            "only original sources may anchor an editorial pilot")
    require(not text_findings(nodes), "positional references are not stable under reordering")
    validate_code_facts(nodes, item["reference_code"])
    for node in nodes:
        require(node.get("support_type") in ("source_supported", "supplementary"),
                "every step must disclose its support type")
        if node["support_type"] == "supplementary":
            require(node["kind"] in ("derived", "knowledge"), "supplements cannot become given facts")
        if node["source_field"] == "reference_code":
            require(node["support_type"] == "source_supported", "code observations are not proof supplements")
    require(nodes[-1]["statement"] == item["reference_code"]
            and nodes[-1]["source_field"] == "reference_code", "unchanged terminal code required")
    validate_parents({"parents": [{"node_id": n["node_id"], "parents": n.get("parents")} for n in nodes]}, nodes)
    validate_justifications({"justifications": [{"node_id": n["node_id"], "text": n.get("justification")}
                                             for n in nodes]}, nodes)


def validate_audit(value):
    validate_review(value, "review_dag")
    require(all(k in value["checks"] and (type(value["checks"][k]) is bool or value["checks"][k] is None)
                for k in CHECKS), "missing editorial review check")
    if value["decision"] == "accept":
        require(all(value["checks"][k] is True for k in CHECKS), "unresolved editorial review")


def prepare(source_root, source_file, root, previous_pilot=None):
    source_root, source_file = Path(source_root), Path(source_file)
    old_manifest = read_json(source_root / "prepared-manifest.json")
    old_items = read_json(source_root / "items.json")
    old_selection = read_json(source_root / "selection.json")
    require(digest(old_items) == old_manifest["items_sha256"]
            and digest(old_selection) == old_manifest["selection_sha256"], "original five-item cohort changed")
    prior_completion = read_json(source_root / "completion.json")
    require(prior_completion["status"] == "processed" and not prior_completion["global_stop"],
            "prior reference campaign must be finished")
    prior_requests = list(source_root.glob("items/*/*/attempt-*/request.json"))
    spent = sum(read_json(p)["reserved_tokens"] for p in prior_requests)
    editorial_calls, editorial_reserved = 0, 0
    previous_manifest_hash = None
    if previous_pilot is not None:
        previous = Path(previous_pilot)
        completion = read_json(previous / "completion.json")
        require(completion["status"] == "processed" and not completion["global_stop"],
                "previous pilot must be safely finished before a revision")
        prior_manifest = read_json(previous / "editorial-manifest.json")
        require(prior_manifest["protocol"] == PROTOCOL, "wrong predecessor protocol")
        requests = list(previous.glob("items/*/*/attempt-*/request.json"))
        editorial_calls = prior_manifest.get("prior_editorial_calls", 0) + len(requests)
        editorial_reserved = prior_manifest.get("prior_editorial_reserved_tokens", 0) + sum(
            read_json(p)["reserved_tokens"] for p in requests)
        previous_manifest_hash = digest(prior_manifest)
    require(editorial_calls < 12 and editorial_reserved < 600000, "editorial allocation exhausted")
    # This small additional allocation belongs to the original five-item budget.
    require(len(prior_requests) + 12 <= 160 and spent + 600000 <= 8000000, "shared approval exceeded")
    sources = read_json(source_file)
    if previous_pilot is not None:
        require(digest(sources) == prior_manifest["source_snapshots_sha256"], "revision changed sources")
    require(isinstance(sources, list) and len(sources) == 2, "fixed two-item source feasibility canary")
    require({s["question_id"] for s in sources} == {"abc396_a", "abc398_c"}, "pilot sources changed")
    by_qid = {i["question_id"]: i for i in old_items if i["platform"] == "atcoder"}
    items = []
    for source in sources:
        original = by_qid[source["question_id"]]
        item = {**original, "reference_origin": "official_editorial", "reference_execution": "not_executed",
                "reference_code": source["reference_code"], "official_editorial": source["editorial"],
                "editorial_source": source}
        validate_editorial(item)
        items.append(item)
    root = private_dir(root)
    selection = {"selected_ids": [i["item_id"] for i in items], "selected_count": len(items),
                 "candidate_count": old_selection["candidate_count"], "original_canary_count": len(old_items),
                 "sampling": "both AtCoder items in original frozen five; verified official sources, not scores",
                 "claim": "source feasibility only; not representative of all difficulties/platforms"}
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    write_once(root / "editorial-manifest.json", {
        "protocol": PROTOCOL, "items_sha256": digest(items), "selection_sha256": digest(selection),
        "source_snapshots_sha256": digest(sources), "prior_reference_calls": len(prior_requests),
        "prior_reference_reserved_tokens": spent,
        "prior_editorial_calls": editorial_calls, "prior_editorial_reserved_tokens": editorial_reserved,
        "previous_editorial_manifest_sha256": previous_manifest_hash,
        "max_calls": 12 - editorial_calls, "max_reserved_tokens": 600000 - editorial_reserved,
        "claim": "pending isolated code tests and independent semantic audit; never formal-eligible here"})
    write_once(root / "sources.json", sources)
    return selection


class EditorialPilot(Pipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(self.config.prompt_version == PROTOCOL and through == "review_dag", "wrong pilot protocol")
        require(self.config.max_calls <= 12 and self.config.max_reserved_tokens <= 600000,
                "editorial canary allocation exceeded")
        manifest = read_json(self.root / "editorial-manifest.json")
        require(self.config.max_calls <= manifest.get("max_calls", 12)
                and self.config.max_reserved_tokens <= manifest.get("max_reserved_tokens", 600000),
                "config exceeds remaining shared editorial allocation")
        items = read_json(self.root / "items.json")
        require(manifest["protocol"] == PROTOCOL and digest(items) == manifest["items_sha256"]
                and digest(read_json(self.root / "selection.json")) == manifest["selection_sha256"]
                and digest(read_json(self.root / "sources.json")) == manifest["source_snapshots_sha256"],
                "frozen editorial inputs changed")
        for item in items:
            validate_editorial(item)
        return super().run(limit, progress, through)

    def process(self, item, through="review_dag"):
        data = {"question": public_question(item), "editorial": item["official_editorial"],
                "reference_code": item["reference_code"], "reference_execution": "not_executed"}
        stage = "atomize"
        try:
            graph = self.request_stage(stage, item, data, payload(stage, data, self.config),
                                       lambda value: validate_graph(value, item))
            stage = "review_dag"
            audit_data = {**data, "candidate": graph}
            audit = self.request_stage(stage, item, audit_data, payload(stage, audit_data, self.config), validate_audit)
            if audit["decision"] != "accept":
                return self._finish(item, "rejected" if audit["decision"] == "reject" else "needs_review",
                                    stage, audit["reason"])
            candidate = {"schema_version": "editorial_feasibility_dag_v1", "construction_protocol": PROTOCOL,
                         "item_id": item["item_id"], "source": item, "nodes": graph["nodes"],
                         "dag_review": audit, "formal_eligible": False,
                         "calculation_check": {"status": "not_executed"},
                         "quality_status": "source_grounded_candidate_pending_execution_and_independent_audit",
                         "limitation": "official prose/code, model-derived steps/edges, same-model review; not official gold DAG"}
            # Deliberately NOT dag.json / model_accepted: cannot enter formal export.
            write_once(self.root / "items" / item["item_id"] / "editorial-candidate.json", candidate)
            return self._finish(item, "editorial_candidate", stage, "source feasibility passed; execution and audit still required")
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
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--editorials", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous-pilot", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        print(json.dumps(prepare(args.source, args.editorials, args.root, args.previous_pilot)))


if __name__ == "__main__":
    main()
