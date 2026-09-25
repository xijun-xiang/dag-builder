"""Fail-closed checks for the API's reported controls, not semantic quality.

These checks detect violations after a response. They cannot enforce a provider's
billing limit or cancel work already running at the provider.
"""


def token_count(value):
    return type(value) is int and value >= 0


def reported_tokens(response):
    """Conservatively reconcile valid reported usage, even if fields disagree."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return 0
    candidates = [0]
    for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
        if token_count(usage.get(key)):
            candidates.append(usage[key])
    if all(token_count(usage.get(k)) for k in ("prompt_tokens", "completion_tokens")):
        candidates.append(usage["prompt_tokens"] + usage["completion_tokens"])
    return max(candidates)


def check_response(request, response, reserved_tokens, *, strict=False, content_gated=False):
    """Return only safe diagnostics; never include response text or credentials.

    Legacy runs may lack detailed usage. Strict new runs require all usage fields
    and the requested model ID. Explicit control violations are rejected in both.
    """
    violations = []
    warnings = []
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in usage or strict:
            if not token_count(usage.get(key)):
                violations.append("missing_or_invalid_" + key)
    if all(
        token_count(usage.get(k))
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    ):
        if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
            violations.append("inconsistent_usage_total")
    if token_count(usage.get("completion_tokens")):
        if usage["completion_tokens"] > request["max_tokens"]:
            (warnings if content_gated else violations).append("completion_exceeds_requested_max_tokens")
    if reported_tokens(response) > reserved_tokens:
        violations.append("usage_exceeds_reserved_allowance")
    disabled = (
        request.get("thinking", {}).get("type") == "disabled"
        or request.get("reasoning_effort") == "none"
    )
    choices = response.get("choices", [])
    if not isinstance(choices, list):
        choices = []  # The stage parser separately rejects malformed completions.
    has_reasoning = any(
        bool(c.get("message", {}).get("reasoning_content"))
        for c in choices
        if isinstance(c, dict) and isinstance(c.get("message"), dict)
    )
    details = usage.get("completion_tokens_details")
    reasoning_tokens = (
        details.get("reasoning_tokens") if isinstance(details, dict) else None
    )
    if disabled and (
        has_reasoning or (token_count(reasoning_tokens) and reasoning_tokens > 0)
    ):
        violations.append("reasoning_returned_when_disabled")
    if strict and response.get("model") != request.get("model"):
        violations.append("response_model_mismatch")
    result = {
        "contract_version": 2 if content_gated else 1,
        "strict": strict,
        "violations": violations,
        "reported_tokens": reported_tokens(response),
        "accounted_tokens": max(reserved_tokens, reported_tokens(response)),
    }
    if content_gated:
        result["warnings"] = warnings
        result["policy"] = "content_gated_v1; reported usage above reservation still stops scheduling"
    return result
