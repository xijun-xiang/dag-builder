"""Versioned 175-question flow after all 60 t2ance CPU candidates are tested.

This extends the audited split-repair release without changing any DAG verdict.
Only the 53 previously untested t2ance CPU statuses may change. It is still a
model-candidate inventory, not a formal or human-approved DAG dataset.
"""

import argparse
import hashlib
import html
import json
from collections import Counter
from pathlib import Path

from .livecodebench_dag import verify_execution
from .livecodebench_repair_continuation_export import _read_jsonl
from .schemas import require
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .t2ance_source import canary_seven, remaining_cpu_batch

PROTOCOL = "lcb-v6-split-repair-cpu60-flow-v1"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def merge_cpu_rows(rows, cpu):
    """Change only previously held t2ance rows, retaining all DAG outcomes."""
    updated = []
    seen = set()
    for row in rows:
        item_id = row["item_id"]
        if item_id not in cpu:
            updated.append(row)
            continue
        require(row["source_tier"] == "t2ance" and row["status"] == "held_without_cpu"
                and row["cpu_status"] == "not_submitted" and item_id not in seen,
                "CPU extension would change a non-held or duplicate row")
        seen.add(item_id)
        original, result = cpu[item_id]
        require(original["item_id"] == item_id, "CPU result item changed")
        status = result["status"]
        updated.append({**row, "cpu_status": status,
                        "status": "dag_not_run" if status == "passed" else "cpu_not_passed",
                        "reason": ("independent CPU passed; DAG not run"
                                   if status == "passed" else
                                   "independent CPU did not pass all frozen tests")})
    require(seen == set(cpu), "CPU extension misses a held question")
    return updated


def _html(rows, report):
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(row[key])) + "</td>"
                        for key in ("question_id", "source_tier", "cpu_status", "status", "reason"))
                   + "</tr>" for row in rows)
    return ("<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">"
            "<title>LiveCodeBench v6：CPU60 流转</title><style>body{font:15px system-ui;"
            "max-width:1400px;margin:2rem auto;color:#172238}table{border-collapse:collapse;"
            "width:100%}td,th{border:1px solid #ccd3dc;padding:.5rem;text-align:left}"
            "tr:nth-child(even){background:#f5f7fb}</style>"
            "<h1>LiveCodeBench v6：175 题流转（t2ance CPU60）</h1>"
            "<p>CPU 测试已覆盖 60 份 t2ance 参考代码；DAG 模型审核数未因此增加。"
            "模型候选不是官方或人工 gold，不能据此声称达到正式数据门槛。</p>"
            "<p><a href=\"flow_175.jsonl\">逐题流转</a> · "
            "<a href=\"accepted_candidates.jsonl\">原接受候选</a> · "
            "<a href=\"unified/pals_dag_unified_v1.html\">DAG 查看器</a></p>"
            "<pre>" + html.escape(json.dumps(report["counts"], ensure_ascii=False, indent=2))
            + "</pre><table><tr><th>题号</th><th>来源</th><th>CPU</th><th>DAG 状态</th>"
            "<th>原因</th></tr>" + body + "</table></html>").encode("utf-8")


