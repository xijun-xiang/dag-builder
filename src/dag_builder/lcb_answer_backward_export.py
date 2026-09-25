"""Immutable 175-question release after audited answer-backward review.

This overlays one bounded structural follow-up on the previous model-candidate
release. A same-model accept is not human validation or official gold.
"""

import argparse
import hashlib
import html
import json
import os
from collections import Counter
from pathlib import Path

from .lcb_answer_backward_review_audit import audit as audit_review
from .livecodebench_dag_revision_export import _jsonl, _sha
from .livecodebench_repair_continuation_export import _read_jsonl
from .schemas import require
from .storage import digest, private_dir, read_json, run_lock, write_bytes_once, write_once
from .unified import convert_file, convert_record


PROTOCOL = "lcb-v6-dag-revision-answerbackward-candidates-v1"
PREVIOUS = "lcb-v6-dag-revision-candidates-v1"


def _html(rows, report):
    columns = ("question_id", "source_tier", "cpu_status", "status",
               "revision_run", "answer_backward_model_status", "reason")
    labels = ("题号", "来源", "CPU", "状态", "原回修批次", "旁支复审", "原因")
    header = "<tr>" + "".join("<th>" + label + "</th>" for label in labels) + "</tr>"
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(row.get(key, ""))) +
                                   "</td>" for key in columns) + "</tr>" for row in rows)
    counts = html.escape(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    return ("<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">"
            "<title>LiveCodeBench v6 DAG 旁支复审</title>"
            "<style>body{font:15px system-ui;max-width:1400px;margin:2rem auto;color:#172238}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd3dc;"
            "padding:.5rem;text-align:left;vertical-align:top}tr:nth-child(even)"
            "{background:#f5f7fb}</style><h1>LiveCodeBench v6：175 题 DAG 流转</h1>"
            "<p>原 58 题回修中的 11 道旁支问题只做删旁支并重新审核；未通过者保留原状态。"
            "模型候选不是人工或官方 gold，CPU 通过也不等于 DAG 正确。</p>"
            "<p><a href=\"flow_175.jsonl\">逐题流转</a> · "
            "<a href=\"accepted_candidates.jsonl\">模型候选</a> · "
            "<a href=\"unified/pals_dag_unified_v1.html\">统一 DAG 查看器</a></p>"
            "<pre>" + counts + "</pre><table>" + header + body + "</table></html>").encode("utf-8")


def _merge(previous_rows, previous_accepted, review_rows, *, run_name):
    require(len(previous_rows) == 175 and len(previous_accepted) == 89,
            "previous 175-question model-candidate release changed")
    old = {row["item_id"]: row for row in previous_rows}
    require(len(old) == 175 and len(review_rows) == 11
            and all(old[item_id]["status"] == "needs_review"
                    and old[item_id]["source_tier"] == "CALIBRI"
                    and old[item_id]["revision_run"] == "lcb-dag-revision58-rest50-v3"
                    for item_id in review_rows),
            "review cohort differs from frozen CALIBRI structural failures")
    accepted = list(previous_accepted)
    merged = []
    for original in previous_rows:
        row = dict(original)
        item_id = row["item_id"]
        if item_id in review_rows:
            item, result, dag, evidence = review_rows[item_id]
            require(item["item_id"] == item_id
                    and item["question_id"] == row["question_id"]
                    and item["execution_evidence"]["status"] == row["cpu_status"] == "passed"
                    and row["revision_result_sha256"] == digest(evidence["previous_result"])
                    and result["status"] in ("model_accepted", "needs_review", "rejected")
                    and (dag is not None) == (result["status"] == "model_accepted"),
                    "review differs from frozen source or prior result")
            row.update(answer_backward_run=run_name,
                       answer_backward_model_status=result["status"],
                       answer_backward_reason=result["reason"],
                       answer_backward_result_sha256=digest(result),
                       answer_backward_dag_sha256=digest(dag) if dag else None)
            if dag is not None:
                require(dag["source"] == item and dag["formal_eligible"] is False
                        and result["dag_sha256"] == digest(dag),
                        "accepted follow-up DAG differs from frozen review")
                row.update(status="model_accepted", reason=result["reason"])
                accepted.append({"schema_version": PROTOCOL, "item_id": item_id,
                                 "question_id": item["question_id"],
                                 "status": "model_accepted", "source": item,
                                 "dag": dag, "dag_sha256": digest(dag),
                                 "cpu_result_sha256": item["execution_evidence"]["result_sha256"],
                                 "full_cpu_result_sha256": item["execution_evidence"]["result_sha256"],
                                 "model_accepted": True, "human_approved": False,
                                 "formal_eligible": False, "revision_run": run_name,
                                 "revision_development_cohort": False,
                                 "source_status": "calibri_derived_tested_reference"})
        merged.append(row)
    require(len(accepted) == len({record["item_id"] for record in accepted})
            == sum(row["status"] == "model_accepted" for row in merged),
            "accepted subset and 175-question flow disagree")
    accepted.sort(key=lambda record: str(record["question_id"]))
    return merged, accepted


def export(previous_release, review_run, output):
    previous_release, review_run, output = map(lambda path: Path(path).resolve(),
                                                (previous_release, review_run, output))
    require(output.parent == previous_release.parent
            and output.parent.name == "releases"
            and output != previous_release
            and not output.is_relative_to(review_run),
            "new sibling release required")
    previous_manifest = read_json(previous_release / "manifest.json")
    require(previous_manifest["schema_version"] == PREVIOUS
            and previous_manifest["flow_sha256"] == _sha(previous_release / "flow_175.jsonl")
            and previous_manifest["accepted_sha256"] == _sha(
                previous_release / "accepted_candidates.jsonl")
            and previous_manifest["model_accepted"] == 89,
            "previous release manifest or content changed")
    previous_rows = _read_jsonl(previous_release / "flow_175.jsonl")
    previous_accepted = _read_jsonl(previous_release / "accepted_candidates.jsonl")
    completion = read_json(review_run / "completion.json")
    review_audit = audit_review(review_run)
    require(completion["status"] == "processed"
            and read_json(review_run / "offline-audit.json") == review_audit
            and review_audit["mechanical_pass"] is True
            and review_audit["selected"] == 11
            and review_audit["protocol"] == "lcb-answer-backward-review-v1"
            and review_audit["previous_audit_sha256"] ==
            previous_manifest["rest_audit_sha256"],
            "answer-backward review incomplete or audit does not match release")
    items = read_json(review_run / "items.json")
    require(len(items) == 11 and len({item["item_id"] for item in items}) == 11,
            "duplicate or missing answer-backward review item")
    reviewed = {}
    for item in items:
        item_id = item["item_id"]
        directory = review_run / "items" / item_id
        result = read_json(directory / "result.json")
        dag_path = directory / "dag.json"
        reviewed[item_id] = (item, result,
                             read_json(dag_path) if dag_path.is_file() else None,
                             read_json(review_run / "evidence" / (item_id + ".json")))
    rows, accepted = _merge(previous_rows, previous_accepted, reviewed,
                            run_name=review_run.name)
    accepted_bytes, flow_bytes = _jsonl(accepted), _jsonl(rows)
    accepted_sha = hashlib.sha256(accepted_bytes).hexdigest()
    for record in accepted:
        convert_record(record, "livecodebench_v6", accepted_sha)
    report = {"schema_version": PROTOCOL,
              "release_status": "model_reviewed_candidates_independent_semantic_audit_pending",
              "original_questions": 175, "prior_model_accepted": 89,
              "answer_backward_reviewed": 11,
              "answer_backward_accepted": review_audit["counts"].get("model_accepted", 0),
              "model_accepted": len(accepted),
              "counts": dict(Counter(row["status"] for row in rows)),
              "human_approved": 0, "formal_eligible": False,
              "meets_50_percent_model_candidate_target": len(accepted) >= 88,
              "prior_manifest_sha256": digest(previous_manifest),
              "prior_flow_sha256": previous_manifest["flow_sha256"],
              "prior_accepted_sha256": previous_manifest["accepted_sha256"],
              "review_audit_sha256": digest(review_audit),
              "flow_sha256": hashlib.sha256(flow_bytes).hexdigest(),
              "accepted_sha256": accepted_sha,
              "claim": "Same-model source-derived candidates; no human or official gold"}
    output = private_dir(output)
    require(not any(path.name != ".lock" for path in output.iterdir()),
            "release output is not empty")
    write_bytes_once(output / "flow_175.jsonl", flow_bytes)
    write_bytes_once(output / "accepted_candidates.jsonl", accepted_bytes)
    unified = convert_file(output / "accepted_candidates.jsonl", "livecodebench_v6",
                           accepted_sha, output / "unified")
    report["unified_manifest_sha256"] = digest(unified)
    write_once(output / "manifest.json", report)
    write_bytes_once(output / "flow_175.html", _html(rows, report))
    return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-release", type=Path, required=True)
    parser.add_argument("--review-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with run_lock(args.output):
        print(json.dumps(export(args.previous_release, args.review_run, args.output),
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
