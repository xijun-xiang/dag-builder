"""Versioned 175-question export after an audited partial CALIBRI repair.

The original 35-item repair may be paused, but its terminal items are not
resampled. Only its operationally paused items can come from the completed
transport continuation. The historical interim exporter supplies the other
140 outcomes and the independently audited t2ance stratum unchanged.
"""

import hashlib
import html
import json
from collections import Counter
from pathlib import Path

from .calibri_repair import verify_repair
from .calibri_repair_transport import verify_partial_transport
from .config import Config
from .livecodebench_dag import verify_execution
from .livecodebench_interim_export import _jsonl, _record, export as export_interim
from .calibri_full_export import _event
from .schemas import require
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .unified import convert_file, convert_record

PROTOCOL = "lcb-v6-source-stratified-candidates-split-repair-v1"
OLD_REPAIR = "calibri-full-repair-v2-dns-continuation-v2"
CALIBRI_SOURCE = "calibri-full175-v1"
CALIBRI_CPU = "calibri-full-execution93-v2/b1-results"


def _read_jsonl(path):
    raw = Path(path).read_bytes()
    require(bool(raw) and raw.endswith(b"\n"), "incomplete baseline JSONL")
    lines = raw.decode("utf-8").split("\n")
    require(lines[-1] == "" and all(line.strip() for line in lines[:-1]),
            "blank baseline JSONL row")
    return [json.loads(line) for line in lines[:-1]]


def _assert_old_snapshot(old, continuation):
    """The baseline old run must be byte-identical to the audited snapshot."""
    from .calibri_repair_transport import _source_files

    expected = continuation["source_files"]
    actual = {str(path.relative_to(old)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in _source_files(old)}
    require(actual == expected, "old repair differs from audited frozen snapshot")


def _split_repair_cohort(old_ids, terminal_ids, continued_ids, audited_ids):
    """A full, disjoint partition is mandatory; a pause is never a verdict."""
    old_ids, terminal_ids, continued_ids, audited_ids = (
        list(old_ids), list(terminal_ids), list(continued_ids), list(audited_ids))
    old, terminal, continued, audited = map(set, (
        old_ids, terminal_ids, continued_ids, audited_ids))
    require(len(old_ids) == len(old) == 35
            and len(terminal_ids) == len(terminal) == 11
            and len(continued_ids) == len(continued) == 24
            and len(audited_ids) == len(audited) == 24
            and terminal.isdisjoint(continued) and terminal | continued == old
            and audited == continued,
            "35-item repair split is incomplete, overlapping, or unaudited")
    return terminal, continued


def _merge_repair_rows(baseline_rows, baseline_accepted, repair_ids, events,
                       items, cpu, old_run, new_run):
    """Replace only repair-pending rows, preserving every other row verbatim."""
    rows = [dict(row) for row in baseline_rows]
    accepted = [dict(row, schema_version=PROTOCOL) for row in baseline_accepted]
    by_id = {row["item_id"]: row for row in rows}
    require(len(by_id) == len(rows) == 175, "baseline does not cover 175 unique items")
    require(set(repair_ids) == set(events) and set(repair_ids) <= set(items),
            "repair event cohort differs from frozen source")
    for item_id in repair_ids:
        row = by_id[item_id]
        item = items[item_id]
        require(row["question_id"] == item["question_id"]
                and row["source_tier"] == "CALIBRI"
                and row["cpu_status"] == "passed"
                and row["repair_selected"] is True
                and row["status"] == "repair_pending_audit"
                and item_id not in {value["item_id"] for value in accepted},
                "repair item was not held out in interim baseline")
        origin, event = events[item_id]
        require(origin in (old_run, new_run) and event is not None,
                "repair item has no audited terminal event")
        result = event["result"]
        require(result["item_id"] == item_id
                and result["status"] in ("model_accepted", "needs_review", "rejected"),
                "pause or unknown result cannot be exported")
        row.update(status=result["status"], reason=result["reason"],
                   repair_terminal_run=origin.name,
                   repair_result_sha256=event["result_sha256"],
                   repair_dag_sha256=digest(event["dag"]) if event["dag"] else None)
        if event["dag"] is not None:
            cpu_result = cpu[item_id][1]
            require(cpu_result["status"] == "passed", "accepted code lacks CPU proof")
            record = _record(item, event, cpu_result, "CALIBRI", cpu_result)
            record["schema_version"] = PROTOCOL
            accepted.append(record)
    rows.sort(key=lambda row: row["question_id"])
    accepted.sort(key=lambda row: row["question_id"])
    require(len(rows) == len({row["question_id"] for row in rows}) == 175
            and len(accepted) == sum(row["status"] == "model_accepted" for row in rows)
            and len(accepted) == len({row["item_id"] for row in accepted}),
            "merged 175-question flow or accepted subset mismatch")
    return rows, accepted


def _html(rows, manifest):
    counts = html.escape(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(row[key])) + "</td>"
                                         for key in ("question_id", "source_tier",
                                                     "cpu_status", "status", "reason")) + "</tr>"
                   for row in rows)
    return ("<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">"
            "<title>LiveCodeBench v6：分批回修流转</title><style>body{font:15px system-ui;"
            "max-width:1400px;margin:2rem auto;color:#172238}table{border-collapse:collapse;"
            "width:100%}td,th{border:1px solid #ccd3dc;padding:.5rem;text-align:left;"
            "vertical-align:top}tr:nth-child(even){background:#f5f7fb}</style>"
            "<h1>LiveCodeBench v6：175 题分批回修流转</h1>"
            "<p>35 题旧批次终态与传输中断后续跑终态已独立审计并合并。"
            "本页是模型审核候选，不是官方或人工 gold，也不表示已达论文正式发布门槛。</p>"
            "<p><a href=\"flow_175.jsonl\">逐题流转 JSONL</a> · "
            "<a href=\"accepted_candidates.jsonl\">接受候选</a> · "
            "<a href=\"unified/pals_dag_unified_v1.html\">统一 DAG 查看器</a></p>"
            "<pre>" + counts + "</pre><table><tr><th>题号</th><th>来源</th><th>CPU</th>"
            "<th>DAG 状态</th><th>原因</th></tr>" + body + "</table></html>").encode("utf-8")


