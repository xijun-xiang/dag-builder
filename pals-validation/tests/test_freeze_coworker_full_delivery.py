"""Full-delivery freeze retains every audited row and binds both experiment arms."""

import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/freeze_coworker_full_delivery.py"
SPEC = importlib.util.spec_from_file_location("freeze_coworker_full_delivery", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

from pals_validation.io import digest  # noqa: E402
from pals_validation.prepare import prepare  # noqa: E402
from pals_validation.run import init_run  # noqa: E402


def simple_record():
    nodes = [
        {"node_id": 1, "kind": "given", "parents": [], "statement": "Two apples are given.",
         "source_field": "question", "source_quote": "Two apples", "justification": "Given"},
        {"node_id": 2, "kind": "derived", "parents": [1],
         "statement": "Adding one apple gives three.", "source_field": "solution",
         "source_quote": "2 + 1 = 3", "justification": "Addition"},
        {"node_id": 3, "kind": "answer", "parents": [2], "statement": "Three apples.",
         "source_field": "solution", "source_quote": "3", "justification": "Answer"},
    ]
    return {"schema_version": "pals_dag_unified_v1", "item_id": "fixture-item",
            "benchmark": "gsm8k",
            "problem": {"question": "There are two apples. Add one. How many?",
                        "domain": "grade_school_math", "choices": None, "entry_point": None},
            "answer": {"kind": "text", "value": "3"},
            "dag": {"schema_version": "pals_step_dag_v1", "nodes": nodes,
                    "nodes_sha256": digest(nodes)},
            "review": {"model_accepted": True, "human_approved": False,
                       "source_status": "model_accepted"},
            "provenance": {"dataset": "openai/gsm8k", "subset": "main", "split": "test",
                           "source_row": 1, "source_id": "openai/gsm8k:main:test:1",
                           "source_record_sha256": "0" * 64}}


class FullDeliveryReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def make_tiny_release(self):
        release = self.base / "release"
        release.mkdir()
        name = "scoreable-gsm8k.jsonl"
        data = MODULE.jsonl([simple_record()])
        (release / name).write_bytes(data)
        manifest = {"protocol": MODULE.PROTOCOL, "selection_seed": 20260915,
                    "compatible_experiments": ["e1", "e2"],
                    "files": {name: {"sha256": MODULE.sha(data), "records": 1,
                                     "benchmark": "gsm8k"}}}
        (release / "manifest.json").write_bytes(MODULE.canonical(manifest))
        return release, name, manifest

    def test_one_unchanged_source_is_bound_to_both_experiments(self):
        release, name, _ = self.make_tiny_release()
        prepared = self.base / "prepared"
        output = prepare(release / name, prepared, seed=20260915,
                         expected_sha=MODULE.sha((release / name).read_bytes()),
                         benchmark="gsm8k")
        self.assertEqual(output["counts"]["e2"], 1)
        self.assertNotIn("frozen_source_experiment", output)
        self.assertEqual(output["compatible_experiments"], ["e1", "e2"])
        self.assertEqual(output["frozen_cohort"]["release_protocol"], MODULE.PROTOCOL)
        config = {"model_path": "synthetic", "model_revision": "fixture", "backend": "mock",
                  "temperatures": [0.3, 0.7, 1.2], "repeats": 2, "seed": 1, "device": "cpu",
                  "dtype": "float32", "attention": "eager", "max_context": 4096,
                  "max_new_tokens": 100, "chat_template_kwargs": {}}
        config_path = self.base / "config.json"
        config_path.write_text(json.dumps(config))
        for arm in ("e1", "e2"):
            run = self.base / arm
            result = init_run(prepared, config_path, run, arm, shards=1)
            self.assertGreater(result["jobs"], 0)

    def test_prepare_refuses_mutated_full_release_source_and_wrong_seed(self):
        release, name, _ = self.make_tiny_release()
        with self.assertRaisesRegex(ValueError, "seed or experiment compatibility"):
            prepare(release / name, self.base / "wrong-seed", seed=7,
                    benchmark="gsm8k")
        with (release / name).open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "hash or benchmark mismatch"):
            prepare(release / name, self.base / "mutated-source", benchmark="gsm8k")

    def test_run_rejects_incompatible_full_release_arm(self):
        release, name, _ = self.make_tiny_release()
        prepared = self.base / "prepared"
        prepare(release / name, prepared, benchmark="gsm8k")
        manifest_path = prepared / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["compatible_experiments"] = ["e1"]
        manifest_path.write_text(json.dumps(manifest))
        config = {"model_path": "synthetic", "model_revision": "fixture", "backend": "mock",
                  "temperatures": [0.3], "repeats": 2, "seed": 1, "device": "cpu",
                  "dtype": "float32", "attention": "eager", "max_context": 4096,
                  "max_new_tokens": 100, "chat_template_kwargs": {}}
        config_path = self.base / "config.json"
        config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "experiment compatibility mismatch"):
            init_run(prepared, config_path, self.base / "e2", "e2", shards=1)

    def test_full_local_zip_matches_audit_when_available(self):
        workspace = next((p for p in Path(__file__).resolve().parents
                          if (p / "artifacts/pals-coworker-dag-repair-20260924/audit-v4").is_dir()), None)
        source = Path.home() / "Downloads/dag_data_gsm8k_mmlu_math_mmlu_psych.zip"
        if workspace is None or not source.is_file():
            self.skipTest("Coworker source ZIP and audit are private local fixtures")
        audit = workspace / "artifacts/pals-coworker-dag-repair-20260924/audit-v4"
        release = self.base / "full-release"
        result = MODULE.freeze(source, audit, release)
        self.assertEqual(result["source_delivered"], 1539)
        self.assertEqual(result["scoreable_total"], 1531)
        self.assertEqual(result["e2_anchor_total"], 1463)
        self.assertEqual([result["files"][f"scoreable-{p}.jsonl"]["records"]
                          for p in MODULE.PACKAGES], [530, 415, 586])
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((release / "manifest.json").stat().st_mode), 0o600)
        original = MODULE.parse_jsonl((audit / "flow_1539.jsonl").read_bytes(), "audit")
        flow = MODULE.parse_jsonl((release / "flow_1539.jsonl").read_bytes(), "release")
        self.assertEqual(len(flow), 1539)
        self.assertEqual(sum(row["e2_status"] == "anchored" for row in flow), 1463)
        self.assertEqual(sum(row["e2_status"] == "not_applicable_single_scored_step"
                             for row in flow), 8)
        for before, after in zip(original, flow):
            self.assertEqual({key: after[key] for key in before}, before)
        for package, expected_anchor in MODULE.EXPECTED_E2_ANCHORS.items():
            name = f"scoreable-{package}.jsonl"
            prepared = self.base / f"prepared-{package}"
            manifest = prepare(release / name, prepared, benchmark=(
                "gsm8k" if package == "gsm8k" else "mmlu"),
                expected_sha=result["files"][name]["sha256"])
            self.assertEqual(manifest["questions"], MODULE.EXPECTED_SCOREABLE[package])
            self.assertEqual(manifest["counts"]["e2"], expected_anchor)
            self.assertEqual(manifest["compatible_experiments"], ["e1", "e2"])
        with self.assertRaises(FileExistsError):
            MODULE.freeze(source, audit, release)


if __name__ == "__main__":
    unittest.main()
