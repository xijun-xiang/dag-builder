"""Fail-closed Linux/seccomp answer tests for generated HumanEval and LCB code.

The child sees no reference outputs for LCB, no credentials, and no usable file,
network or process-creation syscalls after installing the filter. This is a
bounded compatible scorer, not a claim of running the official leaderboard.
"""

from __future__ import annotations

import ast
import ctypes
import ctypes.util
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation

POLICY = "pals-e3-generated-code-seccomp-v1"
IMPORTS = {"sys", "math", "collections", "itertools", "functools", "heapq",
           "bisect", "typing", "array", "operator", "string", "decimal",
           "fractions", "statistics", "random", "copy", "re"}
SYSCALLS = ("read", "write", "close", "fstat", "lseek", "brk", "mmap", "mprotect",
            "munmap", "mremap", "madvise", "rt_sigaction", "rt_sigprocmask",
            "rt_sigreturn", "sigaltstack", "exit", "exit_group", "clock_gettime",
            "gettimeofday", "time", "getpid", "gettid", "futex", "sched_yield",
            "getrandom", "getrusage", "restart_syscall")
MAX_CODE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 8 * 1024**2


class UnsupportedCode(Exception):
    pass


def static_check(code: str) -> None:
    if not isinstance(code, str) or len(code.encode()) > MAX_CODE_BYTES:
        raise UnsupportedCode("code_size_outside_policy")
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return  # Syntax failure is a task-level program error, not an unsafe import.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name not in IMPORTS for alias in node.names):
                raise UnsupportedCode("unreviewed_import")
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in IMPORTS:
                raise UnsupportedCode("unreviewed_import")
        if isinstance(node, ast.Name) and node.id in {
                "eval", "exec", "compile", "open", "__import__", "globals", "locals",
                "vars", "getattr", "setattr", "delattr", "breakpoint"}:
            raise UnsupportedCode("dynamic_or_io_operation")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise UnsupportedCode("introspection_attribute")


def _install_seccomp() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Linux seccomp required")
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise RuntimeError("libseccomp unavailable")
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [ctypes.c_uint32], ctypes.c_void_p
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_load.argtypes, lib.seccomp_load.restype = [ctypes.c_void_p], ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x00050000 | errno.EPERM)
    if not ctx:
        raise RuntimeError("seccomp_init failed")
    try:
        for name in SYSCALLS:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add(ctx, 0x7fff0000, number, 0):
                raise RuntimeError("seccomp allow rule failed: " + name)
        if lib.seccomp_load(ctx):
            raise RuntimeError("seccomp_load failed")
    finally:
        lib.seccomp_release(ctx)
    for name, probe in (("filesystem", lambda: os.open("/dev/null", os.O_RDONLY)),
                        ("network", socket.socket)):
        try:
            handle = probe()
        except PermissionError:
            continue
        if name == "filesystem":
            os.close(handle)
        else:
            handle.close()
        raise RuntimeError("isolation self-probe failed: " + name)


class _BoundedOutput(io.StringIO):
    def write(self, text):
        if self.tell() + len(text) > MAX_OUTPUT_BYTES:
            raise RuntimeError("output_limit")
        return super().write(text)


def _child() -> None:
    payload = json.load(sys.stdin)
    if set(payload) != {"code", "benchmark", "io_type", "entry_point", "inputs", "test"}:
        raise RuntimeError("unexpected grader payload")
    sys.stdin.close()
    static_check(payload["code"])
    for module in sorted(IMPORTS):
        __import__(module)
    # Compile before seccomp; generated code is never executed before the filter.
    try:
        program = compile(payload["code"], "<model-answer>", "exec")
        official_test = (compile(payload["test"], "<humaneval-tests>", "exec")
                         if payload["benchmark"] == "humaneval" else None)
        syntax_error = None
    except SyntaxError as error:
        program, official_test, syntax_error = None, None, type(error).__name__
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
    stdout = sys.stdout
    sink = _BoundedOutput()
    sys.stdout = sink
    sys.stderr = sink
    if payload["io_type"] == "stdin":
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload["inputs"].encode()), encoding="utf-8")
    _install_seccomp()
    result = {"policy": POLICY, "isolation_probes_passed": True}
    try:
        if syntax_error:
            raise SyntaxError("generated code does not parse")
        namespace = {"__name__": "__main__"}
        if payload["io_type"] == "functional":
            from typing import Dict, List, Optional, Set, Tuple
            namespace.update(Dict=Dict, List=List, Optional=Optional, Set=Set, Tuple=Tuple)
        try:
            exec(program, namespace)
        except SystemExit as error:
            if payload["io_type"] != "stdin" or error.code not in (None, 0):
                raise
        if payload["benchmark"] == "humaneval":
            exec(official_test, namespace)
            candidate = namespace[payload["entry_point"]]
            check = namespace["check"]
            calls = [0]
            def counted(*args, **kwargs):
                calls[0] += 1
                return candidate(*args, **kwargs)
            check(counted)
            if not calls[0]:
                raise RuntimeError("official_check_did_not_call_candidate")
            result.update(status="executed", value=True, test_calls=calls[0])
        elif payload["io_type"] == "functional":
            entry = payload["entry_point"]
            candidate = (getattr(namespace["Solution"](), entry)
                         if "Solution" in namespace else namespace[entry])
            value = candidate(*payload["inputs"])
            result.update(status="executed", value=list(value) if isinstance(value, tuple) else value)
        else:
            result.update(status="executed", value=sink.getvalue())
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_OUTPUT_BYTES:
            raise RuntimeError("output_limit")
    except BaseException as error:
        encoded = json.dumps({**result, "status": "program_error",
                              "exception_type": type(error).__name__}, ensure_ascii=False)
    stdout.write(encoded + "\n")
    stdout.flush()


