"""Source-bound, one-candidate revision of held LiveCodeBench v6 DAGs.

The previous releases and responses remain immutable. This protocol permits
adding, deleting and rewriting reasoning steps, but not changing the question,
reference program or CPU evidence. A fresh-context semantic review can still
reject the revised candidate. It never retries a semantic verdict in a loop.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from .calibri_normalize import assemble_graph, normalize, public_input
from .client import CallFailure
from .config import Config
from .livecodebench_repair_continuation_export import _read_jsonl
from .pipeline import Pipeline
from .schemas import InvalidOutput, require, text
from .stages import payload
from .storage import digest, private_dir, read_json, write_once
from .t2ance_v4 import assemble_graph_v4, normalize_v4, validate_audit_v4

PROTOCOL = "lcb-dag-revision-v1"
PROTOCOL_V2 = "lcb-dag-revision-v2"
PROTOCOL_V3 = "lcb-dag-revision-v3"
PROTOCOL_V4 = "lcb-dag-revision-v4"
PROTOCOLS = (PROTOCOL, PROTOCOL_V2, PROTOCOL_V3, PROTOCOL_V4)
CALIBRI_RUNS = ("calibri-normalize4-v2", "calibri-full-continuation87-v1")
T2ANCE_RUNS = (
    "t2ance-v4-heldout6-20260925",
    "t2ance-v4-batch0-rest8-20260925",
    "t2ance-v4-batch1-budget-continuation-20260925",
    "t2ance-v4-batch2-budget-continuation-20260925",
    "t2ance-v4-batch3-budget-continuation-20260925",
    "t2ance-v4-old7-budget-continuation-20260925",
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_index(base):
    result = {}
    for tier, names in (("CALIBRI", CALIBRI_RUNS), ("t2ance", T2ANCE_RUNS)):
        for name in names:
            run = base / name
            for item in read_json(run / "items.json"):
                item_id = item["item_id"]
                require(item_id not in result, "duplicate source item across frozen runs")
                result[item_id] = (tier, name, item)
    require(len(result) == 151, "expected 91 CALIBRI and 60 t2ance source items")
    return result


def _compact_prior(normalized, graph):
    """Remove repeated source spans/code while retaining all prior claim text."""
    if normalized is not None:
        normalized = {"protocol": normalized["protocol"],
                      "steps": [{key: node[key] for key in (
                          "node_id", "kind", "statement", "source_refs",
                          "support_type", "normalization_note")}
                          for node in normalized["nodes"]],
                      "omissions": normalized["omissions"]}
    if graph is not None:
        graph = {"nodes": [{key: node[key] for key in (
            "node_id", "kind", "parents", "justification") if key in node}
            | {"statement": "<frozen reference program attached in C source units>"
               if node["kind"] == "answer" else node["statement"]}
            for node in graph["nodes"]]}
    return normalized, graph


def prepare(base, baseline, root, *, question_ids=None, excluded_question_ids=(),
            protocol=PROTOCOL):
    base, baseline, root = map(lambda p: Path(p).resolve(), (base, baseline, root))
    require(protocol in PROTOCOLS, "unknown revision protocol")
    require(root.is_relative_to(base) and not root.is_relative_to(base / "releases"),
            "revision run must be a new private batch under the campaign")
    rows = _read_jsonl(baseline / "flow_175.jsonl")
    manifest = read_json(baseline / "manifest.json")
    require(manifest["schema_version"] == "lcb-v6-t2ance-v4-candidates-v1"
            and manifest["original_questions"] == len(rows) == 175
            and manifest["flow_sha256"] == _sha(baseline / "flow_175.jsonl"),
            "frozen 175-question baseline changed")
    held = [row for row in rows if row["status"] == "needs_review"]
    require(len(held) == 58 and len({row["item_id"] for row in held}) == 58,
            "expected 58 unique held DAG cases")
    ids = None if question_ids is None else set(question_ids)
    excluded = set(excluded_question_ids)
    require(not ids or len(ids) == len(question_ids), "duplicate requested question")
    require(not excluded.intersection(ids or set()), "included and excluded question overlap")
    if ids is not None:
        require(ids <= {str(row["question_id"]) for row in held},
                "requested question is not in the held cohort")
    require(excluded <= {str(row["question_id"]) for row in held},
            "excluded question is not in the held cohort")
    selected = [row for row in held if (ids is None or str(row["question_id"]) in ids)
                and str(row["question_id"]) not in excluded]
    selected.sort(key=lambda row: str(row["question_id"]))
    require(selected, "empty revision selection")
    source = _source_index(base)
    items, feedback, origins = [], {}, {}
    for row in selected:
        item_id = row["item_id"]
        tier, name, item = source[item_id]
        require(tier == row["source_tier"] and item["question_id"] == row["question_id"]
                and item["execution_evidence"]["status"] == "passed",
                "held row does not match CPU-passed frozen source")
        result = read_json(base / name / "evidence/results" / (item_id + ".json"))
        require(result["status"] == "passed"
                and result["code_sha256"] == digest(item["reference_code"])
                and item["execution_evidence"]["result_sha256"] == digest(result),
                "reference code differs from independent CPU evidence")
        prior = base / name / "items" / item_id
        prior_normalized = (read_json(prior / "normalization.json")
                            if (prior / "normalization.json").exists() else None)
        prior_graph = (read_json(prior / "review_dag/input.json")["input"].get("candidate")
                       if (prior / "review_dag/input.json").exists() else None)
        if row.get("repair_terminal_run"):
            latest = base / row["repair_terminal_run"] / "items" / item_id
            if (latest / "review_dag/input.json").exists():
                prior_graph = read_json(latest / "review_dag/input.json")["input"].get("candidate")
        if protocol == PROTOCOL_V3:
            prior_normalized, prior_graph = _compact_prior(prior_normalized, prior_graph)
        note = {"source_tier": tier, "prior_status": row["status"],
                "prior_reason": row["reason"],
                "prior_normalization": prior_normalized,
                "prior_candidate": prior_graph,
                "prior_graph_may_precede_latest_feedback": row.get("repair_terminal_run") is not None}
        items.append(item)
        feedback[item_id] = note
        origins[item_id] = {"source_run": name, "source_item_sha256": digest(item),
                            "cpu_result_sha256": digest(result),
                            "feedback_sha256": digest(note)}
    root = private_dir(root)
    require(not any(root.iterdir()), "revision output must be a new empty directory")
    selection = {"protocol": protocol, "selected_ids": [i["item_id"] for i in items],
                 "selected_question_ids": [str(i["question_id"]) for i in items],
                 "baseline_flow_sha256": manifest["flow_sha256"],
                 "excluded_held_question_ids": sorted(excluded),
                 "no_score_selection": True, "maximum_semantic_revisions": 1}
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    for item_id, note in feedback.items():
        write_once(root / "feedback" / (item_id + ".json"), note)
    write_once(root / "revision-manifest.json", {
        "protocol": protocol, "baseline": str(baseline), "selected": len(items),
        "items_sha256": digest(items), "selection_sha256": digest(selection),
        "origins": origins, "human_approved": 0, "formal_eligible": False,
    })
    return {"selected": len(items), "source_tiers": {
        tier: sum(feedback[i["item_id"]]["source_tier"] == tier for i in items)
        for tier in ("CALIBRI", "t2ance")}}


def verify_prepared(root, config):
    root = Path(root)
    manifest = read_json(root / "revision-manifest.json")
    items, selection = read_json(root / "items.json"), read_json(root / "selection.json")
    require(config.task_type == "livecodebench" and config.prompt_version in PROTOCOLS
            and config.solution_source == "reference_dag_revision"
            and manifest["protocol"] == selection["protocol"] == config.prompt_version
            and manifest["items_sha256"] == digest(items)
            and manifest["selection_sha256"] == digest(selection)
            and len(items) == manifest["selected"]
            and [i["item_id"] for i in items] == selection["selected_ids"],
            "revision input, selection or config changed")
    for item in items:
        item_id = item["item_id"]
        origin = manifest["origins"][item_id]
        feedback = read_json(root / "feedback" / (item_id + ".json"))
        require(digest(item) == origin["source_item_sha256"]
                and digest(feedback) == origin["feedback_sha256"]
                and item["execution_evidence"]["result_sha256"] == origin["cpu_result_sha256"]
                and feedback["source_tier"] in ("CALIBRI", "t2ance"),
                "revision source or feedback changed")
    if config.prompt_version == PROTOCOL_V4:
        from .lcb_revision_followup import verify_followup
        verify_followup(root, manifest, selection, items)
    return items


def _normalize(value, item, tier):
    require(isinstance(value, dict) and set(value) == {"steps", "omissions", "change_summary"}
            and text(value["change_summary"]), "revision needs steps, omissions and edit summary")
    proposal = {key: value[key] for key in ("steps", "omissions")}
    return (normalize(proposal, item, prompt_version="calibri-lcb-normalize-v3")
            if tier == "CALIBRI" else normalize_v4(proposal, item))


class LCBDAGRevisionPipeline(Pipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(through == "review_dag", "revision must finish semantic review")
        verify_prepared(self.root, self.config)
        return super().run(limit, progress, through)

    def process(self, item, through="review_dag"):
        directory = self.root / "items" / item["item_id"]
        if (directory / "result.json").exists():
            return read_json(directory / "result.json")
        feedback = read_json(self.root / "feedback" / (item["item_id"] + ".json"))
        tier, stage = feedback["source_tier"], "revise"
        public = public_input(item)
        try:
            revision_public = dict(public)
            if self.config.prompt_version in (PROTOCOL_V3, PROTOCOL_V4):
                # Source units already contain every code line. Avoid sending
                # the full program a second time in the same revision request.
                revision_public.pop("reference_code")
            revision_input = {**revision_public, "source_tier": tier,
                              "prior_feedback": feedback}
            revision = self.request_stage(stage, item, revision_input,
                payload(stage, revision_input, self.config),
                lambda value: _normalize(value, item, tier))
            normalized = _normalize(revision, item, tier)
            write_once(directory / "normalization.json", normalized)
            stage = "dependencies"
            graph_input = {**public, "source_tier": tier, "normalized": normalized}
            assembler = assemble_graph if tier == "CALIBRI" else assemble_graph_v4
            dependencies = self.request_stage(stage, item, graph_input,
                payload(stage, graph_input, self.config),
                lambda value: assembler(value, normalized, item))
            graph = assembler(dependencies, normalized, item)
            stage = "review_dag"
            review_input = {**public, "source_tier": tier,
                            "normalized": normalized, "candidate": graph}
            # The revision prompt asks for the complete, stronger v4 check set
            # for both tiers; no CALIBRI candidate may pass on fewer checks.
            reviewer = validate_audit_v4
            audit = self.request_stage(stage, item, review_input,
                payload(stage, review_input, self.config), reviewer)
            if audit["decision"] != "accept":
                return self._finish(item, "rejected" if audit["decision"] == "reject"
                                    else "needs_review", stage, audit["reason"])
            dag = {"schema_version": "reference_dag_v1",
                   "construction_protocol": self.config.prompt_version,
                   "item_id": item["item_id"], "source": item, "nodes": graph["nodes"],
                   "normalization": normalized, "dag_review": audit,
                   "revision_change_summary": revision["change_summary"],
                   "revision_feedback_sha256": digest(feedback),
                   "execution_evidence": item["execution_evidence"],
                   "calculation_check": {"status": "reference_tests_passed"},
                   "formal_eligible": False,
                   "quality_status": "model_reviewed_pending_independent_semantic_audit",
                   "limitation": "Source-derived explanation; same-model revision and review, not human or official gold"}
            if tier == "t2ance":
                dag["graph_transformation"] = graph["transformation"]
            write_once(directory / "dag.json", dag)
            return self._finish(item, "model_accepted", stage,
                                "pending independent release audit", digest(dag))
        except InvalidOutput as error:
            return self._finish(item, "needs_review", stage, str(error))
        except CallFailure as error:
            if not (self.resilient and error.category == "transient_retries_exhausted"):
                self._stop.set()
            return {"item_id": item["item_id"], "status": "paused",
                    "stage": stage, "reason": error.category}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "baseline", "root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--question-ids", help="Comma-separated fixed canary question IDs")
    parser.add_argument("--exclude-question-ids", help="Comma-separated completed canary IDs")
    parser.add_argument("--protocol", choices=PROTOCOLS, default=PROTOCOL)
    args = parser.parse_args()
    parse = lambda value: None if value is None else tuple(x for x in value.split(",") if x)
    print(json.dumps(prepare(args.base, args.baseline, args.root,
        question_ids=parse(args.question_ids),
        excluded_question_ids=parse(args.exclude_question_ids) or (),
        protocol=args.protocol)))


if __name__ == "__main__":
    main()
