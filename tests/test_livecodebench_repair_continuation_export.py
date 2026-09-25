"""Synthetic split-repair export guards; no private benchmark rows or API."""

import unittest
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from dag_builder.livecodebench_repair_continuation_export import (
    PROTOCOL, _html, _merge_repair_rows, _read_jsonl, _split_repair_cohort,
    export,
)
from dag_builder.schemas import InvalidOutput
from dag_builder.storage import digest, private_dir, read_json, write_bytes_once, write_once
from dag_builder.unified import convert_record
from test_unified import livecodebench


def fixture():
    original = livecodebench()
    cpu_result = {"status": "passed", "job": "synthetic"}
    original["source"]["execution_evidence"]["result_sha256"] = digest(cpu_result)
    original["dag"]["source"] = original["source"]
    original["dag_sha256"] = digest(original["dag"])
    items = {}
    rows = []
    accepted_dag = None
    for index in range(175):
        item_id = f"item-{index:03d}"
        question_id = f"question-{index:03d}"
        if index == 0:
            item = {**original["source"], "item_id": item_id, "question_id": question_id}
            dag = {**original["dag"], "item_id": item_id, "source": item}
            accepted_dag = dag
        else:
            item = {"item_id": item_id, "question_id": question_id}
            dag = None
        items[item_id] = item
        rows.append({"item_id": item_id, "question_id": question_id,
                     "source_tier": "CALIBRI" if index < 35 else "none",
                     "cpu_status": "passed" if index < 35 else "not_submitted",
                     "repair_selected": index < 35,
                     "status": "repair_pending_audit" if index < 35 else "no_qualified_source",
                     "reason": "pending"})
    old = Path("/synthetic/old")
    new = Path("/synthetic/new")
    events = {}
    for index in range(35):
        item_id = f"item-{index:03d}"
        status = "model_accepted" if index == 0 else (
            "rejected" if index % 2 else "needs_review")
        result = {"item_id": item_id, "status": status,
                  "reason": "synthetic verdict"}
        events[item_id] = (old if index < 11 else new,
                           {"result": result, "result_sha256": digest(result),
                            "dag": accepted_dag if index == 0 else None})
    cpu = {"item-000": ({"code": original["source"]["reference_code"]}, cpu_result)}
    return rows, items, events, cpu, old, new


