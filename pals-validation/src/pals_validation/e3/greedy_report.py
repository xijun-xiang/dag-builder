"""Question-weighted AS/NS, MMLU subject summaries and explicit missingness."""
from collections import Counter
import csv
from pathlib import Path
from statistics import fmean

from ..io import read, save, sha256, verify
from .greedy import model_slots, load
from .schema import require

METRICS = ("G", "M", "NLL")


def estimate(values, seed=2026100601, draws=5000):
    if not values:
        return {"n": 0, "mean": None, "ci95": None}
    import numpy as np
    a = np.asarray(values, dtype=np.float64)
    require(np.isfinite(a).all(), "nonfinite summary")
    rng, boot = np.random.default_rng(seed), []
    for start in range(0, draws, 32):
        indices = rng.integers(0, len(a), size=(min(32, draws - start), len(a)))
        boot.extend(a[indices].mean(axis=1).tolist())
    boot.sort()
    return {"n": len(a), "mean": float(a.mean()),
            "ci95": [boot[int(.025 * (draws - 1))], boot[int(.975 * (draws - 1))]]}


def group_summary(rows, **labels):
    valid = [r for r in rows if r["summary"] and r["summary"]["G"] is not None]
    decided = [r["correct"] for r in rows if r["correct"] is not None]
    return {**labels, "total": len(rows), "process_valid": sum(r["process_valid"] for r in rows),
        "scoreable": len(valid), "one_step": sum(r["process_valid"] and r["step_count"] == 1 for r in rows),
        "invalid_reasons": dict(Counter(r["process_reason"] for r in rows if not r["process_valid"])),
        "step_count_distribution": dict(Counter(r["step_count"] for r in rows)),
        "outcome_statuses": dict(Counter(r["outcome_status"] for r in rows)),
        "length_terminations": sum(r.get("finish_reason") == "length" for r in rows),
        "context_capped": sum(r.get("generation_budget", {}).get("context_capped", False) for r in rows),
        "output_budget_distribution": dict(Counter(r["generation_budget"]["effective_max_new_tokens"]
                                                    for r in rows if "generation_budget" in r)),
        "answer_decided": len(decided), "accuracy": fmean(decided) if decided else None,
        **{metric: estimate([r["summary"][metric] for r in valid]) for metric in METRICS}}


