"""Explicitly interim 175-question LCB flow while CALIBRI repair is paused.

Only completed, independently replayed runs may contribute accepted candidates.
The paused repair is inventory evidence, not a source of released DAGs.
"""

import hashlib
import html
import json
from collections import Counter
from pathlib import Path

from .calibri_continuation import audit_completed as audit_continuation
from .calibri_full_export import _event
from .calibri_repair import verify_repair
from .calibri_review_resume import audit_completed as audit_review_resume
from .config import Config
from .livecodebench_dag import verify_execution
from .schemas import require
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .t2ance_audit import audit_completed as audit_t2ance
from .t2ance_pipeline import verify_prepared as verify_t2ance_prepared
from .unified import convert_file, convert_record

PROTOCOL = "lcb-v6-source-stratified-candidates-v1"


def _jsonl(rows):
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                   for row in rows).encode("utf-8")


def _html(rows, counts):
    headers = "<tr><th>题号</th><th>来源</th><th>CPU</th><th>DAG 状态</th><th>原因</th></tr>"
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(row[key])) + "</td>"
                                      for key in ("question_id", "source_tier", "cpu_status",
                                                  "status", "reason")) + "</tr>" for row in rows)
    return ("<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">"
            "<title>LiveCodeBench v6 阶段性流转</title><style>body{font:15px system-ui;"
            "max-width:1400px;margin:2rem auto;color:#172238}table{border-collapse:collapse;"
            "width:100%}td,th{border:1px solid #ccd3dc;padding:.5rem;text-align:left;"
            "vertical-align:top}tr:nth-child(even){background:#f5f7fb}</style>"
            "<h1>LiveCodeBench v6：175 题阶段性流转</h1>"
            "<p>CALIBRI 回修尚未完成；本页不是最终数据发布。模型审核候选并非官方或人工 gold。"
            "未做独立 CPU 复验的 t2ance 候选不计合格。</p>"
            "<p><a href=\"flow_175.jsonl\">逐题流转 JSONL</a> · "
            "<a href=\"accepted_candidates.jsonl\">阶段性接受候选</a> · "
            "<a href=\"unified/pals_dag_unified_v1.html\">统一 DAG 查看器</a></p>"
            "<pre>" + html.escape(json.dumps(counts, ensure_ascii=False, indent=2)) + "</pre>"
            "<table>" + headers + body + "</table></html>").encode("utf-8")


def _record(item, event, cpu_result, origin, evidence_result):
    dag = event["dag"]
    require(origin in ("CALIBRI", "t2ance")
            and dag is not None and dag["source"]["item_id"] == item["item_id"]
            and all(dag["source"].get(key) == item.get(key)
                    for key in ("question_id", "question", "raw_output", "reference_code"))
            and dag["source"].get("execution_evidence", {}).get("status") == "passed"
            and dag["source"]["execution_evidence"]["result_sha256"] == digest(evidence_result)
            and cpu_result["status"] == evidence_result["status"] == "passed",
            "candidate source or CPU evidence mismatch")
    return {"schema_version": PROTOCOL, "item_id": item["item_id"],
            "question_id": item["question_id"], "status": "model_accepted",
            "source": dag["source"], "dag": dag, "dag_sha256": digest(dag),
            "cpu_result_sha256": digest(evidence_result),
            "full_cpu_result_sha256": digest(cpu_result), "model_accepted": True,
            "human_approved": False, "formal_eligible": False,
            "source_status": ("calibri_derived_tested_reference" if origin == "CALIBRI"
                              else "t2ance_derived_tested_reference")}


