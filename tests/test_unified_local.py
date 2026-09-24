import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.storage import digest, write_once
from dag_builder.unified import export_roots, validate_row


class UnifiedLocalTest(unittest.TestCase):
    def test_exports_accepted_gsm8k(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            parent = Path(temporary)
            root = parent / "run"
            root.mkdir(mode=0o700)
            item = {
                "dataset": "openai/gsm8k",
                "revision": "a" * 40,
                "config": "main",
                "split": "test",
                "row": 0,
                "task_type": "gsm8k",
                "item_id": "1" * 20,
                "question": "What is one plus one?",
                "gold_answer": "2",
                "raw_answer": "1 + 1 = 2.\n#### 2",
                "source_content_sha256": "b" * 64,
            }
            nodes = [
                {
                    "node_id": 1,
                    "kind": "given",
                    "statement": "The expression is one plus one.",
                    "parents": [],
                    "source_field": "question",
                    "source_quote": "one plus one",
                    "justification": "Quoted from the question.",
                },
                {
                    "node_id": 2,
                    "kind": "answer",
                    "statement": "One plus one is 2.",
                    "parents": [1],
                    "source_field": "solution",
                    "source_quote": "1 + 1 = 2",
                    "justification": "Adding one and one gives two.",
                },
            ]
            dag = {
                "schema_version": "reference_dag_v1",
                "item_id": item["item_id"],
                "source": item,
                "reference_solution": {"answer": "2", "rationale": "1 + 1 = 2."},
                "nodes": nodes,
                "quality_status": "model_reviewed_pending_human_review",
            }
            result = {
                "item_id": item["item_id"],
                "status": "model_accepted",
                "stage": "review_dag",
                "reason": "accepted",
                "dag_sha256": digest(dag),
            }
            write_once(root / "items.json", [item])
            write_once(root / "items" / item["item_id"] / "dag.json", dag)
            write_once(root / "items" / item["item_id"] / "result.json", result)
            output = parent / "delivery"
            manifest = export_roots([root], "gsm8k", output)
            self.assertEqual(manifest["records"], 1)
            row = json.loads(
                (output / "pals_dag_unified_v1.jsonl").read_text().strip()
            )
            validate_row(row)
            self.assertEqual(row["answer"], {"kind": "text", "value": "2"})
            self.assertEqual(row["dag"]["nodes_sha256"], digest(nodes))


if __name__ == "__main__":
    unittest.main()
