"""CPU-only flow updates never manufacture DAG acceptances."""

import unittest

from dag_builder.livecodebench_cpu53_export import merge_cpu_rows


class CPU53ExportTests(unittest.TestCase):
    def test_only_held_t2ance_rows_change(self):
        held = {"item_id": "a", "question_id": "q1", "source_tier": "t2ance",
                "cpu_status": "not_submitted", "status": "held_without_cpu",
                "reason": "not tested"}
        accepted = {"item_id": "b", "question_id": "q2", "source_tier": "CALIBRI",
                    "cpu_status": "passed", "status": "model_accepted", "reason": "audited"}
        updated = merge_cpu_rows([held, accepted], {"a": ({"item_id": "a"},
                                                      {"status": "passed"})})
        self.assertEqual(updated[0]["status"], "dag_not_run")
        self.assertEqual(updated[0]["cpu_status"], "passed")
        self.assertEqual(updated[1], accepted)
        self.assertEqual(held["status"], "held_without_cpu")
        with self.assertRaises(ValueError):
            merge_cpu_rows([accepted], {"b": ({"item_id": "b"}, {"status": "passed"})})


if __name__ == "__main__":
    unittest.main()
