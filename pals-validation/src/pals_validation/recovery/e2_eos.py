"""Llama-only E2 EOS boundary recovery, scoring existing text without generation."""
import argparse
from collections import Counter
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from ..analyze import analyze, check_score, e2_analysis
from ..backend import HFBackend
from ..io import digest, read, save, sha256, verify
from ..metrics import pair, repeats
from ..protocol import is_code_step, parse_step
from ..run import now, validate_run

VERSION = "llama-e2-natural-eos-v1"
MODULE = "pals_validation.recovery.e2_eos"
PROJECT = Path("/work/projects/polyullm/xxj/PALS")
SOURCE_ROOT = PROJECT / "runs/20261009-llama-fivebench-e1e2-v2"
SOURCES = {
    "gpqa-formal": SOURCE_ROOT / "gpqa/e2/llama3-8b/formal-e2",
    "gpqa-canary": SOURCE_ROOT / "gpqa/e2/llama3-8b/canary/e2",
    "humaneval-canary": SOURCE_ROOT / "humaneval/e2/llama3-8b/canary/e2",
}
EXPECTED_CELLS = {"gpqa-formal": 312, "gpqa-canary": 6, "humaneval-canary": 6}
STRUCTURE = re.compile(r"[<\[]\s*/?\s*(?:step|think|answer|analysis|final|result)\b|\bRESULT\s*:", re.I)
LIST_OR_HEADING = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|(?:step|步骤)\s*\d+\b)", re.I | re.M)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def select_target(row, contract, task_type):
    """Keep strict-valid rows exactly; recover only an unambiguous EOS text span.

    This syntax-only policy does not certify correctness, coherence, or logical
    atomicity. It never adds a closing tag, edits text, or repairs truncation.
    """
    text = row["raw_text"]
    strict = parse_step(text, task_type)
    require(strict == row["parse"], "stored strict parse changed")
    if strict["valid"] and row["finish_reason"] == "boundary":
        return {"kind": "strict", "target": strict["body"], "reason": None}
    def reject(reason):
        return {"kind": "invalid", "target": None, "reason": reason}
    if row["finish_reason"] != "eos":
        return reject("not_natural_eos")
    ids = row["generated_token_ids"]
    eos = contract["eos_token_ids"]
    if not ids or ids[-1] not in eos or any(t in eos for t in ids[:-1]):
        return reject("ambiguous_eos_tokens")
    if len(ids) >= contract["max_new_tokens"]:
        return reject("at_output_cap")
    if STRUCTURE.search(text):
        return reject("structural_tag_or_fragment")
    body = text.strip()
    if not body:
        return reject("empty")
    if "```" in body or "~~~" in body or LIST_OR_HEADING.search(body):
        return reject("code_fence_list_or_heading")
    if re.search(r"\n\s*\n", body):
        return reject("multiple_paragraphs")
    if task_type in ("humaneval", "livecodebench") and is_code_step(body):
        return reject("code_instead_of_algorithm_step")
    return {"kind": "eos_recovered", "target": body, "reason": None}


def provenance_files(source):
    p = read(source / "protocol.json")
    names = set(p["files"]) | {"protocol.json"}
    for folder in ("generations", "results"):
        names.update(str(x.relative_to(source)) for x in (source / folder).glob("*.json"))
    names.update(str(x.relative_to(source)) for x in (source / "workers").glob("*/completion.json"))
    return {n: sha256(source / n) for n in sorted(names)}


def model_stats(model, files):
    return {n: {"size": (model / n).stat().st_size, "mtime_ns": (model / n).stat().st_mtime_ns}
            for n in files}


def implementation_files():
    src = Path(__file__).resolve().parents[1]
    return {str(p.relative_to(src)): sha256(p) for p in sorted(src.rglob("*.py"))}


def check_plan(root, sources=False):
    p = read(root / "plan.json")
    require(p["protocol_id"] == digest({k: v for k, v in p.items() if k != "protocol_id"}), "plan digest")
    require(p["version"] == VERSION and p["implementation"] == implementation_files(), "frozen code changed")
    verify(root, p["files"])
    if sources:
        for s in p["sources"].values():
            verify(Path(s["path"]), s["files"])
    return p


