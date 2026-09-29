"""Real-model numerical gate before any scientific generation or scoring."""

from __future__ import annotations

import os
from pathlib import Path

from ..io import read, save
from .run import _backend, validate_run
from .schema import require


def reference_check(run: str | Path) -> dict:
    run = Path(run).resolve(strict=True)
    protocol, config, _, problems = validate_run(run)
    require(config["backend"] == "hf", "reference gate requires a real model")
    require(bool(os.environ.get("SLURM_JOB_ID")), "reference gate requires Slurm")
    allowed = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
    require(allowed in run.parents, "reference run outside PALS")
    path = run / "reference.json"
    if path.exists():
        saved = read(path)
        require(saved["protocol_id"] == protocol["protocol_id"] and saved["status"] == "PASS",
                "invalid existing reference gate")
        return saved
    backend = _backend(config)
    base = backend.base_prompt(problems[0])
    full = base + "<step>Read the task carefully.</step>\n<step>"
    deleted = base + "\n<step>"
    target = "Use the previous statement to identify a useful next action."
    first = backend.score_pair(full, deleted, target)
    second = backend.score_pair(full, deleted, target)
    largest = max(abs(a - b) for key in ("full_logprobs", "deleted_logprobs")
                  for a, b in zip(first["evidence"][key], second["evidence"][key]))
    require(largest <= 1e-5, "repeated scoring gate failed")
    torch = backend.torch
    errors = {}
    for label in ("full", "deleted"):
        evidence = first["evidence"]
        context, ids = evidence[label + "_context_ids"], evidence["target_ids"]
        tokens = torch.tensor([context + ids], dtype=torch.long, device=backend.config["device"])
        labels = tokens.clone()
        labels[:, :len(context)] = -100
        with torch.inference_mode():
            loss = backend.model(input_ids=tokens, attention_mask=torch.ones_like(tokens),
                                 labels=labels, use_cache=False).loss.item()
        errors[label] = abs(loss - first["score"][label + "_nll"])
    require(all(error <= .005 for error in errors.values()),
            "native masked-loss differs from token evidence")
    result = {"protocol_id": protocol["protocol_id"], "status": "PASS",
              "repeat_max_abs": largest, "masked_loss_errors": errors,
              "model_versions": backend.versions,
              "first_problem_id": problems[0]["problem_id"]}
    save(path, result)
    return result
