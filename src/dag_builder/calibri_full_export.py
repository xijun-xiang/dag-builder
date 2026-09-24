"""Reviewable LiveCodeBench v6 CALIBRI candidate export with all 175 outcomes."""

import json
import hashlib
from collections import Counter
from pathlib import Path

from .calibri_continuation import audit_completed as audit_continuation
from .calibri_normalize import validate_audit
from .calibri_repair import CHECKED_PROTOCOL, validate_repair_audit
from .calibri_review_resume import audit_completed as audit_review_resume
from .livecodebench_dag import verify_execution
from .schemas import require
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .unified import convert_file, convert_record

PROTOCOL = "calibri-lcb-v6-model-candidates-v1"


def _event(root, item_id):
    directory = Path(root) / "items" / item_id
    if not (directory / "result.json").exists():
        return None
    result = read_json(directory / "result.json")
    require(result["item_id"] == item_id
            and result["status"] in ("model_accepted", "rejected", "needs_review"),
            "invalid terminal result")
    dag = None
    if result["status"] == "model_accepted":
        dag = read_json(directory / "dag.json")
        require(result["dag_sha256"] == digest(dag) and dag["item_id"] == item_id
                and dag["formal_eligible"] is False,
                "candidate DAG identity or release gate mismatch")
        if "repair_record" in dag:
            validate_repair_audit(dag["dag_review"], dag["construction_protocol"])
        else:
            validate_audit(dag["dag_review"])
        require(dag["dag_review"]["decision"] == "accept", "unaccepted candidate")
    else:
        require(not (directory / "dag.json").exists(), "nonaccepting run published a DAG")
    return {"run": Path(root).name, "result": result, "result_sha256": digest(result),
            "dag": dag}


def _html(rows, manifest):
    data = json.dumps({"rows": rows, "manifest": manifest}, ensure_ascii=False, allow_nan=False)
    data = data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return ("""<!doctype html><html lang="zh"><meta charset="utf-8"><title>LiveCodeBench v6 DAG 审核</title>
<style>body{font:15px system-ui;margin:24px auto;max-width:1280px;background:#f5f7fb;color:#172238}header,article{background:white;padding:20px;margin:14px;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}input,select{padding:8px;margin:6px}summary{cursor:pointer;font-weight:600}</style>
<header><h1>LiveCodeBench v6：CALIBRI 派生 DAG</h1><p>175题流转；模型审核候选不等于官方或人工gold。输入候选91题，接受子集由下方清单给出。</p><p><a href="unified/pals_dag_unified_v1.jsonl">PALS统一实验输入</a> · <a href="unified/pals_dag_unified_v1.html">统一查看器</a> · <a href="accepted_candidates.jsonl">原始接受候选</a> · <a href="flow_175.jsonl">175题流转</a> · <a href="manifest.json">清单</a></p><pre id="summary"></pre><input id="query" placeholder="题号或ID"><select id="status"><option value="">全部状态</option></select><span id="count"></span></header><main id="list"></main>
<script id="data" type="application/json">""" + data + """</script><script>
const src=JSON.parse(document.getElementById('data').textContent),rows=src.rows;
const el=(tag,value)=>{const x=document.createElement(tag);x.textContent=String(value??'');return x};
document.getElementById('summary').textContent=JSON.stringify(src.manifest.counts,null,2);
const query=document.getElementById('query'),status=document.getElementById('status');
[...new Set(rows.map(r=>r.status))].sort().forEach(s=>{const o=el('option',s);o.value=s;status.append(o)});
function draw(){const list=document.getElementById('list');list.replaceChildren();const shown=rows.filter(r=>(!status.value||r.status===status.value)&&[r.question_id,r.item_id].join(' ').toLowerCase().includes(query.value.toLowerCase()));document.getElementById('count').textContent=shown.length+' / '+rows.length;
for(const r of shown){const box=el('article',''),details=el('details','');details.append(el('summary',r.question_id+' · '+r.status));details.append(el('p',r.reason));
if(r.source){details.append(el('h3','题目'));details.append(el('pre',r.source.question));details.append(el('h3','CALIBRI 原始解释'));details.append(el('pre',r.source.raw_output));}
const dag=r.dag;if(dag){details.append(el('h3','审核通过的候选图'));const table=el('table',''),head=el('tr','');['节点','类型','断言','父节点','理由','来源引文'].forEach(x=>head.append(el('th',x)));table.append(head);for(const n of dag.nodes){const tr=el('tr','');[n.node_id,n.kind,n.statement,(n.parents||[]).join(', '),n.justification,n.source_quote].forEach(x=>tr.append(el('td',x)));table.append(tr)}details.append(table);details.append(el('h3','测试通过的参考代码'));details.append(el('pre',dag.source.reference_code));}
const history=el('details','');history.append(el('summary','处理历史与出处'));history.append(el('pre',JSON.stringify({events:r.history,execution:r.cpu_status,source_origin:r.source?.origin},null,2)));details.append(history);box.append(details);list.append(box)}}status.onchange=draw;query.oninput=draw;draw();</script></html>""")


