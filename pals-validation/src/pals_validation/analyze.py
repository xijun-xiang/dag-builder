"""Strict offline acceptance, paired common-target tables, question bootstrap."""
import csv
from collections import defaultdict
from pathlib import Path
from .io import read, save, sha256
from .metrics import bootstrap, pair, repeats, trajectory
from .run import validate_run
from .protocol import parse_step


def check_score(row):
    ev = row["evidence"]
    if len(ev["target_ids"]) != len(ev["full_logprobs"]):
        raise ValueError("Target/logprob length mismatch")
    expected = pair(ev["full_logprobs"], ev["deleted_logprobs"])
    if any(abs(row["score"][k] - value) > 1e-8 for k, value in expected.items()):
        raise ValueError("Score cannot be reproduced from saved token probabilities")


def e1_analysis(results):
    groups = defaultdict(lambda: defaultdict(dict))
    for r in results:
        j = r["job"]
        groups[j["item_id"]][j["variant"]][j["target_id"]] = r["score"]
    rows, comparisons = [], defaultdict(list)
    for item, variants in groups.items():
        for label, values in variants.items():
            rows.append({"item_id": item, "variant": label, **trajectory(list(values.values()))})
        pairs = [("original", "original_break"), ("original", "forest_baseline"),
                 ("forest_baseline", "legal"), ("forest_baseline", "forest_break")]
        for before, after in pairs:
            if before not in variants or after not in variants:
                continue
            common = sorted(set(variants[before]) & set(variants[after]))
            if not common:
                continue
            a, b = [trajectory([variants[v][i] for i in common]) for v in (before, after)]
            comparisons[before + "->" + after].append({"item_id": item, "common_ids": common,
                "delta_G": b["G"] - a["G"], "delta_M": b["M"] - a["M"], "delta_NLL": b["NLL"] - a["NLL"]})
        labels = ("forest_baseline", "legal", "forest_break")
        if all(label in variants for label in labels):
            common = sorted(set.intersection(*(set(variants[label]) for label in labels)))
            if common:
                base, legal, broken = [trajectory([variants[label][i] for i in common]) for label in labels]
                comparisons["fair_break_minus_legal"].append({"item_id": item, "common_ids": common,
                    "delta_M": (broken["M"] - base["M"]) - (legal["M"] - base["M"]),
                    "delta_G": broken["G"] - legal["G"], "delta_NLL": broken["NLL"] - legal["NLL"]})
        if "parent" in variants and "control" in variants:
            common = set(variants["parent"]) & set(variants["control"])
            for target in sorted(common):
                comparisons["parent_minus_control"].append({"item_id": item, "target_id": target,
                    "delta_g": variants["parent"][target]["g"] - variants["control"][target]["g"]})
    summaries = {label: {key: bootstrap([r[key] for r in values])
                         for key in values[0] if key.startswith("delta_")}
                 for label, values in comparisons.items()}
    return {"trajectories": rows, "paired_questions": dict(comparisons), "statistics": summaries}


def e2_analysis(results, config):
    cells, groups = [], defaultdict(dict)
    for r in results:
        j = r["job"]
        summary = repeats(r["rows"], config["repeats"])
        if summary != r["summary"]:
            raise ValueError("Stored repeat summary differs from recomputation")
        cell = {"item_id": j["item_id"], "temperature": j["temperature"], **summary}
        cells.append(cell)
        groups[j["item_id"]][j["temperature"]] = cell
    temperatures = sorted(config["temperatures"])
    complete = [item for item, data in groups.items()
                if all(t in data and data[t]["complete"] for t in temperatures)]
    main = {}
    for t in temperatures:
        main[str(t)] = {k: bootstrap([groups[item][t][k] for item in complete])
                       for k in ("N", "D", "mean_g", "std_full_nll", "std_deleted_nll")}
    contrasts = {}
    for low, high in zip(temperatures, temperatures[1:]):
        contrasts[f"{high}-{low}"] = bootstrap([groups[q][high]["D"] - groups[q][low]["D"] for q in complete])
    if len(temperatures) > 2:
        contrasts[f"{temperatures[-1]}-{temperatures[0]}"] = bootstrap(
            [groups[q][temperatures[-1]]["D"] - groups[q][temperatures[0]]["D"] for q in complete])
    return {"cells": cells, "complete_question_ids": complete, "complete_questions": len(complete),
            "planned_questions": len(groups), "main_complete_cohort": main, "D_contrasts": contrasts,
            "note": "Cells include failures; primary cohort requires all planned repeats at every temperature."}


