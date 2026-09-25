"""Offline, immutable HumanEval campaign preparation with carried-forward costs.

This does not make API calls or execute reference/test code. Prior run roots must
include every generation/probe run charged to the same user authorization.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from dag_builder.config import Config
from dag_builder.humaneval_source import prepare_humaneval
from dag_builder.response_contract import reported_tokens
from dag_builder.pipeline import implementation
from dag_builder.storage import digest, read_json, run_lock, write_bytes_once, write_once


def audit_prior_runs(roots):
    roots = [Path(root).resolve(strict=True) for root in roots]
    if len(roots) != len(set(roots)):
        raise ValueError("duplicate prior root")
    records = []
    for root in roots:
        for path in sorted(root.glob("items/*/*/attempt-*/request.json")):
            request = read_json(path)
            allowance = request["reserved_tokens"]
            if type(allowance) is not int or allowance <= 0:
                raise ValueError("invalid prior allowance")
            response_path = path.with_name("response.json")
            usage = reported_tokens(read_json(response_path)["body"]) if response_path.exists() else 0
            records.append({
                "request_path": str(path),
                "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "reserved_tokens": allowance,
                "reported_tokens": usage,
                "accounted_tokens": max(allowance, usage),
                "response_received": response_path.exists(),
                "response_sha256": hashlib.sha256(response_path.read_bytes()).hexdigest() if response_path.exists() else None,
            })
    return {"prior_roots": [str(root) for root in roots], "attempts": records,
            "calls": len(records), "accounted_tokens": sum(r["accounted_tokens"] for r in records),
            "policy": "max(request allowance, reported usage); unknown calls are not free; no unused allowance refunds"}


def probe_policy(probe, allow_failed_cap=False):
    """A user-approved operational exception is NOT a passed cap probe."""
    result = read_json(probe / "probe_result.json")
    if result.get("passed") is True:
        return {"status": "passed", "result_sha256": digest(result)}
    rows = result.get("results", [])
    if not (allow_failed_cap and len(rows) == 2
            and rows[0].get("probe") == "short" and rows[0].get("status") == "passed"
            and rows[1].get("probe") == "cap" and rows[1].get("status") == "failed"
            and rows[1].get("reason") == "response_contract_violation"
            and result.get("actual_request_max_tokens") == 32768):
        raise ValueError("API probe has not passed; no matching explicit content-gated exception")
    return {"status": "failed_cap_preserved_user_authorized_content_gate",
            "result_sha256": digest(result), "cap_probe_passed": False,
            "policy": "Reject non-stop/invalid content; no field fallback or JSON repair; stop on reported usage above reservation"}


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--source-file", required=True, type=Path)
    p.add_argument("--revision", required=True)
    p.add_argument("--expected-sha256", required=True)
    p.add_argument("--exclusions", required=True, type=Path)
    p.add_argument("--prior-run", required=True, action="append", type=Path)
    p.add_argument("--passed-probe", required=True, type=Path)
    p.add_argument("--allow-failed-cap-probe", action="store_true",
                   help="Requires explicit user authorization; retain cap failure and apply per-response gates")
    p.add_argument("--total-call-budget", type=int, default=1200)
    p.add_argument("--total-token-budget", type=int, default=24_000_000)
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--prompt-version", choices=("humaneval-reference-v2", "humaneval-reference-v3"), default="humaneval-reference-v2")
    args = p.parse_args()
    probe = args.passed_probe.resolve(strict=True)
    if probe not in [r.resolve(strict=True) for r in args.prior_run]:
        raise ValueError("probe cost must be included in prior runs")
    policy = probe_policy(probe, args.allow_failed_cap_probe)
    audit = audit_prior_runs(args.prior_run)
    probe_config = Config.load(probe / "run_config.json")
    config = Config(
        task_type="humaneval", prompt_version=args.prompt_version,
        solution_source="reference_code_explanation", thinking="enabled", reasoning_effort=probe_config.reasoning_effort,
        response_format="json_object", strict_response_contract=True,
        max_tokens=32768, timeout_seconds=900, workers=args.workers, rate_limit_retries=0,
        content_gated_response=args.allow_failed_cap_probe,
        tls_max_version=probe_config.tls_max_version,
        transport=probe_config.transport,
        max_calls=args.total_call_budget-audit["calls"], max_reserved_tokens=args.total_token_budget-audit["accounted_tokens"],
    )
    for key in ("base_url", "model", "thinking", "reasoning_effort", "response_format", "strict_response_contract", "tls_max_version", "transport"):
        if getattr(config, key) != getattr(probe_config, key):
            raise ValueError("production control profile differs from probe")
    with run_lock(args.root):
        selection = prepare_humaneval(args.root, args.source_file, args.revision, args.expected_sha256,
                                      exclusions=read_json(args.exclusions))
        write_once(args.root / "config.json", config.to_dict())
        write_once(args.root / "authorization-ledger.json", {
            "total_authorized_calls": args.total_call_budget, "total_authorized_accounted_tokens": args.total_token_budget,
            "prior": audit, "remaining_calls": config.max_calls,
            "remaining_accounted_tokens": config.max_reserved_tokens,
            "note": "Stop when the shared allowance is exhausted; larger per-call caps may prevent full coverage. This is not a provider-side billing guarantee.",
            "scope": "Fixed HumanEval source cohort, at most four transport attempts per stage, no semantic resampling",
            "user_authorization": "2026-09-21 user authorized expanded API cost capacity, workers up to 32 and six strict output/data rules",
        })
        write_once(args.root / "quality-plan.json", {
            "protocol": config.prompt_version,
            "source_candidates": selection["candidate_count"],
            "source_excluded": selection["excluded"],
            "eligible_candidates": selection["selected_count"],
            "gate_task_ids": selection["selected_task_ids"][:5],
            "gate": "Process these five unchanged candidates through all stages; inspect semantic/structural outcomes before continuing remaining items. No automatic resampling to make the gate pass.",
            "acceptance": "Per-item structural validation plus separate solution and DAG model reviews; no PALS effect, node-count or branching acceptance threshold.",
            "limitations": "Model-reviewed synthetic explanations of official reference code, not official CoT or human-certified gold. Code/tests not executed. Upstream model revision behind provider alias unresolved.",
            "thinking": "Native reasoning_content archived separately; only message.content formal rationale is used.",
            "api_probe": str(probe), "api_probe_policy": policy,
        })
        import dag_builder
        package = Path(dag_builder.__file__).parent
        origin = implementation()
        for name in origin["source_files"]:
            write_bytes_once(args.root / "controller/code/dag_builder" / name, (package / name).read_bytes())
        write_once(args.root / "controller/code/snapshot_origin.json", origin)
        write_once(args.root / "code_origin.json", origin)
    print(json.dumps({"root": str(args.root), "eligible": selection["selected_count"],
                      "gate": selection["selected_task_ids"][:5], "remaining_calls": config.max_calls,
                      "remaining_tokens": config.max_reserved_tokens}))


if __name__ == "__main__":
    main()
