"""Only fix the known malformed-label prefix escape; preserve frozen v1."""
import re
from .e2_eos import select_target as select_v1

VERSION = "llama-e2-natural-eos-v2"
TAG_PREFIX = re.compile(r"[<\[]\s*/?\s*(?:step|think|answer|analysis|final|result)", re.I)


def select_target(row, contract, task_type):
    result = select_v1(row, contract, task_type)
    if result["kind"] == "eos_recovered" and TAG_PREFIX.search(row["raw_text"]):
        return {"kind": "invalid", "target": None, "reason": "structural_tag_or_fragment"}
    return result
