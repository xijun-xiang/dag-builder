"""Greedy full-cohort E3: immutable inputs, persistent GPU workers, CPU audit.

This is a new protocol, not a continuation or relabelling of old T=.7 outputs.
One attempt per question. Complete raw output can be scored on recovery; a
generation attempt without sealed raw output is uncertain and cannot be retried.
"""
from copy import deepcopy
import math
import os
from pathlib import Path
import time

from ..io import digest, read, save, sha256, verify
from ..locking import exclusive_lock
from ..metrics import pair
from . import greedy_data
from .audit import _score_matches, token_encoder
from .backend import E3HFBackend, E3MockBackend, generation_options
from .evaluate import evaluate_answer
from .greedy_parse import VERSION as PARSER_VERSION, parse
from .metrics import summarize_trace
from .protocol import score_text_pair
from .run import _batch_seed, code_hashes
from .schema import require, validate_problem

VERSION = "e3-native-greedy-v1"
SLOTS = {"qwen25": "Qwen2.5-7B-Instruct", "phi4mini": "Phi-4-mini-instruct", "qwen3": "Qwen3-14B"}
PUBLIC_COUNTS = greedy_data.COUNTS


def make_config(old: dict) -> dict:
    config = {k: deepcopy(old[k]) for k in ("backend", "model", "hf_runtime", "generation", "scoring")}
    config.update(protocol_version=VERSION, prompt_version="native-greedy-prompt-v1",
                  parser_version=PARSER_VERSION, shards=8, automatic_generation_retries=0)
    config["generation"].update(temperature=0., do_sample=False)
    return config


def validate_config(config: dict):
    require(set(config) == {"backend", "model", "hf_runtime", "generation", "scoring",
            "protocol_version", "prompt_version", "parser_version", "shards", "automatic_generation_retries"}, "config fields")
    require(config["protocol_version"] == VERSION and config["prompt_version"] == "native-greedy-prompt-v1"
            and config["parser_version"] == PARSER_VERSION, "greedy protocol changed")
    require(config["backend"] in ("hf", "mock") and config["model"]["id"] in SLOTS.values(), "model/backend")
    require(config["generation"] == {"samples_per_question": 1, "temperature": 0., "do_sample": False,
            "top_p": 1., "top_k": 0, "repetition_penalty": 1., "num_beams": 1, "batch_size": 8,
            "max_new_tokens": {"knowledge_math": 8192, "code": 16384}, "master_seed": 2026092903}, "generation changed")
    require(config["scoring"] == {"temperature": 1., "target_batch_size": 1}, "score temperature changed")
    require(config["shards"] == 8 and config["automatic_generation_retries"] == 0, "execution changed")
    if config["model"]["id"] == SLOTS["qwen3"]:
        require(config["hf_runtime"]["chat_template_kwargs"] == {"enable_thinking": False}, "thinking mode changed")


def implementation_hashes():
    hashes = code_hashes()
    root = Path(__file__).resolve().parents[3]
    for name in ("e3_greedy_launch.py", "b1-e3-greedy.sbatch", "e3_greedy_submit.py"):
        path = root / "scripts" / name
        if path.exists():
            hashes["scripts/" + name] = sha256(path)
    return hashes


def batches_for(problems: list, config: dict) -> list:
    groups = {}
    for problem in sorted(problems, key=lambda p: (p["benchmark"], p["problem_id"])):
        groups.setdefault(problem["benchmark"], []).append(problem["problem_id"])
    grouped = {}
    for benchmark, ids in sorted(groups.items()):
        batches = []
        for start in range(0, len(ids), 8):
            members = ids[start:start + 8]
            batch = {"benchmark": benchmark, "problem_ids": members,
                     "seed": _batch_seed(config["generation"]["master_seed"], config["model"]["id"], members)}
            batch["batch_id"] = digest(batch)
            batches.append(batch)
        grouped[benchmark] = batches
    # First batch of each benchmark belongs to the formal cohort, not a discarded canary.
    return [v[0] for v in grouped.values()] + [b for v in grouped.values() for b in v[1:]]


