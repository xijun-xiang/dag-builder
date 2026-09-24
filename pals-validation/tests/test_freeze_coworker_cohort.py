"""The score-blind cohort freeze preserves rows and refuses unknown decisions."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/freeze_coworker_cohort.py"
SPEC = importlib.util.spec_from_file_location("freeze_coworker_cohort", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FreezeCoworkerCohortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.audit = self.base / "audit"
        self.audit.mkdir()
        packages = ("gsm8k", "mmlu_math", "mmlu_psych_social")
        flow, candidates = [], []
        for index in range(1539):
            package = packages[index % len(packages)]
            row = {"item_id": f"item{index}", "payload": index}
            eligible = index < 6
            flow.append({"item_id": row["item_id"], "source_id": f"source{index}",
                         "package": package,
                         "record_sha256": MODULE.sha(MODULE.json.dumps(
                             row, ensure_ascii=False, sort_keys=True, indent=2,
                             allow_nan=False).encode("utf-8") + b"\n"),
                         "e1_fair_pair_mechanical": eligible,
                         "e2_anchor_mechanical": eligible})
            if eligible:
                candidates.append(row)
        outputs = {
            "flow_1539.jsonl": MODULE.jsonl(flow),
            "e1_mechanical_candidates.jsonl": MODULE.jsonl(candidates),
            "e2_mechanical_candidates.jsonl": MODULE.jsonl(candidates),
        }
        for name, data in outputs.items():
            (self.audit / name).write_bytes(data)
        (self.audit / "manifest.json").write_text(json.dumps({
            "protocol": "coworker-dag-conservative-audit-v1",
            "delivered_total": 1539,
            "source_zip_sha256": "a" * 64,
            "selection_seed": 20260915,
            "outputs_sha256": {name: MODULE.sha(data) for name, data in outputs.items()},
        }))
        self.decisions = self.base / "decisions.json"

    def write_decisions(self, exclusions):
        self.decisions.write_text(json.dumps({
            "protocol": "score_blind_semantic_exclusions_v1",
            "excluded_source_ids": exclusions,
        }))

    def test_exclusion_is_recorded_without_rewriting_an_included_row(self):
        self.write_decisions({"source0": "Pre-score semantic review"})
        result = MODULE.freeze(self.audit, self.decisions, self.base / "release")
        self.assertEqual(result["source_delivered"], 1539)
        self.assertEqual(result["files"]["e1-gsm8k.jsonl"]["records"], 1)
        rows = MODULE.read_jsonl((self.base / "release/e1-gsm8k.jsonl").read_bytes())
        self.assertEqual(rows, [{"item_id": "item3", "payload": 3}])
        flow = MODULE.read_jsonl((self.base / "release/flow_1539.jsonl").read_bytes())
        self.assertEqual(flow[0]["score_blind_exclusion"], "Pre-score semantic review")
        self.assertFalse(flow[0]["e1_in_frozen_cohort"])
        self.assertEqual(sum(row["e1_in_frozen_cohort"] for row in flow), 5)

    def test_unknown_exclusion_and_hash_tampering_fail(self):
        self.write_decisions({"not_in_source": "unknown"})
        with self.assertRaisesRegex(ValueError, "Unknown"):
            MODULE.freeze(self.audit, self.decisions, self.base / "bad1")
        self.write_decisions({})
        with (self.audit / "e1_mechanical_candidates.jsonl").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            MODULE.freeze(self.audit, self.decisions, self.base / "bad2")


if __name__ == "__main__":
    unittest.main()
