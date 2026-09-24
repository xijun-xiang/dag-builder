"""Score-blind cohort freezing preserves data and records independent E1/E2 decisions."""

import importlib.util
import json
import stat
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/freeze_coworker_cohort.py"
SPEC = importlib.util.spec_from_file_location("freeze_coworker_cohort", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

from pals_validation.io import digest  # noqa: E402


def record_for(index):
    package = ("gsm8k", "mmlu_math", "mmlu_psych_social")[index % 3]
    gsm8k = package == "gsm8k"
    subset = ("main" if gsm8k else "college_mathematics" if package == "mmlu_math"
              else "professional_psychology")
    source_id = (f"openai/gsm8k:main:test:{index}" if gsm8k
                 else f"cais/mmlu:{subset}:test:{index}")
    # Two length-2 branches converge, so the unchanged forest operator yields
    # a legal sibling swap and a deterministic one-edge inversion.
    solution_origin = index < 3
    nodes = []
    for node_id, kind, parents in ((1, "given", []), (2, "derived", [1]),
                                   (3, "knowledge" if solution_origin else "given", []),
                                   (4, "derived", [3]), (5, "derived", [2, 4]),
                                   (6, "answer", [5])):
        source_field = ("solution" if (solution_origin and node_id > 1)
                        or node_id in (5, 6) else "question")
        nodes.append({"node_id": node_id, "kind": kind, "parents": parents,
                      "statement": f"Step {node_id}", "source_field": source_field,
                      "source_quote": f"Quote {node_id}", "justification": "Fixture"})
    return {
        "schema_version": "pals_dag_unified_v1", "item_id": f"item{index}",
        "benchmark": "gsm8k" if gsm8k else "mmlu",
        "problem": {"question": f"Question {index}?", "domain": "grade_school_math" if gsm8k else subset,
                    "choices": None if gsm8k else ["one", "two", "three", "four"],
                    "entry_point": None},
        "answer": {"kind": "text" if gsm8k else "choice", "value": "1" if gsm8k else "A"},
        "dag": {"schema_version": "pals_step_dag_v1", "nodes": nodes,
                "nodes_sha256": digest(nodes)},
        "review": {"model_accepted": True, "human_approved": False,
                   "source_status": "model_accepted"},
        "provenance": {"dataset": "openai/gsm8k" if gsm8k else "cais/mmlu",
                       "subset": subset, "split": "test", "source_row": index,
                       "source_id": source_id},
    }


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
            row = record_for(index) if index < 6 else {"item_id": f"item{index}"}
            eligible = index < 6
            source_id = (row["provenance"]["source_id"] if eligible else f"source{index}")
            flow.append({"item_id": row["item_id"], "source_id": source_id,
                         "package": package,
                         "record_sha256": MODULE.sha((json.dumps(
                             row, ensure_ascii=False, sort_keys=True, indent=2,
                             allow_nan=False) + "\n").encode("utf-8")),
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
        self.flow = flow

    def write_decisions(self, e1=None, e2=None, legacy=False):
        if legacy:
            payload = {"protocol": "score_blind_semantic_exclusions_v1",
                       "excluded_source_ids": e1 or {}}
        else:
            payload = {"protocol": "score_blind_experiment_exclusions_v2",
                       "excluded_source_ids": {"e1": e1 or {}, "e2": e2 or {}}}
        self.decisions.write_text(json.dumps(payload))

    def test_arm_specific_exclusion_and_primary_rule_preserve_e2(self):
        self.write_decisions({self.flow[0]["source_id"]: "E1 edge is semantically invalid"},
                             {self.flow[1]["source_id"]: "E2 target is ambiguous"})
        release = self.base / "release"
        result = MODULE.freeze(self.audit, self.decisions, release)
        self.assertEqual(result["source_delivered"], 1539)
        self.assertEqual(result["files"]["e1-gsm8k.jsonl"]["records"], 0)
        self.assertEqual((release / "e1-gsm8k.jsonl").read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((release / "manifest.json").stat().st_mode), 0o600)
        self.assertEqual(result["files"]["e2-gsm8k.jsonl"]["records"], 2)
        self.assertEqual(result["files"]["e2-mmlu_math.jsonl"]["records"], 1)
        self.assertEqual(result["files"]["e1-mechanical-secondary-gsm8k.jsonl"]["records"], 1)
        rows = MODULE.read_jsonl((release / "e2-gsm8k.jsonl").read_bytes())
        self.assertEqual(rows, [record_for(0), record_for(3)])
        flow = MODULE.read_jsonl((release / "flow_1539.jsonl").read_bytes())
        self.assertEqual(len(flow), 1539)
        self.assertEqual(flow[0]["e1_decision"], "semantic_exclusion")
        self.assertEqual(flow[0]["e2_decision"], "included")
        self.assertEqual(flow[1]["e1_decision"], "primary")
        self.assertEqual(flow[1]["e2_decision"], "semantic_exclusion")
        self.assertEqual(flow[3]["e1_decision"], "mechanical_secondary_only")
        self.assertEqual(flow[3]["e1_selected_break_parent"]["source_field"], "question")
        self.assertEqual(flow[3]["e1_primary_eligibility_reason"],
                         "selected_parent_not_solution_derived_or_knowledge")
        self.assertFalse(flow[3]["e1_in_frozen_cohort"])
        self.assertTrue(flow[3]["e1_in_mechanical_secondary"])

    def test_v1_global_exclusion_applies_to_both_experiments(self):
        self.write_decisions({self.flow[0]["source_id"]: "Legacy shared exclusion"}, legacy=True)
        result = MODULE.freeze(self.audit, self.decisions, self.base / "legacy")
        self.assertEqual(result["decision_protocol"], "score_blind_semantic_exclusions_v1")
        self.assertEqual(result["exclusions"]["e1"], result["exclusions"]["e2"])
        flow = MODULE.read_jsonl((self.base / "legacy/flow_1539.jsonl").read_bytes())
        self.assertEqual(flow[0]["e1_decision"], "semantic_exclusion")
        self.assertEqual(flow[0]["e2_decision"], "semantic_exclusion")

    def test_unknown_exclusion_hash_tampering_and_missing_candidate_fail(self):
        self.write_decisions({"not_in_source": "unknown"})
        with self.assertRaisesRegex(ValueError, "Unknown"):
            MODULE.freeze(self.audit, self.decisions, self.base / "bad1")
        self.write_decisions()
        with (self.audit / "e1_mechanical_candidates.jsonl").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            MODULE.freeze(self.audit, self.decisions, self.base / "bad2")
        self.assertFalse((self.base / "bad2").exists())

    def test_malformed_decisions_are_rejected(self):
        self.decisions.write_text('{"protocol":"score_blind_experiment_exclusions_v2",'
                                  '"excluded_source_ids":{"e1":{},"e1":{},"e2":{}}}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            MODULE.freeze(self.audit, self.decisions, self.base / "bad3")
        self.decisions.write_text('{"protocol":"score_blind_experiment_exclusions_v2",'
                                  '"excluded_source_ids":{"e1":{}}}')
        with self.assertRaisesRegex(ValueError, "name E1 and E2"):
            MODULE.freeze(self.audit, self.decisions, self.base / "bad4")

    def test_certified_candidate_cannot_disappear_from_audit_list(self):
        self.write_decisions()
        path = self.audit / "e1_mechanical_candidates.jsonl"
        rows = MODULE.read_jsonl(path.read_bytes())
        truncated = MODULE.jsonl(rows[:-1])
        path.write_bytes(truncated)
        manifest_path = self.audit / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["outputs_sha256"][path.name] = MODULE.sha(truncated)
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Candidate set differs"):
            MODULE.freeze(self.audit, self.decisions, self.base / "bad5")
        self.assertFalse((self.base / "bad5").exists())


if __name__ == "__main__":
    unittest.main()
