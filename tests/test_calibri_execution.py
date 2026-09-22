"""Execution-bundle provenance tests; never run generated programs."""

import copy
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_calibri_execution as preparation
from dag_builder.storage import digest, read_json, write_once
from test_calibri_source import fixture


class CALIBRIExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source, self.calibri = self.root / "source", self.root / "calibri"
        item, raw = fixture()
        self.bundle = {"opaque": "synthetic tests only"}
        item.update(item_id="fixture0", platform="atcoder", starter_code="",
                    source_content_sha256="frozen-source", tests_sha256=digest(self.bundle))
        originals = [{**item, "item_id": f"fixture{i}"} for i in range(5)]
        original_manifest = {"items_sha256": digest(originals)}
        self.item = {**item, "origin": {"config": "livecodebench_qwen3", "sample_index": 0,
                                       "selected_columns_sha256": digest(raw)},
                     "reference_code": raw["program"][0], "raw_output": raw["output"][0]}
        write_once(self.source / "items.json", originals)
        write_once(self.source / "prepared-manifest.json", original_manifest)
        write_once(self.source / "tests/fixture0.json", self.bundle)
        write_once(self.calibri / "source-rows/livecodebench_qwen3/fixture0.json", raw)
        self.manifest = {"protocol": "calibri-lcb-source-v2",
                         "source_prepared_manifest_sha256": digest(original_manifest)}
        self.rebind([self.item])

    def tearDown(self):
        self.temp.cleanup()

    def rebind(self, items):
        selection = {"selected_ids": [i["item_id"] for i in items], "excluded": []}
        self.manifest.update(items_sha256=digest(items), selection_sha256=digest(selection))
        self.calibri.mkdir(exist_ok=True, mode=0o700)
        # Deliberate fixture mutation tests; production uses write-once storage.
        for name, value in (("items.json", items), ("selection.json", selection),
                            ("calibri-manifest.json", self.manifest)):
            (self.calibri / name).write_text(json.dumps(value))

    def test_prepare_is_bound_deterministic_and_does_not_execute(self):
        tests = [{"split": "public", "index": 0, "inputs": "x", "expected": "x"}]
        output = self.root / "bundle"
        with patch.object(preparation, "decode_tests", return_value=tests):
            stats = preparation.prepare(self.calibri, self.source, output)
        self.assertEqual(stats["execution_candidates"], 1)
        rows = read_json(output / "execution-input.json")
        self.assertEqual(rows[0]["code"], self.item["reference_code"])
        self.assertEqual(gzip.decompress((output / "execution-input.json.gz").read_bytes()),
                         (output / "execution-input.json").read_bytes())
        manifest = read_json(output / "input-manifest.json")
        self.assertEqual(manifest["inputs_sha256"], digest(rows))
        self.assertEqual(manifest["selected"], 5)
        self.assertFalse((output / "completion.json").exists())

    def test_tampered_raw_test_or_selected_program_refused(self):
        bad = copy.deepcopy(self.item)
        bad["reference_code"] = "print('changed')"
        self.rebind([bad])
        with self.assertRaisesRegex(ValueError, "selected sample"):
            preparation.prepare(self.calibri, self.source, self.root / "bad-code")
        self.rebind([self.item])
        (self.source / "tests/fixture0.json").write_text('{"changed": true}')
        with self.assertRaisesRegex(ValueError, "test bundle"):
            preparation.prepare(self.calibri, self.source, self.root / "bad-test")

    def test_duplicate_candidates_cannot_inflate_denominator(self):
        self.rebind([self.item, self.item])
        with self.assertRaisesRegex(ValueError, "inventory"):
            preparation.prepare(self.calibri, self.source, self.root / "duplicates")


if __name__ == "__main__":
    unittest.main()
