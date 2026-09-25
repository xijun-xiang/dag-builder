"""Offline 175-question export after the fixed v3 DAG revision cohort.

This is a model-candidate release, not a human-approved or official-gold set.
The eight-question prompt-development cohort remains explicitly identified.
"""

import argparse
import hashlib
import html
import json
import os
from collections import Counter
from pathlib import Path

from .livecodebench_dag_revision_audit import audit as audit_run
from .livecodebench_repair_continuation_export import _read_jsonl
from .schemas import require
from .storage import digest, private_dir, read_json, run_lock, write_bytes_once, write_once
from .unified import convert_file, convert_record

PROTOCOL = "lcb-v6-dag-revision-candidates-v1"
BASELINE = "lcb-v6-t2ance-v4-candidates-v1"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _jsonl(rows):
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                   for row in rows).encode("utf-8")


def _audited_rows(run):
    run = Path(run).resolve()
    completion = read_json(run / "completion.json")
    require(completion["status"] == "processed", "revision run has not completed")
    audit = audit_run(run)
    require(audit["mechanical_pass"] is True
            and read_json(run / "offline-audit.json") == audit
            and audit["protocol"] == "lcb-dag-revision-v3",
            "revision raw audit missing, stale or from a different protocol")
    items = read_json(run / "items.json")
    manifest = read_json(run / "revision-manifest.json")
    require(len(items) == audit["selected"] == manifest["selected"],
            "revision item count changed")
    rows = {}
    for item in items:
        item_id = item["item_id"]
        result = read_json(run / "items" / item_id / "result.json")
        require(result["item_id"] == item_id and result["status"] in
                ("model_accepted", "needs_review", "rejected"),
                "nonterminal revision result")
        dag_path = run / "items" / item_id / "dag.json"
        dag = read_json(dag_path) if dag_path.exists() else None
        require((dag is not None) == (result["status"] == "model_accepted"),
                "DAG and result status disagree")
        if dag is not None:
            require(dag["source"] == item and result["dag_sha256"] == digest(dag)
                    and dag["formal_eligible"] is False,
                    "accepted revised graph differs from source/result")
        rows[item_id] = (item, result, dag, manifest["origins"][item_id])
    return rows, audit


def _merge(baseline_rows, baseline_accepted, canary, rest, *, canary_name, rest_name):
    require(len(baseline_rows) == 175 and len(baseline_accepted) == 70,
            "175-question baseline or 70 accepted candidates changed")
    held = {row["item_id"] for row in baseline_rows if row["status"] == "needs_review"}
    require(len(held) == 58 and len(canary) == 8 and len(rest) == 50
            and set(canary).isdisjoint(rest) and set(canary) | set(rest) == held,
            "8+50 revision cohort is not the exact frozen 58-question holdout")
    original = {row["item_id"] for row in baseline_accepted}
    require(len(original) == 70 and original.isdisjoint(held),
            "baseline accepted and held rows overlap")
    revisions = {**{key: (canary_name, *value) for key, value in canary.items()},
                 **{key: (rest_name, *value) for key, value in rest.items()}}
    updated, accepted = [], list(baseline_accepted)
    for row in baseline_rows:
        row = dict(row)
        item_id = row["item_id"]
        if item_id in revisions:
            run_name, item, result, dag, origin = revisions[item_id]
            require(row["status"] == "needs_review"
                    and row["question_id"] == item["question_id"]
                    and row["source_tier"] in ("CALIBRI", "t2ance")
                    and row["source_tier"] == ("CALIBRI" if item["reference_origin"] ==
                                                "calibri_model_output" else "t2ance")
                    and row["cpu_status"] == item["execution_evidence"]["status"] == "passed"
                    and origin["source_item_sha256"] == digest(item)
                    and origin["cpu_result_sha256"] ==
                    item["execution_evidence"]["result_sha256"],
                    "revision candidate differs from frozen baseline/source/CPU")
            row.update(prior_status=row["status"], prior_reason=row["reason"],
                       status=result["status"], reason=result["reason"],
                       revision_run=run_name, revision_protocol="lcb-dag-revision-v3",
                       revision_result_sha256=digest(result),
                       revision_dag_sha256=digest(dag) if dag else None,
                       revision_development_cohort=run_name == canary_name)
            if dag is not None:
                accepted.append({"schema_version": PROTOCOL, "item_id": item_id,
                                 "question_id": item["question_id"],
                                 "status": "model_accepted", "source": item,
                                 "dag": dag, "dag_sha256": digest(dag),
                                 "cpu_result_sha256": origin["cpu_result_sha256"],
                                 "full_cpu_result_sha256": origin["cpu_result_sha256"],
                                 "model_accepted": True, "human_approved": False,
                                 "formal_eligible": False,
                                 "revision_run": run_name,
                                 "revision_development_cohort": run_name == canary_name,
                                 "source_status": ("calibri_derived_tested_reference" if
                                                   row["source_tier"] == "CALIBRI" else
                                                   "t2ance_derived_tested_reference")})
        updated.append(row)
    require(len(updated) == len({row["item_id"] for row in updated}) == 175
            and len(accepted) == len({row["item_id"] for row in accepted})
            and len(accepted) == sum(row["status"] == "model_accepted" for row in updated),
            "merged 175-question flow and accepted subset disagree")
    accepted.sort(key=lambda row: str(row["question_id"]))
    return updated, accepted


