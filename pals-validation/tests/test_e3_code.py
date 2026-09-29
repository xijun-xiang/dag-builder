"""Pure code-harness contract tests; never execute generated code locally."""

import base64
import json
import pickle
import unittest
import zlib

from pals_validation.e3.code_harness import UnsupportedCode, equal_lcb, static_check
from pals_validation.e3.code_tests import decode_lcb_tests


class CodeEvaluationContractTests(unittest.TestCase):
    def test_official_style_lcb_comparison(self):
        self.assertTrue(equal_lcb("1.00 2\n", "1 2.0\n", "stdin"))
        self.assertFalse(equal_lcb("1.01\n", "1\n", "stdin"))
        self.assertTrue(equal_lcb([1, 2], [1, 2], "functional"))
        self.assertFalse(equal_lcb(1, True, "functional"))

    def test_safe_private_test_decoder(self):
        case = [{"input": "7", "output": "7", "testtype": "functional"}]
        text = json.dumps(case)
        compressed = base64.b64encode(zlib.compress(pickle.dumps(text))).decode()
        gold = {"io_type": "functional", "public_test_cases": text,
                "private_test_cases": compressed}
        rows = decode_lcb_tests(gold)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["inputs"], [7])
        self.assertEqual(rows[1]["split"], "private")
        gold["private_test_cases"] = base64.b64encode(
            zlib.compress(pickle.dumps(len))).decode()
        with self.assertRaises(ValueError):
            decode_lcb_tests(gold)

    def test_unsafe_or_unreviewed_code_is_not_executed(self):
        static_check("from math import sqrt\ndef f(x): return sqrt(x)")
        with self.assertRaises(UnsupportedCode):
            static_check("import os\ndef f(): return os.listdir('/')")
        with self.assertRaises(UnsupportedCode):
            static_check("def f(x): return x.__class__")
        # Invalid Python is a benchmark-level program error, not a reason to
        # execute or to silently discard the question.
        static_check("def f(:")


if __name__ == "__main__":
    unittest.main()