def prepare(root):
    from transformers import AutoTokenizer
    require(os.environ.get("SLURM_JOB_ID"), "prepare requires allocated CPU job")
    root.mkdir(mode=0o700)  # Exclusive: interrupted preparation is never resumed.
    for name in ("source-audits", "tasks", "claims", "scores", "workers", "merged", "analysis"):
        (root / name).mkdir(mode=0o700)
    source_meta, datasets, task_index = {}, {}, []
    shared_config, model_files, tokenizer = None, None, None
    for label, source in SOURCES.items():
        protocol, config = validate_run(source)
        require(protocol["experiment"] == "e2" and protocol["scientific_evidence"], "real E2 required")
        require("Meta-Llama-3-8B-Instruct" in config["model_path"] and config["max_context"] == 8192 and
                config["model_revision"] == "e9f7e7d3fa08b550ea228e38bb5501d35be92c0d",
                "Llama model identity")
        require(config["repeats"] == 8 and config["temperatures"] == [.3, .7, 1.2], "repeat protocol")
        model = Path(config["model_path"])
        if shared_config is None:
            shared_config, model_files = config, protocol["model_files"]
            verify(model, model_files)  # Once on CPU, not eight times while holding GPUs.
            tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True, trust_remote_code=False)
        else:
            ignored = {"generation_prompt_version", "max_new_tokens"}
            require({k:v for k,v in config.items() if k not in ignored} ==
                    {k:v for k,v in shared_config.items() if k not in ignored}, "scoring config drift")
            require(model_files == protocol["model_files"], "model files differ")
        old_summary = analyze(source, root / "source-audits" / label)
        jobs = read(source / "jobs.json")
        require(len(jobs) == EXPECTED_CELLS[label], "source cell denominator")
        cases = {c["item_id"]: c for c in read(source / "inputs/cases.json")}
        context = object.__new__(HFBackend)
        context.tokenizer, context.config = tokenizer, config
        cells, counts, rejections = [], Counter(), Counter()
        for job in jobs:
            jid = job["job_id"]
            gen = read(source / "generations" / (jid + ".json"))
            old = read(source / "results" / (jid + ".json"))
            case = cases[job["item_id"]]
            require(gen["prompt"] == context.context(case, job["prefix_ids"], for_generation=True), "prompt replay")
            require(gen["prompt_token_ids"] == tokenizer.encode(gen["prompt"], add_special_tokens=False), "prompt tokens")
            contract = gen["generation_contract"]
            require(contract["max_new_tokens"] == config["max_new_tokens"] and
                    contract["max_context"] == config["max_context"] and
                    contract["prompt_version"] == config.get("generation_prompt_version", "v1") and
                    contract["boundary"] == "</step>" and contract["constrained_decoding"] is False,
                    "generation contract")
            full = context.context(case, job["prefix_ids"])
            require(job["prefix_ids"].count(job["deleted_id"]) == 1, "deletion")
            deleted = context.context(case, [i for i in job["prefix_ids"] if i != job["deleted_id"]])
            contexts = {"full_context_ids": tokenizer.encode(full, add_special_tokens=False),
                        "deleted_context_ids": tokenizer.encode(deleted, add_special_tokens=False)}
            decisions = []
            for row, saved in zip(gen["rows"], old["rows"]):
                require(tokenizer.decode(row["generated_token_ids"], skip_special_tokens=True) == row["raw_text"],
                        "raw text does not decode from tokens")
                decision = select_target(row, contract, case.get("task_type"))
                kind = decision["kind"]
                counts[kind] += 1
                if kind == "invalid": rejections[decision["reason"]] += 1
                target = decision["target"]
                if target is not None:
                    ev = {"target_text": target, "target_ids": tokenizer.encode(target, add_special_tokens=False), **contexts}
                    require(ev["target_ids"] and max(len(contexts[k]) for k in contexts)+len(ev["target_ids"]) <= 8192,
                            "score capacity")
                    if kind == "strict":
                        require(saved["status"] == "ok" and all(saved["evidence"][k] == v for k,v in ev.items()),
                                "strict score target or context changed")
                    else:
                        key = digest([label, jid, row["repeat"]])
                        task = {"key": key, "source": label, "job_id": jid, "repeat": row["repeat"],
                                "generation_sha256": old["generation_sha256"], "evidence_input": ev}
                        save(root / "tasks" / (key + ".json"), task)
                        task_index.append({"key": key, "cost": len(ev["full_context_ids"])+len(ev["target_ids"])})
                        decision["task_key"] = key
                decisions.append(decision)
            cells.append({"job": job, "decisions": decisions})
        save(root / (label + ".json"), cells)
        source_meta[label] = {"path": str(source), "protocol_id": protocol["protocol_id"],
                              "files": provenance_files(source), "config": config}
        datasets[label] = {"counts": dict(counts), "rejections": dict(rejections),
                           "strict_complete_questions": old_summary["complete_questions"],
                           "planned_questions": old_summary["planned_questions"]}
    task_index.sort(key=lambda x: (-x["cost"], x["key"]))
    save(root / "queue.json", task_index)
    frozen = [root / "queue.json", *[root / (label + ".json") for label in SOURCES],
              *sorted((root / "tasks").glob("*.json"))]
    plan = {"version": VERSION, "created": now(), "generation_calls": 0,
            "sources": source_meta, "datasets": datasets, "config": shared_config,
            "model_files": model_files, "model_stats": model_stats(Path(shared_config["model_path"]), model_files),
            "implementation": implementation_files(), "tasks": len(task_index),
            "files": {str(p.relative_to(root)): sha256(p) for p in frozen}}
    save(root / "plan.json", {**plan, "protocol_id": digest(plan)})
    save(root / "ready.json", {"status": "PASS", "plan_sha256": sha256(root / "plan.json"),
                               "datasets": datasets, "tasks": len(task_index), "job_id": os.environ["SLURM_JOB_ID"]})
    print(read(root / "ready.json"), flush=True)