def analyze(run, output):
    run, output = Path(run), Path(output)
    protocol, config = validate_run(run)
    jobs = read(run / "jobs.json")
    cases = {c["item_id"]: c for c in read(run / "inputs/cases.json")}
    files = {p.stem: p for p in (run / "results").glob("*.json")}
    expected = {j["job_id"] for j in jobs}
    if set(files) != expected:
        raise ValueError(f"Incomplete/unexpected results: missing={len(expected-set(files))}, extra={len(set(files)-expected)}")
    for shard in range(protocol["shards"]):
        done = read(run / "workers" / str(shard) / "completion.json")
        assigned = {j["job_id"] for i, j in enumerate(jobs) if i % protocol["shards"] == shard}
        if set(done["result_hashes"]) != assigned or done["status"] != "complete":
            raise ValueError("Shard completion mismatch")
        for job_id, expected_hash in done["result_hashes"].items():
            if sha256(files[job_id]) != expected_hash:
                raise ValueError("Completed result changed")
    results = []
    step_rows = []
    for job in jobs:
        r = read(files[job["job_id"]])
        if r["job"] != job or r["protocol_id"] != protocol["protocol_id"] or r["status"] != "ok":
            raise ValueError("Result identity/status mismatch")
        if r["scientific_evidence"] != protocol["scientific_evidence"]:
            raise ValueError("Backend evidence label mismatch")
        if job["kind"] == "e2":
            if len(r["rows"]) != config["repeats"] or [x["repeat"] for x in r["rows"]] != list(range(config["repeats"])):
                raise ValueError("Missing/duplicate repeat slots")
            generation_path = run / "generations" / (job["job_id"] + ".json")
            if sha256(generation_path) != r["generation_sha256"]:
                raise ValueError("Raw generation checkpoint changed")
            generation = read(generation_path)
            if generation["job_id"] != job["job_id"] or generation["protocol_id"] != protocol["protocol_id"]:
                raise ValueError("Generation identity mismatch")
            if len(generation["rows"]) != len(r["rows"]):
                raise ValueError("Generation/scoring repeat mismatch")
            for raw, row in zip(generation["rows"], r["rows"]):
                if any(row.get(key) != value for key, value in raw.items()):
                    raise ValueError("Generation was changed during scoring")
                parsed = parse_step(raw["raw_text"], cases[job["item_id"]].get("task_type"))
                valid = parsed["valid"] and raw["finish_reason"] == "boundary"
                if parsed != raw["parse"] or (row["status"] == "ok") != valid:
                    raise ValueError("Generation validity mismatch")
                if valid and row["evidence"]["target_text"] != parsed["body"]:
                    raise ValueError("Scored target differs from generated step")
            scored = [(row["repeat"], row) for row in r["rows"] if row["status"] == "ok"]
        else:
            if r["evidence"]["target_text"] != job["target"]:
                raise ValueError("E1 target mismatch")
            scored = [(None, r)]
        for repeat, row in scored:
            check_score(row)
            step_rows.append({"item_id": job["item_id"], "variant": job.get("variant", "fixed_prefix"),
                              "target_id": job.get("target_id"), "temperature": job["temperature"],
                              "repeat": repeat, **row["score"]})
        results.append(r)
    analysis = e1_analysis(results) if protocol["experiment"] == "e1" else e2_analysis(results, config)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    summary = {"protocol_id": protocol["protocol_id"], "experiment": protocol["experiment"],
               "scientific_evidence": protocol["scientific_evidence"], "accepted_jobs": len(jobs),
               "token_arithmetic_verified": True, **analysis}
    save(output / "summary.json", summary)
    for name, rows in (("steps.csv", step_rows), ("cells.csv", analysis.get("cells", analysis.get("trajectories", [])))):
        with (output / name).open("x", encoding="utf-8", newline="") as stream:
            if rows:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    text = "# PALS validation 验收\n\n"
    text += "真实模型数值记录（不等于科学主张已成立）。\n" if protocol["scientific_evidence"] else "**仅合成 Mock 流程测试，不是模型实验结果。**\n"
    text += f"\n实验：{protocol['experiment']}；完成任务：{len(jobs)}；逐token算术复核通过。\n"
    text += "\n完整统计见 summary.json，逐步骤 g/NLL 见 steps.csv。置信区间按题重采样；合法换序CI含0不表示等效。\n"
    if protocol["experiment"] == "e2":
        text += f"\n全部温度完整配对：{analysis['complete_questions']}/{analysis['planned_questions']}题。无效输出保留，不填零。\n"
    with (output / "报告.md").open("x", encoding="utf-8") as stream:
        stream.write(text)
    return summary