class SplitRepairExportTests(unittest.TestCase):
    def test_split_requires_all_35_disjoint_and_audited(self):
        old = [f"item-{index:03d}" for index in range(35)]
        terminal, continued = old[:11], old[11:]
        self.assertEqual(_split_repair_cohort(old, terminal, continued, continued),
                         (set(terminal), set(continued)))
        for changed in (continued[:-1], continued + terminal[:1], continued + continued[:1]):
            with self.assertRaises(InvalidOutput):
                _split_repair_cohort(old, terminal, changed, changed)
        with self.assertRaises(InvalidOutput):
            _split_repair_cohort(old, terminal, continued, continued[:-1])

    def test_merge_keeps_175_and_only_releases_terminal_accept(self):
        rows, items, events, cpu, old, new = fixture()
        merged, accepted = _merge_repair_rows(rows, [], set(events), events,
                                               items, cpu, old, new)
        self.assertEqual(len(merged), 175)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["schema_version"], PROTOCOL)
        self.assertFalse(accepted[0]["human_approved"])
        self.assertFalse(accepted[0]["formal_eligible"])
        self.assertEqual(convert_record(accepted[0], "livecodebench_v6", "a" * 64)[
            "review"]["source_status"], "calibri_derived_tested_reference")
        self.assertEqual(merged[0]["repair_terminal_run"], "old")
        self.assertEqual(merged[11]["repair_terminal_run"], "new")
        self.assertEqual(merged[40], rows[40])
        self.assertEqual(sum(row["status"] == "repair_pending_audit" for row in merged), 0)

    def test_paused_result_or_preaccepted_baseline_fails_closed(self):
        rows, items, events, cpu, old, new = fixture()
        wrong = deepcopy(events)
        wrong["item-000"][1]["result"]["status"] = "paused"
        with self.assertRaises(InvalidOutput):
            _merge_repair_rows(rows, [], set(events), wrong, items, cpu, old, new)
        rows[0]["status"] = "model_accepted"
        with self.assertRaises(InvalidOutput):
            _merge_repair_rows(rows, [], set(events), events, items, cpu, old, new)

    def test_new_version_keeps_t2ance_explicit_and_html_escapes(self):
        t2ance = livecodebench()
        t2ance["schema_version"] = PROTOCOL
        t2ance["source_status"] = "t2ance_derived_tested_reference"
        t2ance["source"]["reference_origin"] = "t2ance_model_output"
        t2ance["dag"]["source"] = t2ance["source"]
        t2ance["dag"]["construction_protocol"] = "t2ance-lcb-normalize-v1"
        t2ance["dag"]["normalization"] = {"protocol": "t2ance-lcb-normalize-v1"}
        t2ance["dag_sha256"] = digest(t2ance["dag"])
        self.assertEqual(convert_record(t2ance, "livecodebench_v6", "a" * 64)[
            "review"]["source_status"], "t2ance_derived_tested_reference")
        page = _html([{"question_id": "<bad>", "source_tier": "CALIBRI",
                       "cpu_status": "passed", "status": "rejected",
                       "reason": "<script>alert(1)</script>"}],
                     {"counts": {"rejected": 1}}).decode("utf-8")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_jsonl_unicode_line_separator_is_not_a_new_record(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "one.jsonl"
            path.write_text(json.dumps({"question": "line 1\u2028line 2"}, ensure_ascii=False)
                            + "\n", encoding="utf-8")
            self.assertEqual(_read_jsonl(path), [{"question": "line 1\u2028line 2"}])

    def test_full_export_only_promotes_audited_terminal_partition(self):
        rows, items, events, cpu, _, _ = fixture()
        baseline_candidate = livecodebench()
        baseline_candidate["item_id"] = baseline_candidate["source"]["item_id"] = "item-040"
        baseline_candidate["question_id"] = baseline_candidate["source"]["question_id"] = "question-040"
        baseline_candidate["dag"]["item_id"] = "item-040"
        baseline_candidate["dag"]["source"] = baseline_candidate["source"]
        baseline_candidate["dag_sha256"] = digest(baseline_candidate["dag"])
        rows[40].update(source_tier="CALIBRI", cpu_status="passed",
                        status="model_accepted", reason="synthetic development acceptance")
        with tempfile.TemporaryDirectory() as name:
            base = private_dir(Path(name).resolve() / "base")
            old = private_dir(base / "calibri-full-repair-v2-dns-continuation-v2")
            new = private_dir(base / "completed-partial")
            output = base / "releases" / "synthetic"
            write_once(old / "completion.json", {"status": "paused"})
            write_once(new / "completion.json", {"status": "processed"})
            write_once(new / "run_config.json", {"synthetic": True})
            transport = {"source_run": str(old),
                         "source_files": {},
                         "terminal_ids": [f"item-{index:03d}" for index in range(11)],
                         "paused_selected_ids": [f"item-{index:03d}" for index in range(11, 35)]}
            write_once(new / "calibri-repair-manifest.json",
                       {"partial_transport_continuation": transport})
            audit = {"mechanical_pass": True,
                     "transport_continuation": {
                         "historical_terminal_count": 11,
                         "historical_terminal_replayed_count": 11,
                         "continued_count": 24,
                         "continued_terminal_replayed_count": 24,
                         "historical_terminal_rows": [
                             {"item_id": f"item-{index:03d}"} for index in range(11)]}}
            write_once(new / "offline-audit.json", audit)
            source = private_dir(base / "calibri-full175-v1")
            write_once(source / "items.json", list(items.values()))
            completion = {"status": "synthetic CPU"}
            interim_manifest = {"repair_completion_sha256": digest(read_json(old / "completion.json")),
                                "calibri_cpu_completion_sha256": digest(completion),
                                "t2ance_source": 60, "t2ance_cpu_sampled": 7}

            def baseline_export(_base, root):
                root = private_dir(root)
                write_bytes_once(root / "flow_175.jsonl", "".join(
                    json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode())
                write_bytes_once(root / "accepted_candidates.jsonl",
                                 (json.dumps(baseline_candidate, ensure_ascii=False) + "\n").encode())
                return interim_manifest

            def audited_event(root, item_id):
                is_old = root.name == "source-run-evidence"
                index = int(item_id[-3:])
                self.assertEqual(is_old, index < 11)
                return events[item_id][1]

            with patch("dag_builder.config.Config.load", return_value=object()), \
                 patch("dag_builder.livecodebench_repair_continuation_export.verify_partial_transport",
                       return_value=[items[f"item-{index:03d}"] for index in range(11, 35)]), \
                 patch("scripts.audit_calibri_repair.audit", return_value=audit), \
                 patch("dag_builder.livecodebench_repair_continuation_export._assert_old_snapshot"), \
                 patch("dag_builder.livecodebench_repair_continuation_export.verify_repair",
                       return_value=list(items.values())[:35]), \
                 patch("dag_builder.livecodebench_repair_continuation_export._event",
                       side_effect=audited_event), \
                 patch("dag_builder.livecodebench_repair_continuation_export.export_interim",
                       side_effect=baseline_export), \
                 patch("dag_builder.livecodebench_repair_continuation_export.verify_execution",
                       return_value=(cpu, completion)):
                report = export(base, new, output)
            self.assertEqual(report["old_repair_terminal_count"], 11)
            self.assertEqual(report["new_repair_terminal_count"], 24)
            self.assertEqual(report["model_accepted"], 2)
            self.assertEqual(len(_read_jsonl(output / "flow_175.jsonl")), 175)
            self.assertEqual(len(_read_jsonl(output / "accepted_candidates.jsonl")), 2)
            self.assertEqual(len(_read_jsonl(output / "unified/pals_dag_unified_v1.jsonl")), 2)
            self.assertFalse(read_json(output / "manifest.json")["formal_eligible"])

    def test_output_cannot_overlap_frozen_runs_or_artifact_root(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name).resolve() / "base"
            old = base / "calibri-full-repair-v2-dns-continuation-v2"
            new = base / "completed-partial"
            old.mkdir(parents=True)
            new.mkdir()
            for output in (base, old, old / "accidental", new, new / "accidental",
                           base / "calibri-full175-v1" / "accidental"):
                with self.subTest(output=output), self.assertRaises(InvalidOutput):
                    export(base, new, output)
                self.assertFalse((output / "interim-baseline").exists())


if __name__ == "__main__":
    unittest.main()
