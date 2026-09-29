"""Decode frozen LCB public/private cases without executing pickle globals.

The narrow string-only decoder follows the existing dag-builder reference gate;
the resulting tests never enter the generation or PALS scoring prompts.
"""

import base64
import io
import json
import pickle
import pickletools
import zlib

MAX_BYTES = 64 * 1024**2
STRING_OPS = {"PROTO", "FRAME", "BINUNICODE", "SHORT_BINUNICODE", "BINUNICODE8",
              "UNICODE", "MEMOIZE", "BINPUT", "LONG_BINPUT", "PUT", "STOP"}


class StringOnlyUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ValueError("pickle globals forbidden")

    def persistent_load(self, pid):
        raise ValueError("pickle persistent objects forbidden")


def _private(payload: str) -> list:
    if not isinstance(payload, str) or len(payload.encode()) > MAX_BYTES:
        raise ValueError("LCB private test payload too large")
    if payload.lstrip().startswith("["):
        value = json.loads(payload)
    else:
        packed = base64.b64decode(payload, validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(packed, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not inflater.eof or inflater.unconsumed_tail or inflater.unused_data:
            raise ValueError("Invalid or oversized compressed LCB tests")
        ops = list(pickletools.genops(raw))
        if (not ops or len(ops) > 12 or any(op.name not in STRING_OPS for op, _, _ in ops)
                or ops[-1][0].name != "STOP" or ops[-1][2] != len(raw) - 1):
            raise ValueError("Only a pickled JSON string is supported")
        decoded = StringOnlyUnpickler(io.BytesIO(raw)).load()
        if not isinstance(decoded, str):
            raise ValueError("Decoded private tests must be JSON text")
        value = json.loads(decoded)
    if not isinstance(value, list) or not value:
        raise ValueError("LCB private tests must be a nonempty list")
    return value


def decode_lcb_tests(gold: dict) -> list[dict]:
    public = json.loads(gold["public_test_cases"])
    private = _private(gold["private_test_cases"])
    if not isinstance(public, list) or not public or len(public) + len(private) > 20000:
        raise ValueError("LCB test count outside frozen bounds")
    io_type = gold["io_type"]
    result = []
    for split, cases in (("public", public), ("private", private)):
        for index, case in enumerate(cases):
            if (not isinstance(case, dict) or case.get("testtype") != io_type or
                    any(not isinstance(case.get(key), str) for key in ("input", "output"))):
                raise ValueError("LCB test type or I/O mismatch")
            if len(case["input"].encode()) + len(case["output"].encode()) > MAX_BYTES:
                raise ValueError("Individual LCB test too large")
            if io_type == "functional":
                inputs = [json.loads(line) for line in case["input"].splitlines()]
                expected = json.loads(case["output"])
            elif io_type == "stdin":
                inputs, expected = case["input"], case["output"]
            else:
                raise ValueError("Unknown LCB test I/O type")
            result.append({"split": split, "index": index, "inputs": inputs,
                           "expected": expected})
    return result
