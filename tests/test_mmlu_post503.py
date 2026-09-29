"""Post-503 continuation does not silently erase exhausted transport items."""

import tempfile
import unittest
from pathlib import Path

from dag_builder.storage import write_once
from scripts.run_mmlu_after_503 import law_pauses_are_only_known_exhausted


class Post503ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.known = {"a" * 20: "solve", "b" * 20: "review_dag"}
        write_once(self.root / "post503_continuation_provenance.json",
                   {"law_exhausted_503": self.known})

    def invocation(self, rows, *, global_stop=False):
        write_once(self.root / "subjects/professional_law/invocations/test.json",
                   {"ended_at": "2026-09-29T10:00:00+00:00",
                    "results": rows, "global_stop": global_stop})

    def test_exact_exhausted_set_can_remain_unresolved(self):
        self.invocation([{"item_id": item, "stage": stage, "status": "paused",
                          "reason": "transient_retries_exhausted"}
                         for item, stage in self.known.items()])
        self.assertTrue(law_pauses_are_only_known_exhausted(self.root))

    def test_new_paused_case_is_not_silently_accepted(self):
        self.invocation([{"item_id": item, "stage": stage, "status": "paused",
                          "reason": "transient_retries_exhausted"}
                         for item, stage in self.known.items()] +
                        [{"item_id": "c" * 20, "stage": "solve", "status": "paused",
                          "reason": "transient_retries_exhausted"}])
        self.assertFalse(law_pauses_are_only_known_exhausted(self.root))

    def test_global_stop_is_not_equivalent_to_known_failures(self):
        self.invocation([{"item_id": item, "stage": stage, "status": "paused",
                          "reason": "transient_retries_exhausted"}
                         for item, stage in self.known.items()], global_stop=True)
        self.assertFalse(law_pauses_are_only_known_exhausted(self.root))


if __name__ == "__main__":
    unittest.main()