def init(prepared: Path, configs: dict, policy: dict, output: Path) -> dict:
    meta, problems = greedy_data.validate(prepared)
    require(set(configs) == set(SLOTS), "exactly three models required")
    require(not output.exists(), "new run must not exist")
    for slot, config in configs.items():
        validate_config(config)
        require(config["model"]["id"] == SLOTS[slot] and
                (config["backend"] == "hf") == meta["scientific_evidence"], "model/data mismatch")
    output.mkdir(mode=0o700, parents=True)
    for name in ("inputs", "grading", "logs", "submissions", "scratch"):
        (output / name).mkdir(mode=0o700)
    save(output / "inputs/problems.json", problems)
    save(output / "inputs/prepared-manifest.json", meta)
    save(output / "grading/answers.json", read(prepared / "grading/answers.json"))
    save(output / "grading/policy.json", policy)
    files = {str(p.relative_to(output)): sha256(p) for name in ("inputs", "grading")
             for p in (output / name).glob("*.json")}
    plans = {}
    for slot, config in configs.items():
        folder = output / slot
        folder.mkdir(mode=0o700)
        for name in ("attempts", "raw", "receipts", "scores", "outcomes", "workers", "locks", "logs", "scratch", "startup"):
            (folder / name).mkdir(mode=0o700)
        plans[slot] = batches_for(problems, config)
        save(folder / "config.json", config)
        save(folder / "batches.json", plans[slot])
        for name in ("config.json", "batches.json"):
            files[f"{slot}/{name}"] = sha256(folder / name)
        if config["backend"] == "hf":
            save(folder / "model-files.json", read(config["model"]["files_manifest"]))
            files[f"{slot}/model-files.json"] = sha256(folder / "model-files.json")
    manifest = {"protocol_version": VERSION, "files": files, "code_hashes": implementation_hashes(),
        "scientific_evidence": meta["scientific_evidence"], "counts": meta["counts"],
        "initial_batches": len(meta["counts"]), "generation_attempts_per_question": 1,
        "coverage_review": {"overall": .9, "per_benchmark": .75},
        "analysis": {"bootstrap_draws": 5000, "seed": 2026100601,
                     "primary": ["G", "M"], "auxiliary": ["NLL"],
                     "step_bins": [[2, 2], [3, 5], [6, None]]}}
    manifest["protocol_id"] = digest(manifest)
    save(output / "manifest.json", manifest)
    return {"protocol_id": manifest["protocol_id"], "counts": meta["counts"],
            "batches_per_model": {k: len(v) for k, v in plans.items()}}


def load(root: Path, slot: str):
    require(slot in SLOTS and not root.is_symlink(), "invalid run/slot")
    manifest = read(root / "manifest.json")
    require(manifest["protocol_version"] == VERSION and manifest["protocol_id"] ==
            digest({k: v for k, v in manifest.items() if k != "protocol_id"}), "protocol changed")
    verify(root, manifest["files"])
    require(manifest["code_hashes"] == implementation_hashes(), "frozen code changed")
    config = read(root / slot / "config.json")
    validate_config(config)
    problems = read(root / "inputs/problems.json")
    for problem in problems:
        validate_problem(problem)
    batches = read(root / slot / "batches.json")
    require(batches == batches_for(problems, config), "batch schedule changed")
    return manifest, config, {p["problem_id"]: p for p in problems}, batches


def reference(backend, problem: dict, protocol_id: str) -> dict:
    base = backend.base_prompt(problem)
    full = base + "<step>Read the task carefully.</step>\n<step>"
    deleted, target = base + "\n<step>", "Identify the next useful reasoning operation."
    a, b = backend.score_pair(full, deleted, target), backend.score_pair(full, deleted, target)
    error = max(abs(x - y) for side in ("full_logprobs", "deleted_logprobs")
                for x, y in zip(a["evidence"][side], b["evidence"][side]))
    require(error <= 1e-5, "repeat scoring numerical failure")
    errors, native_losses = {}, {}
    if isinstance(backend, E3HFBackend):
        torch = backend.torch
        for label in ("full", "deleted"):
            context, ids = a["evidence"][label + "_context_ids"], a["evidence"]["target_ids"]
            tokens = torch.tensor([context + ids], dtype=torch.long, device=backend.config["device"])
            labels = tokens.clone()
            labels[:, :len(context)] = -100
            with torch.inference_mode():
                loss = backend.model(input_ids=tokens, attention_mask=torch.ones_like(tokens),
                                     labels=labels, use_cache=False).loss.item()
            errors[label] = abs(loss - a["score"][label + "_nll"])
            native_losses[label] = loss
        require(all(e <= .005 for e in errors.values()), "native masked-loss failure")
    return {"status": "PASS", "protocol_id": protocol_id, "repeat_max_abs": error,
            "masked_loss_errors": errors, "synthetic": not isinstance(backend, E3HFBackend),
            "versions": backend.versions, "first": a, "repeat": b, "native_losses": native_losses}


