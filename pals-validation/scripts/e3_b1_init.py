"""Freeze the three existing B1 model snapshots and initialize E3 without inference.

Run only on a B1 Slurm CPU allocation. Inputs and outputs must all be below
/work/projects/polyullm/xxj/PALS. This script does not execute candidate code.
"""

import argparse
import os
from importlib.metadata import version
from pathlib import Path

from pals_validation.e3.data import validate_prepared
from pals_validation.e3 import code_harness, code_tests
from pals_validation.e3.protocol import messages
from pals_validation.e3.run import init_run
from pals_validation.io import read, save, sha256

ROOT = Path("/work/projects/polyullm/xxj/PALS")
SLOTS = {
    "qwen25": ("Qwen2.5-7B-Instruct", "a09a35458c702b33eeacc393d103063234e8bc28"),
    "phi4mini": ("Phi-4-mini-instruct", "cfbefacb99257ffa30c83adab238a50856ac3083"),
    "qwen3": ("Qwen3-14B", "40c069824f4251a91eefaf281ebe4c544efd3e18"),
}


def bounded(path: str, expected_parent: Path, *, must_exist: bool) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("All paths must be absolute")
    if must_exist:
        resolved = candidate.resolve(strict=True)
    else:
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("Output path already exists")
        candidate.parent.resolve(strict=True)
        resolved = candidate
    if expected_parent.resolve(strict=True) not in resolved.parents:
        raise ValueError("E3 path crosses its approved B1 boundary")
    return resolved


