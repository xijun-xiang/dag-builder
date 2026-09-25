"""Full-source preparation uses synthetic tests; candidate programs never run."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_calibri_full_execution as preparation
from audit_calibri_full_execution import expected_rows
from dag_builder.livecodebench_source import REVISION, normalize_livecodebench
from dag_builder.storage import digest, read_json, write_bytes_once, write_once
from test_livecodebench import fixture


class FullExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        rows = [fixture(n) for n in range(175)]
        for row in rows:
            row["private_test_cases"] = row["public_test_cases"]
        raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
        self.source = self.root / "test6.jsonl"
        write_bytes_once(self.source, raw)
        self.source_hash = hashlib.sha256(raw).hexdigest()
        originals, _ = normalize_livecodebench(rows, REVISION)
        self.items = [dict(originals[0], reference_code="class Solution:\n def identity(self,x): return x\n")]
        self.manifest = {"protocol": "calibri-lcb-source-full-v1", "items_sha256": digest(self.items),
                         "full_cohort_sha256": digest(originals)}
        write_once(self.root / "items.json", self.items)
        write_once(self.root / "manifest.json", self.manifest)

    def tearDown(self):
        self.temp.cleanup()

    def run_prepare(self, output="bundle"):
        with patch.object(preparation, "SOURCE_SHA256", self.source_hash):
            return preparation.prepare(self.root / "items.json", self.root / "manifest.json",
                                       self.source, self.root / output)

    def test_complete_source_binding_and_missing_candidates_preserved(self):
        result = self.run_prepare()
        self.assertEqual(result["test_count"], 2)
        self.assertEqual(result["original_questions"], 175)
        manifest = read_json(self.root / "bundle/input-manifest.json")
        self.assertEqual(len(manifest["not_executed"]), 174)
        self.assertEqual(manifest["calibri_manifest_sha256"], digest(self.manifest))
        self.assertFalse((self.root / "bundle/completion.json").exists())
        originals, bundles = normalize_livecodebench([
            json.loads(line) for line in self.source.read_text().splitlines()], REVISION)
        self.assertEqual(expected_rows(self.items, originals, bundles),
                         read_json(self.root / "bundle/execution-input.json"))
        changed = dict(self.items[0], question='different')
        with self.assertRaisesRegex(ValueError, 'pinned v6 source'):
            expected_rows([changed], originals, bundles)

    def test_changed_source_or_candidate_cannot_prepare(self):
        self.source.write_bytes(self.source.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "original v6 source"):
            self.run_prepare()
        self.items[0]["reference_code"] += "# changed\n"
        (self.root / "items.json").write_text(json.dumps(self.items))
        with self.assertRaisesRegex(ValueError, "candidate identity"):
            self.run_prepare()

    def test_same_manifest_does_not_permit_question_rewrite(self):
        self.items[0]["question"] = "Different task"
        self.manifest["items_sha256"] = digest(self.items)
        (self.root / "items.json").write_text(json.dumps(self.items))
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "source identity"):
            self.run_prepare()


if __name__ == "__main__":
    unittest.main()
