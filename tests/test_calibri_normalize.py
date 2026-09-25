"""Pure source-binding contracts; no API calls or reference-code execution."""

import copy
import unittest

from dag_builder import calibri_normalize as contract
from dag_builder.schemas import REVIEW_CHECKS
from dag_builder.validation import validate_parents


def fixture():
    item = {"task_type": "livecodebench", "question": "Return the input.\n",
            "io_type": "stdin", "entry_point": None, "starter_code": "",
            "raw_output": "Read the input unchanged.\nThe same value satisfies the request.\n",
            "reference_code": "print(input())\n", "reference_execution": "upstream_pass_only",
            "private_tests": "never send this", "local_path": "/private/test"}
    proposal = {"steps": [
        {"kind": "given", "statement": "The requested output equals the supplied input.",
         "source_refs": ["Q0001"], "support_type": "source_supported", "normalization_note": "Task premise."},
        {"kind": "derived", "statement": "Returning the supplied text satisfies the identity request.",
         "source_refs": ["O0001", "O0002"], "support_type": "source_supported", "normalization_note": "Combine explanation."}],
        "omissions": []}
    dependencies = {"dependencies": [
        {"node_id": 1, "parents": [], "justification": "Stated in the task."},
        {"node_id": 2, "parents": [1], "justification": "The identical text meets the specification."}],
        "answer_parents": [2], "answer_justification": "The program implements that operation."}
    return item, proposal, dependencies


class CALIBRINormalizeTests(unittest.TestCase):
    def test_lossless_unicode_offsets_and_public_allowlist(self):
        item, _, _ = fixture()
        item["raw_output"] = "中文\r\n\r\nαβ\n"
        units = contract.source_units(item)
        for unit in units:
            field = "raw_output" if unit["source_field"] == "calibri_output" else unit["source_field"]
            self.assertEqual(item[field][unit["start"]:unit["end"]], unit["text"])
        self.assertEqual([u["unit_id"] for u in units if u["source_field"] == "calibri_output"],
                         ["O0001", "O0003"])
        public = contract.public_input(item)
        self.assertNotIn("private_tests", public)
        self.assertNotIn("never send this", str(public))
        self.assertNotIn("/private/test", str(public))

    def test_program_assigns_ids_quotes_and_exact_answer(self):
        item, proposal, dependencies = fixture()
        normalized = contract.normalize(proposal, item)
        self.assertEqual([n["node_id"] for n in normalized["nodes"]], [1, 2])
        self.assertEqual(normalized["nodes"][1]["source_quote"], "Read the input unchanged.\n")
        graph = contract.assemble_graph(dependencies, normalized, item)
        self.assertEqual(graph["nodes"][-1]["statement"], item["reference_code"])
        self.assertIs(graph["nodes"][-1]["excluded_from_pals"], True)
        self.assertNotIn("node_id", proposal["steps"][0])

    def test_invalid_or_fabricated_anchors_rejected(self):
        item, proposal, _ = fixture()
        for refs in ([], ["O9999"], ["O0001", "O0001"], [1]):
            bad = copy.deepcopy(proposal)
            bad["steps"][1]["source_refs"] = refs
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                contract.normalize(bad, item)
        bad = copy.deepcopy(proposal)
        bad["steps"][1]["source_quote"] = "invented"
        with self.assertRaises(ValueError):
            contract.normalize(bad, item)

    def test_supplement_not_given_and_code_not_a_proof(self):
        item, proposal, _ = fixture()
        for changes in ({"support_type": "supplementary", "kind": "given"},
                        {"source_refs": ["C0001"]},
                        {"statement": "By step 1 the result follows."},
                        {"statement": "print(input())"}):
            bad = copy.deepcopy(proposal)
            bad["steps"][1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                contract.normalize(bad, item)

    def test_orphans_cycles_and_rewritten_ids_rejected(self):
        item, proposal, dependencies = fixture()
        normalized = contract.normalize(proposal, item)
        for changes in ({"answer_parents": [1]},
                        {"dependencies": [{"node_id": 1, "parents": [2], "justification": "bad"},
                                          dependencies["dependencies"][1]]},
                        {"dependencies": list(reversed(dependencies["dependencies"]))}):
            bad = {**dependencies, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                contract.assemble_graph(bad, normalized, item)

    def test_minimal_direct_edges_are_v2_only(self):
        nodes = [{"node_id": 1, "kind": "given"},
                 {"node_id": 2, "kind": "derived"},
                 {"node_id": 3, "kind": "derived"},
                 {"node_id": 4, "kind": "answer"}]
        rows = {"parents": [
            {"node_id": 1, "parents": []},
            {"node_id": 2, "parents": [1]},
            {"node_id": 3, "parents": [2]},
            {"node_id": 4, "parents": [2, 3]},
        ]}
        validate_parents(rows, nodes)
        with self.assertRaisesRegex(ValueError, "transitively redundant"):
            validate_parents(rows, nodes, require_minimal=True)
        rows["parents"][-1]["parents"] = [3]
        validate_parents(rows, nodes, require_minimal=True)

    def test_accept_requires_all_semantic_checks(self):
        keys = (*REVIEW_CHECKS["review_dag"], *contract.REVIEW_CHECKS)
        review = {"decision": "accept", "checks": dict.fromkeys(keys, True), "issues": [],
                  "reason": "Each claim checked against the source and program."}
        contract.validate_audit(review)
        for value in (False, None, 1):
            bad = copy.deepcopy(review)
            bad["checks"]["source_meaning_preserved"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                contract.validate_audit(bad)


if __name__ == "__main__":
    unittest.main()