def summarize(root: Path, output: Path):
    require(output is not None and not output.exists(), "new output directory required")
    models, hashes, protocol_id = {}, {}, None
    slots = model_slots(read(root / "manifest.json"))
    for slot in slots:
        manifest, _, _, _ = load(root, slot)
        protocol_id = protocol_id or manifest["protocol_id"]
        require(manifest["protocol_id"] == protocol_id, "different protocol")
        path = root / slot / "audit.json"
        audit = read(path)
        require(audit["status"] == "PASS" and audit["include_outcomes"] and
                audit["protocol_id"] == protocol_id, "unaccepted audit")
        verify(root / slot, audit["files"])
        models[slot] = audit["items"]
        hashes[slot] = sha256(path)
    primary, subjects, paired, sensitivity = [], [], [], []
    for slot, rows in models.items():
        for benchmark in sorted(manifest["counts"]):
            group = [r for r in rows if r["benchmark"] == benchmark]
            primary.append(group_summary(group, model=slots[slot], benchmark=benchmark))
            for label, predicate in (("exclude_title_body_recovery", lambda r:
                    "title_and_body_explicit_boundary" not in r["segmentation_methods"]),
                    ("exclude_length_terminations", lambda r: r["finish_reason"] != "length")):
                sensitivity.append(group_summary([r for r in group if predicate(r)],
                    model=slots[slot], benchmark=benchmark, population=label))
            for label, predicate in (("two_steps", lambda r: r["step_count"] == 2),
                                     ("three_to_five", lambda r: 3 <= r["step_count"] <= 5),
                                     ("six_or_more", lambda r: r["step_count"] >= 6)):
                sensitivity.append(group_summary([r for r in group if predicate(r)],
                                                  model=slots[slot], benchmark=benchmark, step_bin=label))
        for subject in sorted({r["subset"] for r in rows if r["benchmark"] == "mmlu"}):
            subjects.append(group_summary([r for r in rows if r["benchmark"] == "mmlu" and r["subset"] == subject],
                                          model=slots[slot], subject=subject))
    for benchmark in sorted(manifest["counts"]):
        by_model = {slot: {r["problem_id"]: r for r in rows if r["benchmark"] == benchmark and
                          r["summary"] and r["summary"]["G"] is not None} for slot, rows in models.items()}
        common = sorted(set.intersection(*(set(rows) for rows in by_model.values())))
        for slot, rows in by_model.items():
            paired.append(group_summary([rows[i] for i in common], model=slots[slot], benchmark=benchmark,
                                        population="cohort_common_valid", models_in_cohort=len(slots)))
    macro = []
    for slot in slots:
        per_subject = [r for r in subjects if r["model"] == slots[slot]]
        # Explicit subject-weighted point estimates. No subject-as-question bootstrap.
        macro.append({"model": slots[slot], "subjects_total": len(per_subject),
            "subjects_scoreable": sum(r["scoreable"] > 0 for r in per_subject),
            **{m: fmean(r[m]["mean"] for r in per_subject if r[m]["mean"] is not None)
               if any(r[m]["mean"] is not None for r in per_subject) else None for m in METRICS}})
    output.mkdir(parents=True, mode=0o700)
    result = {"protocol_id": protocol_id, "scientific_evidence": manifest["scientific_evidence"],
        "audit_hashes": hashes, "primary_question_weighted": primary, "mmlu_subjects": subjects,
        "mmlu_subject_weighted_point_estimates": macro, "common_valid": paired,
        "step_count_sensitivity": sensitivity,
        "notes": ["AS=G; NS=M. W and cross-repeat D are not primary metrics.",
                  "NLL covers the same target steps as g, not the whole generated output.",
                  "CIs resample questions, not repeated generations.",
                  "Historical CoEvalChain results are separate measurements; no automatic matching of incompatible scopes."]}
    if "full_continuation" in manifest:
        result["continuation"] = manifest["full_continuation"]
        result["notes"] += ["Coverage is report-only by prior authorization; PASS means integrity, not high coverage.",
            "Undefined scores are N/A, never zero. AS/NS and their CIs describe scoreable outputs only.",
            "Source attempts without sealed raw are infrastructure N/A and are never regenerated.",
            "Fixed batch membership, seeds, prompts, budgets and v2 parser are unchanged; worker dispatch changed."]
    save(output / "summary.json", result)
    with (output / "questions.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["model", "problem_id", "benchmark", "subset", "process_valid",
                  "process_reason", "step_count", "mean_step_tokens", "G", "M", "NLL", "correct", "outcome_status"])
        writer.writeheader()
        for slot, rows in models.items():
            for row in rows:
                writer.writerow({"model": slots[slot], **{k: row[k] for k in ("problem_id", "benchmark", "subset",
                    "process_valid", "process_reason", "step_count", "correct", "outcome_status")},
                    "mean_step_tokens": fmean(row["step_token_lengths"]) if row["step_token_lengths"] else None,
                    **{m: row["summary"][m] if row["summary"] else None for m in METRICS}})
    lines = ["# E3 贪心原生轨迹全量结果", "", "每题一次生成，生成 T=0、评分 T=1；AS=G，NS=M。", "",
             "| 基准 | 模型 | 可评分/总题 | AS [95% CI] | NS [95% CI] | 本次正确率（可判题） |", "|---|---|---:|---|---|---|"]
    def fmt(value):
        return "N/A" if value["mean"] is None else f"{value['mean']:.6f} [{value['ci95'][0]:.6f}, {value['ci95'][1]:.6f}]"
    for r in primary:
        accuracy = "N/A" if r["accuracy"] is None else f"{r['accuracy']:.2%} ({r['answer_decided']})"
        lines.append(f"| {r['benchmark']} | {r['model']} | {r['scoreable']}/{r['total']} | {fmt(r['G'])} | {fmt(r['M'])} | {accuracy} |")
    lines += ["", "MMLU 主表暂列题目等权汇总；57 子集及子集等权值见 summary.json。与历史主表合并前必须核对其汇总口径。",
              "无效原因、一步轨迹、步骤粒度、共同有效题集和不利结果均保留。当前不伪造或代填历史 CoEvalChain 数值。"]
    if "full_continuation" in manifest:
        lines += ["", "本轮按预先授权采用全分母报告：格式覆盖率不作为停跑门槛。审计 PASS 只表示完整性通过，",
                  "不表示模型覆盖率高。AS/NS 及区间仅描述可评分轨迹，不能推广到被排除的无效轨迹。",
                  "原始中断且无完整输出的题目记基础设施 N/A，不重新生成；与模型格式失败分开统计。"]
    (output / "报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "PASS", "primary_rows": len(primary), "output": str(output)}
