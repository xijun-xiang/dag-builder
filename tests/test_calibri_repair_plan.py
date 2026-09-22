"""Checked inventory/relocation regression tests; fake API and synthetic data."""

import copy
import tempfile
import unittest
from pathlib import Path

from dag_builder.calibri_normalize import normalize
from dag_builder.calibri_repair import (
    CALIBRIRepairPipeline, CHECKED_PROTOCOL, PROTOCOL, assemble_repaired_graph,
    prepare_repair, verify_repair,
)
from dag_builder.calibri_repair_plan import AUDIT_CHECKS, apply_checked_repair
from dag_builder.storage import read_json, write_once
from test_calibri_normalize import fixture
from test_calibri_pipeline import config
from test_calibri_repair import Client, failed_parent, offline_audit, repair_outputs


def checked_plan():
    return {"additions": [], "removals": [], "moves": [],
            "premise_checks": [{"node_id": 2, "required_node_ids": [1], "required_additions": [],
                                "question_refs_used": ["Q0001"], "background_assumptions": [],
                                "reason": "The task premise establishes the required identity output."}],
            "deletion_checks": [], "reason": "Synthetic inventory."}


def moved_fixture():
    item, proposal, _ = fixture()
    proposal["steps"].append({"kind": "given", "statement": "The program prints the input text.",
        "source_refs": ["C0001"], "support_type": "source_supported", "normalization_note": "Observed output operation."})
    plan = checked_plan()
    plan["moves"] = [{"node_id": 3, "before_node_id": 2, "reason": "The output rule is needed by the conclusion."}]
    plan["premise_checks"][0]["required_node_ids"] = [1, 3]
    return item, normalize(proposal, item), plan


def prepare_revision(folder):
    parent, outputs = failed_parent(folder)
    history = folder / "repair-v1"
    prepare_repair(parent, history)
    bad = repair_outputs(outputs)
    bad["review_dag"].update(decision="reject", issues=["Synthetic semantic issue."], reason="Synthetic semantic issue.")
    bad["review_dag"]["checks"]["dependencies_sufficient"] = False
    CALIBRIRepairPipeline(history, config(prompt_version=PROTOCOL, max_calls=9), Client(bad, PROTOCOL)).run()
    write_once(history / "completion.json", {"status": "processed"})
    run = folder / "repair-v2"
    prepare_repair(parent, run, prompt_version=CHECKED_PROTOCOL, history=history, max_calls=6)
    outputs = repair_outputs(outputs)
    outputs["repair"] = checked_plan()
    outputs["review_dag"]["checks"].update(dict.fromkeys(AUDIT_CHECKS, True))
    return parent, history, run, outputs


