"""MMLU-only, opt-in classification of a provider's token-cap overrun.

The response remains in the raw ledger and its reported usage is fully charged.
Only an otherwise valid, stopped response that exceeds both the requested
completion cap and its conservative reservation becomes infrastructure N/A.
"""

import hashlib
from pathlib import Path

from .storage import read_json, write_once


POLICY = {
    "schema_version": "mmlu_provider_overrun_policy_v1",
    "model": "deepseek-v4-flash",
    "prompt_version": "mmlu-general-thinking-v4",
    "only_violation": "usage_exceeds_reserved_allowance",
    "required_warning": "completion_exceeds_requested_max_tokens",
    "required_finish_reason": "stop",
    "classification": "infrastructure_omitted",
    "authorized_by": "user_2026-09-29",
}


class ProviderOverrunOmission(Exception):
    """A response was recorded and charged, but must not be parsed as data."""


def load_provider_overrun_policy(subject_root, config):
    if config.task_type != "mmlu":
        return None
    subject_root = Path(subject_root)
    manifest = subject_root.parent.parent / "provider_overrun_policy.json"
    if not manifest.exists():
        return None
    if (manifest.is_symlink() or subject_root.parent.name != "subjects"
            or config.model != POLICY["model"]
            or config.prompt_version != POLICY["prompt_version"]
            or not config.strict_response_contract
            or not config.content_gated_response):
        raise ValueError("provider overrun policy outside its frozen MMLU protocol")
    if read_json(manifest) != POLICY:
        raise ValueError("provider overrun policy changed")
    return POLICY


def eligible_provider_overrun(policy, request, response, check):
    if policy is None or check["violations"] != [policy["only_violation"]]:
        return False
    if check.get("warnings") != [policy["required_warning"]]:
        return False
    payload = request["payload"]
    choices = response.get("choices")
    usage = response.get("usage")
    return (
        payload.get("model") == response.get("model") == policy["model"]
        and isinstance(choices, list) and len(choices) == 1
        and isinstance(choices[0], dict)
        and choices[0].get("finish_reason") == policy["required_finish_reason"]
        and isinstance(usage, dict)
        and type(usage.get("completion_tokens")) is int
        and usage["completion_tokens"] > payload["max_tokens"]
        and check["reported_tokens"] > request["reserved_tokens"]
    )


def record_provider_overrun(attempt, policy, check):
    """Write immutable, text-free evidence identifying the exact raw files."""
    attempt = Path(attempt)
    request_path, response_path = attempt / "request.json", attempt / "response.json"
    request = read_json(request_path)
    evidence = {
        "schema_version": policy["schema_version"],
        "classification": policy["classification"],
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
        "response_sha256": hashlib.sha256(response_path.read_bytes()).hexdigest(),
        "reserved_tokens": request["reserved_tokens"],
        "reported_tokens": check["reported_tokens"],
        "overage_tokens": check["reported_tokens"] - request["reserved_tokens"],
        "contract_violations": check["violations"],
        "contract_warnings": check["warnings"],
    }
    write_once(attempt / "provider_overrun_omission.json", evidence)
    return evidence
