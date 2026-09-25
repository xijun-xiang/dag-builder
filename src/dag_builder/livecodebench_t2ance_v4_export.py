"""Audit and publish the complete v4 t2ance model-candidate inventory.

The previous 175-question release is immutable. New v4 verdicts supersede old
t2ance pilot verdicts; CALIBRI verdicts stay unchanged. This is still neither
human-approved nor official-gold data.
"""

import argparse
import hashlib
import html
import json
from collections import Counter
from pathlib import Path

from .calibri_full_export import _event
from .livecodebench_repair_continuation_export import _read_jsonl
from .schemas import require
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .t2ance_audit import audit_completed as audit_first_pass
from .t2ance_budget_continuation import audit_continuation
from .unified import convert_file, convert_record

PROTOCOL = "lcb-v6-t2ance-v4-candidates-v1"
FINAL_RUNS = (
    "t2ance-v4-heldout6-20260925",
    "t2ance-v4-batch0-rest8-20260925",
    "t2ance-v4-batch1-budget-continuation-20260925",
    "t2ance-v4-batch2-budget-continuation-20260925",
    "t2ance-v4-batch3-budget-continuation-20260925",
    "t2ance-v4-old7-budget-continuation-20260925",
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _jsonl(rows):
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                              allow_nan=False) + "\n" for row in rows).encode()


def _html(rows, counts):
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(row[key])) + "</td>"
                                  for key in ("question_id", "source_tier", "cpu_status",
                                              "status", "reason")) + "</tr>" for row in rows)
    return ("<!doctype html><html lang='zh'><meta charset='utf-8'>"
            "<title>LiveCodeBench v6 DAG 流转</title><style>body{font:15px system-ui;"
            "max-width:1400px;margin:2rem auto;color:#172238}table{border-collapse:collapse;"
            "width:100%}td,th{border:1px solid #ccd3dc;padding:.5rem;text-align:left;"
            "vertical-align:top}tr:nth-child(even){background:#f5f7fb}</style>"
            "<h1>LiveCodeBench v6：175 题 DAG 流转</h1>"
            "<p>60 份 t2ance 参考程序通过冻结测试并完成 v4 DAG 构图；"
            "模型审核候选不等于人工验收、官方 gold 或正式 PALS 数据。</p>"
            "<p><a href='flow_175.jsonl'>逐题流转</a> · "
            "<a href='accepted_candidates.jsonl'>模型候选</a> · "
            "<a href='unified/pals_dag_unified_v1.html'>统一 DAG 查看器</a></p>"
            "<pre>" + html.escape(json.dumps(counts, ensure_ascii=False, indent=2)) + "</pre>"
            "<table><tr><th>题号</th><th>来源</th><th>CPU</th><th>DAG 状态</th>"
            "<th>原因</th></tr>" + body + "</table></html>").encode()


def _audited_events(base):
    source = read_json(base / "t2ance-source-v1/selected/items.json")
    by_id = {item["item_id"]: item for item in source}
    require(len(source) == len(by_id) == 60, "t2ance source is not 60 unique items")
    events, proofs = {}, []
    for name in FINAL_RUNS:
        run = base / name
        continuation = (run / "budget-continuation-manifest.json").exists()
        audit = (audit_continuation(run) if continuation else audit_first_pass(run))
        require(read_json(run / "offline-audit.json") == audit
                and audit["mechanical_pass"], "missing or stale raw-content audit")
        items = read_json(run / "items.json")
        require(len(items) == audit["selected"]
                and len(items) == len({i["item_id"] for i in items}),
                "audited run has duplicate or missing selected items")
        for item in items:
            item_id = item["item_id"]
            require(item_id in by_id and item_id not in events
                    and all(item[k] == by_id[item_id][k]
                            for k in ("question_id", "question", "raw_output",
                                      "reference_code", "source_content_sha256")),
                    "v4 run duplicates or changes a source item")
            event = _event(run, item_id)
            result = read_json(run / "evidence/results" / (item_id + ".json"))
            require(event is not None and result["status"] == "passed"
                    and result["code_sha256"] == digest(item["reference_code"])
                    and result["tests_sha256"] == item["tests_sha256"]
                    and item["execution_evidence"]["result_sha256"] == digest(result),
                    "v4 DAG is not bound to independent CPU result")
            events[item_id] = (item, event, result)
        proofs.append({"run": name, "selected": len(items),
                       "counts": audit["counts"], "audit_sha256": digest(audit),
                       "api_attempts": audit["api_attempts"],
                       "reserved_tokens": audit["reserved_tokens"]})
    require(set(events) == set(by_id), "v4 runs do not cover all 60 CPU-passed items")
    return events, proofs