class CheckedPlanTests(unittest.TestCase):
    def test_only_existing_root_moves_and_statement_stays_identical(self):
        item, old, plan = moved_fixture()
        new, record = apply_checked_repair(plan, old, item)
        self.assertEqual([n["statement"] for n in new["nodes"]],
                         [old["nodes"][i]["statement"] for i in (0, 2, 1)])
        self.assertEqual(record["node_mapping"], [{"old_node_id": 1, "new_node_id": 1},
            {"old_node_id": 2, "new_node_id": 3}, {"old_node_id": 3, "new_node_id": 2}])
        self.assertEqual(record["premise_inventory"][0]["required_ancestor_ids"], [1, 2])
        self.assertEqual(record["protocol"], CHECKED_PROTOCOL)

    def test_wrong_moves_and_incomplete_inventory_fail(self):
        item, old, good = moved_fixture()
        values = []
        for moves in ([], [{"node_id": 2, "before_node_id": 1, "reason": "derived move"}],
                      [{"node_id": 1, "before_node_id": 2, "reason": "move later"}],
                      good["moves"] * 2):
            values.append({**good, "moves": moves})
        values += [{**good, "premise_checks": []}, {**good, "premise_checks": good["premise_checks"] * 2}]
        for changes in ({"required_node_ids": [2]}, {"required_node_ids": [999]},
                        {"required_node_ids": [1, 1]}, {"required_additions": [0]},
                        {"question_refs_used": ["Q9999"]}, {"required_node_ids": [3]}):
            value = copy.deepcopy(good)
            value["premise_checks"][0].update(changes)
            values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply_checked_repair(value, old, item)

    def test_removal_requires_full_impact_coverage_and_cannot_remove_used_fact(self):
        item, old, plan = moved_fixture()
        plan["moves"] = []
        plan["removals"] = [{"node_id": 3, "reason": "Synthetic redundancy example."}]
        plan["premise_checks"][0]["required_node_ids"] = [1]
        plan["deletion_checks"] = [{"node_id": 3, "retained_conclusions_checked": [2],
                                    "required_by_node_ids": [], "reason": "Synthetic general proof suffices."}]
        apply_checked_repair(plan, old, item)
        for changes in ({"retained_conclusions_checked": []}, {"required_by_node_ids": [2]}, {"node_id": True}):
            bad = copy.deepcopy(plan)
            bad["deletion_checks"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                apply_checked_repair(bad, old, item)
        plan["premise_checks"][0]["required_node_ids"] = [1, 3]
        with self.assertRaises(ValueError):
            apply_checked_repair(plan, old, item)

    def test_inventory_must_be_an_actual_dependency_not_merely_connected_to_answer(self):
        item, old, plan = moved_fixture()
        normalized, record = apply_checked_repair(plan, old, item)
        deps = {"dependencies": [
            {"node_id": 1, "parents": [], "justification": "Task."},
            {"node_id": 2, "parents": [], "justification": "Observed operation."},
            {"node_id": 3, "parents": [1], "justification": "Only the task requirement is used."}],
            "answer_parents": [2, 3], "answer_justification": "Both branches connect."}
        with self.assertRaisesRegex(ValueError, "drops an inventoried"):
            assemble_repaired_graph(deps, normalized, item, record)
        deps["dependencies"][2]["parents"] = [1, 2]
        assemble_repaired_graph(deps, normalized, item, record)

    def test_addition_indices_survive_reordering(self):
        item, old, plan = moved_fixture()
        plan["additions"] = [{"before_node_id": 2, "statement": "The input must be returned unchanged.",
                              "source_refs": ["Q0001"], "reason": "Question condition."}]
        plan["premise_checks"][0]["required_additions"] = [0]
        _, record = apply_checked_repair(plan, old, item)
        self.assertEqual(record["premise_inventory"][0]["node_id"], 4)
        self.assertEqual(record["premise_inventory"][0]["required_ancestor_ids"], [1, 3, 2])


class CheckedPipelineTests(unittest.TestCase):
    def test_revision_binds_history_budget_and_offline_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, history, run, outputs = prepare_revision(Path(temp).resolve())
            conf = config(prompt_version=CHECKED_PROTOCOL, max_calls=6)
            self.assertEqual(len(verify_repair(run, conf)), 1)
            self.assertEqual(read_json(run / "calibri-normalization-manifest.json")["prior_calls"], 5)
            client = Client(outputs, CHECKED_PROTOCOL)
            pipeline = CALIBRIRepairPipeline(run, conf, client)
            self.assertEqual(pipeline.run()["results"][0]["status"], "model_accepted")
            pipeline.run()
            self.assertEqual(len(client.calls), 3)
            self.assertNotIn("never send this", str(client.calls))
            write_once(run / "completion.json", {"status": "processed"})
            write_once(run / "code_origin.json", {"source_files": {}, "git_commit": "synthetic"})
            self.assertEqual(offline_audit(run)["cumulative_requests"], 8)
            with self.assertRaises(ValueError):
                prepare_repair(parent, run.parent / "third", prompt_version=CHECKED_PROTOCOL, history=run)

    def test_history_tampering_fails_before_api(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, run, outputs = prepare_revision(Path(temp).resolve())
            path = run / "revision-history/items" / ("a" * 20) / "review_dag/output.json"
            path.write_text('{}')
            client = Client(outputs, CHECKED_PROTOCOL)
            with self.assertRaisesRegex(ValueError, "history changed"):
                CALIBRIRepairPipeline(run, config(prompt_version=CHECKED_PROTOCOL, max_calls=6), client).run()
            self.assertFalse(client.calls)

    def test_semantic_rejection_is_terminal_and_no_auto_release(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, run, outputs = prepare_revision(Path(temp).resolve())
            outputs["review_dag"].update(decision="reject", issues=["Incomplete inventory."], reason="Incomplete inventory.")
            outputs["review_dag"]["checks"]["premise_inventory_complete"] = False
            client = Client(outputs, CHECKED_PROTOCOL)
            pipeline = CALIBRIRepairPipeline(run, config(prompt_version=CHECKED_PROTOCOL, max_calls=6), client)
            self.assertEqual(pipeline.run()["results"][0]["status"], "rejected")
            pipeline.run()
            self.assertEqual(len(client.calls), 3)
            self.assertFalse(list(run.glob("items/*/dag.json")))


if __name__ == "__main__":
    unittest.main()