def export(source, execution, continuation, repair, development_runs, output):
    source, execution, continuation, repair = map(lambda p: Path(p).resolve(),
                                                   (source, execution, continuation, repair))
    development_runs = [Path(p).resolve() for p in development_runs]
    output = private_dir(output)
    require(len(development_runs) == 4, "four ordered development runs required")
    source_items = read_json(source / "items.json")
    source_selection = read_json(source / "selection.json")
    index = read_json(source / "candidate-index.json")
    manifest = read_json(source / "calibri-manifest.json")
    require(manifest["protocol"] == "calibri-lcb-source-full-v1"
            and digest(source_items) == manifest["items_sha256"]
            and digest(source_selection) == manifest["selection_sha256"]
            and digest(index) == manifest["index_sha256"], "source freeze changed")
    indexed = {row["question_id"] for row in index}
    candidates = {item["item_id"]: item for item in source_items}
    excluded = {row["item_id"]: row for row in source_selection["excluded"]}
    require(len(indexed) == 175 and len(candidates) == 93 and len(excluded) == 82
            and len({item["question_id"] for item in source_items}
                    | {row["question_id"] for row in excluded.values()}) == 175,
            "175-question source flow changed")
    cpu, cpu_completion = verify_execution(execution, execution / "input-manifest.json")
    require(read_json(execution / "input-manifest.json")["calibri_manifest_sha256"] == digest(manifest)
            and read_json(execution / "offline-audit.json")["mechanical_pass"] is True,
            "CPU execution/source offline proof missing")
    require(set(cpu) == set(candidates) and cpu_completion["passed"] == 91,
            "CPU validation cohort changed")
    require(all(cpu[item_id][0]["code"] == item["reference_code"]
                for item_id, item in candidates.items()),
            "CPU-executed code differs from frozen CALIBRI reference")
    require(read_json(continuation / "offline-continuation-audit.json") == audit_continuation(continuation),
            "continuation audit changed")
    continued = {i["item_id"]: i for i in read_json(continuation / "items.json")}
    require(len(continued) == 87 and set(continued) <= set(candidates),
            "new-question continuation mismatch")
    dev_ids = set(candidates) - set(continued) - {key for key, (_, row) in cpu.items()
                                                  if row["status"] != "passed"}
    require(len(dev_ids) == 4, "development cohort mismatch")
    require(all(read_json(run / "completion.json")["status"] == "processed"
                for run in development_runs + [repair]), "an input run is unfinished")
    require(audit_review_resume(development_runs[-1])["mechanical_pass"],
            "development fixed-graph review audit failed")
    from .calibri_repair import verify_repair
    from .config import Config
    repair_items = {i["item_id"]: i for i in verify_repair(repair, Config.load(repair / "run_config.json"))}
    require(set(repair_items) <= set(continued), "repair includes excluded/development item")
    repair_report = read_json(repair / "offline-audit.json")
    require(repair_report["mechanical_pass"]
            and repair_report["completion_sha256"] == digest(read_json(repair / "completion.json")),
            "repair offline audit missing or stale")
    rows, accepted = [], []
    for item in source_items:
        item_id = item["item_id"]
        cpu_status = cpu[item_id][1]["status"]
        history = []
        if cpu_status == "passed":
            runs = development_runs if item_id in dev_ids else [continuation, repair]
            for run in runs:
                event = _event(run, item_id)
                if event:
                    history.append({"run": event["run"], "result": event["result"],
                                    "result_sha256": event["result_sha256"]})
            require(bool(history), "CPU-passed item has no DAG outcome")
            latest = next(event for run in reversed(runs)
                          if (event := _event(run, item_id)) is not None)
            status, reason, dag = latest["result"]["status"], latest["result"]["reason"], latest["dag"]
            if dag:
                require(item["item_id"] == dag["item_id"]
                        and all(dag["source"].get(k) == item.get(k) for k in
                                ("question_id", "question", "raw_output", "reference_code"))
                        and dag["source"].get("execution_evidence", {}).get("status") == "passed",
                        "accepted DAG source changed")
                accepted.append({"schema_version": PROTOCOL, "item_id": item_id,
                                 "question_id": item["question_id"], "source": dag["source"],
                                 "dag": dag, "dag_sha256": digest(dag),
                                 "cpu_result_sha256": dag["source"]["execution_evidence"]["result_sha256"],
                                 "full_cpu_result_sha256": digest(cpu[item_id][1]),
                                 "model_accepted": True, "human_approved": False,
                                 "formal_eligible": False,
                                 "source_status": "calibri_derived_tested_reference"})
        else:
            status, reason, dag = "cpu_limit_failed", "fixed CPU limit exceeded", None
        rows.append({"schema_version": PROTOCOL, "item_id": item_id,
                     "question_id": item["question_id"], "status": status,
                     "reason": reason, "cpu_status": cpu_status,
                     "source_status": "calibri_candidate", "source": item,
                     "history": history, "dag": dag,
                     "model_accepted": dag is not None, "human_approved": False,
                     "formal_eligible": False})
    for item_id, row in excluded.items():
        rows.append({"schema_version": PROTOCOL, "item_id": item_id,
                     "question_id": row["question_id"], "status": "no_calibri_source",
                     "reason": row["reason"], "cpu_status": "not_submitted",
                     "source_status": "excluded_before_CPU", "source": None,
                     "history": [], "dag": None, "model_accepted": False,
                     "human_approved": False, "formal_eligible": False})
    rows.sort(key=lambda r: r["question_id"])
    accepted.sort(key=lambda r: r["question_id"])
    require(len(rows) == 175 and len({r["item_id"] for r in rows}) == 175
            and {r["question_id"] for r in rows} == indexed
            and sum(r["cpu_status"] == "passed" for r in rows) == 91
            and len(accepted) == sum(r["model_accepted"] for r in rows),
            "full flow or accepted subset mismatch")
    require(bool(accepted), "no accepted DAGs to release")
    accepted_bytes = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                             for row in accepted).encode("utf-8")
    accepted_hash = hashlib.sha256(accepted_bytes).hexdigest()
    # Validate every row against the common PALS contract before publishing files.
    for row in accepted:
        convert_record(row, "livecodebench_v6", accepted_hash)
    counts = dict(Counter(row["status"] for row in rows))
    output_manifest = {"schema_version": PROTOCOL, "total_questions": 175,
                       "calibri_source_candidates": 93, "cpu_passed": 91,
                       "cpu_limit_failed": 2, "no_calibri_source": 82,
                       "model_accepted": len(accepted), "human_approved": 0,
                       "formal_eligible": False, "counts": counts,
                       "source_manifest_sha256": digest(manifest),
                       "cpu_completion_sha256": digest(cpu_completion),
                       "continuation_audit_sha256": digest(read_json(
                           continuation / "offline-continuation-audit.json")),
                       "repair_audit_sha256": digest(repair_report),
                       "flow_sha256": digest(rows), "accepted_sha256": digest(accepted),
                       "quality": "CALIBRI-derived, frozen reference code CPU-tested, same-model DAG audited; not official or human gold"}
    write_bytes_once(output / "flow_175.jsonl", "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for row in rows).encode("utf-8"))
    write_bytes_once(output / "accepted_candidates.jsonl", accepted_bytes)
    unified_manifest = convert_file(output / "accepted_candidates.jsonl", "livecodebench_v6",
                                    accepted_hash, output / "unified")
    output_manifest["unified_manifest_sha256"] = digest(unified_manifest)
    write_once(output / "manifest.json", output_manifest)
    write_bytes_once(output / "dag_viewer.html", _html(rows, output_manifest).encode())
    return output_manifest


def main():
    import argparse
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "execution", "continuation", "repair", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--development-run", type=Path, action="append", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.output):
        print(json.dumps(export(args.source, args.execution, args.continuation,
                                args.repair, args.development_run, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