def _invoke(payload: dict, scratch: Path) -> dict:
    started = time.monotonic()
    try:
        with tempfile.TemporaryFile(mode="w+b", dir=scratch) as output:
            process = subprocess.run(
                [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child"],
                input=json.dumps(payload, ensure_ascii=False).encode(), stdout=output,
                stderr=subprocess.STDOUT, timeout=12, cwd=scratch, close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"})
            output.seek(0)
            raw = output.read(MAX_OUTPUT_BYTES + 1024)
        if len(raw) > MAX_OUTPUT_BYTES + 1000:
            result = {"status": "program_error", "reason": "output_limit"}
        else:
            try:
                result = json.loads(raw.decode().splitlines()[-1])
            except (IndexError, ValueError, UnicodeError):
                result = {}
            if (process.returncode or result.get("policy") != POLICY or
                    result.get("isolation_probes_passed") is not True):
                result = {"status": "infrastructure_error", "reason": "isolation_or_child_failure",
                          "returncode": process.returncode}
    except subprocess.TimeoutExpired:
        result = {"status": "program_timeout", "reason": "per_test_wall_limit"}
    return {**result, "seconds": time.monotonic() - started}


def equal_lcb(actual, expected, io_type: str) -> bool:
    if io_type == "functional":
        return actual == expected and (not isinstance(expected, bool) or type(actual) is bool)
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    lines = lambda text: [line.strip() for line in text.strip().split("\n")]
    a, b = lines(actual), lines(expected)
    if len(a) != len(b):
        return False
    for left, right in zip(a, b):
        if left == right:
            continue
        try:
            x, y = [Decimal(token) for token in left.split()], [Decimal(token) for token in right.split()]
        except InvalidOperation:
            return False
        if x != y:
            return False
    return True


def grade_code_answer(problem: dict, gold: dict, code: str, scratch: Path) -> dict:
    """Grade only within a frozen Slurm CPU job; never on the login node."""
    from .code_tests import decode_lcb_tests
    from ..io import digest
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("CUDA_VISIBLE_DEVICES"):
        raise RuntimeError("Code grading requires a GPU-free Slurm CPU allocation")
    fence = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
    scratch = scratch.resolve(strict=True)
    if fence not in scratch.parents or not scratch.is_dir():
        raise RuntimeError("Code grading scratch outside PALS")
    code_sha256 = hashlib.sha256(code.encode()).hexdigest()
    try:
        static_check(code)
    except UnsupportedCode as error:
        return {"status": "harness_unsupported", "correct": None,
                "reason": str(error), "code_sha256": code_sha256, "policy": POLICY}
    if gold["kind"] == "humaneval_code":
        # The original prompt supplies imports and docstrings. The complete
        # generated function follows it and shadows its unimplemented stub.
        full_code = problem["question"] + "\n" + code
        try:
            static_check(full_code)
        except UnsupportedCode as error:
            return {"status": "harness_unsupported", "correct": None,
                    "reason": "prompt_or_code_" + str(error), "code_sha256": code_sha256,
                    "policy": POLICY}
        payload = {"code": full_code, "benchmark": "humaneval", "io_type": "functional",
                   "entry_point": gold["entry_point"], "inputs": [], "test": gold["test"]}
        tests_sha256 = hashlib.sha256(gold["test"].encode()).hexdigest()
        checked = _invoke(payload, scratch)
        status = checked["status"]
        return {"status": ("correct" if status == "executed" else status),
                "correct": True if status == "executed" else
                           None if status == "infrastructure_error" else False,
                "code_sha256": code_sha256, "tests_sha256": tests_sha256,
                "policy": POLICY, "test_calls": checked.get("test_calls"),
                "seconds": checked["seconds"], "reason": checked.get("reason") or
                checked.get("exception_type")}
    if gold["kind"] != "lcb_code":
        raise ValueError("unknown generated-code benchmark")
    try:
        tests = decode_lcb_tests(gold)
    except (ValueError, KeyError, TypeError) as error:
        return {"status": "infrastructure_error", "correct": None,
                "reason": "frozen_lcb_tests_invalid:" + type(error).__name__,
                "code_sha256": code_sha256, "policy": POLICY}
    results = []
    for case in tests:
        payload = {"code": code, "benchmark": "livecodebench", "io_type": gold["io_type"],
                   "entry_point": gold["entry_point"], "inputs": case["inputs"], "test": None}
        checked = _invoke(payload, scratch)
        status = checked["status"]
        if status == "executed":
            status = "passed" if equal_lcb(checked["value"], case["expected"], gold["io_type"]) else "wrong_answer"
        results.append({"split": case["split"], "index": case["index"], "status": status,
                        "test_sha256": digest(case), "seconds": checked["seconds"]})
        if status != "passed":
            break  # A single failed official test proves this one-shot answer did not pass.
    statuses = {row["status"] for row in results}
    status = ("correct" if statuses == {"passed"} and len(results) == len(tests) else
              "infrastructure_error" if "infrastructure_error" in statuses else
              "program_timeout" if "program_timeout" in statuses else
              "program_error" if "program_error" in statuses else "incorrect")
    return {"status": status, "correct": True if status == "correct" else
            None if status == "infrastructure_error" else False,
            "code_sha256": code_sha256, "tests_sha256": digest(tests),
            "policy": POLICY, "tests_planned": len(tests), "tests_executed": len(results),
            "tests_passed": sum(row["status"] == "passed" for row in results),
            "test_results": results}


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]:
        raise SystemExit("only --child is supported")
    _child()
