"""Two paid synthetic probes; no dataset, retries, or automatic mode fallback."""

import json
from dataclasses import replace

from .client import CallFailure
from .pipeline import Pipeline, implementation
from .stages import request_controls
from .storage import run_lock, write_once


def probe_contract(root, config, client):
    # Separate tiny diagnostic budget. Campaign owners must charge these calls
    # to the shared authorization as well as to this immutable probe directory.
    config = replace(
        config,
        workers=1,
        rate_limit_retries=0,
        max_calls=min(config.max_calls, 2),
        max_reserved_tokens=min(config.max_reserved_tokens, 32768),
        strict_response_contract=True,
    )
    if config.thinking != "disabled":
        raise ValueError("this probe requires an explicitly non-thinking profile")
    runner = Pipeline(root, config, client)
    with run_lock(root):
        write_once(runner.root / "run_config.json", config.to_dict())
        write_once(runner.root / "implementation.json", implementation())
        runner._restore_budget()
        results = []
        for name, instruction in (
            ("short", 'Return exactly this JSON object: {"ok":true}'),
            ("cap", 'Return a JSON object with key "numbers" containing an array of every integer from 0 through 9999. Write all integers; no ranges, ellipses, formulas or explanations.'),
        ):
            # Reuse the production sampling/control serialization, not an SDK's
            # different extra_body convention; only messages/output cap differ.
            request = request_controls(config)
            request["messages"] = [{"role": "user", "content": instruction}]
            request["max_tokens"] = min(config.max_tokens, 64)
            directory = runner.root / "items" / ("0" * 20) / name
            try:
                response = runner._call(directory, request)
            except CallFailure as error:
                results.append(
                    {"probe": name, "status": "failed", "reason": error.category}
                )
                break
            choices = response.get("choices", [])
            choice = (
                choices[0]
                if isinstance(choices, list) and len(choices) == 1
                and isinstance(choices[0], dict)
                else {}
            )
            finish = choice.get("finish_reason")
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if name == "short":
                try:
                    passed = finish == "stop" and json.loads(content) == {"ok": True}
                except (ValueError, TypeError):
                    passed = False
            else:
                passed = finish == "length"
            results.append({
                "probe": name,
                "status": "passed" if passed else "failed",
                "finish_reason": finish,
                "usage": response.get("usage"),
            })
            if not passed:
                break
        result = {
            "passed": len(results) == 2 and all(r["status"] == "passed" for r in results),
            "results": results,
            "attempt_count": runner.calls,
            "reserved_tokens": runner.reserved_tokens,
            "accounted_tokens": runner.accounted_tokens,
            "scope": "Synthetic API compatibility only; not DAG quality or server-side billing guarantee",
        }
        write_once(runner.root / "probe_result.json", result)
        return result