def model_files(folder: Path) -> dict[str, str]:
    result = {}
    for path in sorted(folder.rglob("*")):
        name = path.relative_to(folder)
        if any(part in (".cache", ".git") for part in name.parts):
            continue
        if path.is_symlink():
            raise ValueError("Model snapshot contains a symlink: " + str(name))
        if path.is_file():
            result[str(name)] = sha256(path)
    if not any(name.endswith(".safetensors") for name in result):
        raise ValueError("Model snapshot has no safetensor weights")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--model-matrix", required=True)
    parser.add_argument("--configs", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--harness-selftest", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Model hashing and tokenization preflight require Slurm")
    root = ROOT.resolve(strict=True)
    prepared = bounded(args.prepared, root / "artifacts", must_exist=True)
    matrix_path = bounded(args.model_matrix, root / "runs", must_exist=True)
    configs = bounded(args.configs, root / "configs", must_exist=True)
    runs = bounded(args.runs, root / "runs", must_exist=True)
    selftest_path = bounded(args.harness_selftest, root / "runs", must_exist=True)
    selftest = read(selftest_path)
    if (selftest.get("status") != "PASS" or
            selftest.get("policy") != code_harness.POLICY or
            selftest.get("isolation_probes_passed") is not True or
            selftest.get("harness_sha256") != sha256(code_harness.__file__) or
            selftest.get("decoder_sha256") != sha256(code_tests.__file__)):
        raise ValueError("Frozen safe-code harness gate has not passed")
    if not configs.is_dir() or any(configs.iterdir()):
        raise ValueError("Config destination must be a new, empty directory")
    if not runs.is_dir() or any(path.name not in ("logs", "scratch") for path in runs.iterdir()):
        raise ValueError("Run destination may only contain precreated logs/scratch")
    prepared_manifest, problems = validate_prepared(prepared)
    if prepared_manifest["schema_version"] != "pals_e3_prepared_v1":
        raise ValueError("Synthetic data are not permitted in B1 E3")
    matrix = read(matrix_path)
    if set(matrix["models"]) != set(SLOTS) or matrix["shared"]["attention"] != "sdpa":
        raise ValueError("Unexpected historical model matrix")
    from transformers import AutoTokenizer
    token_preflight = {}
    for slot, (model_id, revision) in SLOTS.items():
        prior = matrix["models"][slot]
        if prior["model_revision"] != revision:
            raise ValueError("Model revision changed: " + slot)
        model = bounded(prior["model_path"], root / "models" / "hf", must_exist=True)
        if not model.is_dir():
            raise ValueError("Model directory missing: " + slot)
        kwargs = prior["chat_template_kwargs"]
        tokenizer = AutoTokenizer.from_pretrained(str(model), local_files_only=True,
                                                   trust_remote_code=False)
        longest = (None, 0)
        for problem in problems:
            prompt = tokenizer.apply_chat_template(messages(problem), tokenize=False,
                                                    add_generation_prompt=True, **kwargs)
            tokens = len(tokenizer.encode(prompt, add_special_tokens=False))
            budget = 16384 if problem["benchmark"] in ("humaneval", "livecodebench") else 8192
            if tokens + budget > prior["max_context"]:
                raise ValueError(f"Context budget fails: {slot} {problem['problem_id']} ")
            if tokens > longest[1]:
                longest = (problem["problem_id"], tokens)
        token_preflight[slot] = {"largest_problem_id": longest[0],
                                 "largest_prompt_tokens": longest[1],
                                 "max_context": prior["max_context"]}
    (runs / "logs").mkdir(mode=0o700, exist_ok=True)
    save(configs / "evaluation-policy.json", {
        "schema_version": "pals_e3_evaluation_policy_v1",
        "choice_numeric": "deterministic_answer_block_v1",
        "gpqa_duplicate_choices": "retain_process_outcome_na_two_cases",
        "human_eval_code": "official_tests_seccomp_v1",
        "livecodebench_code": "official_compatible_tests_seccomp_v1",
        "harness_policy": code_harness.POLICY,
        "harness_sha256": sha256(code_harness.__file__),
        "decoder_sha256": sha256(code_tests.__file__),
        "harness_selftest_sha256": sha256(selftest_path),
        "claim": "Safe bounded compatible code scoring; not official leaderboard equivalence"})
    runtime = {"torch": version("torch"), "transformers": version("transformers")}
    for slot, (model_id, revision) in SLOTS.items():
        prior = matrix["models"][slot]
        model = Path(prior["model_path"])
        files_path = configs / f"{slot}-model-files.json"
        save(files_path, model_files(model))
        config = {
            "schema_version": "pals_e3_config_v1", "backend": "hf",
            "protocol_version": "native-trace-v1", "prompt_version": "native-trace-prompt-v1",
            "parser_version": "strict-tag-v1",
            "scoring_version": "adjacent-deletion-explicit-boundary-v1",
            "model": {"id": model_id, "path": str(model), "revision": revision,
                      "files_manifest": str(files_path)},
            "hf_runtime": {"dtype": "bfloat16", "attention": "sdpa",
                           "max_context": prior["max_context"], "cpu_threads": 4,
                           "chat_template_kwargs": prior["chat_template_kwargs"],
                           "runtime_versions": runtime, "reviewed_local_code": None},
            "generation": {"samples_per_question": 1, "temperature": .7, "top_p": 1.,
                           "top_k": 0, "repetition_penalty": 1., "num_beams": 1,
                           "do_sample": True, "batch_size": 8,
                           "max_new_tokens": {"knowledge_math": 8192, "code": 16384},
                           "master_seed": 2026092903},
            "scoring": {"temperature": 1., "target_batch_size": 1},
            "common_scorer": {"model_id": "Qwen3-14B", "fraction": .1,
                              "rounding": "ceil", "selection_seed": 2026092903},
            "execution": {"shards": 8, "automatic_generation_retries": 0},
            "evaluation_manifest": str(configs / "evaluation-policy.json"),
            "analysis": {"bootstrap_draws": 5000, "bootstrap_seed": 2026092903}}
        config_path = configs / f"{slot}.json"
        save(config_path, config)
        init_run(prepared, config_path, runs / slot)
    save(runs / "deployment.json", {"source_manifest_sha256":
         prepared_manifest["source_manifest_sha256"], "model_matrix_sha256": sha256(matrix_path),
         "configs": str(configs), "token_preflight": token_preflight,
         "runtime_versions": runtime, "slurm_job_id": os.environ["SLURM_JOB_ID"]})
    print({"status": "E3_INITIALIZED", "runs": str(runs),
           "token_preflight": token_preflight}, flush=True)


if __name__ == "__main__":
    main()
