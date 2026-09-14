"""Synthetic fixtures only; no GPQA benchmark text or paid API calls."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from dag_builder.config import Config
from dag_builder.gpqa_source import (
    FIELDS,
    normalize_gpqa,
    select_gpqa,
    select_gpqa_extension,
)
from dag_builder.pipeline import Pipeline
from dag_builder.report import render
from dag_builder.schemas import InvalidOutput
from dag_builder.stages import (
    REFERENCE_STAGES,
    payload,
    prompt,
    stage_input,
    stages_for,
    validate,
)
from dag_builder.storage import read_json, write_once
from test_builder import OUTPUTS, QUESTION, RATIONALE


def fixture(record="fixture-1", domain="Physics"):
    return {
        "Record ID": record,
        "High-level domain": domain,
        "Question": QUESTION,
        "Explanation": RATIONALE,
        "Correct Answer": "3 m/s",
        "Incorrect Answer 1": "2 m/s",
        "Incorrect Answer 2": "6 m/s",
        "Incorrect Answer 3": "12 m/s",
    }


def config():
    return Config(task_type="gpqa", prompt_version="gpqa-reference-v1", workers=1)


class ReferenceClient:
    def __init__(self, item):
        self.calls = []
        self.outputs = deepcopy(OUTPUTS)
        answer = self.outputs["atomize"]["nodes"][-1]
        answer.update(
            statement="The selected answer is " + item["gold_answer"] + ": 3 m/s.",
            source_field="correct_answer",
            source_quote="3 m/s",
        )

    def complete(self, request):
        self.calls.append(request)
        stage = next(
            s
            for s in REFERENCE_STAGES
            if request["messages"][0]["content"]
            == prompt(s, "gpqa-reference-v1", "gpqa")
        )
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(self.outputs[stage])},
                }
            ],
            "usage": {"total_tokens": 100},
        }


class GPQATests(unittest.TestCase):
    def test_extension_fifty_no_overlap_and_proportional_allocation(self):
        rows = []
        for domain, size in (("Physics", 40), ("Chemistry", 50), ("Biology", 12)):
            for index in range(size):
                row = fixture(f"{domain}-{index}", domain)
                row["Question"] += f" Unique synthetic source {domain} {index}."
                rows.append(row)
        items = normalize_gpqa(rows)
        prior, _ = select_gpqa(items, 30)
        chosen, manifest = select_gpqa_extension(items, prior, 50)
        self.assertEqual(len(chosen), 50)
        self.assertFalse({i["item_id"] for i in prior} & {i["item_id"] for i in chosen})
        self.assertEqual(
            manifest["domain_counts"], {"Biology": 1, "Chemistry": 28, "Physics": 21}
        )
        self.assertEqual((chosen, manifest), select_gpqa_extension(items, prior, 50))
        with self.assertRaisesRegex(InvalidOutput, "insufficient"):
            select_gpqa_extension(items, prior, 100)
        changed = deepcopy(prior)
        changed[0]["official_explanation"] += " altered"
        with self.assertRaisesRegex(InvalidOutput, "differs"):
            select_gpqa_extension(items, changed, 50)

    def test_complete_revision_bundle_used(self):
        row = fixture()
        for field in FIELDS:
            row["Extra Revised " + field] = row[field] + " revised"
        item = normalize_gpqa([row])[0]
        self.assertEqual(item["official_explanation"], RATIONALE + " revised")
        self.assertTrue(
            all(v.startswith("Extra Revised") for v in item["source_fields"].values())
        )
        self.assertEqual(
            item["choices"]["ABCD".index(item["gold_answer"])], "3 m/s revised"
        )

    def test_partial_revision_not_silently_mixed(self):
        row = fixture()
        row["Extra Revised Explanation"] = "A different version."
        with self.assertRaisesRegex(InvalidOutput, "incomplete"):
            normalize_gpqa([row])

    def test_duplicate_id_and_empty_explanation_fail(self):
        with self.assertRaises(InvalidOutput):
            normalize_gpqa([fixture(), fixture()])
        row = fixture()
        row["Explanation"] = " "
        with self.assertRaises(InvalidOutput):
            normalize_gpqa([row])

    def test_balanced_selection_repeatable_and_choice_mapping(self):
        rows = []
        for domain in ("Physics", "Biology", "Chemistry"):
            for index in range(3):
                row = fixture(f"{domain}-{index}", domain)
                row["Question"] += f" Fixture {index}."
                rows.append(row)
        items = normalize_gpqa(rows)
        first = select_gpqa(items, 6)
        self.assertEqual(first, select_gpqa(items, 6))
        self.assertEqual(
            first[1]["domain_counts"], {"Biology": 2, "Chemistry": 2, "Physics": 2}
        )
        for item in items:
            self.assertEqual(
                item["choices"]["ABCD".index(item["gold_answer"])], "3 m/s"
            )

    def test_reference_protocol_never_solves(self):
        item = normalize_gpqa([fixture()])[0]
        self.assertEqual(stages_for(config()), REFERENCE_STAGES)
        data = stage_input("review_solution", item, {})
        self.assertEqual(data["solution"]["rationale"], RATIONALE)
        self.assertEqual(data["solution"]["origin"], "official_expert_explanation")
        with self.assertRaises(InvalidOutput):
            stage_input("solve", item, {})
        with self.assertRaises(ValueError):
            prompt("solve", "gpqa-reference-v1", "gpqa")
        with self.assertRaises(ValueError):
            Config(task_type="gpqa")
        sent = json.loads(
            payload("review_solution", data, config())["messages"][1]["content"]
        )
        self.assertEqual(set(sent["question"]["choices"]), set("ABCD"))

    def test_duplicate_choice_record_preserved_but_excluded(self):
        row = fixture()
        row["Incorrect Answer 1"] = row["Correct Answer"]
        item = normalize_gpqa([row])[0]
        self.assertEqual(item["source_eligibility_issue"], "duplicate_choice_text")
        with self.assertRaisesRegex(InvalidOutput, "insufficient"):
            select_gpqa([item], 3)

    def test_answer_source_restricted_and_quotes_exact(self):
        item = normalize_gpqa([fixture()])[0]
        data = stage_input("atomize", item, {})
        output = ReferenceClient(item).outputs["atomize"]
        validate("atomize", output, data)
        output["nodes"][0].update(source_field="correct_answer", source_quote="3 m/s")
        with self.assertRaisesRegex(InvalidOutput, "reasoning premise"):
            validate("atomize", output, data)
        output = ReferenceClient(item).outputs["atomize"]
        output["nodes"][-1]["source_quote"] = "invented quote"
        with self.assertRaisesRegex(InvalidOutput, "verbatim"):
            validate("atomize", output, data)

    def test_five_stage_run_and_cached_resume(self):
        item = normalize_gpqa([fixture()])[0]
        client = ReferenceClient(item)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_once(root / "items.json", [item])
            write_once(root / "selection.json", {"selected_ids": [item["item_id"]]})
            for _ in range(2):
                result = Pipeline(root, config(), client).run()
                self.assertEqual(result["results"][0]["status"], "model_accepted")
            self.assertEqual(len(client.calls), 5)
            target = root / "items" / item["item_id"]
            self.assertFalse((target / "solve").exists())
            dag = read_json(target / "dag.json")
            self.assertEqual(dag["reference_solution"]["rationale"], RATIONALE)
            self.assertEqual(dag["construction_protocol"], "gpqa-reference-v1")
            self.assertIn("官方专家", (render(root) / "review.html").read_text())

    def test_detached_launcher_snapshot_and_worker_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts/run_private_pilot.py"
        spec = importlib.util.spec_from_file_location("private_pilot_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        item = normalize_gpqa([fixture()])[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_once(root / "items.json", [item])
            write_once(root / "selection.json", {"selected_ids": [item["item_id"]]})
            write_once(root / "input-config.json", config().to_dict())
            key = root / "fixture-key"
            key.write_text("non-secret-test-fixture")
            key.chmod(0o600)
            args = [
                str(script),
                "--root",
                str(root),
                "--config",
                str(root / "input-config.json"),
                "--key-file",
                str(key),
            ]
            # implementation() invokes subprocess too; compute it before mocking Popen.
            from dag_builder.pipeline import implementation

            origin = implementation()
            with (
                patch.object(sys, "argv", args),
                patch("subprocess.Popen") as child,
                patch("dag_builder.pipeline.implementation", return_value=origin),
            ):
                child.return_value.pid = 12345
                module.main()
            frozen = root / "controller/code"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json; from dag_builder.pipeline import implementation; print(json.dumps(implementation()))",
                ],
                env=dict(os.environ, PYTHONPATH=str(frozen)),
                capture_output=True,
                text=True,
                check=True,
                cwd=root,
            )
            self.assertEqual(json.loads(result.stdout), origin)
            with (
                patch.object(sys, "argv", args + ["--worker"]),
                patch(
                    "dag_builder.client.APIClient", return_value=ReferenceClient(item)
                ),
            ):
                module.main()
            self.assertEqual(read_json(root / "completion.json")["status"], "processed")
            self.assertFalse(list((root / "stops").glob("*.json")))
            # Do not leak test-only import paths to later tests.
            sys.path.remove(str(frozen))


if __name__ == "__main__":
    unittest.main()
