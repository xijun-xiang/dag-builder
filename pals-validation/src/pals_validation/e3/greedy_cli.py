"""Explicit stages for the new E3 protocol; no implicit scheduler calls."""
import argparse
import json
import os
from pathlib import Path

from ..io import read, sha256, save
from . import greedy, greedy_data
from . import greedy_extension as extension
from .code_harness import POLICY
from . import code_harness, code_tests
from .run import _model_files
from .schema import require
from .deployment import assert_project_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=("prepare", "init", "gpu-worker", "evaluate-worker", "audit", "summarize"))
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--prepared", type=Path)
    p.add_argument("--sources", type=Path)
    p.add_argument("--configs", type=Path)
    p.add_argument("--selftest", type=Path)
    p.add_argument("--recover-from", type=Path, help="Read-only sealed v1 source; never regenerate existing output")
    p.add_argument("--cohort", choices=("original", "extension", "extension-llama3", "extension-internlm3"), default="original")
    p.add_argument("--slot", choices=tuple(greedy.SLOTS) + tuple(extension.SLOTS))
    p.add_argument("--shard", type=int)
    p.add_argument("--deadline", type=float, default=float("inf"))
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    os.umask(0o077)
    if args.stage == "prepare":
        result = greedy_data.prepare(args.prepared, args.sources, args.root)
    elif args.stage == "init":
        require(os.environ.get("SLURM_JOB_ID") and not os.environ.get("CUDA_VISIBLE_DEVICES"), "CPU init only")
        for path in (args.root.parent, args.prepared, args.configs, args.selftest):
            assert_project_path(path)
        check = read(args.selftest)
        require(check["status"] == "PASS" and check["isolation_probes_passed"] and
                check["harness_sha256"] == sha256(code_harness.__file__) and
                check["decoder_sha256"] == sha256(code_tests.__file__), "safety selftest differs")
        expanded = args.cohort != "original"
        deployment_slot = args.cohort.removeprefix("extension-") if args.cohort.startswith("extension-") else None
        slots = ({deployment_slot: extension.SLOTS[deployment_slot]} if deployment_slot is not None
                 else extension.SLOTS if expanded else greedy.SLOTS)
        require(not args.recover_from or args.cohort == "original", "extension does not import legacy runs")
        configs = {slot: greedy.make_config(read(args.configs / (slot + ".json")),
                   extension_slot=slot if expanded else None) for slot in slots}
        for config in configs.values():
            greedy.validate_config(config)
            _model_files(config)
        architectures = {}
        if expanded:
            from .cpu_compatibility import probe
            for slot, config in configs.items():
                try:
                    architectures[slot] = probe(config)
                    save(args.selftest.parent / (slot + "-architecture.json"), architectures[slot])
                except Exception as exc:
                    save(args.selftest.parent / (slot + "-architecture-failure.json"),
                         {"status":"FAIL", "type":type(exc).__name__, "error":str(exc)})
                    raise
        # All real prompt/context checks are CPU-only and precede any generation.
        from transformers import AutoTokenizer
        from ..model_policy import local_code_policy
        from .protocol import messages
        _, problems = greedy_data.validate(args.prepared)
        context_checks = {}
        for slot, config in configs.items():
            runtime = {**config["hf_runtime"], "model_revision": config["model"]["revision"]}
            native_config = read(Path(config["model"]["path"]) / "config.json")
            require(runtime["max_context"] <= native_config["max_position_embeddings"],
                    "declared context exceeds native model limit")
            tokenizer = AutoTokenizer.from_pretrained(config["model"]["path"], local_files_only=True,
                trust_remote_code=local_code_policy(config["model"]["path"], runtime))
            maxima, violations, all_lengths = {}, [], {}
            for start in range(0, len(problems), 64):
                chunk = problems[start:start + 64]
                prompts = [tokenizer.apply_chat_template(messages(p, config["prompt_version"]), tokenize=False,
                    add_generation_prompt=True, **runtime["chat_template_kwargs"]) for p in chunk]
                lengths = [len(ids) for ids in tokenizer(prompts, add_special_tokens=False)["input_ids"]]
                for problem, length in zip(chunk, lengths):
                    bench = problem["benchmark"]
                    budget = config["generation"]["max_new_tokens"][
                        "code" if bench in ("humaneval", "livecodebench") else "knowledge_math"]
                    maxima[bench] = max(maxima.get(bench, 0), length)
                    all_lengths[problem["problem_id"]] = length
                    if args.cohort == "original" and length + budget > runtime["max_context"]:
                        violations.append({"problem_id": problem["problem_id"], "prompt_tokens": length,
                                           "reserved_output": budget})
            batch_budgets = []
            if expanded:
                for batch in greedy.batches_for(problems, config):
                    lengths = [all_lengths[i] for i in batch["problem_ids"]]
                    try:
                        item = extension.generation_budget(config, batch["benchmark"], lengths)
                        batch_budgets.append({"batch_id": batch["batch_id"], "benchmark": batch["benchmark"],
                                              "problem_ids": batch["problem_ids"], **item})
                    except ValueError as exc:
                        violations.append({"batch_id": batch["batch_id"], "problem_ids": batch["problem_ids"],
                                           "prompt_lengths": lengths, "reason": str(exc)})
            context_checks[slot] = {"checked": len(problems), "max_prompt_tokens": maxima,
                                    "max_context": runtime["max_context"], "violations": violations}
            if expanded:
                context_checks[slot]["batch_budgets"] = batch_budgets
                context_checks[slot]["cpu_architecture"] = architectures[slot]
        save(args.selftest.parent / "context-checks.json", context_checks)
        require(all(not row["violations"] for row in context_checks.values()),
                "input plus output reservation exceeds context; no GPU generation submitted")
        policy = {"human_eval_code": "official_tests_seccomp_v1",
                  "livecodebench_code": "official_compatible_tests_seccomp_v1",
                  "harness_policy": POLICY, "harness_sha256": sha256(code_harness.__file__),
                  "decoder_sha256": sha256(code_tests.__file__), "selftest_sha256": sha256(args.selftest)}
        recovery_plan = None
        if args.recover_from:
            assert_project_path(args.recover_from)
            from .greedy_recovery import plan, import_sealed
            recovery_plan = plan(args.recover_from)
        result = greedy.init(args.prepared, configs, policy, args.root, recovery_plan=recovery_plan,
                             deployment_slot=deployment_slot)
        if args.recover_from:
            import_sealed(args.recover_from, args.root, recovery_plan)
        save(args.selftest.parent / "init.json", {"status": "PASS", "job_id": os.environ["SLURM_JOB_ID"],
            "run": str(args.root), "protocol_id": result["protocol_id"],
            "selftest_sha256": sha256(args.selftest),
            "context_checks_sha256": sha256(args.selftest.parent / "context-checks.json")})
    elif args.stage == "gpu-worker":
        ok = greedy.gpu_worker(args.root, args.slot, args.shard, args.deadline)
        if not ok:
            raise SystemExit(75)
        result = {"status": "PASS", "slot": args.slot, "shard": args.shard}
    elif args.stage == "evaluate-worker":
        result = greedy.evaluate(args.root, args.slot, args.shard)
    elif args.stage == "audit":
        result = greedy.audit(args.root, args.slot)
    else:
        from .greedy_report import summarize
        result = summarize(args.root, args.output)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
