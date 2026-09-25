"""LCB reference verification under Linux default-deny seccomp, CPU Slurm only.

Expected outputs never enter the candidate process. Every testcase uses a fresh
bounded process. No generated Python is executed by --prepare or on the login node.
"""
import argparse
import ast
import base64
import collections
import ctypes
import ctypes.util
import errno
import io
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

POLICY = "lcb-reference-seccomp-v1"
IMPORTS = {"sys", "math", "collections", "itertools", "functools", "heapq", "bisect",
           "typing", "array", "operator", "string", "decimal", "fractions", "statistics",
           "random", "copy", "re"}
SYSCALLS = ("read", "write", "close", "fstat", "lseek", "brk", "mmap", "mprotect",
            "munmap", "mremap", "madvise", "rt_sigaction", "rt_sigprocmask", "rt_sigreturn",
            "sigaltstack", "exit", "exit_group", "clock_gettime", "gettimeofday", "time",
            "getpid", "gettid", "futex", "sched_yield", "getrandom", "getrusage", "restart_syscall")
FENCE = Path("/work/projects/polyullm/xxj/PALS")
MAX_OUTPUT = 8 * 1024**2


def digest(value):
    import hashlib
    return hashlib.sha256((json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    indent=2, allow_nan=False) + "\n").encode()).hexdigest()


def file_hash(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def static_check(code):
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(n.name not in IMPORTS for n in node.names):
                raise ValueError("unreviewed import")
        if isinstance(node, ast.ImportFrom) and (node.level or node.module not in IMPORTS):
            raise ValueError("unreviewed import")
        if isinstance(node, ast.Name) and node.id in {
                "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars",
                "getattr", "setattr", "delattr", "breakpoint"}:
            raise ValueError("unreviewed dynamic operation")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("unreviewed introspection")


def install_seccomp():
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
                raise RuntimeError("seccomp rule failed")
        if lib.seccomp_load(ctx):
            raise RuntimeError("seccomp_load failed")
    finally:
        lib.seccomp_release(ctx)
    # No generated code runs until both of these fail with EPERM.
    for label, probe in (("filesystem", lambda: os.open("/dev/null", os.O_RDONLY)),
                         ("network", lambda: socket.socket())):
        try:
            handle = probe()
        except PermissionError:
            continue
        os.close(handle) if label == "filesystem" else handle.close()
        raise RuntimeError("isolation probe failed")


class BoundedOutput(io.StringIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_OUTPUT:
            raise RuntimeError("output limit")
        return super().write(value)


def child():
    # Isolated child gets no expected output, credentials or project paths.
    payload = json.load(sys.stdin)
    if set(payload) != {"code", "io_type", "entry_point", "inputs"}:
        raise RuntimeError("unexpected child payload")
    sys.stdin.close()
    static_check(payload["code"])
    for module in sorted(IMPORTS):
        __import__(module)
    code = compile(payload["code"], "<model-reference>", "exec")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
    sink = BoundedOutput()
    stdout = sys.stdout
    sys.stdout = sink
    sys.stderr = sink
    raw_input = payload["inputs"] if payload["io_type"] == "stdin" else ""
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw_input.encode()), encoding="utf-8")
    install_seccomp()
    result = {"policy": POLICY, "isolation_probes_passed": True}
    try:
        namespace = {"__name__": "__main__"}
        # Match common LeetCode type annotations without importing user code.
        if payload["io_type"] == "functional":
            from typing import List, Optional, Tuple, Dict, Set
            namespace.update(List=List, Optional=Optional, Tuple=Tuple, Dict=Dict, Set=Set)
        try:
            exec(code, namespace)
        except SystemExit as error:
            if payload["io_type"] != "stdin" or error.code not in (None, 0):
                raise
        value = (getattr(namespace["Solution"](), payload["entry_point"])(*payload["inputs"])
                 if payload["io_type"] == "functional" else sink.getvalue())
        result.update(status="executed", value=value)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_OUTPUT:
            raise RuntimeError("output limit")
    except BaseException as error:
        encoded = json.dumps(dict(result, status="runtime_error", exception_type=type(error).__name__))
    stdout.write(encoded + "\n")
    stdout.flush()