def _html(rows, report):
    headers = "<tr><th>题号</th><th>来源</th><th>CPU</th><th>状态</th><th>回修批次</th><th>原因</th></tr>"
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(row.get(key, ""))) + "</td>"
                                     for key in ("question_id", "source_tier", "cpu_status",
                                                 "status", "revision_run", "reason")) + "</tr>"
                   for row in rows)
    return ("<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">"
            "<title>LiveCodeBench v6 DAG 回修流转</title>"
            "<style>body{font:15px system-ui;max-width:1400px;margin:2rem auto;color:#172238}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd3dc;"
            "padding:.5rem;text-align:left;vertical-align:top}tr:nth-child(even)"
            "{background:#f5f7fb}</style><h1>LiveCodeBench v6：175 题 DAG 回修流转</h1>"
            "<p>8 题是提示词开发，另 50 题为同协议扩展。模型审核候选不是人工或官方 gold；"
            "CPU 通过也不等于 DAG 正确。</p><p><a href=\"flow_175.jsonl\">逐题流转</a> · "
            "<a href=\"accepted_candidates.jsonl\">模型候选</a> · "
            "<a href=\"unified/pals_dag_unified_v1.html\">统一 DAG 查看器</a></p><pre>"
            + html.escape(json.dumps(report["counts"], ensure_ascii=False, indent=2))
            + "</pre><table>" + headers + body + "</table></html>").encode("utf-8")


def export(baseline, canary_run, rest_run, output):
    baseline, canary_run, rest_run, output = map(lambda p: Path(p).resolve(),
                                                  (baseline, canary_run, rest_run, output))
    require(output != baseline and not baseline.is_relative_to(output)
            and not output.is_relative_to(canary_run)
            and not output.is_relative_to(rest_run)
            and output.parent == baseline.parent
            and output.parent.name == "releases", "output must be a new named release")
    manifest = read_json(baseline / "manifest.json")
    require(manifest["schema_version"] == BASELINE
            and manifest["flow_sha256"] == _sha(baseline / "flow_175.jsonl")
            and manifest["accepted_sha256"] == _sha(baseline / "accepted_candidates.jsonl"),
            "frozen baseline changed")
    baseline_rows = _read_jsonl(baseline / "flow_175.jsonl")
    baseline_accepted = _read_jsonl(baseline / "accepted_candidates.jsonl")
    canary, canary_audit = _audited_rows(canary_run)
    rest, rest_audit = _audited_rows(rest_run)
    require(read_json(canary_run / "selection.json")["baseline_flow_sha256"] ==
            read_json(rest_run / "selection.json")["baseline_flow_sha256"] ==
            manifest["flow_sha256"], "revision batches used different baselines")
    rows, accepted = _merge(baseline_rows, baseline_accepted, canary, rest,
                            canary_name=canary_run.name, rest_name=rest_run.name)
    accepted_bytes, flow_bytes = _jsonl(accepted), _jsonl(rows)
    accepted_sha = hashlib.sha256(accepted_bytes).hexdigest()
    for record in accepted:
        convert_record(record, "livecodebench_v6", accepted_sha)
    report = {"schema_version": PROTOCOL, "release_status":
              "model_reviewed_candidates_independent_semantic_audit_pending",
              "original_questions": 175, "baseline_accepted": 70,
              "revision_development_questions": 8, "revision_expansion_questions": 50,
              "revision_development_accepted": canary_audit["counts"].get("model_accepted", 0),
              "revision_expansion_accepted": rest_audit["counts"].get("model_accepted", 0),
              "model_accepted": len(accepted),
              "counts": dict(Counter(row["status"] for row in rows)),
              "accepted_by_source": dict(Counter(record["source_status"] for record in accepted)),
              "meets_50_percent_model_candidate_target": len(accepted) >= 88,
              "human_approved": 0, "formal_eligible": False,
              "baseline_flow_sha256": manifest["flow_sha256"],
              "baseline_accepted_sha256": manifest["accepted_sha256"],
              "canary_audit_sha256": digest(canary_audit),
              "rest_audit_sha256": digest(rest_audit),
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
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--canary-run", type=Path, required=True)
    parser.add_argument("--rest-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with run_lock(args.output):
        print(json.dumps(export(args.baseline, args.canary_run, args.rest_run,
                                args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