def score_input(backend, ev):
    a = backend._score(ev["full_context_ids"], ev["target_ids"])
    b = backend._score(ev["deleted_context_ids"], ev["target_ids"])
    return {"score": pair(a, b), "evidence": {**ev, "full_logprobs": a, "deleted_logprobs": b}}


def numeric_reference(backend, ev):
    import torch
    a, b = score_input(backend, ev), score_input(backend, ev)
    error = max(abs(x-y) for k in ("full_logprobs", "deleted_logprobs")
                for x,y in zip(a["evidence"][k], b["evidence"][k]))
    errors, losses = {}, {}
    for label in ("full", "deleted"):
        ctx = ev[label + "_context_ids"]
        ids = torch.tensor([ctx + ev["target_ids"]], device=backend.config["device"])
        labels = ids.clone(); labels[:, :len(ctx)] = -100
        with torch.inference_mode():
            loss = backend.model(input_ids=ids, attention_mask=torch.ones_like(ids), labels=labels, use_cache=False).loss.item()
        errors[label] = abs(loss - a["score"][label + "_nll"])
        losses[label] = loss
    require(math.isfinite(error) and error <= 1e-5 and all(math.isfinite(v) and v <= .005 for v in errors.values()),
            "native loss or repeat numeric gate")
    return {"status": "PASS", "repeat_max_abs": error, "masked_loss_errors": errors, "probe": a,
            "repeat_probe": b, "native_losses": losses,
            "versions": backend.versions}


def worker(root, index):
    require(os.environ.get("SLURM_JOB_ID"), "GPU allocation required")
    p = check_plan(root)
    require(read(root / "ready.json")["plan_sha256"] == sha256(root / "plan.json"), "prepare receipt")
    name = str(index)
    save(root / "workers" / (name + "-attempt.json"), {"job_id": os.environ["SLURM_JOB_ID"], "started": now()})
    require(model_stats(Path(p["config"]["model_path"]), p["model_files"]) == p["model_stats"], "model stat changed")
    queue = read(root / "queue.json")
    done = []
    if queue:
        backend = HFBackend(p["config"]["model_path"], p["config"])
        probe = read(root / "tasks" / (queue[index % len(queue)]["key"] + ".json"))
        save(root / "workers" / (name + "-reference.json"), numeric_reference(backend, probe["evidence_input"]))
        # Shared longest-first queue; no all-worker coverage barrier and no generation.
        for entry in queue:
            key = entry["key"]
            try:
                save(root / "claims" / (key + ".json"), {"worker": index, "job_id": os.environ["SLURM_JOB_ID"], "time": now()})
            except FileExistsError:
                continue
            task = read(root / "tasks" / (key + ".json"))
            output = score_input(backend, task["evidence_input"])
            check_score(output)
            save(root / "scores" / (key + ".json"), {"task_sha256": sha256(root / "tasks" / (key + ".json")),
                 "protocol_id": p["protocol_id"], "worker": index, "status": "ok", **output})
            done.append(key)
            print(key, "scored", len(done), flush=True)
    save(root / "workers" / (name + "-complete.json"), {"worker": index, "job_id": os.environ["SLURM_JOB_ID"],
         "protocol_id": p["protocol_id"], "keys": done, "ended": now()})