def _record(item, event, result):
    dag = event["dag"]
    require(dag is not None and dag["source"] == item
            and dag["construction_protocol"] == "t2ance-lcb-normalize-v4"
            and dag["normalization"]["protocol"] == "t2ance-lcb-normalize-v4",
            "accepted v4 DAG differs from frozen item")
    return {"schema_version": PROTOCOL, "item_id": item["item_id"],
            "question_id": item["question_id"], "status": "model_accepted",
            "source": item, "dag": dag, "dag_sha256": digest(dag),
            "cpu_result_sha256": digest(result),
            "model_accepted": True, "human_approved": False,
            "formal_eligible": False,
            "source_status": "t2ance_derived_tested_reference"}


def export(base, baseline, output):
    base, baseline, output = map(lambda p: Path(p).resolve(), (base, baseline, output))
    require(output.is_relative_to(base / "releases") and output != baseline
            and not output.is_relative_to(baseline), "output must be a new release")
    old_manifest = read_json(baseline / "manifest.json")
    require(old_manifest["schema_version"] == "lcb-v6-split-repair-cpu60-flow-v1"
            and old_manifest["original_questions"] == 175
            and old_manifest["t2ance_cpu_passed"] == 60
            and _sha(baseline / "flow_175.jsonl") == old_manifest["flow_sha256"]
            and _sha(baseline / "accepted_candidates.jsonl") ==
            old_manifest["accepted_sha256"], "CPU60 baseline changed")
    rows = _read_jsonl(baseline / "flow_175.jsonl")
    old_accepted = _read_jsonl(baseline / "accepted_candidates.jsonl")
    require(len(rows) == 175 and len({row["item_id"] for row in rows}) == 175
            and len(old_accepted) == old_manifest["model_accepted"],
            "baseline denominator changed")
    events, proofs = _audited_events(base)
    accepted = [row for row in old_accepted
                if row["source_status"] == "calibri_derived_tested_reference"]
    require(len(accepted) == 44, "unexpected CALIBRI candidate count")
    updated = []
    for row in rows:
        item_id = row["item_id"]
        if item_id not in events:
            updated.append(row)
            continue
        item, event, cpu_result = events[item_id]
        require(row["source_tier"] == "t2ance" and row["cpu_status"] == "passed"
                and row["question_id"] == item["question_id"],
                "v4 result would overwrite another source tier")
        verdict = event["result"]
        updated.append({**row, "status": verdict["status"],
                        "reason": verdict["reason"],
                        "construction_protocol": "t2ance-lcb-normalize-v4"})
        if verdict["status"] == "model_accepted":
            accepted.append(_record(item, event, cpu_result))
    updated.sort(key=lambda row: row["question_id"])
    accepted.sort(key=lambda row: row["question_id"])
    require(len(updated) == 175 and len({r["question_id"] for r in updated}) == 175
            and len({r["item_id"] for r in accepted}) == len(accepted)
            and len(accepted) == sum(r["status"] == "model_accepted" for r in updated)
            and not any(r["status"] == "dag_not_run" for r in updated),
            "175-question v4 flow is incomplete")
    accepted_bytes = _jsonl(accepted)
    accepted_hash = hashlib.sha256(accepted_bytes).hexdigest()
    for record in accepted:
        convert_record(record, "livecodebench_v6", accepted_hash)
    flow_bytes = _jsonl(updated)
    counts = dict(Counter(row["status"] for row in updated))
    report = {"schema_version": PROTOCOL,
              "release_status": "model_reviewed_candidates_semantic_audit_pending",
              "original_questions": 175, "t2ance_cpu_tested": 60,
              "t2ance_v4_counts": dict(Counter(event["result"]["status"]
                                                for _, event, _ in events.values())),
              "model_accepted": len(accepted),
              "accepted_by_source": dict(Counter(r["source_status"] for r in accepted)),
              "human_approved": 0, "formal_eligible": False,
              "meets_50_percent_model_candidate_target": len(accepted) >= 88,
              "counts": counts, "runs": proofs,
              "baseline_manifest_sha256": digest(old_manifest),
              "baseline_flow_sha256": old_manifest["flow_sha256"],
              "flow_sha256": hashlib.sha256(flow_bytes).hexdigest(),
              "accepted_sha256": accepted_hash,
              "claim": "CPU-passed and same-model reviewed DAG candidates only; no human or official gold"}
    output = private_dir(output)
    require(not any(path.name != ".lock" for path in output.iterdir()),
            "v4 output must be empty")
    write_bytes_once(output / "flow_175.jsonl", flow_bytes)
    write_bytes_once(output / "accepted_candidates.jsonl", accepted_bytes)
    unified = convert_file(output / "accepted_candidates.jsonl", "livecodebench_v6",
                           accepted_hash, output / "unified")
    report["unified_manifest_sha256"] = digest(unified)
    write_bytes_once(output / "flow_175.html", _html(updated, counts))
    write_once(output / "manifest.json", report)
    return report


def main():
    import os
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "baseline", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.base, args.baseline, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
