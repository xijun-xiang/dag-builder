"""Explicit, separately frozen completion cohort; never relabel the old three models."""
from .schema import require

VERSION = "e3-native-greedy-two-model-extension-v1"
SLOTS = {"llama3": "Meta-Llama-3-8B-Instruct", "internlm3": "InternLM3-8B-Instruct"}
CONTEXTS = {"llama3": 8192, "internlm3": 32768}
REVISIONS = {"llama3": "e9f7e7d3fa08b550ea228e38bb5501d35be92c0d",
             "internlm3": "28c99415adaf61767bd1c619f4f99f308fdfd223"}


def budget_policy(slot):
    require(slot in SLOTS, "unknown extension model")
    return {"mode": "batch-context-cap-v1", "reserve_tokens": 64} if slot == "llama3" else {
        "mode": "fixed-v1", "reserve_tokens": 0}


def generation_budget(config, benchmark, prompt_lengths):
    """Cap by the padded batch width, never truncate input or extend model RoPE.

    All rows in the pre-frozen batch share this cap. This makes the physical
    padded sequence fit the model as well as each row's unpadded sequence.
    """
    require(bool(prompt_lengths) and all(type(n) is int and n > 0 for n in prompt_lengths),
            "missing/invalid prompt lengths")
    requested = config["generation"]["max_new_tokens"][
        "code" if benchmark in ("humaneval", "livecodebench") else "knowledge_math"]
    policy = config.get("budget_policy", {"mode": "fixed-v1", "reserve_tokens": 0})
    require(policy in ({"mode": "fixed-v1", "reserve_tokens": 0},
                       {"mode": "batch-context-cap-v1", "reserve_tokens": 64}), "unknown budget policy")
    width, limit = max(prompt_lengths), config["hf_runtime"]["max_context"]
    effective = min(requested, limit - width - policy["reserve_tokens"]) if policy["mode"] != "fixed-v1" else requested
    require(effective > 0 and width + effective + policy["reserve_tokens"] <= limit,
            "prompt plus output reservation exceeds context; no input truncation")
    return {"policy": policy, "requested_max_new_tokens": requested,
            "effective_max_new_tokens": effective, "prompt_width": width,
            "max_context": limit, "context_capped": effective < requested}


def verify_budget(config, batch, output):
    """Replay the budget from retained prompts, not a trusted PASS flag."""
    rows = output["rows"]
    expected = generation_budget(config, batch["benchmark"], [len(r["prompt_token_ids"]) for r in rows])
    contract = output["generation_contract"]
    require(contract["budget"] == expected and contract["max_new_tokens"] == expected["effective_max_new_tokens"]
            and contract["max_context"] == expected["max_context"], "generation budget evidence differs")
    for row in rows:
        n = len(row["generated_token_ids"])
        require(row["prompt_width"] == expected["prompt_width"] and row["left_pad_tokens"] ==
                expected["prompt_width"] - len(row["prompt_token_ids"]), "padding evidence differs")
        require(0 < n <= expected["effective_max_new_tokens"] and row["stop_token_length"] == n,
                "generated token budget violated")
        require(row["finish_reason"] in ("boundary", "eos", "length"), "unknown generation end")
        require(row["finish_reason"] != "length" or n == expected["effective_max_new_tokens"],
                "length termination before frozen budget")
    return expected
