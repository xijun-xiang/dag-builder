"""Offline tests for preserving uncertain attempts across an interrupted run."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_mmlu_interrupted_continuation import attempts  # noqa: E402


class InterruptedContinuationTests(unittest.TestCase):
    def test_attempts_charge_every_request_and_classify_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "subjects/s/items/abc/solve"
            for index, terminal in enumerate(("response", "error", None)):
                attempt = base / f"attempt-{index:02d}"
                attempt.mkdir(parents=True)
                (attempt / "request.json").write_text(json.dumps({
                    "reserved_tokens": index + 10,
                }))
                if terminal:
                    (attempt / f"{terminal}.json").write_text("{}")
            requests, responses, errors, unknown, reserved = attempts(root)
            self.assertEqual((len(requests), len(responses), len(errors),
                              len(unknown), reserved), (3, 1, 1, 1, 33))
            self.assertIn("attempt-02/request.json", unknown[0])

    def test_attempts_reject_conflicting_terminal_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "subjects/s/items/abc/solve/attempt-00"
            attempt.mkdir(parents=True)
            (attempt / "request.json").write_text('{"reserved_tokens": 10}')
            (attempt / "response.json").write_text("{}")
            (attempt / "error.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "both present"):
                attempts(root)


if __name__ == "__main__":
    unittest.main()
