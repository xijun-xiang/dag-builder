"""Prepare one failure-directed follow-up for the 16 still-unreviewed v3 graphs.

This is a development salvage pass, not an independent validation sample.
Semantic rejects, quarantines and the 11 answer-backward cases are excluded.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from .livecodebench_dag_revision import PROTOCOL_V4, _compact_prior
from .livecodebench_repair_continuation_export import _read_jsonl
from .schemas import require
from .storage import digest, private_dir, read_json, write_once

PREVIOUS_SCHEMA = "lcb-v6-dag-revision-answerbackward-candidates-v1"
RUN_NAMES = ("lcb-dag-revision58-canary8-v4", "lcb-dag-revision58-rest50-v3")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _prior_audits(base):
    from .livecodebench_dag_revision_audit import audit
    result = {}
    for name in RUN_NAMES:
        run = base / name
        record = audit(run)
        require(read_json(run / "offline-audit.json") == record
                and record["protocol"] == "lcb-dag-revision-v3",
                "prior revision lacks a matching offline audit")
        result[name] = digest(record)
    return result


def _candidate(run, item_id):
    directory = run / "items" / item_id
    normalized_path = directory / "normalization.json"
    review_path = directory / "review_dag/input.json"
    normalized = read_json(normalized_path) if normalized_path.is_file() else None
    graph = read_json(review_path)["input"]["candidate"] if review_path.is_file() else None
    return _compact_prior(normalized, graph)


def prepare(base, previous_release, root):
    base, previous_release, root = map(lambda p: Path(p).resolve(),
                                        (base, previous_release, root))
    require(root.parent == base and previous_release.parent == base / "releases",
            "follow-up must use a new private batch inside this campaign")
    previous_manifest = read_json(previous_release / "manifest.json")
    rows = _read_jsonl(previous_release / "flow_175.jsonl")
    require(previous_manifest["schema_version"] == PREVIOUS_SCHEMA
            and previous_manifest["flow_sha256"] == _sha(previous_release / "flow_175.jsonl")
            and len(rows) == 175, "frozen 175-question release changed")
    selected = [row for row in rows if row["status"] == "needs_review"
                and "answer_backward_run" not in row]
    require(len(selected) == 16 and all(row["revision_run"] in RUN_NAMES
                                        for row in selected),
            "expected 16 fixed non-pruning follow-up cases")
    selected.sort(key=lambda row: str(row["question_id"]))
    prior_audits = _prior_audits(base)
    items, feedback, origins = [], {}, {}
    for row in selected:
        item_id = row["item_id"]
        name = row["revision_run"]
        run = base / name
        item = next((value for value in read_json(run / "items.json")
                     if value["item_id"] == item_id), None)
        require(item is not None and item["question_id"] == row["question_id"]
                and item["execution_evidence"]["status"] == row["cpu_status"] == "passed",
                "prior item or CPU evidence changed")
        result = read_json(run / "items" / item_id / "result.json")
        require(result["status"] == "needs_review"
                and row["revision_result_sha256"] == digest(result)
                and result["reason"] == row["reason"],
                "prior failure reason changed")
        normalized, graph = _candidate(run, item_id)
        note = {"source_tier": row["source_tier"],
                "prior_status": result["status"],
                "prior_stage": result["stage"],
                "prior_reason": result["reason"],
                "prior_normalization": normalized,
                "prior_candidate": graph,
                "prior_graph_may_precede_latest_feedback": False,
                "revision_scope": "one source-bound correction of this concrete failure; no forced acceptance"}
        items.append(item)
        feedback[item_id] = note
        origins[item_id] = {"source_run": name, "source_item_sha256": digest(item),
                            "cpu_result_sha256": item["execution_evidence"]["result_sha256"],
                            "prior_result_sha256": digest(result),
                            "prior_audit_sha256": prior_audits[name],
                            "feedback_sha256": digest(note)}
    root = private_dir(root)
    require(not any(root.iterdir()), "follow-up output must be a new empty directory")
    selection = {"protocol": PROTOCOL_V4,
                 "selected_ids": [item["item_id"] for item in items],
                 "selected_question_ids": [str(item["question_id"]) for item in items],
                 "baseline_flow_sha256": previous_manifest["flow_sha256"],
                 "previous_manifest_sha256": digest(previous_manifest),
                 "prior_audit_sha256": prior_audits,
                 "no_score_selection": True,
                 "maximum_semantic_revisions": 1,
                 "cohort": "16 remaining non-pruning needs_review cases only"}
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    for item_id, note in feedback.items():
        write_once(root / "feedback" / (item_id + ".json"), note)
    write_once(root / "revision-manifest.json", {
        "protocol": PROTOCOL_V4, "baseline": str(previous_release),
        "selected": len(items), "items_sha256": digest(items),
        "selection_sha256": digest(selection), "origins": origins,
        "human_approved": 0, "formal_eligible": False})
    return {"selected": len(items), "source_tiers": {
        tier: sum(feedback[item["item_id"]]["source_tier"] == tier for item in items)
        for tier in ("CALIBRI", "t2ance")}}


def verify_followup(root, manifest, selection, items):
    root = Path(root)
    base = root.parent
    previous_release = Path(manifest["baseline"])
    previous_manifest = read_json(previous_release / "manifest.json")
    rows = _read_jsonl(previous_release / "flow_175.jsonl")
    expected = [row for row in rows if row["status"] == "needs_review"
                and "answer_backward_run" not in row]
    expected.sort(key=lambda row: str(row["question_id"]))
    require(previous_manifest["schema_version"] == PREVIOUS_SCHEMA
            and previous_manifest["flow_sha256"] == selection["baseline_flow_sha256"] ==
            _sha(previous_release / "flow_175.jsonl")
            and digest(previous_manifest) == selection["previous_manifest_sha256"]
            and len(expected) == len(items) == 16
            and [row["item_id"] for row in expected] == selection["selected_ids"],
            "frozen follow-up cohort changed")
    audited = _prior_audits(base)
    require(audited == selection["prior_audit_sha256"],
            "previous raw revision audits changed")
    for row, item in zip(expected, items):
        item_id = item["item_id"]
        origin = manifest["origins"][item_id]
        run = base / origin["source_run"]
        old_result = read_json(run / "items" / item_id / "result.json")
        note = read_json(root / "feedback" / (item_id + ".json"))
        require(row["revision_run"] == origin["source_run"] in RUN_NAMES
                and row["revision_result_sha256"] == digest(old_result) ==
                origin["prior_result_sha256"]
                and audited[origin["source_run"]] == origin["prior_audit_sha256"]
                and note["prior_reason"] == old_result["reason"] == row["reason"]
                and note["prior_status"] == old_result["status"] == "needs_review"
                and note["prior_stage"] == old_result["stage"]
                and (note["prior_normalization"], note["prior_candidate"]) ==
                _candidate(run, item_id),
                "follow-up feedback no longer matches original failure")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--previous-release", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.base, args.previous_release, args.root),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
