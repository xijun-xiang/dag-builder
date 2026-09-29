"""Immutable E3 run identities and once-only generation lifecycle."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..io import digest, read, save, sha256, verify
from ..locking import exclusive_lock
from ..model_policy import local_code_policy
from .backend import E3HFBackend, E3MockBackend
from .data import validate_prepared
from .evaluate import evaluate_answer
from .metrics import summarize_trace
from .protocol import parse_trace, score_text_pair
from .schema import CODE_BENCHMARKS, require

CONFIG_FIELDS = {"schema_version", "backend", "protocol_version", "prompt_version",
                 "parser_version", "scoring_version", "model", "hf_runtime", "generation",
                 "scoring", "common_scorer", "execution", "evaluation_manifest", "analysis"}


def code_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    files = sorted(root.rglob("*.py"))
    require(bool(files), "empty source snapshot")
    result = {str(p.relative_to(root)): sha256(p) for p in files}
    scripts = root.parents[1] / "scripts"
    for name in ("b1-e3-gpu.sbatch", "b1-e3-cpu.sbatch", "e3_slurm_worker.py",
                 "e3_canary.py", "e3_launch_workers.py", "e3_b1_init.py",
                 "b1-e3-init.sbatch", "e3_harness_selftest.py",
                 "b1-e3-harness.sbatch"):
        path = scripts / name
        if path.is_file():
            result["scripts/" + name] = sha256(path)
    return result


def _assert_fields(obj: dict, fields: set[str], label: str) -> None:
    require(isinstance(obj, dict) and set(obj) == fields, label + " fields mismatch")


def validate_config(config: dict) -> None:
    _assert_fields(config, CONFIG_FIELDS, "config")
    require(config["schema_version"] == "pals_e3_config_v1" and
            config["protocol_version"] == "native-trace-v1" and
            config["prompt_version"] == "native-trace-prompt-v1" and
            config["parser_version"] == "strict-tag-v1" and
            config["scoring_version"] == "adjacent-deletion-explicit-boundary-v1",
            "protocol version mismatch")
    require(config["backend"] in ("hf", "mock"), "unknown backend")
    _assert_fields(config["model"], {"id", "path", "revision", "files_manifest"}, "model")
    _assert_fields(config["hf_runtime"], {"dtype", "attention", "max_context", "cpu_threads",
                "chat_template_kwargs", "runtime_versions", "reviewed_local_code"}, "hf_runtime")
    _assert_fields(config["generation"], {"samples_per_question", "temperature", "top_p",
                "top_k", "repetition_penalty", "num_beams", "do_sample", "batch_size",
                "max_new_tokens", "master_seed"}, "generation")
    _assert_fields(config["generation"]["max_new_tokens"], {"knowledge_math", "code"},
                   "generation budget")
    _assert_fields(config["scoring"], {"temperature", "target_batch_size"}, "scoring")
    _assert_fields(config["common_scorer"], {"model_id", "fraction", "rounding",
                "selection_seed"}, "common_scorer")
    _assert_fields(config["execution"], {"shards", "automatic_generation_retries"}, "execution")
    _assert_fields(config["analysis"], {"bootstrap_draws", "bootstrap_seed"}, "analysis")
    g = config["generation"]
    require(g["samples_per_question"] == 1 and g["temperature"] == .7 and
            g["top_p"] == 1.0 and g["top_k"] == 0 and
            g["repetition_penalty"] == 1.0 and g["num_beams"] == 1 and
            g["do_sample"] is True and g["batch_size"] == 8 and
            g["max_new_tokens"] == {"knowledge_math": 8192, "code": 16384} and
            g["master_seed"] == 2026092903, "generation differs from frozen protocol")
    require(config["scoring"] == {"temperature": 1.0, "target_batch_size": 1},
            "scoring differs from frozen protocol")
    require(config["execution"] == {"shards": 8, "automatic_generation_retries": 0},
            "execution differs from frozen protocol")
    require(config["common_scorer"] == {"model_id": "Qwen3-14B", "fraction": .1,
            "rounding": "ceil", "selection_seed": 2026092903},
            "common scorer differs from frozen protocol")
    require(config["analysis"] == {"bootstrap_draws": 5000, "bootstrap_seed": 2026092903},
            "analysis differs from frozen protocol")
    require(type(config["hf_runtime"]["max_context"]) is int and
            config["hf_runtime"]["max_context"] > 0, "invalid context length")
    require(config["model"]["id"] in ("Qwen2.5-7B-Instruct", "Phi-4-mini-instruct",
                                      "Qwen3-14B"), "unplanned model")
    if config["model"]["id"] == "Qwen3-14B":
        require(config["hf_runtime"]["chat_template_kwargs"] == {"enable_thinking": False},
                "Qwen3 thinking must be explicitly disabled")
    require(isinstance(config["evaluation_manifest"], str), "evaluation manifest required")
    if config["backend"] == "hf":
        require(all(isinstance(config["model"][field], str) and
                    not config["model"][field].startswith("REPLACE")
                    for field in ("path", "revision", "files_manifest")),
                "unresolved model identity")
        require(Path(config["evaluation_manifest"]).is_file(),
                "frozen evaluation policy manifest missing")


def _model_files(config: dict) -> dict[str, str]:
    if config["backend"] == "mock":
        return {}
    model = Path(config["model"]["path"]).resolve(strict=True)
    require(model.is_dir() and (model / "config.json").is_file(), "model snapshot missing")
    runtime = {**config["hf_runtime"], "model_revision": config["model"]["revision"]}
    local_code_policy(model, runtime)
    files = {str(p.relative_to(model)): sha256(p) for p in sorted(model.rglob("*")) if p.is_file()
             and ".cache" not in p.relative_to(model).parts and ".git" not in p.relative_to(model).parts}
    require(any(name.endswith(".safetensors") for name in files), "safe tensor weights missing")
    declared = read(config["model"]["files_manifest"])
    require(files == declared, "model files differ from declared SHA256 manifest")
    return files


def _batch_seed(master: int, model: str, ids: list[str]) -> int:
    value = f"e3-batch-v1|{master}|{model}|{'|'.join(ids)}"
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") % (2 ** 63)


def _batches(problems: list[dict], config: dict) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for problem in problems:
        groups.setdefault(problem["benchmark"], []).append(problem)
    result = []
    for benchmark, rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda p: p["problem_id"])
        for start in range(0, len(rows), 8):
            ids = [p["problem_id"] for p in rows[start:start + 8]]
            batch = {"benchmark": benchmark, "problem_ids": ids,
                     "seed": _batch_seed(config["generation"]["master_seed"],
                                         config["model"]["id"], ids)}
            batch["batch_id"] = digest(batch)
            result.append(batch)
    return result


def init_run(prepared: str | Path, config_file: str | Path, output: str | Path) -> dict:
    prepared, config_file, output = Path(prepared), Path(config_file), Path(output)
    prepared_manifest, problems = validate_prepared(prepared)
    config = read(config_file)
    validate_config(config)
    require(not (config["backend"] == "hf" and
                 prepared_manifest["schema_version"] != "pals_e3_prepared_v1"),
            "scientific model requires complete original cohort")
    require(config["backend"] != "mock" or
            prepared_manifest["schema_version"] == "pals_e3_mock_prepared_v1",
            "mock run requires synthetic prepared cohort")
    require(not output.exists(), "run directory already exists")
    model_files = _model_files(config)
    batches = _batches(problems, config)
    prepared_hash = sha256(prepared / "manifest.json")
    source_hashes = code_hashes()
    protocol = {"schema_version": "pals_e3_run_v1", "config_sha256": sha256(config_file),
                "prepared_manifest_sha256": prepared_hash, "code_hashes": source_hashes,
                "model_files": model_files, "batches_sha256": digest(batches),
                "scientific_evidence": config["backend"] == "hf"}
    if config["backend"] == "hf":
        protocol["evaluation_manifest_sha256"] = sha256(config["evaluation_manifest"])
    protocol["protocol_id"] = digest(protocol)
    output.mkdir(mode=0o700, parents=True)
    for name in ("inputs", "batches", "attempts", "generation_batches", "parsed",
                 "scores", "outcomes", "common_scores", "locks", "workers", "failures"):
        (output / name).mkdir(mode=0o700)
    save(output / "inputs" / "problems.json", problems)
    save(output / "inputs" / "common_subset.json", read(prepared / "common_subset.json"))
    save(output / "inputs" / "prepared_manifest.json", prepared_manifest)
    save(output / "inputs" / "grading.json", read(prepared / "grading" / "answers.json"))
    save(output / "config.json", config)
    save(output / "batches.json", batches)
    protocol["input_files"] = {str(p.relative_to(output)): sha256(p) for p in
        sorted((output / "inputs").rglob("*.json"))}
    protocol["input_files"].update({"config.json": sha256(output / "config.json"),
                                     "batches.json": sha256(output / "batches.json")})
    protocol["protocol_id"] = digest({k: v for k, v in protocol.items() if k != "protocol_id"})
    save(output / "protocol.json", protocol)
    return {"run": str(output), "batches": len(batches), "problems": len(problems),
            "scientific_evidence": protocol["scientific_evidence"]}


def validate_run(run: str | Path) -> tuple[dict, dict, list[dict], list[dict]]:
    run = Path(run)
    protocol = read(run / "protocol.json")
    require(protocol["protocol_id"] == digest({k: v for k, v in protocol.items()
                                                if k != "protocol_id"}), "run protocol changed")
    verify(run, protocol["input_files"])
    require(code_hashes() == protocol["code_hashes"], "E3 source changed since run init")
    config, batches, problems = read(run / "config.json"), read(run / "batches.json"), read(run / "inputs" / "problems.json")
    validate_config(config)
    if config["backend"] == "hf":
        require(sha256(config["evaluation_manifest"]) ==
                protocol["evaluation_manifest_sha256"], "evaluation policy changed")
    require(protocol["batches_sha256"] == digest(batches), "batch plan changed")
    return protocol, config, batches, problems


def _backend(config: dict):
    if config["backend"] == "hf":
        for name, expected in read(config["model"]["files_manifest"]).items():
            require(sha256(Path(config["model"]["path"]) / name) == expected,
                    "model snapshot changed: " + name)
    runtime = {**config, "device": "cuda:0" if config["backend"] == "hf" else "cpu"}
    return (E3HFBackend if config["backend"] == "hf" else E3MockBackend)(
        config["model"]["path"], runtime)


def _generation(run: Path, batch: dict, protocol: dict, backend, by_id: dict) -> dict:
    raw_file = run / "generation_batches" / (batch["batch_id"] + ".json")
    started = run / "attempts" / (batch["batch_id"] + ".json")
    if raw_file.exists():
        saved = read(raw_file)
        require(started.exists() and saved["batch"] == batch and
                saved["protocol_id"] == protocol["protocol_id"], "saved generation identity mismatch")
        return saved
    require(not started.exists(), "UNCERTAIN_GENERATION: started without committed raw")
    save(started, {"batch_id": batch["batch_id"], "protocol_id": protocol["protocol_id"],
                   "state": "attempt_started", "slurm_job_id": os.environ.get("SLURM_JOB_ID")})
    raw = backend.generate_batch([by_id[item] for item in batch["problem_ids"]], batch["seed"])
    require([row["problem_id"] for row in raw["rows"]] == batch["problem_ids"],
            "generation returned unexpected question order")
    saved = {"batch": batch, "protocol_id": protocol["protocol_id"],
             "scientific_evidence": protocol["scientific_evidence"], **raw}
    save(raw_file, saved)
    return saved


def _scores_for_batch(run: Path, batch: dict, protocol: dict, backend, by_id: dict) -> int:
    raw_file = run / "generation_batches" / (batch["batch_id"] + ".json")
    require(raw_file.is_file(), "generation batch missing")
    raw = read(raw_file)
    require(raw["batch"] == batch and raw["protocol_id"] == protocol["protocol_id"],
            "generation identity mismatch")
    require([r["problem_id"] for r in raw["rows"]] == batch["problem_ids"],
            "generation membership mismatch")
    completed = 0
    for row in raw["rows"]:
        item = row["problem_id"]
        parsed_file, score_file = (run / "parsed" / (digest(item) + ".json"),
                                   run / "scores" / (digest(item) + ".json"))
        replay = parse_trace(row["raw_text"], row["finish_reason"])
        require(replay == row["parse"], "stored parse differs from replay")
        if not parsed_file.exists():
            save(parsed_file, {"problem_id": item, "raw_sha256": sha256(raw_file), "parse": replay})
        else:
            require(read(parsed_file) == {"problem_id": item, "raw_sha256": sha256(raw_file),
                                          "parse": replay}, "saved parse differs")
        if score_file.exists():
            require(read(score_file)["raw_sha256"] == sha256(raw_file), "score raw identity mismatch")
            completed += 1
            continue
        if not replay["process_valid"]:
            result = {"problem_id": item, "status": "invalid_process", "reason": replay["reason"],
                      "steps": [], "summary": None}
        else:
            base = backend.base_prompt(by_id[item])
            require(base == row["prompt"], "generation/scoring prompt mismatch")
            steps = []
            for index in range(1, len(replay["steps"])):
                full, deleted, target = score_text_pair(base, replay, index)
                scored = backend.score_pair(full, deleted, target)
                steps.append({"index": index, "full_context_text": full,
                              "deleted_context_text": deleted, **scored})
            result = {"problem_id": item, "status": "ok", "reason": None, "steps": steps,
                      "summary": summarize_trace([step["score"] for step in steps])}
        save(score_file, {"protocol_id": protocol["protocol_id"],
                          "generation_model": read(run / "config.json")["model"]["id"],
                          "scorer_model": read(run / "config.json")["model"]["id"],
                          "raw_sha256": sha256(raw_file), "scientific_evidence":
                          protocol["scientific_evidence"], **result})
        completed += 1
    return completed


def _evaluate_batch(run: Path, batch: dict, protocol: dict, by_id: dict,
                    grading: dict, code_policy: dict | None) -> int:
    raw_file = run / "generation_batches" / (batch["batch_id"] + ".json")
    require(raw_file.is_file(), "generation batch missing")
    raw = read(raw_file)
    require(raw["batch"] == batch and raw["protocol_id"] == protocol["protocol_id"],
            "generation identity mismatch")
    require([r["problem_id"] for r in raw["rows"]] == batch["problem_ids"],
            "generation membership mismatch")
    for row in raw["rows"]:
        item = row["problem_id"]
        parsed = parse_trace(row["raw_text"], row["finish_reason"])
        require(parsed == row["parse"], "stored parse differs from replay")
        outcome_file = run / "outcomes" / (digest(item) + ".json")
        result = {"problem_id": item, "protocol_id": protocol["protocol_id"],
                  "raw_sha256": sha256(raw_file),
                  "answer": parsed["answer"],
                  **evaluate_answer(by_id[item], grading[item], parsed["answer"],
                                    code_policy=code_policy, scratch=run / "scratch" / "code-eval")}
        if outcome_file.exists():
            require(read(outcome_file) == result, "saved outcome differs")
        else:
            save(outcome_file, result)
    return len(raw["rows"])


def _common_score(run: Path, source: Path, source_config: dict, source_batches: list[dict],
                  protocol: dict, backend, selected: set[str], shard: int,
                  by_id: dict) -> int:
    name = source_config["model"]["id"]
    require(name != "Qwen3-14B", "Qwen3 own scores already provide common scoring")
    folder = run / "common_scores" / name
    folder.mkdir(mode=0o700, exist_ok=True)
    completed = 0
    for index, batch in enumerate(source_batches):
        if index % 8 != shard or not selected.intersection(batch["problem_ids"]):
            continue
        raw_file = source / "generation_batches" / (batch["batch_id"] + ".json")
        require(raw_file.is_file(), "source generation missing")
        raw = read(raw_file)
        require(raw["batch"] == batch and
                [r["problem_id"] for r in raw["rows"]] == batch["problem_ids"],
                "source generation membership changed")
        for row in raw["rows"]:
            item = row["problem_id"]
            if item not in selected:
                continue
            parsed = parse_trace(row["raw_text"], row["finish_reason"])
            require(parsed == row["parse"], "source parse changed")
            out = folder / (digest(item) + ".json")
            if out.exists():
                require(read(out)["raw_sha256"] == sha256(raw_file), "common score source changed")
                completed += 1
                continue
            if not parsed["process_valid"]:
                result = {"status": "invalid_process", "reason": parsed["reason"],
                          "steps": [], "summary": None}
            else:
                base = backend.base_prompt(by_id[item])
                steps = []
                for step_index in range(1, len(parsed["steps"])):
                    full, deleted, target = score_text_pair(base, parsed, step_index)
                    scored = backend.score_pair(full, deleted, target)
                    steps.append({"index": step_index, "full_context_text": full,
                                  "deleted_context_text": deleted, **scored})
                result = {"status": "ok", "reason": None, "steps": steps,
                          "summary": summarize_trace([step["score"] for step in steps])}
            save(out, {"problem_id": item, "source_model": name, "scorer_model": "Qwen3-14B",
                       "source_protocol_id": raw["protocol_id"], "protocol_id": protocol["protocol_id"],
                       "raw_sha256": sha256(raw_file), **result})
            completed += 1
    return completed


def canary_batches(batches: list[dict]) -> set[str]:
    first = {}
    for batch in batches:
        first.setdefault(batch["benchmark"], batch["batch_id"])
    return set(first.values())


def worker(run: str | Path, stage: str, shard: int, source_run: str | Path | None = None,
           canary: bool = False) -> dict:
    run = Path(run).resolve(strict=True)
    protocol, config, batches, problems = validate_run(run)
    require(stage in ("generate", "score", "evaluate", "common-score"), "unknown stage")
    require(not (canary and stage == "common-score"), "common scorer is post-canary")
    require(0 <= shard < config["execution"]["shards"], "invalid shard")
    if config["backend"] == "hf":
        require(bool(os.environ.get("SLURM_JOB_ID")), "HF worker needs Slurm allocation")
        allowed = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
        require(allowed in run.parents, "HF run must remain inside PALS project")
        reference = read(run / "reference.json")
        require(reference["protocol_id"] == protocol["protocol_id"] and
                reference["status"] == "PASS" and
                reference["repeat_max_abs"] <= 1e-5 and
                all(value <= .005 for value in reference["masked_loss_errors"].values()),
                "real-model numerical reference gate failed")
    canary_ids = canary_batches(batches) if canary else None
    selected = [batch for index, batch in enumerate(batches)
                if index % config["execution"]["shards"] == shard and
                (canary_ids is None or batch["batch_id"] in canary_ids)]
    by_id = {p["problem_id"]: p for p in problems}
    grading = read(run / "inputs" / "grading.json") if stage == "evaluate" else None
    code_policy = (read(config["evaluation_manifest"])
                   if stage == "evaluate" and config["backend"] == "hf" else None)
    source_info = None
    if stage == "common-score":
        require(source_run is not None and config["model"]["id"] == "Qwen3-14B",
                "common scorer requires Qwen3 run and source run")
        source = Path(source_run).resolve(strict=True)
        source_protocol, source_config, source_batches, source_problems = validate_run(source)
        require(source_protocol["prepared_manifest_sha256"] == protocol["prepared_manifest_sha256"]
                and {p["problem_id"] for p in source_problems} == set(by_id),
                "common scorer cohort mismatch")
        source_info = (source, source_config, source_batches)
        lock_name = f"common-score-{source_config['model']['id']}-{shard}.lock"
    else:
        require(source_run is None, "source run only applies to common scoring")
        lock_name = f"{stage}{'-canary' if canary else ''}-{shard}.lock"
    with exclusive_lock(run / "locks" / lock_name):
        backend = None
        completed = 0
        if stage == "common-score":
            backend = _backend(config)
            source, source_config, source_batches = source_info
            completed = _common_score(run, source, source_config, source_batches, protocol,
                                      backend, set(read(run / "inputs" / "common_subset.json")),
                                      shard, by_id)
        else:
            for batch in selected:
                if stage != "evaluate" and backend is None:
                    backend = _backend(config)
                if stage == "generate":
                    _generation(run, batch, protocol, backend, by_id)
                    completed += len(batch["problem_ids"])
                elif stage == "score":
                    completed += _scores_for_batch(run, batch, protocol, backend, by_id)
                else:
                    completed += _evaluate_batch(run, batch, protocol, by_id, grading, code_policy)
        status = {"stage": stage + ("-canary" if canary else ""), "shard": shard, "batches": len(selected),
                  "problems": completed, "protocol_id": protocol["protocol_id"]}
        if source_info:
            status["source_model"] = source_info[1]["model"]["id"]
            status["source_run_protocol_id"] = read(source_info[0] / "protocol.json")["protocol_id"]
        done = run / "workers" / (lock_name.removesuffix(".lock") + ".json")
        if done.exists():
            require(read(done) == status, "stage completion changed")
        else:
            save(done, status)
        return status