def verify_reference(receipt: dict, protocol_id: str, real: bool):
    """Recompute the numerical gate from retained token evidence, not a PASS label."""
    require(receipt["status"] == "PASS" and receipt["protocol_id"] == protocol_id and
            receipt["synthetic"] == (not real), "reference identity")
    for result in (receipt["first"], receipt["repeat"]):
        evidence = result["evidence"]
        require(len(evidence["target_ids"]) == len(evidence["full_logprobs"]) ==
                len(evidence["deleted_logprobs"]) > 0, "reference token count")
        expected = pair(evidence["full_logprobs"], evidence["deleted_logprobs"])
        require(all(abs(result["score"][k] - v) <= 1e-8 for k, v in expected.items()), "reference arithmetic")
    first, repeat = receipt["first"]["evidence"], receipt["repeat"]["evidence"]
    require(all(first[k] == repeat[k] for k in ("target_ids", "full_context_ids", "deleted_context_ids")),
            "reference repeat input differs")
    delta = max(abs(x - y) for side in ("full_logprobs", "deleted_logprobs")
                for x, y in zip(first[side], repeat[side]))
    require(math.isfinite(delta) and delta <= 1e-5 and delta == receipt["repeat_max_abs"], "reference repeat failure")
    require(set(receipt["native_losses"]) == ({"full", "deleted"} if real else set()), "native loss missing")
    errors = {side: abs(loss - receipt["first"]["score"][side + "_nll"])
              for side, loss in receipt["native_losses"].items()}
    require(errors == receipt["masked_loss_errors"] and all(math.isfinite(v) and v <= .005 for v in errors.values()),
            "reference masked-loss failure")


def raw_batch(folder: Path, batch: dict, protocol_id: str) -> dict:
    path = folder / "raw" / (batch["batch_id"] + ".json")
    receipt = read(folder / "receipts" / path.name)
    require(receipt == {"protocol_id": protocol_id, "sha256": sha256(path)}, "raw not sealed")
    raw = read(path)
    require(raw["protocol_id"] == protocol_id and raw["batch"] == batch, "wrong raw provenance")
    require(raw["output"]["seed"] == batch["seed"] and
            raw["output"]["batch_size"] == len(batch["problem_ids"]), "raw batch settings changed")
    require([r["problem_id"] for r in raw["output"]["rows"]] == batch["problem_ids"], "raw question coverage")
    return raw


