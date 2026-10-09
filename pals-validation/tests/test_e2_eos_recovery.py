"""CPU-only boundary and immutable merge tests; no model generation or remote calls."""
from copy import deepcopy
import inspect
from pathlib import Path
import tempfile
import unittest

from pals_validation.backend import MockBackend
from pals_validation.protocol import parse_step
from pals_validation.recovery.e2_eos import select_target, merge_rows, score_input, numeric_reference
from pals_validation.recovery import e2_eos as M
from pals_validation.io import save, read, sha256, digest
from pals_validation.metrics import repeats


CONTRACT = {"eos_token_ids": [99, 100], "max_new_tokens": 4096}


def raw(text, finish="eos", tokens=None, task="humaneval"):
    return {"raw_text": text, "finish_reason": finish, "parse": parse_step(text, task),
            "generated_token_ids": [1, 2, 99] if tokens is None else tokens, "repeat": 0}


class BoundaryTests(unittest.TestCase):
    def test_eos_missing_tag_preserves_body_verbatim(self):
        r = raw("  Return the result.\n")
        before = deepcopy(r)
        d = select_target(r, CONTRACT, "humaneval")
        self.assertEqual(d, {"kind":"eos_recovered", "target":"Return the result.", "reason":None})
        self.assertEqual(r, before)
        self.assertNotIn("</step>", d["target"])

    def test_no_new_semantic_or_quality_filter(self):
        for text in ("Consider", "Finished reasoning.", "nonsensical words transport violet as."):
            self.assertEqual(select_target(raw(text), CONTRACT, "humaneval")["kind"], "eos_recovered")

    def test_strict_valid_preserved_even_if_new_rule_is_conservative(self):
        text = "First paragraph.\n\nSecond paragraph.\n</step>"
        r = raw(text, "boundary")
        self.assertEqual(select_target(r, CONTRACT, "humaneval")["target"], r["parse"]["body"])
        self.assertEqual(select_target(r, CONTRACT, "humaneval")["kind"], "strict")

    def test_length_unknown_and_boundary_failures_never_recovered(self):
        for finish in ("length", "unknown", "boundary"):
            self.assertEqual(select_target(raw("A sentence.", finish), CONTRACT, "humaneval")["kind"], "invalid")

    def test_ambiguous_eos_and_at_cap(self):
        for tokens in ([], [1], [99, 1, 99], [1]*4095+[99], [1]*4096+[99]):
            self.assertEqual(select_target(raw("A sentence.", tokens=tokens), CONTRACT, "humaneval")["kind"], "invalid")

    def test_structures_nested_malformed_and_answer_blocks_rejected(self):
        for text in ("<step>A sentence.", "A sentence.</step", "A [/step>",
                     "<STEP>A sentence.", "<answer>A", "RESULT: A", "<think>text",
                     "A sentence.</step>"):
            self.assertEqual(select_target(raw(text), CONTRACT, "humaneval")["kind"], "invalid", text)

    def test_multi_step_or_code_eos_rejected_without_truncating(self):
        for text in ("First.\n\nSecond.", "1. First.\n2. Second.", "Step 2: Next.",
                     "A paragraph.\n- New step.", "```python\nreturn True\n```", "return True",
                     "## Conclusion\nDone", "~~~\ncode\n~~~"):
            self.assertEqual(select_target(raw(text), CONTRACT, "humaneval")["kind"], "invalid", text)

    def test_wrapped_prose_and_mathematical_less_than_allowed(self):
        r = raw("If x < y,\nreturn the comparison result.")
        self.assertEqual(select_target(r, CONTRACT, "humaneval")["kind"], "eos_recovered")

    def test_empty_and_tampered_parse(self):
        self.assertEqual(select_target(raw(" \n"), CONTRACT, "humaneval")["reason"], "empty")
        r = raw("A sentence."); r["parse"]["valid"] = True
        with self.assertRaisesRegex(ValueError, "stored strict parse"):
            select_target(r, CONTRACT, "humaneval")

    def test_policy_is_temperature_and_outcome_blind(self):
        r = raw("A wrong answer.")
        expected = select_target(r, CONTRACT, "humaneval")
        for t in (.3, .7, 1.2):
            r.update(temperature=t, correct=t==.3, mean_g=t)
            self.assertEqual(select_target(r, CONTRACT, "humaneval"), expected)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.raw = [raw("Existing.</step>", "boundary"), raw("Recover this."), raw("Truncated", "length")]
        for i,r in enumerate(self.raw): r["repeat"] = i
        self.backend = MockBackend(None, {})
        self.old = []
        for i,r in enumerate(self.raw):
            row = {**deepcopy(r), "status": "ok" if i==0 else "invalid_generation"}
            if i==0: row.update(self.backend.score(None, [], None, r["parse"]["body"]))
            self.old.append(row)
        self.decisions = [select_target(r, CONTRACT, "humaneval") for r in self.raw]
        self.decisions[1]["task_key"] = "new"
        self.additions = {"new": self.backend.score(None, [], None, "Recover this.")}

    def test_merge_reuses_strict_score_and_never_changes_raw(self):
        original = deepcopy(self.old)
        rows = merge_rows({"rows":self.raw}, {"rows":self.old}, self.decisions, self.additions)
        self.assertEqual(self.old, original)
        self.assertEqual(rows[0]["evidence"], original[0]["evidence"])
        for row,raw_row in zip(rows,self.raw):
            self.assertTrue(all(row[k]==v for k,v in raw_row.items()))
        self.assertFalse(rows[1]["parse"]["valid"])
        self.assertEqual(rows[1]["status"], "ok")
        self.assertEqual(rows[2]["status"], "invalid_generation")

    def test_missing_new_score_fails_closed(self):
        with self.assertRaises(KeyError):
            merge_rows({"rows":self.raw}, {"rows":self.old}, self.decisions, {})

    def test_tampered_target_rejected(self):
        self.additions["new"]["evidence"]["target_text"] = "Modified."
        with self.assertRaisesRegex(ValueError, "target mismatch"):
            merge_rows({"rows":self.raw}, {"rows":self.old}, self.decisions, self.additions)

    def test_tampered_old_raw_rejected(self):
        self.old[0]["raw_text"] = "Rewritten."
        with self.assertRaisesRegex(ValueError, "raw changed"):
            merge_rows({"rows":self.raw}, {"rows":self.old}, self.decisions, self.additions)

    def test_mismatched_slot_count_rejected(self):
        with self.assertRaisesRegex(ValueError, "row count"):
            merge_rows({"rows":self.raw}, {"rows":self.old[:-1]}, self.decisions, self.additions)

    def test_scoring_functions_have_no_generation_call(self):
        for f in (score_input, numeric_reference):
            self.assertNotIn(".generate(", inspect.getsource(f))

    def test_end_to_end_offline_audit_merges_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/"new"; source=Path(folder)/"old"; model=Path(folder)/"model"
            root.mkdir(); source.mkdir(); model.mkdir()
            for name in ("tasks","claims","scores","workers","merged","analysis"):
                (root/name).mkdir()
            for name in ("generations","results"): (source/name).mkdir()
            save(model/"config.json", {"synthetic":True})
            label="gpqa-formal"; jid="fixture"; key=digest([label,jid,1])
            job={"job_id":jid,"item_id":"q1","kind":"e2","temperature":.3}
            generation={"rows":self.raw,"generation_contract":CONTRACT}
            save(source/"generations/fixture.json", generation)
            old={"job":job,"rows":self.old,"summary":repeats(self.old,3)}
            save(source/"results/fixture.json",old)
            decisions=deepcopy(self.decisions); decisions[1]["task_key"]=key
            save(root/(label+".json"),[{"job":job,"decisions":decisions}])
            ev=deepcopy(self.additions["new"]["evidence"])
            ev.update(full_context_ids=[1],deleted_context_ids=[2])
            task={"key":key,"generation_sha256":sha256(source/"generations/fixture.json"),
                  "evidence_input":{k:v for k,v in ev.items() if not k.endswith("logprobs")}}
            save(root/"tasks"/(key+".json"),task); save(root/"queue.json",[{"key":key,"cost":5}])
            source_hashes={str(p.relative_to(source)):sha256(p) for p in source.rglob("*.json")}
            p={"version":M.VERSION,"implementation":M.implementation_files(),"tasks":1,"datasets":{},
               "config":{"model_path":str(model)},"model_files":{"config.json":sha256(model/"config.json")},
               "sources":{label:{"path":str(source),"files":source_hashes,"config":{"repeats":3,"temperatures":[.3]}}},
               "files":{str(f.relative_to(root)):sha256(f) for f in [root/"queue.json",root/(label+".json"),root/"tasks"/(key+".json")]}}
            p["protocol_id"]=digest(p); save(root/"plan.json",p)
            save(root/"ready.json",{"status":"PASS","plan_sha256":sha256(root/"plan.json")})
            scored={**deepcopy(self.additions["new"]),"evidence":ev}
            save(root/"scores"/(key+".json"),{**scored,"worker":0,"protocol_id":p["protocol_id"],
                 "task_sha256":sha256(root/"tasks"/(key+".json"))})
            save(root/"claims"/(key+".json"),{"worker":0,"job_id":"job"})
            save(root/"workers/0-complete.json",{"worker":0,"job_id":"job","protocol_id":p["protocol_id"],"keys":[key]})
            save(root/"workers/0-reference.json",{"status":"PASS","repeat_max_abs":0.,"masked_loss_errors":{"full":0.,"deleted":0.},
                 "native_losses":{name:scored["score"][name+"_nll"] for name in ("full","deleted")},
                 "probe":scored,"repeat_probe":scored})
            save(root/"gpu-complete.json",{"status":"PASS","workers":1,"protocol_id":p["protocol_id"],"job_id":"job"})
            M.audit(root)
            self.assertEqual(read(root/"audit.json")["status"],"PASS")
            analysis=read(root/"analysis"/(label+".json"))
            self.assertEqual(analysis["strict"]["cells"][0]["valid"],1)
            self.assertEqual(analysis["eos_recovery"]["cells"][0]["valid"],2)
            self.assertIsNone(analysis["strict"]["cells"][0]["D"])
            self.assertIsNotNone(analysis["eos_recovery"]["cells"][0]["D"])
            self.assertEqual(source_hashes,{str(p.relative_to(source)):sha256(p) for p in source.rglob("*.json")})


if __name__ == "__main__": unittest.main()