def _checked_output_path(base, continuation_run, output):
    """Reject paths that could contaminate frozen evidence before taking a lock."""
    base, continuation_run, output = (Path(base).resolve(), Path(continuation_run).resolve(),
                                      Path(output).resolve())
    require(not base.is_relative_to(output)
            and not continuation_run.is_relative_to(output)
            and not output.is_relative_to(continuation_run),
            "export output overlaps frozen input")
    if output.is_relative_to(base):
        relative = output.relative_to(base).parts
        require(len(relative) >= 2 and relative[0] == "releases",
                "export inside artifact root must be a named releases child")
    return output


def export(base, continuation_run, output):
    """Offline export; refuses incomplete or stale raw audits, never calls APIs."""
    from scripts.audit_calibri_repair import audit as audit_repair

    base, continuation_run = Path(base).resolve(), Path(continuation_run).resolve()
    output = _checked_output_path(base, continuation_run, output)
    output = private_dir(output)
    require(not any(path.name != ".lock" for path in output.iterdir()),
            "export output must be empty")
    old = base / OLD_REPAIR
    config = Config.load(continuation_run / "run_config.json")
    continued_items = verify_partial_transport(continuation_run, config)
    completion = read_json(continuation_run / "completion.json")
    require(completion["status"] == "processed", "continued repair has not completed")
    audit = audit_repair(continuation_run)
    require(audit["mechanical_pass"] is True
            and read_json(continuation_run / "offline-audit.json") == audit,
            "new repair raw audit absent or stale")
    repair_manifest = read_json(continuation_run / "calibri-repair-manifest.json")
    continuation = repair_manifest["partial_transport_continuation"]
    require(Path(continuation["source_run"]).resolve() == old.resolve(),
            "continuation was not prepared from the intended old repair")
    _assert_old_snapshot(old, continuation)
    old_items = verify_repair(old, Config.load(old / "run_config.json"))
    terminal, continued = _split_repair_cohort(
        [item["item_id"] for item in old_items], continuation["terminal_ids"],
        [item["item_id"] for item in continued_items], continuation["paused_selected_ids"])
    transport = audit["transport_continuation"]
    require(transport["historical_terminal_count"] == len(terminal)
            and transport["historical_terminal_replayed_count"] == len(terminal)
            and transport["continued_count"] == len(continued_items)
            and transport["continued_terminal_replayed_count"] == len(continued_items)
            and set(row["item_id"] for row in transport["historical_terminal_rows"]) == terminal,
            "historical terminal raw audit missing")
    frozen_old = continuation_run / "source-run-evidence"
    events = {item_id: (old, _event(frozen_old, item_id)) for item_id in terminal}
    events.update({item_id: (continuation_run, _event(continuation_run, item_id))
                   for item_id in continued})
    require(all(event is not None for _, event in events.values()),
            "repair continuation has a nonterminal item")

    # The old interim export, in a named subdirectory, supplies already audited
    # development, full CPU, t2ance and no-source outcomes without changing its
    # historical logic or reinterpreting its paused repair as processed.
    interim_manifest = export_interim(base, output / "interim-baseline")
    baseline = output / "interim-baseline"
    baseline_rows = _read_jsonl(baseline / "flow_175.jsonl")
    baseline_accepted = _read_jsonl(baseline / "accepted_candidates.jsonl")
    repair_ids = {item["item_id"] for item in old_items}
    require(interim_manifest["repair_completion_sha256"] == digest(read_json(old / "completion.json"))
            and sum(row["status"] == "repair_pending_audit" for row in baseline_rows)
                == len(repair_ids), "interim repair cohort changed")
    source_items = {item["item_id"]: item for item in
                    read_json(base / CALIBRI_SOURCE / "items.json")}
    cpu_root = base / CALIBRI_CPU
    cpu, cpu_completion = verify_execution(cpu_root, cpu_root / "input-manifest.json")
    require(digest(cpu_completion) == interim_manifest["calibri_cpu_completion_sha256"],
            "interim and final CALIBRI CPU evidence differ")
    rows, accepted = _merge_repair_rows(baseline_rows, baseline_accepted, repair_ids,
                                        events, source_items, cpu, old, continuation_run)
    accepted_bytes, flow_bytes = _jsonl(accepted), _jsonl(rows)
    accepted_sha = hashlib.sha256(accepted_bytes).hexdigest()
    for record in accepted:
        convert_record(record, "livecodebench_v6", accepted_sha)
    counts = dict(Counter(row["status"] for row in rows))
    report = {"schema_version": PROTOCOL,
              "release_status": "audited_model_candidates_not_human_gold",
              "original_questions": 175, "calibri_source": 93, "calibri_cpu_passed": 91,
              "t2ance_source": interim_manifest["t2ance_source"],
              "t2ance_cpu_sampled": interim_manifest["t2ance_cpu_sampled"],
              "model_accepted": len(accepted), "accepted_by_source": dict(Counter(
                  record["source_status"] for record in accepted)),
              "human_approved": 0, "formal_eligible": False, "counts": counts,
              "repair_original_count": len(repair_ids),
              "old_repair_terminal_count": len(terminal),
              "new_repair_terminal_count": len(continued),
              "interim_manifest_sha256": digest(interim_manifest),
              "old_repair_completion_sha256": digest(read_json(old / "completion.json")),
              "new_repair_completion_sha256": digest(completion),
              "new_repair_audit_sha256": digest(audit),
              "new_repair_manifest_sha256": digest(repair_manifest),
              "old_repair_snapshot_inventory_sha256": digest(continuation["source_files"]),
              "flow_sha256": hashlib.sha256(flow_bytes).hexdigest(),
              "accepted_sha256": accepted_sha,
              "quality": "Source-derived CPU-tested reference; model-reviewed DAG, not official or human gold"}
    write_bytes_once(output / "flow_175.jsonl", flow_bytes)
    write_bytes_once(output / "accepted_candidates.jsonl", accepted_bytes)
    unified = convert_file(output / "accepted_candidates.jsonl", "livecodebench_v6",
                           accepted_sha, output / "unified")
    report["unified_manifest_sha256"] = digest(unified)
    write_once(output / "manifest.json", report)
    write_bytes_once(output / "flow_175.html", _html(rows, report))
    return report


def main():
    import argparse
    import os
    from .storage import run_lock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--continuation-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    checked_output = _checked_output_path(args.base, args.continuation_run, args.output)
    with run_lock(checked_output):
        print(json.dumps(export(args.base, args.continuation_run, checked_output),
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
