from pathlib import Path
import tempfile
import unittest

from pals_validation.context_audit import audit_prepared
from pals_validation.io import save, sha256
from pals_validation.backend import HFBackend


class Tokenizer:
    def apply_chat_template(self, messages, **kwargs):
        return str(messages) + "<assistant>"
    def encode(self, text, **kwargs):
        return list(text.encode())


class ContextTests(unittest.TestCase):
    def test_counts_both_scoring_conditions_and_original_generation_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = {"item_id":"1", "task_type":"gsm8k", "question":"Compute this.",
                    "steps":[{"node_id":"a", "statement":"First."}, {"node_id":"b", "statement":"Next."}]}
            common = {"item_id":"1", "prefix_ids":["a"], "deleted_id":"a"}
            save(root / "cases.json", [case])
            save(root / "jobs.json", [{**common, "kind":"e1", "job_id":"e1", "variant":"original", "target":"Next."},
                                      {**common, "kind":"e2", "job_id":"e2"}])
            save(root / "manifest.json", {"files":{n:sha256(root/n) for n in ("cases.json", "jobs.json")}})
            config = {"max_context":8192, "max_new_tokens":4096, "chat_template_kwargs":{}}
            result = audit_prepared(Tokenizer(), root, config)
            self.assertEqual(result["status"], "PASS")
            before = {p.name:sha256(p) for p in root.iterdir()}
            result = audit_prepared(Tokenizer(), root, {**config, "max_context":1})
            self.assertEqual(result["status"], "CAPACITY_REVIEW_REQUIRED")
            for kind in ("e1", "e2"):
                self.assertEqual(result["arms"][kind]["planned_jobs"], 1)
                self.assertEqual(len(result["arms"][kind]["violations"]), 1)
            self.assertEqual(result["arms"]["e2"]["violations"][0]["reserved_output"], 4096)
            self.assertEqual(before, {p.name:sha256(p) for p in root.iterdir()})


if __name__ == "__main__":
    unittest.main()