def process_batch(root: Path, slot: str, batch: dict, backend, manifest, problems) -> list:
    folder = root / slot
    bid, protocol_id = batch["batch_id"], manifest["protocol_id"]
    attempt, raw_path = folder / "attempts" / (bid + ".json"), folder / "raw" / (bid + ".json")
    selected = [problems[i] for i in batch["problem_ids"]]
    if not raw_path.exists():
        require(not attempt.exists(), "UNCERTAIN_GENERATION_NO_RETRY: " + bid)
        save(attempt, {"protocol_id": protocol_id, "batch": batch, "job_id": os.environ.get("SLURM_JOB_ID")})
        output = backend.generate_batch(selected, batch["seed"])
        save(raw_path, {"protocol_id": protocol_id, "batch": batch, "output": output})
        save(folder / "receipts" / raw_path.name, {"protocol_id": protocol_id, "sha256": sha256(raw_path)})
    require(read(attempt)["protocol_id"] == protocol_id and read(attempt)["batch"] == batch, "attempt differs")
    raw = raw_batch(folder, batch, protocol_id)
    encode = (lambda s: backend.tokenizer.encode(s, add_special_tokens=False)) if isinstance(backend, E3HFBackend) else lambda s: list(s.encode())
    result = []
    for problem, row in zip(selected, raw["output"]["rows"]):
        require(row["prompt"] == backend.base_prompt(problem), "generation prompt mismatch")
        parsed = parse(row["raw_text"], row["finish_reason"], problem["benchmark"])
        path = folder / "scores" / (digest(problem["problem_id"]) + ".json")
        if path.exists():
            saved = read(path)
            require(saved["protocol_id"] == protocol_id and saved["raw_sha256"] == sha256(raw_path), "score source changed")
            _score_matches(saved, parsed, row["prompt"], encode)
        else:
            steps = []
            if parsed["process_valid"]:
                for index in range(1, len(parsed["steps"])):
                    full, deleted, target = score_text_pair(row["prompt"], parsed, index)
                    steps.append({"index": index, "full_context_text": full, "deleted_context_text": deleted,
                                  **backend.score_pair(full, deleted, target)})
            saved = {"protocol_id": protocol_id, "problem_id": problem["problem_id"],
                "raw_sha256": sha256(raw_path), "parser_version": PARSER_VERSION,
                "status": "ok" if parsed["process_valid"] else "invalid_process", "reason": parsed["reason"],
                "steps": steps, "summary": summarize_trace([s["score"] for s in steps]) if parsed["process_valid"] else None}
            _score_matches(saved, parsed, row["prompt"], encode)
            save(path, saved)
        result.append({"problem_id": problem["problem_id"], "benchmark": problem["benchmark"],
                       "process_valid": parsed["process_valid"], "steps": len(parsed["steps"]),
                       "G_defined": bool(saved["summary"] and saved["summary"]["G"] is not None),
                       "finish_reason": row["finish_reason"]})
    return result


def coverage(rows: list, policy: dict) -> dict:
    groups = {}
    for row in rows:
        groups.setdefault(row["benchmark"], []).append(row)
    counts = {name: {"planned": len(group), "process_valid": sum(r["process_valid"] for r in group),
                    "G_defined": sum(r["G_defined"] for r in group)} for name, group in groups.items()}
    valid = sum(r["process_valid"] for r in rows)
    ok = bool(rows) and valid >= policy["overall"] * len(rows) and all(
        c["process_valid"] >= policy["per_benchmark"] * c["planned"] for c in counts.values())
    return {"coverage_gate": ok, "by_benchmark": counts, "planned": len(rows), "process_valid": valid}


def gpu_worker(root: Path, slot: str, shard: int, deadline: float = float("inf"), barrier: bool = True):
    manifest, config, problems, batches = load(root, slot)
    require(0 <= shard < 8, "shard outside allocation")
    if config["backend"] == "hf":
        require(bool(os.environ.get("SLURM_JOB_ID")) and os.environ.get("CUDA_VISIBLE_DEVICES") and
                root.resolve() == root and Path("/work/projects/polyullm/xxj/PALS") in root.parents, "GPU execution boundary")
    with exclusive_lock(root / slot / "locks" / f"gpu-{shard}.lock"):
        runtime = {**config, "device": "cuda:0"}
        backend = E3HFBackend(config["model"]["path"], runtime) if config["backend"] == "hf" else E3MockBackend("", runtime)
        receipt = reference(backend, next(iter(problems.values())), manifest["protocol_id"])
        run_id = os.environ.get("SLURM_JOB_ID", "mock")
        save(root / slot / "workers" / f"reference-{run_id}-{shard}.json", receipt)
        own = [(i, b) for i, b in enumerate(batches) if i % 8 == shard]
        recent = {}
        for initial in (True, False):
            observed = []
            for index, batch in own:
                if (index < manifest["initial_batches"]) != initial:
                    continue
                if not initial and time.time() >= deadline:
                    save(root / slot / "workers" / f"partial-{run_id}-{shard}.json", {"status": "CHECKPOINTED", "next_batch": batch["batch_id"]})
                    return False
                latest = process_batch(root, slot, batch, backend, manifest, problems)
                observed.extend(latest)
                window = recent.setdefault(batch["benchmark"], [])
                window.extend(latest)
                del window[:-32]
                if len(window) == 32 and sum(r["process_valid"] for r in window) < 24:
                    save(root / slot / "workers" / f"format-stop-{run_id}-{shard}.json",
                         {"status": "SYSTEMATIC_FORMAT_FAILURE", "benchmark": batch["benchmark"], "rows": window})
                    raise RuntimeError("systematic format failure in 32-question window; raw retained")
                print(f"{slot} shard={shard} batch={index} persisted", flush=True)
            if initial:
                path = root / slot / "startup" / f"{run_id}-{shard}.json"
                save(path, {"protocol_id": manifest["protocol_id"], "rows": observed})
                if barrier:
                    go = root / slot / "startup" / f"{run_id}-go.json"
                    while not go.exists():
                        if time.time() >= deadline:
                            raise RuntimeError("startup deadline; no new generation")
                        time.sleep(1)
                    require(read(go)["protocol_id"] == manifest["protocol_id"], "wrong startup release")
        save(root / slot / "workers" / f"gpu-complete-{run_id}-{shard}.json",
             {"status": "PASS", "protocol_id": manifest["protocol_id"], "batches": len(own)})
    return True


