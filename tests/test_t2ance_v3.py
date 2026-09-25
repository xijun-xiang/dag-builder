"""Versioned v3 graph transforms are replayable and never semantic proof."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.calibri_normalize import source_units
from dag_builder.config import Config
from dag_builder.stages import prompt, stages_for
from dag_builder.storage import read_json, write_once
from dag_builder.t2ance_audit import audit_completed
from dag_builder.t2ance_pipeline import T2ancePipeline, prepare, verify_prepared
from dag_builder.livecodebench_dag import sha256
from dag_builder.t2ance_v3 import (
    EXTRA_REVIEW_CHECKS, PROTOCOL, assemble_graph_v3, normalize_v3,
    validate_audit_v3,
)
from test_t2ance_pipeline import Client, prepared_fixture


def graph_case():
    item = {"task_type": "livecodebench", "question": "Return the supplied input.\n",
            "io_type": "stdin", "entry_point": None, "starter_code": "",
            "raw_output": "Copy the input unchanged.\nThis satisfies the identity task.\n",
            "reference_code": "print(input())\n",
            "reference_origin": "t2ance_model_output"}
    self_anchors = {u["unit_id"] for u in source_units(item)}
    assert {"Q0001", "O0001", "O0002", "C0001"} <= self_anchors
    proposal = {"steps": [
        {"kind": "given", "statement": "The output must match the supplied input.",
         "source_refs": ["Q0001"], "support_type": "source_supported",
         "normalization_note": "Operative question premise."},
        {"kind": "derived", "statement": "Copying the supplied value preserves it.",
         "source_refs": ["O0001"], "support_type": "source_supported",
         "normalization_note": "Algorithm operation."},
        {"kind": "derived", "statement": "The copied value meets the identity requirement.",
         "source_refs": ["O0002"], "support_type": "source_supported",
         "normalization_note": "Correctness conclusion."},
        {"kind": "given", "statement": "The program prints one line.",
         "source_refs": ["C0001"], "support_type": "source_supported",
         "normalization_note": "Incidental operational observation."}], "omissions": []}
    dependencies = {"dependencies": [
        {"node_id": 1, "parents": [], "justification": "Explicit task requirement."},
        {"node_id": 2, "parents": [1], "justification": "Source operation preserves the input."},
        {"node_id": 3, "parents": [1, 2], "justification": "The operation meets the task."},
        {"node_id": 4, "parents": [], "justification": "Not needed in the answer proof."}],
        "answer_parents": [2, 3],
        "answer_justification": "The program implements the identity operation."}
    return item, proposal, dependencies


class T2anceV3Tests(unittest.TestCase):
    def test_answer_backward_selection_and_only_transitive_reduction(self):
        item, proposal, dependencies = graph_case()
        normalized = normalize_v3(proposal, item)
        graph = assemble_graph_v3(dependencies, normalized, item)
        self.assertEqual([n["original_node_id"] for n in graph["nodes"]], [1, 2, 3, 5])
        self.assertEqual([n["parents"] for n in graph["nodes"]], [[], [1], [2], [3]])
        transformation = graph["transformation"]
        self.assertEqual(transformation["selected_original_ids"], [1, 2, 3])
        self.assertEqual(transformation["question_root_original_ids"], [1])
        self.assertEqual([d["original_node_id"] for d in transformation["discarded_nodes"]], [4])
        self.assertEqual({(e["child_original_id"], e["parent_original_id"],
                           e["via_original_id"]) for e in transformation["removed_transitive_edges"]},
                         {(3, 1, 2), (5, 2, 3)})
        self.assertEqual(graph["nodes"][-1]["statement"], item["reference_code"])
        self.assertEqual(normalized["nodes"][3]["statement"],
                         transformation["discarded_nodes"][0]["statement"])

    def test_missing_premise_and_question_root_remain_failures(self):
        item, proposal, dependencies = graph_case()
        normalized = normalize_v3(proposal, item)
        missing = copy.deepcopy(dependencies)
        missing["dependencies"][1]["parents"] = []
        with self.assertRaisesRegex(ValueError, "missing premise"):
            assemble_graph_v3(missing, normalized, item)
        no_question_root = copy.deepcopy(dependencies)
        no_question_root["answer_parents"] = [4]
        with self.assertRaisesRegex(ValueError, "question-root"):
            assemble_graph_v3(no_question_root, normalized, item)
        invalid_source = copy.deepcopy(proposal)
        invalid_source["steps"][2]["source_refs"] = ["O9999"]
        with self.assertRaisesRegex(ValueError, "unknown or duplicate source unit"):
            normalize_v3(invalid_source, item)

    def test_fresh_review_checks_are_mandatory_for_acceptance(self):
        from dag_builder.calibri_normalize import REVIEW_CHECKS
        from dag_builder.schemas import REVIEW_CHECKS as COMMON_CHECKS
        checks = dict.fromkeys((*COMMON_CHECKS["review_dag"], *REVIEW_CHECKS,
                                *EXTRA_REVIEW_CHECKS), True)
        review = {"decision": "accept", "checks": checks, "issues": [],
                  "reason": "Synthetic review of each claim and transformation."}
        validate_audit_v3(review)
        for key in EXTRA_REVIEW_CHECKS:
            bad = copy.deepcopy(review)
            bad["checks"][key] = None
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,
                                                                 "not semantically accepted"):
                validate_audit_v3(bad)

    def test_pipeline_and_offline_audit_replay_v3(self):
        with tempfile.TemporaryDirectory() as folder:
            run, outputs = prepared_fixture(Path(folder).resolve(), prompt_version=PROTOCOL)
            for key in EXTRA_REVIEW_CHECKS:
                outputs["review_dag"]["checks"][key] = True
            config = Config(task_type="livecodebench", prompt_version=PROTOCOL,
                            solution_source="t2ance_reference_normalization", workers=1,
                            max_calls=24, max_reserved_tokens=2000000)
            self.assertEqual(stages_for(config), ("normalize", "dependencies", "review_dag"))
            self.assertIn("answer", prompt("normalize", PROTOCOL, "livecodebench"))
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
            directory = run / "items" / ("a" * 20)
            graph = read_json(directory / "dag.json")
            self.assertEqual(graph["construction_protocol"], PROTOCOL)
            self.assertFalse(graph["formal_eligible"])
            self.assertEqual(graph["graph_transformation"]["selected_original_ids"], [1, 2])
            dag_path, result_path = directory / "dag.json", directory / "result.json"
            old_dag, old_result = dag_path.read_text(), result_path.read_text()
            graph["graph_transformation"]["selected_original_ids"] = [2]
            dag_path.write_text(json.dumps(graph))
            changed_result = read_json(result_path)
            from dag_builder.storage import digest
            changed_result["dag_sha256"] = digest(graph)
            result_path.write_text(json.dumps(changed_result))
            with self.assertRaises(ValueError):
                audit_completed(run)
            dag_path.write_text(old_dag)
            result_path.write_text(old_result)

    def test_semantic_counterexample_exclusion_is_code_bound_and_fail_closed(self):
        from dag_builder.storage import digest
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            prepared_fixture(root, prompt_version=PROTOCOL)
            item = read_json(root / "source/items.json")[0]
            exclusion = {"protocol": "t2ance-semantic-quarantine-v1", "records": [{
                "item_id": item["item_id"],
                "reference_code_sha256": digest(item["reference_code"]),
                "counterexample": {"input": "0\n", "expected": "No", "observed": "Yes"},
                "reason": "Independent counterexample to the tested program."}]}
            manifest = root / "semantic-quarantine.json"
            write_once(manifest, exclusion)
            arguments = (root / "source", root / "execution",
                         root / "execution/input-manifest.json", root / "quarantined-run")
            with self.assertRaisesRegex(ValueError, "no independent CPU-passing"):
                prepare(*arguments, prompt_version=PROTOCOL, semantic_quarantine=manifest)
            exclusion["records"][0]["reference_code_sha256"] = "0" * 64
            bad = root / "invalid-quarantine.json"
            write_once(bad, exclusion)
            with self.assertRaisesRegex(ValueError, "not bound to the frozen code"):
                prepare(root / "source", root / "execution",
                        root / "execution/input-manifest.json", root / "invalid-run",
                        prompt_version=PROTOCOL, semantic_quarantine=bad)

    def test_semantic_quarantine_preserves_another_cpu_passed_candidate(self):
        from dag_builder.storage import digest
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            prepared_fixture(root, prompt_version=PROTOCOL)
            source, execution = root / "source", root / "execution"
            first = read_json(source / "items.json")[0]
            second = copy.deepcopy(first)
            second["item_id"] = "d" * 20
            items = [first, second]
            (source / "items.json").write_text(json.dumps(items))
            source_manifest = read_json(source / "t2ance-manifest.json")
            source_manifest["items_sha256"] = digest(items)
            (source / "t2ance-manifest.json").write_text(json.dumps(source_manifest))
            write_once(source / "source-rows" / (second["item_id"] + ".json"),
                       read_json(source / "source-rows" / (first["item_id"] + ".json")))
            original = read_json(execution / "execution-input.json")[0]
            cloned = copy.deepcopy(original)
            cloned["item_id"] = second["item_id"]
            inputs = [original, cloned]
            (execution / "execution-input.json").write_text(json.dumps(inputs))
            result = read_json(execution / "results" / (first["item_id"] + ".json"))
            result["item_id"] = second["item_id"]
            second_result = execution / "results" / (second["item_id"] + ".json")
            write_once(second_result, result)
            cpu_manifest = read_json(execution / "input-manifest.json")
            cpu_manifest.update(planned=2, inputs_sha256=digest(inputs),
                t2ance_manifest_sha256=digest(source_manifest), source_candidates=2,
                sample_ids=[first["item_id"], second["item_id"]])
            (execution / "input-manifest.json").write_text(json.dumps(cpu_manifest))
            completion = read_json(execution / "completion.json")
            completion.update(executed=2, passed=2,
                input_manifest_sha256=sha256(execution / "input-manifest.json"))
            completion["results"][second_result.name] = sha256(second_result)
            (execution / "completion.json").write_text(json.dumps(completion))
            quarantine = {"protocol": "t2ance-semantic-quarantine-v1", "records": [{
                "item_id": first["item_id"],
                "reference_code_sha256": digest(first["reference_code"]),
                "counterexample": {"input": "0\n", "expected": "No", "observed": "Yes"},
                "reason": "Independent counterexample."}]}
            quarantine_path = root / "quarantine.json"
            write_once(quarantine_path, quarantine)
            run = root / "quarantined-v3"
            selection = prepare(source, execution, execution / "input-manifest.json", run,
                                prompt_version=PROTOCOL, semantic_quarantine=quarantine_path)
            self.assertEqual(selection["cpu_passed"], 2)
            self.assertEqual(selection["selected_ids"], [second["item_id"]])
            self.assertIn({"item_id": first["item_id"],
                           "reason": "independent_semantic_counterexample"},
                          selection["excluded"])
            config = Config(task_type="livecodebench", prompt_version=PROTOCOL,
                            solution_source="t2ance_reference_normalization", workers=1,
                            max_calls=24, max_reserved_tokens=2000000)
            self.assertEqual([i["item_id"] for i in verify_prepared(run, config)],
                             [second["item_id"]])
            frozen = run / "evidence/quarantine-results" / (first["item_id"] + ".json")
            corrupted = read_json(frozen)
            corrupted["status"] = "wrong_answer"
            frozen.write_text(json.dumps(corrupted))
            with self.assertRaises(ValueError):
                verify_prepared(run, config)

    def test_terminal_replay_rejects_forged_verdict_and_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            run, outputs = prepared_fixture(Path(folder).resolve(), prompt_version=PROTOCOL)
            for key in EXTRA_REVIEW_CHECKS:
                outputs["review_dag"]["checks"][key] = False
            outputs["review_dag"].update(decision="reject", issues=["Wrong boundary."],
                                          reason="Wrong boundary.")
            config = Config(task_type="livecodebench", prompt_version=PROTOCOL,
                            solution_source="t2ance_reference_normalization", workers=1,
                            max_calls=24, max_reserved_tokens=2000000)
            write_once(run / "config.json", config.to_dict())
            snapshot = {"source_files": {}, "code_sha256": "synthetic"}
            write_once(run / "code_origin.json", snapshot)
            write_once(run / "controller/code/snapshot_origin.json", snapshot)
            result = T2ancePipeline(run, config, Client(outputs, PROTOCOL)).run()
            self.assertEqual(result["results"][0]["status"], "rejected")
            completion = {"status": "processed", "global_stop": False,
                "summary": {"selected": 1, "api_attempts": result["attempt_count"],
                            "counts": {"rejected": 1}}}
            completion_path = run / "completion.json"
            write_once(completion_path, completion)
            self.assertTrue(audit_completed(run)["mechanical_pass"])
            result_path = run / "items" / ("a" * 20) / "result.json"
            original = result_path.read_text()
            forged = read_json(result_path)
            forged["status"] = "needs_review"
            result_path.write_text(json.dumps(forged))
            completion["summary"]["counts"] = {"needs_review": 1}
            completion_path.write_text(json.dumps(completion))
            with self.assertRaisesRegex(ValueError, "verdict changed"):
                audit_completed(run)
            result_path.write_text(original)
            completion["summary"]["counts"] = {"rejected": 1}
            completion_path.write_text(json.dumps(completion))
            forged = read_json(result_path)
            forged["reason"] = "Forged failure reason"
            result_path.write_text(json.dumps(forged))
            with self.assertRaisesRegex(ValueError, "reason changed"):
                audit_completed(run)

    def test_terminal_replay_binds_structural_failure_stage_to_raw_content(self):
        with tempfile.TemporaryDirectory() as folder:
            run, outputs = prepared_fixture(Path(folder).resolve(), prompt_version=PROTOCOL)
            outputs["normalize"]["steps"][1]["source_refs"] = ["O9999"]
            config = Config(task_type="livecodebench", prompt_version=PROTOCOL,
                            solution_source="t2ance_reference_normalization", workers=1,
                            max_calls=24, max_reserved_tokens=2000000)
            write_once(run / "config.json", config.to_dict())
            snapshot = {"source_files": {}, "code_sha256": "synthetic"}
            write_once(run / "code_origin.json", snapshot)
            write_once(run / "controller/code/snapshot_origin.json", snapshot)
            result = T2ancePipeline(run, config, Client(outputs, PROTOCOL)).run()
            self.assertEqual((result["results"][0]["status"], result["results"][0]["stage"]),
                             ("needs_review", "normalize"))
            write_once(run / "completion.json", {"status": "processed", "global_stop": False,
                "summary": {"selected": 1, "api_attempts": result["attempt_count"],
                            "counts": {"needs_review": 1}}})
            self.assertTrue(audit_completed(run)["mechanical_pass"])
            result_path = run / "items" / ("a" * 20) / "result.json"
            original = result_path.read_text()
            forged = read_json(result_path)
            forged["stage"] = "dependencies"
            result_path.write_text(json.dumps(forged))
            with self.assertRaises(ValueError):
                audit_completed(run)
            result_path.write_text(original)
            response = run / "items" / ("a" * 20) / "normalize/attempt-00/response.json"
            changed = read_json(response)
            raw = json.loads(changed["body"]["choices"][0]["message"]["content"])
            raw["steps"][1]["source_refs"] = ["O0001"]
            changed["body"]["choices"][0]["message"]["content"] = json.dumps(raw)
            response.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                audit_completed(run)


if __name__ == "__main__":
    unittest.main()
