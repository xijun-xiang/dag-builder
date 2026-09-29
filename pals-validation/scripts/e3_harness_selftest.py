"""CPU-only synthetic safety/compatibility gate; no benchmark candidate is run."""

import json
import os
from pathlib import Path

from pals_validation.e3.code_harness import POLICY, _invoke, grade_code_answer
from pals_validation.e3 import code_tests
from pals_validation.io import save, sha256


def main() -> None:
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("CUDA_VISIBLE_DEVICES"):
        raise RuntimeError("The code harness self-test requires a GPU-free Slurm job")
    root = Path(os.environ["PALS_HARNESS_RUN"]).resolve(strict=True)
    fence = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
    if fence not in root.parents:
        raise RuntimeError("Self-test output is outside PALS")
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    probe = _invoke({"code": "def identity(x):\n    return x\n", "benchmark": "livecodebench",
                     "io_type": "functional", "entry_point": "identity", "inputs": [7],
                     "test": None}, scratch)
    if (probe.get("status") != "executed" or probe.get("value") != 7 or
            probe.get("policy") != POLICY or probe.get("isolation_probes_passed") is not True):
        raise RuntimeError("Seccomp filesystem/network probes or functional execution failed")
    question = "def identity(x):\n    \"\"\"Return the input.\"\"\"\n"
    he_problem = {"question": question}
    he_gold = {"kind": "humaneval_code", "entry_point": "identity",
               "test": "def check(candidate):\n    assert candidate(7) == 7\n"}
    good_he = grade_code_answer(he_problem, he_gold, "def identity(x):\n    return x\n", scratch)
    bad_he = grade_code_answer(he_problem, he_gold, "def identity(x):\n    return 0\n", scratch)
    tests = [{"input": "7", "output": "7", "testtype": "functional"}]
    lcb_gold = {"kind": "lcb_code", "io_type": "functional", "entry_point": "identity",
                "public_test_cases": json.dumps(tests), "private_test_cases": json.dumps(tests)}
    good_lcb = grade_code_answer({}, lcb_gold, "def identity(x):\n    return x\n", scratch)
    bad_lcb = grade_code_answer({}, lcb_gold, "def identity(x):\n    return 0\n", scratch)
    unsafe = grade_code_answer({}, lcb_gold, "import os\ndef identity(x): return x", scratch)
    std_tests = [{"input": "7\n", "output": "7\n", "testtype": "stdin"}]
    std_gold = {"kind": "lcb_code", "io_type": "stdin", "entry_point": None,
                "public_test_cases": json.dumps(std_tests),
                "private_test_cases": json.dumps(std_tests)}
    good_stdin = grade_code_answer({}, std_gold, "print(input())", scratch)
    statuses = {"human_good": good_he["status"], "human_bad": bad_he["status"],
                "lcb_good": good_lcb["status"], "lcb_bad": bad_lcb["status"],
                "unsafe_import": unsafe["status"], "stdin_good": good_stdin["status"]}
    expected = {"human_good": "correct", "human_bad": "program_error",
                "lcb_good": "correct", "lcb_bad": "incorrect",
                "unsafe_import": "harness_unsupported", "stdin_good": "correct"}
    if statuses != expected:
        raise RuntimeError("Synthetic harness controls failed: " + repr(statuses))
    save(root / "selftest.json", {"status": "PASS", "policy": POLICY,
         "statuses": statuses, "isolation_probes_passed": True,
         "harness_sha256": sha256(Path(__file__).resolve().parents[1] / "src" /
                                  "pals_validation" / "e3" / "code_harness.py"),
         "decoder_sha256": sha256(code_tests.__file__),
         "slurm_job_id": os.environ["SLURM_JOB_ID"]})
    print(json.dumps({"status": "PASS", "controls": statuses}), flush=True)


if __name__ == "__main__":
    main()