def equal_output(actual, expected, io_type):
    if io_type == "functional":
        return actual == expected and (not isinstance(expected, bool) or type(actual) is bool)
    if not isinstance(actual, str):
        return False
    lines = lambda value: [" ".join(line.split()) for line in value.strip().splitlines()]
    # Conservative exact text/numeric comparison, not the official float-tolerant scorer.
    return lines(actual) == lines(expected)


def evaluate_test(code, io_type, entry_point, test, cwd):
    payload = {"code": code, "io_type": io_type, "entry_point": entry_point, "inputs": test["inputs"]}
    started = time.monotonic()
    try:
        process = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=10, cwd=cwd,
            close_fds=True, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        if len(process.stdout.encode()) > MAX_OUTPUT + 1000 or len(process.stderr.encode()) > MAX_OUTPUT:
            result = {"status": "output_limit"}
        else:
            try:
                value = json.loads(process.stdout)
            except (ValueError, TypeError):
                value = {}
            if process.returncode or value.get("policy") != POLICY or value.get("isolation_probes_passed") is not True:
                result = {"status": "execution_or_isolation_error", "returncode": process.returncode}
            elif value.get("status") != "executed":
                result = {"status": value.get("status", "execution_error"),
                          "exception_type": value.get("exception_type")}
            else:
                result = {"status": "passed" if equal_output(value["value"], test["expected"], io_type)
                          else "wrong_answer", "actual_sha256": digest(value["value"])}
    except subprocess.TimeoutExpired:
        result = {"status": "timeout"}
    return {**result, "split": test["split"], "index": test["index"],
            "test_sha256": digest(test), "seconds": time.monotonic() - started}


def prepare(source, output):
    from dag_builder.livecodebench_tests import decode_tests
    from dag_builder.livecodebench_reference import validate_reference
    from dag_builder.storage import private_dir, read_json, write_once
    source = Path(source).resolve(strict=True)
    original = read_json(source / "prepared-manifest.json")
    items = read_json(source / "items.json")
    if digest(items) != original["items_sha256"]:
        raise ValueError("source item hash mismatch")
    rows, rejected = [], []
    for item in items:
        item_dir = source / "items" / item["item_id"]
        result = read_json(item_dir / "result.json")
        if result["status"] != "reference_candidate":
            rejected.append({"item_id": item["item_id"], "status": result["status"]})
            continue
        reference = read_json(item_dir / "reference.json")
        value = read_json(item_dir / "reference_code/output.json")
        tests = read_json(source / "tests" / (item["item_id"] + ".json"))
        if (digest(value) != reference["output_sha256"] or digest(value["code"]) != reference["code_sha256"]
                or digest(tests) != item["tests_sha256"] or item["tests_sha256"] != reference["tests_sha256"]
                or item["source_content_sha256"] != reference["source_content_sha256"]):
            raise ValueError("reference/source/test hash mismatch")
        validate_reference(value, item)
        try:
            static_check(value["code"])
        except ValueError as error:
            rejected.append({"item_id": item["item_id"], "status": "unsupported_execution_contract",
                             "reason": str(error)})
            continue
        rows.append({"item_id": item["item_id"], "io_type": item["io_type"],
                     "entry_point": item["entry_point"], "code": value["code"],
                     "code_sha256": reference["code_sha256"], "source_content_sha256": item["source_content_sha256"],
                     "tests_sha256": item["tests_sha256"], "tests": decode_tests(tests, item["io_type"])})
    if not rows:
        raise ValueError("no reference programs eligible for execution")
    root = private_dir(output)
    write_once(root / "execution-input.json", rows)
    write_once(root / "input-manifest.json", {
        "protocol": POLICY, "source_prepared_manifest_sha256": digest(original),
        "selected": len(items), "planned": len(rows), "not_executed": rejected,
        "inputs_sha256": digest(rows), "harness_sha256": file_hash(__file__),
        "code_git_commit": __import__("dag_builder.pipeline", fromlist=["implementation"]).implementation()["git_commit"],
        "comparison": "exact JSON equality / whitespace-normalized lines; no float tolerance",
        "claim": "conservative reference quality gate, not official LiveCodeBench leaderboard scores"})
    return {"selected": len(items), "execution_candidates": len(rows),
            "tests": sum(len(r["tests"]) for r in rows), "not_executed": rejected}


