"""Versioned v4 canary remains an auditable proposal, not a quality claim."""

import tempfile
import unittest
from pathlib import Path

from dag_builder.config import Config
from dag_builder.stages import prompt, stages_for
from dag_builder.storage import read_json, write_once
from dag_builder.t2ance_audit import audit_completed
from dag_builder.t2ance_pipeline import T2ancePipeline
from dag_builder.t2ance_v3 import EXTRA_REVIEW_CHECKS as V3_CHECKS
from dag_builder.t2ance_v4 import (EXTRA_REVIEW_CHECKS, PROTOCOL,
                                   validate_audit_v4)
from test_t2ance_pipeline import Client, prepared_fixture


class T2anceV4Tests(unittest.TestCase):
    def test_specific_checks_and_complete_prompt_schema(self):
        config = Config(task_type="livecodebench", prompt_version=PROTOCOL,
                        solution_source="t2ance_reference_normalization",
                        max_calls=24, max_reserved_tokens=2000000)
        self.assertEqual(stages_for(config), ("normalize", "dependencies", "review_dag"))
        self.assertIn("normalization_note", prompt("normalize", PROTOCOL, "livecodebench"))
        self.assertIn("original_node_id", prompt("review_dag", PROTOCOL, "livecodebench"))
        review = {"decision": "accept", "checks": {key: True for key in
                  (*V3_CHECKS, *EXTRA_REVIEW_CHECKS,
                   "statements_correct", "faithful_to_solution", "dependencies_sufficient",
                   "dependencies_minimal", "justifications_complete", "no_new_facts",
                   "source_meaning_preserved", "algorithm_matches_tested_code",
                   "no_silent_error_repair", "omissions_safe",
                   "supplements_disclosed_and_valid", "root_premises_sound",
                   "self_contained_statements", "no_invariant_assumed",
                   "code_facts_grounded")}, "issues": [], "reason": "Synthetic review."}
        validate_audit_v4(review)
        review["checks"][EXTRA_REVIEW_CHECKS[0]] = None
        with self.assertRaisesRegex(ValueError, "v4 semantic review is unresolved"):
            validate_audit_v4(review)

    def test_v4_pipeline_and_raw_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            run, outputs = prepared_fixture(Path(folder).resolve(), prompt_version=PROTOCOL)
            for key in (*V3_CHECKS, *EXTRA_REVIEW_CHECKS):
                outputs["review_dag"]["checks"][key] = True
            config = Config(task_type="livecodebench", prompt_version=PROTOCOL,
                            solution_source="t2ance_reference_normalization", workers=1,
                            max_calls=24, max_reserved_tokens=2000000)
            write_once(run / "config.json", config.to_dict())
            snapshot = {"source_files": {}, "code_sha256": "synthetic"}
            write_once(run / "code_origin.json", snapshot)
            write_once(run / "controller/code/snapshot_origin.json", snapshot)
            result = T2ancePipeline(run, config, Client(outputs, PROTOCOL)).run()
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            write_once(run / "completion.json", {"status": "processed", "global_stop": False,
                "summary": {"selected": 1, "api_attempts": result["attempt_count"],
                            "counts": {"model_accepted": 1}}})
            self.assertTrue(audit_completed(run)["mechanical_pass"])
            dag = read_json(run / "items" / ("a" * 20) / "dag.json")
            self.assertEqual(dag["construction_protocol"], PROTOCOL)
            self.assertFalse(dag["formal_eligible"])


if __name__ == "__main__":
    unittest.main()