def launch(root, workers):
    check_plan(root, sources=True)
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")
    require(len(visible) == workers and all(visible), "GPU visibility mismatch")
    require(not list((root / "workers").glob("*-attempt.json")), "attempt already exists; no retry")
    children = []
    try:
        for i, gpu in enumerate(visible):
            stream = (root / "workers" / (str(i) + ".log")).open("x")
            proc = subprocess.Popen([sys.executable, "-m", MODULE, "worker", "--root", str(root), "--worker", str(i),
                                     "--workers", str(workers)],
                                    stdout=stream, stderr=subprocess.STDOUT,
                                    env={**os.environ, "CUDA_VISIBLE_DEVICES": gpu})
            children.append((proc, stream))
        while any(proc.poll() is None for proc, _ in children):
            require(not any(proc.poll() not in (None, 0) for proc, _ in children), "scoring worker failed")
            time.sleep(1)
        require(all(proc.returncode == 0 for proc,_ in children), "scoring workers failed")
    finally:
        for proc, stream in children:
            if proc.poll() is None: proc.terminate()
        for proc, stream in children:
            try: proc.wait(timeout=20)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            stream.close()
    save(root / "gpu-complete.json", {"status": "PASS", "workers": workers,
         "protocol_id": read(root / "plan.json")["protocol_id"], "job_id": os.environ["SLURM_JOB_ID"]})


def merge_rows(generation, old, decisions, additions):
    require(len(generation["rows"]) == len(old["rows"]) == len(decisions), "row count")
    rows = []
    for raw, previous, decision in zip(generation["rows"], old["rows"], decisions):
        require(all(previous[k] == v for k,v in raw.items()), "raw changed in old result")
        if decision["kind"] == "strict":
            require(previous["status"] == "ok", "missing old score")
            row = {**previous, "recovery": decision}
        elif decision["kind"] == "eos_recovered":
            scored = additions[decision["task_key"]]
            require(scored["evidence"]["target_text"] == decision["target"], "recovery target mismatch")
            row = {**raw, "status": "ok", "score": scored["score"], "evidence": scored["evidence"], "recovery": decision}
        else:
            row = {**previous, "recovery": decision}
        if row["status"] == "ok": check_score(row)
        rows.append(row)
    return rows


