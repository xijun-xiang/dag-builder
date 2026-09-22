"""Synthetic repair contracts, provenance, boundedness and negative controls."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.calibri_normalize import assemble_graph, normalize
from dag_builder.calibri_pipeline import CALIBRIPipeline, prepare
from dag_builder.calibri_repair import (
    CALIBRIRepairPipeline, PROTOCOL, REPAIR_CHECKS, STEP_FIELDS,
    apply_repair, prepare_repair, validate_repair_audit,
)
from dag_builder.stages import prompt
from dag_builder.storage import digest, read_json, write_once
from test_calibri_normalize import fixture
from test_calibri_pipeline import config, setup


class Client:
    def __init__(self, outputs, version):
        self.outputs, self.version, self.calls = outputs, version, []

    def complete(self, request):
        self.calls.append(request)
        stage = next(s for s in self.outputs if request["messages"][0]["content"] ==
                     prompt(s, self.version, "livecodebench"))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.outputs[stage])}}]}


def empty_repair():
    return {"additions": [], "removals": [], "reason": "Only correct the previously missing real dependency."}


def failed_parent(folder):
    source, execution, _, outputs = setup(folder)
    parent = folder / "v2"
    prepare(source, execution, execution / "input-manifest.json", parent,
            prompt_version="calibri-lcb-normalize-v2")
    bad = copy.deepcopy(outputs)
    bad["dependencies"]["answer_parents"] = [1]
    client = Client(bad, "calibri-lcb-normalize-v2")
    result = CALIBRIPipeline(parent, config(prompt_version=client.version), client).run()
    assert result["results"][0]["stage"] == "dependencies"
    write_once(parent / "completion.json", {"status": "processed"})
    return parent, outputs


def repair_outputs(outputs):
    audit = copy.deepcopy(outputs["review_dag"])
    audit["checks"].update(dict.fromkeys(REPAIR_CHECKS, True))
    return {"repair": empty_repair(), "dependencies": outputs["dependencies"], "review_dag": audit}


class RepairContractTests(unittest.TestCase):
    def test_add_remove_and_id_mapping_without_rewriting(self):
        item, proposal, _ = fixture()
        proposal["steps"].append({**proposal["steps"][1], "statement": "This is also an identity transformation."})
        old = normalize(proposal, item)
        value = {"additions": [{"before_node_id": 2, "statement": "The input must be returned.",
                               "source_refs": ["Q0001"], "reason": "Explicit task condition."}],
                 "removals": [{"node_id": 3, "reason": "Redundant restatement of the general identity claim."}],
                 "reason": "Synthetic source-binding test, not a semantic acceptance."}
        new, record = apply_repair(value, old, item)
        self.assertEqual([n["node_id"] for n in new["nodes"]], [1, 2, 3])
        self.assertEqual(record["node_mapping"], [{"old_node_id": 1, "new_node_id": 1},
            {"old_node_id": 2, "new_node_id": 3}, {"old_node_id": 3, "new_node_id": None}])
        self.assertEqual({k: new["nodes"][2][k] for k in STEP_FIELDS}, proposal["steps"][1])
        self.assertEqual(new["nodes"][1]["source_quote"], item["question"])
        self.assertEqual(record["code_sha256"], digest(item["reference_code"]))
        self.assertEqual(len(new["omissions"]), 1)
        self.assertEqual(len(old["nodes"]), 3)

    def test_forbidden_edits_and_source_fabrication_fail(self):
        item, proposal, _ = fixture()
        old = normalize(proposal, item)
        base = {"before_node_id": 2, "statement": "Input must be returned.",
                "source_refs": ["Q0001"], "reason": "A question premise."}
        cases = [{**empty_repair(), "reference_code": "changed"},
                 {**empty_repair(), "steps": []},
                 {**empty_repair(), "removals": [{"node_id": 999, "reason": "invented"}]},
                 {**empty_repair(), "removals": [{"node_id": 1, "reason": "x"}] * 2},
                 {**empty_repair(), "removals": [{"node_id": n, "reason": "x"} for n in (1, 2)]}]
        cases += [{**empty_repair(), "additions": [{**base, **change}]} for change in (
            {"source_refs": ["C0001"]}, {"source_refs": ["O0001"]}, {"source_refs": ["Q9999"]},
            {"before_node_id": True}, {"before_node_id": 99}, {"kind": "derived"},
            {"source_refs": ["Q0001", "Q0001"]}, {"statement": "By step 1 the claim follows."})]
        cases.append({**empty_repair(), "additions": [base] * 9})
        cases.append({**empty_repair(), "additions": [base], "removals": [{"node_id": 2, "reason": "x"}]})
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply_repair(value, old, item)

    def test_source_or_old_node_tampering_rejected(self):
        item, proposal, _ = fixture()
        old = normalize(proposal, item)
        changed = copy.deepcopy(old)
        changed["nodes"][0]["source_quote"] = "invented"
        with self.assertRaises(ValueError):
            apply_repair(empty_repair(), changed, item)
        with self.assertRaises(ValueError):
            apply_repair(empty_repair(), old, {**item, "reference_code": "changed"})

    def test_audit_checks_are_mandatory_not_truthy(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, _, outputs = setup(Path(folder).resolve())
            good = repair_outputs(outputs)["review_dag"]
            validate_repair_audit(good)
            for value in (False, None, 1):
                bad = copy.deepcopy(good)
                bad["checks"][REPAIR_CHECKS[0]] = value
                with self.subTest(value=value), self.assertRaises(ValueError):
                    validate_repair_audit(bad)
            del good["checks"][REPAIR_CHECKS[0]]
            with self.assertRaises(ValueError):
                validate_repair_audit(good)


class RepairPipelineTests(unittest.TestCase):
    def test_repair_is_three_calls_idempotent_and_not_a_release(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            parent, outputs = failed_parent(folder)
            run = folder / "repair"
            selection = prepare_repair(parent, run)
            self.assertEqual(selection["selected_count"], 1)
            client = Client(repair_outputs(outputs), PROTOCOL)
            for _ in range(2):
                result = CALIBRIRepairPipeline(run, config(prompt_version=PROTOCOL, max_calls=9), client).run()
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            self.assertNotIn("never send this", json.dumps(client.calls))
            self.assertNotIn("tests_sha256", json.dumps(client.calls))
            dag = read_json(run / "items" / ("a" * 20) / "dag.json")
            self.assertFalse(dag["formal_eligible"])
            self.assertTrue(dag["nodes"][-1]["excluded_from_pals"])
            self.assertEqual(dag["nodes"][-1]["statement"], dag["source"]["reference_code"])
            self.assertEqual(read_json(parent / "items" / ("a" * 20) / "result.json")["status"], "needs_review")
            self.assertEqual(read_json(run / "calibri-normalization-manifest.json")["prior_calls"], 2)

    def test_another_structural_failure_stops_without_second_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            parent, outputs = failed_parent(folder)
            run = folder / "repair"
            prepare_repair(parent, run)
            bad = repair_outputs(outputs)
            bad["dependencies"]["answer_parents"] = [1]
            client = Client(bad, PROTOCOL)
            pipeline = CALIBRIRepairPipeline(run, config(prompt_version=PROTOCOL, max_calls=9), client)
            self.assertEqual(pipeline.run()["results"][0]["status"], "needs_review")
            pipeline.run()
            self.assertEqual(len(client.calls), 2)
            with self.assertRaisesRegex(ValueError, "one repair pass"):
                prepare_repair(run, folder / "forbidden-second-pass")

    def test_semantic_failure_does_not_publish_or_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            parent, outputs = failed_parent(folder)
            run = folder / "repair"
            prepare_repair(parent, run)
            bad = repair_outputs(outputs)
            bad["review_dag"].update(decision="reject", issues=["Missing premise."], reason="Missing premise.")
            bad["review_dag"]["checks"][REPAIR_CHECKS[-1]] = False
            client = Client(bad, PROTOCOL)
            pipeline = CALIBRIRepairPipeline(run, config(prompt_version=PROTOCOL, max_calls=9), client)
            self.assertEqual(pipeline.run()["results"][0]["status"], "rejected")
            pipeline.run()
            self.assertEqual(len(client.calls), 3)
            self.assertFalse(list(run.glob("items/*/dag.json")))

    def test_tampering_fails_before_requests(self):
        for name in ("repair-seeds/" + "a" * 20 + ".json", "parent-evidence/completion.json", "items.json"):
            with tempfile.TemporaryDirectory() as folder:
                folder = Path(folder).resolve()
                parent, outputs = failed_parent(folder)
                run = folder / "repair"
                prepare_repair(parent, run)
                path = run / name
                value = read_json(path)
                if isinstance(value, list):
                    value[0]["reference_code"] += "# changed"
                else:
                    value["tampered"] = True
                path.write_text(json.dumps(value))
                client = Client(repair_outputs(outputs), PROTOCOL)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    CALIBRIRepairPipeline(run, config(prompt_version=PROTOCOL, max_calls=9), client).run()
                self.assertFalse(client.calls)

    def test_budget_cap_and_parent_failure_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            parent, _ = failed_parent(folder)
            with self.assertRaisesRegex(ValueError, "allocation"):
                prepare_repair(parent, folder / "overbudget", max_calls=600)
            path = parent / "items" / ("a" * 20) / "result.json"
            value = read_json(path)
            value["reason"] = "an invented failure"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "no longer reproduces"):
                prepare_repair(parent, folder / "tampered-parent")


if __name__ == "__main__":
    unittest.main()
