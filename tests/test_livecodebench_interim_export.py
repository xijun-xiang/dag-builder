"""Source-stratified interim export guards, without private benchmark data."""

import unittest
from copy import deepcopy

from dag_builder.livecodebench_interim_export import _html, _record
from dag_builder.schemas import InvalidOutput
from dag_builder.storage import digest
from dag_builder.unified import convert_record
from test_unified import livecodebench


class InterimExportTests(unittest.TestCase):
    def test_distinct_cpu_evidence_and_source_tier_remain_visible(self):
        fixture = livecodebench()
        item, dag = fixture["source"], fixture["dag"]
        first_cpu, full_cpu = {"status": "passed", "job": "development"}, {
            "status": "passed", "job": "full"}
        item["execution_evidence"]["result_sha256"] = digest(first_cpu)
        dag["source"] = item
        event = {"dag": dag}
        record = _record(item, event, full_cpu, "CALIBRI", first_cpu)
        self.assertEqual(record["cpu_result_sha256"], digest(first_cpu))
        self.assertEqual(record["full_cpu_result_sha256"], digest(full_cpu))
        self.assertEqual(convert_record(record, "livecodebench_v6", "a" * 64)[
            "review"]["source_status"], "calibri_derived_tested_reference")
        wrong = deepcopy(item)
        wrong["reference_code"] = "print(2)\n"
        with self.assertRaises(InvalidOutput):
            _record(wrong, event, full_cpu, "CALIBRI", first_cpu)

    def test_interim_html_escapes_untrusted_text_and_labels_stage(self):
        rendered = _html([{"question_id": "<bad>", "source_tier": "t2ance",
                           "cpu_status": "not_submitted", "status": "held_without_cpu",
                           "reason": "<script>alert(1)</script>"}],
                         {"held_without_cpu": 1}).decode("utf-8")
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("阶段性", rendered)


if __name__ == "__main__":
    unittest.main()
