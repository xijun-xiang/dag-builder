"""Bounded decoding of LCB test data, never arbitrary pickle instructions."""

import base64
import io
import json
import pickle
import pickletools
import zlib

from .schemas import require

MAX_BYTES = 64 * 1024**2
STRING_PICKLE_OPS = {"PROTO", "FRAME", "BINUNICODE", "SHORT_BINUNICODE", "BINUNICODE8",
                     "UNICODE", "MEMOIZE", "BINPUT", "LONG_BINPUT", "PUT", "STOP"}


class StringOnlyUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ValueError("pickle globals forbidden")

    def persistent_load(self, pid):
        raise ValueError("pickle persistent objects forbidden")


def private_tests(payload):
    require(isinstance(payload, str) and len(payload.encode()) <= MAX_BYTES, "private payload too large")
    if payload.lstrip().startswith("["):
        value = json.loads(payload)
    else:
        compressed = base64.b64decode(payload, validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES and inflater.eof and not inflater.unconsumed_tail
                and not inflater.unused_data, "invalid/oversized compressed tests")
        ops = list(pickletools.genops(raw))
        require(bool(ops) and len(ops) <= 12 and all(op.name in STRING_PICKLE_OPS for op, _, _ in ops),
                "only a pickled JSON string is supported")
        require(ops[-1][0].name == "STOP" and ops[-1][2] == len(raw) - 1,
                "trailing pickle data")
        decoded = StringOnlyUnpickler(io.BytesIO(raw)).load()
        require(isinstance(decoded, str), "pickled value must be a JSON string")
        value = json.loads(decoded)
    require(isinstance(value, list) and bool(value), "nonempty private tests required")
    return value


def decode_tests(bundle, io_type):
    public = json.loads(bundle["public_test_cases"])
    hidden = private_tests(bundle["private_test_cases"])
    require(isinstance(public, list) and bool(public), "public tests required")
    require(len(public) + len(hidden) <= 20000, "test count exceeds bounded harness")
    result = []
    for split, cases in (("public", public), ("private", hidden)):
        for index, case in enumerate(cases):
            require(isinstance(case, dict) and case.get("testtype") == io_type, "test type mismatch")
            require(all(isinstance(case.get(k), str) for k in ("input", "output")), "test I/O must be strings")
            require(sum(len(case[k].encode()) for k in ("input", "output")) <= MAX_BYTES,
                    "individual test too large")
            if io_type == "functional":
                # Each argument is on its own JSON line in the official format.
                inputs = [json.loads(line) for line in case["input"].splitlines()]
                expected = json.loads(case["output"])
            else:
                inputs, expected = case["input"], case["output"]
            result.append({"split": split, "index": index, "inputs": inputs, "expected": expected})
    return result
