import base64
import json
import pickle
import unittest
import zlib
from unittest.mock import patch

from dag_builder.livecodebench_tests import decode_tests, private_tests


def encoded(value):
    return base64.b64encode(zlib.compress(pickle.dumps(json.dumps(value)))).decode()


class TestDecoding(unittest.TestCase):
    def setUp(self):
        self.cases = [{"input": "7\n3", "output": "10", "testtype": "functional"}]

    def test_only_primitive_string_pickle_is_allowed(self):
        self.assertEqual(private_tests(encoded(self.cases)), self.cases)
        self.assertEqual(private_tests(json.dumps(self.cases)), self.cases)
        for raw in (pickle.dumps(self.cases), pickle.dumps(eval), b"cos\nsystem\n(S'echo BAD'\ntR."):
            payload = base64.b64encode(zlib.compress(raw)).decode()
            with self.assertRaises(ValueError):
                private_tests(payload)

    def test_bounded_decompression_and_trailing_data(self):
        with patch("dag_builder.livecodebench_tests.MAX_BYTES", 200), self.assertRaises(ValueError):
            private_tests(encoded(["a" * 1000]))
        raw = pickle.dumps(json.dumps(self.cases)) + b"garbage"
        with self.assertRaises(ValueError):
            private_tests(base64.b64encode(zlib.compress(raw)).decode())

    def test_public_and_hidden_arguments(self):
        bundle = {"public_test_cases": json.dumps(self.cases), "private_test_cases": encoded(self.cases)}
        tests = decode_tests(bundle, "functional")
        self.assertEqual([t["split"] for t in tests], ["public", "private"])
        self.assertEqual(tests[1]["inputs"], [7, 3])
        self.assertEqual(tests[1]["expected"], 10)
        with self.assertRaises(ValueError):
            decode_tests(bundle, "stdin")

    def test_no_hidden_cases_cannot_pass(self):
        with self.assertRaises(ValueError):
            private_tests("[]")


if __name__ == "__main__":
    unittest.main()