def evaluate(root: Path, slot: str, shard: int):
    manifest, config, problems, batches = load(root, slot)
    require(0 <= shard < 8, "shard outside allocation")
    if config["backend"] == "hf":
        require(os.environ.get("SLURM_JOB_ID") and not os.environ.get("CUDA_VISIBLE_DEVICES") and
                root.resolve() == root and Path("/work/projects/polyullm/xxj/PALS") in root.parents,
                "CPU evaluation boundary")
    folder = root / slot
    gold = read(root / "grading/answers.json")
    policy = read(root / "grading/policy.json")
    scratch = folder / "scratch" / f"cpu-{shard}"
    scratch.mkdir(mode=0o700, exist_ok=True)
    with exclusive_lock(folder / "locks" / f"evaluate-{shard}.lock"):
        for index, batch in enumerate(batches):
            if index % 8 != shard:
                continue
            raw = raw_batch(folder, batch, manifest["protocol_id"])
            for row in raw["output"]["rows"]:
                item = row["problem_id"]
                path = folder / "outcomes" / (digest(item) + ".json")
                raw_hash = sha256(folder / "raw" / (batch["batch_id"] + ".json"))
                if path.exists():
                    require(read(path)["raw_sha256"] == raw_hash and read(path)["protocol_id"] == manifest["protocol_id"], "outcome source differs")
                    continue
                parsed = parse(row["raw_text"], row["finish_reason"], problems[item]["benchmark"])
                outcome = evaluate_answer(problems[item], gold[item], parsed["answer"], code_policy=policy, scratch=scratch)
                save(path, {"protocol_id": manifest["protocol_id"], "problem_id": item,
                            "raw_sha256": raw_hash, **outcome})
    return {"status": "PASS", "slot": slot, "shard": shard}