def run(root):
    root = root.resolve(strict=True)
    if FENCE.resolve(strict=True) not in root.parents or not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("requires authorized PALS Slurm allocation")
    manifest = json.loads((root / "input-manifest.json").read_text())
    rows = json.loads((root / "execution-input.json").read_text())
    if digest(rows) != manifest["inputs_sha256"] or file_hash(__file__) != manifest["harness_sha256"]:
        raise ValueError("frozen execution inputs/harness changed")
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    if scratch.resolve() != scratch:
        raise ValueError("symlinked scratch")
    fixtures = [
        ("class Solution:\n def f(self,x): return x\n", "functional", "f", [7], 7, "passed"),
        ("class Solution:\n def f(self,x): return 0\n", "functional", "f", [7], 7, "wrong_answer"),
        ("print(input())", "stdin", None, "7\n", "7\n", "passed"),
        ("print(0)", "stdin", None, "7\n", "7\n", "wrong_answer"),
        ("import sys\nprint(sys.stdin.buffer.read().decode().strip())", "stdin", None, "7\n", "7", "passed")]
    controls = [evaluate_test(code, kind, entry, {"inputs": inputs, "expected": expected,
                 "split": "harness", "index": i}, scratch)
                for i, (code, kind, entry, inputs, expected, _) in enumerate(fixtures)]
    save(root / "harness-selftest.json", controls)
    if any(r["status"] != fixture[-1] for r, fixture in zip(controls, fixtures)):
        raise RuntimeError("harness controls failed; no benchmark program executed")
    started = datetime.now(timezone.utc).isoformat()
    result_dir = root / "results"
    result_dir.mkdir(mode=0o700)
    results = []
    for row in rows:
        static_check(row["code"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            tests = list(pool.map(lambda test: evaluate_test(row["code"], row["io_type"],
                                   row["entry_point"], test, scratch), row["tests"]))
        passed = bool(tests) and all(t["status"] == "passed" for t in tests)
        result = {"item_id": row["item_id"], "status": "passed" if passed else "not_passed",
                  "counts": dict(collections.Counter(t["status"] for t in tests)), "tests": tests,
                  "code_sha256": row["code_sha256"], "tests_sha256": row["tests_sha256"],
                  "source_content_sha256": row["source_content_sha256"]}
        save(result_dir / (row["item_id"] + ".json"), result)
        results.append(result)
        print(json.dumps({k: result[k] for k in ("item_id", "status", "counts")}), flush=True)
    completion = {"policy": POLICY, "status": "processed", "started_at": started,
                  "ended_at": datetime.now(timezone.utc).isoformat(), "job_id": os.environ["SLURM_JOB_ID"],
                  "hostname": socket.gethostname(), "python": sys.version,
                  "input_manifest_sha256": file_hash(root / "input-manifest.json"),
                  "selected": manifest["selected"], "executed": len(results),
                  "passed": sum(r["status"] == "passed" for r in results),
                  "results": {p.name: file_hash(p) for p in sorted(result_dir.glob("*.json"))},
                  "claim": "all retained test cases checked; no exhaustive correctness or DAG validity claim"}
    save(root / "completion.json", completion)
    print(json.dumps(completion), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.child:
        child()
    elif args.prepare and args.root:
        print(json.dumps(prepare(args.prepare, args.root)))
    elif args.root:
        run(args.root)
    else:
        parser.error("--root required")


if __name__ == "__main__":
    main()