def export(base, split_release, output):
    from scripts.audit_t2ance_full_execution import audit as audit_cpu

    base, split_release, output = map(lambda value: Path(value).resolve(),
                                      (base, split_release, output))
    require(output.is_relative_to(base / "releases") and output != split_release
            and not output.is_relative_to(split_release), "output must be a new release")
    manifest = read_json(split_release / "manifest.json")
    require(manifest["schema_version"] ==
            "lcb-v6-source-stratified-candidates-split-repair-v1"
            and manifest["original_questions"] == 175
            and manifest["t2ance_cpu_sampled"] == 7,
            "not the expected audited split-repair baseline")
    require(_sha(split_release / "flow_175.jsonl") == manifest["flow_sha256"]
            and _sha(split_release / "accepted_candidates.jsonl") == manifest["accepted_sha256"],
            "split-repair baseline changed")
    rows = _read_jsonl(split_release / "flow_175.jsonl")
    accepted = (split_release / "accepted_candidates.jsonl").read_bytes()
    source = base / "t2ance-source-v1/selected"
    items = read_json(source / "items.json")
    source_manifest = read_json(source / "t2ance-manifest.json")
    require(len(items) == 60 and digest(items) == source_manifest["items_sha256"],
            "t2ance source cohort changed")
    raw_source = base / "calibri-full-execution93-v2/source/test6.jsonl"
    old = base / "t2ance-cpu-canary7-b1-111733-v1/execution"
    old_cpu, old_completion = verify_execution(old, old / "input-manifest.json")
    old_audit = read_json(old / "offline-audit.json")
    require(set(old_cpu) == {item["item_id"] for item in canary_seven(items)}
            and old_completion["passed"] == 7
            and old_audit["mechanical_pass"] is True
            and old_audit["tests"] == 297,
            "old t2ance canary CPU proof changed")
    new_root = base / "t2ance-cpu-remaining53-b1-112053-112054-v1/evidence"
    package = base / "t2ance-cpu-remaining53-package-v1"
    fresh, audits = {}, []
    for index in range(4):
        execution = new_root / f"batch-{index}"
        expected_audit = audit_cpu(execution, source, raw_source, package)
        require(read_json(execution / "offline-audit.json") == expected_audit
                and expected_audit["mechanical_pass"] is True,
                "new CPU batch raw audit missing or stale")
        cpu, completion = verify_execution(execution, execution / "input-manifest.json")
        selected = remaining_cpu_batch(items, index)
        require(list(cpu) == [item["item_id"] for item in selected]
                and completion["executed"] == len(selected)
                and not set(cpu).intersection(fresh), "CPU batch partition changed")
        fresh.update(cpu)
        audits.append({"batch": index, "job_id": completion["job_id"],
                       "passed": completion["passed"], "tests": expected_audit["tests"],
                       "audit_sha256": digest(expected_audit),
                       "completion_sha256": digest(completion)})
    require(len(fresh) == 53 and set(fresh).isdisjoint(old_cpu)
            and set(fresh) | set(old_cpu) == {item["item_id"] for item in items},
            "60 CPU candidates are not a complete disjoint partition")
    by_id = {item["item_id"]: item for item in items}
    require(all(original["code"] == by_id[item_id]["reference_code"]
                and original["tests_sha256"] == by_id[item_id]["tests_sha256"]
                for item_id, (original, _) in fresh.items()),
            "fresh CPU result differs from frozen source")
    updated = merge_cpu_rows(rows, fresh)
    require(len(updated) == len({row["question_id"] for row in updated}) == 175
            and sum(row["status"] == "model_accepted" for row in updated)
                == manifest["model_accepted"]
            and all(row["status"] != "held_without_cpu" for row in updated),
            "175-question flow or accepted DAG count changed")
    flow = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                              allow_nan=False) + "\n" for row in updated).encode()
    report = {"schema_version": PROTOCOL,
              "release_status": "audited_model_candidates_not_human_gold",
              "original_questions": 175, "t2ance_source": 60,
              "t2ance_cpu_tested": 60,
              "t2ance_cpu_passed": sum(result["status"] == "passed"
                                        for _, result in (*old_cpu.values(), *fresh.values())),
              "t2ance_frozen_tests": old_audit["tests"] + sum(audit["tests"] for audit in audits),
              "model_accepted": manifest["model_accepted"],
              "human_approved": 0, "formal_eligible": False,
              "counts": dict(Counter(row["status"] for row in updated)),
              "baseline_manifest_sha256": digest(manifest),
              "baseline_flow_sha256": manifest["flow_sha256"],
              "flow_sha256": hashlib.sha256(flow).hexdigest(),
              "accepted_sha256": hashlib.sha256(accepted).hexdigest(),
              "cpu_batches": audits,
              "claim": "CPU-tested reference code; unchanged model-reviewed DAG candidates, not semantic gold"}
    output = private_dir(output)
    require(not any(path.name != ".lock" for path in output.iterdir()),
            "CPU flow output must be empty")
    write_bytes_once(output / "flow_175.jsonl", flow)
    write_bytes_once(output / "accepted_candidates.jsonl", accepted)
    for name in ("manifest.json", "pals_dag_unified_v1.jsonl", "pals_dag_unified_v1.html"):
        write_bytes_once(output / "unified" / name,
                         (split_release / "unified" / name).read_bytes())
    write_bytes_once(output / "flow_175.html", _html(updated, report))
    write_once(output / "manifest.json", report)
    return report


def main():
    import os
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "split-release", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.base, args.split_release, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