def audit(root):
    p = check_plan(root, sources=True)
    require(read(root / "ready.json")["plan_sha256"] == sha256(root / "plan.json"), "ready hash")
    gpu = read(root / "gpu-complete.json")
    require(gpu["status"] == "PASS" and gpu["protocol_id"] == p["protocol_id"], "GPU completion")
    verify(Path(p["config"]["model_path"]), p["model_files"])  # CPU, after releasing GPU.
    keys = {t["key"] for t in read(root / "queue.json")}
    require({f.stem for f in (root / "claims").glob("*.json")} ==
            {f.stem for f in (root / "scores").glob("*.json")} == keys, "missing/unexpected claims/scores")
    claimed, additions = [], {}
    for i in range(gpu["workers"]):
        done = read(root / "workers" / (str(i) + "-complete.json"))
        require(done["worker"] == i and done["job_id"] == gpu["job_id"] and done["protocol_id"] == p["protocol_id"], "worker identity")
        claimed.extend(done["keys"])
        if keys:
            ref = read(root / "workers" / (str(i) + "-reference.json"))
            require(ref["status"] == "PASS" and 0 <= ref["repeat_max_abs"] <= 1e-5 and
                    all(0 <= v <= .005 for v in ref["masked_loss_errors"].values()), "reference gate")
            check_score(ref["probe"])
            check_score(ref["repeat_probe"])
            for name in ("target_text", "target_ids", "full_context_ids", "deleted_context_ids"):
                require(ref["probe"]["evidence"][name] == ref["repeat_probe"]["evidence"][name], "repeat target changed")
            error = max(abs(a-b) for name in ("full_logprobs", "deleted_logprobs")
                        for a,b in zip(ref["probe"]["evidence"][name], ref["repeat_probe"]["evidence"][name]))
            require(error == ref["repeat_max_abs"], "repeat reference arithmetic")
            require(all(abs(ref["native_losses"][name]-ref["probe"]["score"][name+"_nll"]) ==
                        ref["masked_loss_errors"][name] for name in ("full", "deleted")), "masked-loss reference arithmetic")
        for key in done["keys"]:
            claim = read(root / "claims" / (key + ".json"))
            row = read(root / "scores" / (key + ".json"))
            task = read(root / "tasks" / (key + ".json"))
            require(claim["worker"] == row["worker"] == i and claim["job_id"] == gpu["job_id"], "claim owner")
            require(row["protocol_id"] == p["protocol_id"] and row["task_sha256"] == sha256(root / "tasks" / (key + ".json")), "task receipt")
            require(all(row["evidence"][k] == v for k,v in task["evidence_input"].items()), "score target/context drift")
            check_score(row); additions[key] = row
    require(len(claimed) == len(set(claimed)) == len(keys) and set(claimed) == keys, "dispatch not exactly once")
    summaries = {}
    for label, source in p["sources"].items():
        base = Path(source["path"])
        strict, recovered = [], []
        for cell in read(root / (label + ".json")):
            job = cell["job"]; jid = job["job_id"]
            gen = read(base / "generations" / (jid + ".json"))
            old = read(base / "results" / (jid + ".json")); strict.append(old)
            case_type = "humaneval" if label.startswith("humaneval") else "gpqa"
            for raw, decision in zip(gen["rows"], cell["decisions"]):
                recomputed = select_target(raw, gen["generation_contract"], case_type)
                require(recomputed == {k:v for k,v in decision.items() if k != "task_key"}, "decision replay failed")
                if decision["kind"] == "eos_recovered":
                    require(decision["task_key"] == digest([label,jid,raw["repeat"]]), "row task identity")
                    require(read(root / "tasks" / (decision["task_key"] + ".json"))["generation_sha256"] == sha256(base / "generations" / (jid + ".json")), "source generation hash")
            rows = merge_rows(gen, old, cell["decisions"], additions)
            r = {"job": job, "rows": rows, "summary": repeats(rows, source["config"]["repeats"])}
            save(root / "merged" / (label + "-" + jid + ".json"), r); recovered.append(r)
        summaries[label] = {"strict": e2_analysis(strict, source["config"]),
                            "eos_recovery": e2_analysis(recovered, source["config"])}
        save(root / "analysis" / (label + ".json"), summaries[label])
    outputs = [*sorted((root / "scores").glob("*.json")), *sorted((root / "merged").glob("*.json")),
               *sorted((root / "analysis").glob("*.json")), *sorted((root / "workers").glob("*.json"))]
    report = {"status": "PASS", "protocol_id": p["protocol_id"], "version": VERSION,
              "scored_new": len(keys), "generation_calls": 0, "datasets": p["datasets"],
              "complete_questions": {k:{v:s["complete_questions"] for v,s in r.items()} for k,r in summaries.items()},
              "files": {str(f.relative_to(root)): sha256(f) for f in outputs},
              "scope": "Llama-only exploratory parser adaptation; canary separate from formal; no new generations"}
    save(root / "audit.json", report)
    print({k:v for k,v in report.items() if k != "files"}, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("prepare", "launch", "worker", "audit"))
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--worker", type=int)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    os.umask(0o077)
    root = args.root
    require(PROJECT.resolve(strict=True) == PROJECT and root.is_absolute() and root.resolve() == root
            and PROJECT in root.parents and SOURCE_ROOT not in root.parents, "project output boundary")
    require(os.environ.get("SLURM_JOB_ID"), "Slurm required")
    if args.action == "worker":
        require(args.worker is not None and 0 <= args.worker < args.workers, "worker id")
        worker(root, args.worker)
    elif args.action == "launch": launch(root, args.workers)
    else: globals()[args.action](root)


if __name__ == "__main__": main()