def audit(root: Path, slot: str, include_outcomes=True):
    manifest, config, problems, batches = load(root, slot)
    folder, protocol_id = root / slot, manifest["protocol_id"]
    encode = token_encoder(config)
    tokenizer = None
    if config["backend"] == "hf":
        from transformers import AutoTokenizer
        from ..model_policy import local_code_policy
        tokenizer = AutoTokenizer.from_pretrained(config["model"]["path"], local_files_only=True,
            trust_remote_code=local_code_policy(config["model"]["path"],
                {**config["hf_runtime"], "model_revision": config["model"]["revision"]}))
    expected_batches = {b["batch_id"] + ".json" for b in batches}
    expected_items = {digest(i) + ".json" for i in problems}
    for name, expected in (("attempts", expected_batches), ("raw", expected_batches),
                           ("receipts", expected_batches), ("scores", expected_items)):
        require({p.name for p in (folder / name).glob("*.json")} == expected, "incomplete/extra " + name)
    if include_outcomes:
        require({p.name for p in (folder / "outcomes").glob("*.json")} == expected_items, "outcome coverage")
    rows, hashes = [], {}
    for shard in range(8):
        completions = sorted((folder / "workers").glob(f"gpu-complete-*-{shard}.json"))
        require(len(completions) == 1, "missing/ambiguous GPU worker completion")
        completion = completions[0]
        require(read(completion) == {"status": "PASS", "protocol_id": protocol_id,
                "batches": sum(i % 8 == shard for i in range(len(batches)))}, "worker completion differs")
        reference_path = completion.with_name(completion.name.replace("gpu-complete-", "reference-", 1))
        verify_reference(read(reference_path), protocol_id, config["backend"] == "hf")
        for path in (completion, reference_path):
            hashes[str(path.relative_to(folder))] = sha256(path)
    for batch in batches:
        raw_path = folder / "raw" / (batch["batch_id"] + ".json")
        raw = raw_batch(folder, batch, protocol_id)
        attempt = read(folder / "attempts" / raw_path.name)
        require(attempt["batch"] == batch and attempt["protocol_id"] == protocol_id, "attempt identity changed")
        for generated in raw["output"]["rows"]:
            item = generated["problem_id"]
            problem = problems[item]
            if tokenizer is not None:
                from .protocol import messages
                expected_prompt = tokenizer.apply_chat_template(messages(problem, config["prompt_version"]),
                    tokenize=False, add_generation_prompt=True, **config["hf_runtime"]["chat_template_kwargs"])
                require(generated["prompt"] == expected_prompt and
                        generated["prompt_token_ids"] == encode(expected_prompt), "prompt/token replay differs")
                require(tokenizer.decode(generated["generated_token_ids"], skip_special_tokens=True) ==
                        generated["raw_text"], "raw generated token replay differs")
                require(raw["output"]["generation_contract"]["decoding"] == generation_options(config["generation"]),
                        "not the frozen greedy decoder")
            parsed = parse(generated["raw_text"], generated["finish_reason"], problem["benchmark"])
            path = folder / "scores" / (digest(item) + ".json")
            saved = read(path)
            require(saved["protocol_id"] == protocol_id and saved["raw_sha256"] == sha256(raw_path)
                    and saved["problem_id"] == item, "score provenance")
            _score_matches(saved, parsed, generated["prompt"], encode)
            outcome_path = folder / "outcomes" / path.name
            outcome = read(outcome_path) if include_outcomes else {"correct": None, "status": "not_evaluated"}
            if include_outcomes:
                require(outcome["raw_sha256"] == sha256(raw_path) and outcome["protocol_id"] == protocol_id
                        and outcome["problem_id"] == item, "outcome provenance")
                hashes[str(outcome_path.relative_to(folder))] = sha256(outcome_path)
            hashes[str(path.relative_to(folder))] = sha256(path)
            rows.append({"problem_id": item, "benchmark": problem["benchmark"], "subset": problem["subset"],
                "process_valid": parsed["process_valid"], "process_reason": parsed["reason"],
                "step_count": len(parsed["steps"]), "step_token_lengths": [len(encode(s["text"])) for s in parsed["steps"]],
                "segmentation_methods": parsed["segmentation_methods"], "finish_reason": generated["finish_reason"],
                "summary": saved["summary"], "correct": outcome["correct"], "outcome_status": outcome["status"]})
        for name in ("raw", "receipts", "attempts"):
            path = folder / name / raw_path.name
            hashes[str(path.relative_to(folder))] = sha256(path)
    coverage_check = coverage([{**row, "G_defined": bool(row["summary"] and row["summary"]["G"] is not None)}
                               for row in rows], manifest["coverage_review"])
    result = {"status": "PASS", "protocol_id": protocol_id, "slot": slot, "items": rows,
              "coverage_review": coverage_check,
              "files": hashes, "include_outcomes": include_outcomes, "scientific_evidence": manifest["scientific_evidence"]}
    save(folder / ("audit.json" if include_outcomes else "gpu-audit.json"), result)
    require(coverage_check["coverage_gate"], "full cohort coverage needs user review; audit evidence retained")
    return {"status": "PASS", "slot": slot, "items": len(rows)}
