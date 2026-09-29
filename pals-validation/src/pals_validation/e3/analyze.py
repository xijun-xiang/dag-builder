"""Question-level E3 summaries with explicit denominators and paired comparisons."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from ..io import digest, read, save
from ..metrics import bootstrap
from .audit import _score_matches, token_encoder
from .protocol import parse_trace
from .run import validate_run
from .schema import BENCHMARKS, require

METRICS = ("G", "M", "W", "NLL")


def _mean_ci(rows: list[dict], field: str, seed: int) -> dict:
    values = [row[field] for row in rows if row.get(field) is not None]
    return bootstrap(values, seed=seed, draws=5000)


def _item_by_id(run: Path, audit: dict, problems: list[dict]) -> dict[str, dict]:
    audit_rows = {row["problem_id"]: row for row in audit["items"]}
    require(len(audit_rows) == len(problems), "audit item set mismatch")
    return {problem["problem_id"]: {**audit_rows[problem["problem_id"]],
            "benchmark": problem["benchmark"], "subset": problem["subset"]}
            for problem in problems}


def _common_rows(qwen_run: Path, source_run: Path, expected_ids: set[str]) -> dict[str, dict]:
    qproto, qconfig, _, qproblems = validate_run(qwen_run)
    sproto, sconfig, sbatches, _ = validate_run(source_run)
    require(qconfig["model"]["id"] == "Qwen3-14B", "wrong common scorer")
    name = sconfig["model"]["id"]
    folder = qwen_run / "common_scores" / name
    selected = set(read(qwen_run / "inputs" / "common_subset.json"))
    encode = token_encoder(qconfig)
    require({p.stem for p in folder.glob("*.json")} == {digest(item) for item in selected},
            "common score set incomplete or has extra entries")
    for shard in range(8):
        done = read(qwen_run / "workers" / f"common-score-{name}-{shard}.json")
        expected_n = sum(len(selected.intersection(batch["problem_ids"])) for index, batch in enumerate(sbatches)
                         if index % 8 == shard)
        require(done["problems"] == expected_n and done["source_model"] == name and
                done["source_run_protocol_id"] == sproto["protocol_id"] and
                done["protocol_id"] == qproto["protocol_id"], "common scorer worker incomplete")
    batches_by_id = {item: batch for batch in sbatches for item in batch["problem_ids"]}
    raw_cache = {}
    results = {}
    for item in selected:
        batch = batches_by_id[item]
        if batch["batch_id"] not in raw_cache:
            raw_cache[batch["batch_id"]] = read(source_run / "generation_batches" / (batch["batch_id"] + ".json"))
        raw = raw_cache[batch["batch_id"]]
        generated = next(row for row in raw["rows"] if row["problem_id"] == item)
        parsed = parse_trace(generated["raw_text"], generated["finish_reason"])
        row = read(folder / (digest(item) + ".json"))
        require(row["problem_id"] == item and row["source_model"] == name and
                row["scorer_model"] == "Qwen3-14B" and
                row["source_protocol_id"] == sproto["protocol_id"] and
                row["protocol_id"] == qproto["protocol_id"], "common score identity changed")
        if parsed["process_valid"] and len(parsed["steps"]) >= 2:
            prefix = parsed["raw_text"][:parsed["steps"][1]["span"][0]]
            require(row["steps"][0]["full_context_text"].endswith(prefix),
                    "common score prefix mismatch")
            base = row["steps"][0]["full_context_text"][:-len(prefix)]
        else:
            base = ""
        _score_matches(row, parsed, base, encode)
        results[item] = row
    require(selected == expected_ids, "common subset changed")
    return results


def analyze_runs(runs: list[str | Path], audits: list[str | Path], output: str | Path) -> dict:
    require(len(runs) == len(audits) == 3, "three model runs and their audits required")
    output = Path(output)
    require(not output.exists(), "analysis output already exists")
    models = {}
    source_identity = None
    for path, audit_path in zip(runs, audits):
        run = Path(path).resolve(strict=True)
        protocol, config, _, problems = validate_run(run)
        audit = read(Path(audit_path) / "audit.json")
        require(audit["stage"] == "all" and audit["scientific_evidence"] and
                audit["run_protocol_id"] == protocol["protocol_id"] and
                audit["source_run"] == str(run), "non-formal or wrong audit")
        name = config["model"]["id"]
        require(name not in models, "duplicate model")
        prepared = protocol["prepared_manifest_sha256"]
        source_identity = source_identity or prepared
        require(prepared == source_identity, "models use different original cohorts")
        models[name] = {"run": run, "protocol": protocol, "config": config,
                        "items": _item_by_id(run, audit, problems)}
    require(set(models) == {"Qwen2.5-7B-Instruct", "Phi-4-mini-instruct", "Qwen3-14B"},
            "model set differs from E3 protocol")
    primary, conditional, paired, common, cases = [], [], [], [], []
    seed = 2026092903
    for name, model in sorted(models.items()):
        for benchmark in sorted(BENCHMARKS):
            group = sorted((row for row in model["items"].values()
                            if row["benchmark"] == benchmark), key=lambda r: r["problem_id"])
            require(bool(group), "benchmark omitted")
            usable = [{**row["summary"], **{"problem_id": row["problem_id"]}}
                      for row in group if row.get("summary")]
            decided = [r for r in group if r.get("correct") is not None]
            counts = dict(Counter(r.get("outcome", "not_evaluated") for r in group))
            table = {"model": name, "benchmark": benchmark, "planned": len(group),
                     "complete_process": sum(r["process_valid"] for r in group),
                     "G_valid": sum(r.get("summary") is not None and
                                    r["summary"]["G"] is not None for r in group),
                     "W_valid": sum(r.get("summary") is not None and
                                    r["summary"]["W"] is not None for r in group),
                     "outcome_decided": len(decided), "outcome_statuses": counts,
                     "one_generation_accuracy": _mean_ci(decided, "correct", seed),
                     "step_count": _mean_ci(group, "step_count", seed)}
            for metric in METRICS:
                table[metric] = _mean_ci(usable, metric, seed)
            primary.append(table)
            for outcome in (True, False):
                cohort = [{**row["summary"], "problem_id": row["problem_id"]}
                          for row in group if row.get("correct") is outcome and row.get("summary")]
                conditional.append({"model": name, "benchmark": benchmark,
                    "correct": outcome, "n": len(cohort),
                    **{metric: _mean_ci(cohort, metric, seed) for metric in METRICS}})
            valid = [r for r in group if r.get("summary") and r["summary"]["G"] is not None]
            selectors = {
                "highest_M": max(valid, key=lambda r: (r["summary"]["M"], r["problem_id"])) if valid else None,
                "highest_W": max((r for r in valid if r["summary"]["W"] is not None),
                                 key=lambda r: (r["summary"]["W"], r["problem_id"]), default=None),
                "hash_sample": min(valid, key=lambda r: digest([seed, r["problem_id"]])) if valid else None,
                "invalid_process": min((r for r in group if not r["process_valid"]),
                                       key=lambda r: digest([seed, r["problem_id"]]), default=None),
            }
            cases.extend({"model": name, "benchmark": benchmark, "selector": label,
                          "problem_id": item["problem_id"] if item else None}
                         for label, item in selectors.items())
    names = sorted(models)
    for benchmark in sorted(BENCHMARKS):
        for left_i, left in enumerate(names):
            for right in names[left_i + 1:]:
                a, b = models[left]["items"], models[right]["items"]
                ids = sorted(item for item in a.keys() & b.keys() if a[item]["benchmark"] == benchmark)
                row = {"benchmark": benchmark, "left": left, "right": right,
                       "common_questions": len(ids)}
                for metric in METRICS:
                    differences = [b[item]["summary"][metric] - a[item]["summary"][metric]
                        for item in ids if a[item].get("summary") and b[item].get("summary")
                        and a[item]["summary"][metric] is not None
                        and b[item]["summary"][metric] is not None]
                    row[metric + "_difference"] = bootstrap(differences, seed=seed, draws=5000)
                paired.append(row)
    qwen = models["Qwen3-14B"]
    qrun = qwen["run"]
    selected = set(read(qrun / "inputs" / "common_subset.json"))
    for name, model in sorted(models.items()):
        if name == "Qwen3-14B":
            scoring = {item: {"summary": model["items"][item]["summary"]} for item in selected}
        else:
            scoring = _common_rows(qrun, model["run"], selected)
        for benchmark in sorted(BENCHMARKS):
            ids = sorted(item for item in selected if model["items"][item]["benchmark"] == benchmark)
            usable = [scoring[item]["summary"] for item in ids if scoring[item]["summary"]]
            common.append({"model": name, "benchmark": benchmark, "selected": len(ids),
                           "G_valid": sum(row["G"] is not None for row in usable),
                           "W_valid": sum(row["W"] is not None for row in usable),
                           **{metric: _mean_ci(usable, metric, seed) for metric in METRICS}})
    result = {"schema_version": "pals_e3_analysis_v1", "scientific_evidence": True,
              "run_protocols": {name: model["protocol"]["protocol_id"] for name, model in models.items()},
              "run_paths": {name: str(model["run"]) for name, model in models.items()},
              "primary": primary, "conditional": conditional, "paired": paired,
              "common_scorer": common, "cases": cases,
              "note": "W is within-trajectory dispersion; code outcomes require a pinned safe evaluator."}
    output.mkdir(parents=True, mode=0o700)
    save(output / "analysis.json", result)
    save(output / "items.json", {name: model["items"] for name, model in models.items()})
    return {"primary_rows": len(primary), "conditional_rows": len(conditional),
            "paired_rows": len(paired), "common_rows": len(common)}