def export(base, output):
    base, output = Path(base).resolve(), private_dir(output)
    cal_source = base / "calibri-full175-v1"
    cal_execution = base / "calibri-full-execution93-v2/b1-results"
    continuation = base / "calibri-full-continuation87-v1"
    repair = base / "calibri-full-repair-v2-dns-continuation-v2"
    dev_runs = [base / name for name in ("calibri-normalize4-v2", "calibri-repair3-v1",
                                        "calibri-repair2-v2", "calibri-repair2-v2-review64k")]
    t_source = base / "t2ance-source-v1/selected"
    t_execution = base / "t2ance-cpu-canary7-b1-111733-v1/execution"
    t_run = base / "t2ance-dag-canary5-v1"

    index = read_json(cal_source / "candidate-index.json")
    cal_items = read_json(cal_source / "items.json")
    cal_selection = read_json(cal_source / "selection.json")
    cal_manifest = read_json(cal_source / "calibri-manifest.json")
    question_ids = {row["question_id"] for row in index}
    require(len(question_ids) == 175
            and len(cal_items) == 93 and len(cal_selection["excluded"]) == 82
            and digest(index) == cal_manifest["index_sha256"]
            and digest(cal_items) == cal_manifest["items_sha256"]
            and digest(cal_selection) == cal_manifest["selection_sha256"],
            "CALIBRI 175-question source changed")
    cal_cpu, cal_completion = verify_execution(cal_execution,
                                               cal_execution / "input-manifest.json")
    dev_execution = base / "calibri-execution4-v1/b1-results"
    dev_cpu, dev_completion = verify_execution(dev_execution,
                                               dev_execution / "input-manifest.json")
    require(len(cal_cpu) == 93 and cal_completion["passed"] == 91
            and len(dev_cpu) == dev_completion["passed"] == 4
            and read_json(cal_execution / "offline-audit.json")["mechanical_pass"] is True,
            "CALIBRI CPU gate changed")
    require(all(cal_cpu[item["item_id"]][0]["code"] == item["reference_code"]
                for item in cal_items), "CALIBRI full CPU code differs from frozen source")
    require(read_json(continuation / "offline-continuation-audit.json") ==
            audit_continuation(continuation), "CALIBRI continuation audit changed")
    continued_ids = {item["item_id"] for item in read_json(continuation / "items.json")}
    dev_ids = set(cal_cpu) - continued_ids - {key for key, (_, result) in cal_cpu.items()
                                               if result["status"] != "passed"}
    require(len(continued_ids) == 87 and len(dev_ids) == 4, "CALIBRI development split changed")
    require(set(dev_cpu) == dev_ids, "development DAGs belong to different CPU references")
    require(all(dev_cpu[item["item_id"]][0]["code"] == item["reference_code"]
                for item in cal_items if item["item_id"] in dev_ids),
            "CALIBRI development CPU code differs from frozen source")
    require(all(read_json(run / "completion.json")["status"] == "processed"
                for run in dev_runs) and audit_review_resume(dev_runs[-1])["mechanical_pass"],
            "CALIBRI development evidence incomplete")
    repair_config = Config.load(repair / "run_config.json")
    repair_ids = {item["item_id"] for item in verify_repair(repair, repair_config)}
    repair_completion = read_json(repair / "completion.json")
    require(repair_completion["status"] == "paused" and len(repair_ids) == 35
            and repair_ids <= continued_ids, "CALIBRI repair status/cohort changed")

    t_items = read_json(t_source / "items.json")
    t_manifest = read_json(t_source / "t2ance-manifest.json")
    require(len(t_items) == 60 and digest(t_items) == t_manifest["items_sha256"],
            "t2ance source changed")
    t_cpu, t_completion = verify_execution(t_execution, t_execution / "input-manifest.json")
    require(len(t_cpu) == t_completion["passed"] == 7
            and read_json(t_execution / "offline-audit.json")["mechanical_pass"] is True,
            "t2ance CPU sample changed")
    t_config = Config.load(t_run / "config.json")
    t_canary = verify_t2ance_prepared(t_run, t_config)
    require(read_json(t_run / "offline-audit.json") == audit_t2ance(t_run)
            and len(t_canary) == 5, "t2ance DAG audit changed")

    cal_by_question = {item["question_id"]: item for item in cal_items}
    excluded_by_question = {row["question_id"]: row for row in cal_selection["excluded"]}
    t_by_question = {item["question_id"]: item for item in t_items}
    require(len(cal_by_question) == 93 and len(excluded_by_question) == 82
            and len(t_by_question) == 60
            and set(cal_by_question).isdisjoint(excluded_by_question)
            and set(t_by_question) <= set(excluded_by_question)
            and set(cal_by_question) | set(excluded_by_question) == question_ids,
            "source strata overlap or miss a question")

    rows, accepted = [], []
    for question_id in sorted(question_ids):
        if question_id in cal_by_question:
            item = cal_by_question[question_id]
            item_id = item["item_id"]
            cpu_result = cal_cpu[item_id][1]
            if cpu_result["status"] != "passed":
                status, reason, event = "cpu_limit_failed", "independent CPU limit exceeded", None
            else:
                runs = dev_runs if item_id in dev_ids else [continuation]
                events = [_event(run, item_id) for run in runs]
                require(any(events), "CPU-passed CALIBRI item has no audited outcome")
                event = next(value for value in reversed(events) if value is not None)
                status, reason = event["result"]["status"], event["result"]["reason"]
                if item_id in repair_ids:
                    require(status == "needs_review", "repair selected a nonfailure")
                    status, reason = "repair_pending_audit", "paused first-pass repair not released"
                    event = None
            if event is not None and event["dag"] is not None:
                evidence_result = dev_cpu[item_id][1] if item_id in dev_ids else cpu_result
                accepted.append(_record(item, event, cpu_result, "CALIBRI", evidence_result))
            row = {"question_id": question_id, "item_id": item_id,
                   "source_tier": "CALIBRI", "cpu_status": cpu_result["status"],
                   "status": status, "reason": reason,
                   "repair_selected": item_id in repair_ids,
                   "raw_repair_status": (read_json(repair / "items" / item_id / "result.json")["status"]
                                         if (repair / "items" / item_id / "result.json").exists()
                                         else "paused_without_terminal_result")
                   if item_id in repair_ids else None}
        else:
            if question_id not in t_by_question:
                excluded = excluded_by_question[question_id]
                row = {"question_id": question_id, "item_id": excluded["item_id"],
                       "source_tier": "none", "cpu_status": "not_submitted",
                       "status": "no_qualified_source", "reason": "no CALIBRI or t2ance candidate",
                       "repair_selected": False, "raw_repair_status": None}
            else:
                item = t_by_question[question_id]
                item_id = item["item_id"]
                if item_id not in t_cpu:
                    status, reason, event = "held_without_cpu", "source candidate not independently CPU tested", None
                    cpu_status = "not_submitted"
                else:
                    cpu_result = t_cpu[item_id][1]
                    require(cpu_result["status"] == "passed", "t2ance sampled CPU failure changed")
                    cpu_status = "passed"
                    if item_id not in {entry["item_id"] for entry in t_canary}:
                        status, reason, event = "dag_not_run", "CPU passed; not selected for DAG canary", None
                    else:
                        event = _event(t_run, item_id)
                        require(event is not None, "t2ance selected DAG result missing")
                        status, reason = event["result"]["status"], event["result"]["reason"]
                if event is not None and event["dag"] is not None:
                    accepted.append(_record(item, event, cpu_result, "t2ance", cpu_result))
                row = {"question_id": question_id, "item_id": item_id,
                       "source_tier": "t2ance", "cpu_status": cpu_status,
                       "status": status, "reason": reason,
                       "repair_selected": False, "raw_repair_status": None}
        rows.append(row)
    accepted.sort(key=lambda row: row["question_id"])
    rows.sort(key=lambda row: row["question_id"])
    require(len(rows) == len({row["question_id"] for row in rows}) == 175
            and len({row["item_id"] for row in rows}) == 175
            and len(accepted) == sum(row["status"] == "model_accepted" for row in rows),
            "175-question flow or accepted count mismatch")
    accepted_bytes = _jsonl(accepted)
    accepted_hash = hashlib.sha256(accepted_bytes).hexdigest()
    for record in accepted:
        convert_record(record, "livecodebench_v6", accepted_hash)
    flow_bytes = _jsonl(rows)
    counts = dict(Counter(row["status"] for row in rows))
    manifest = {"schema_version": PROTOCOL, "release_status": "interim_paused_repair",
                "original_questions": 175, "calibri_source": 93, "calibri_cpu_passed": 91,
                "t2ance_source": 60, "t2ance_cpu_sampled": 7,
                "model_accepted": len(accepted), "accepted_by_source": dict(Counter(
                    row["source_status"] for row in accepted)),
                "human_approved": 0, "formal_eligible": False, "counts": counts,
                "calibri_source_sha256": digest(cal_manifest),
                "calibri_cpu_completion_sha256": digest(cal_completion),
                "calibri_development_cpu_completion_sha256": digest(dev_completion),
                "calibri_continuation_audit_sha256": digest(read_json(
                    continuation / "offline-continuation-audit.json")),
                "repair_completion_sha256": digest(repair_completion),
                "t2ance_source_sha256": digest(t_manifest),
                "t2ance_cpu_completion_sha256": digest(t_completion),
                "t2ance_dag_audit_sha256": digest(read_json(t_run / "offline-audit.json")),
                "flow_sha256": hashlib.sha256(flow_bytes).hexdigest(),
                "accepted_sha256": accepted_hash}
    write_bytes_once(output / "flow_175.jsonl", flow_bytes)
    write_bytes_once(output / "accepted_candidates.jsonl", accepted_bytes)
    unified = convert_file(output / "accepted_candidates.jsonl", "livecodebench_v6",
                           accepted_hash, output / "unified")
    manifest["unified_manifest_sha256"] = digest(unified)
    write_once(output / "manifest.json", manifest)
    write_bytes_once(output / "flow_175.html", _html(rows, counts))
    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.base, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
