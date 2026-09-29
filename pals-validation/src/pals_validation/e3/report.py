"""Human-readable E3 tables and complete preselected cases from audited runs."""

from __future__ import annotations

import csv
from pathlib import Path

from ..io import digest, read
from .schema import require


def _stat(value: dict) -> str:
    if not value or value["n"] == 0:
        return "N/A (n=0)"
    ci = value["ci95"]
    return f"{value['mean']:.4f} [{ci[0]:.4f}, {ci[1]:.4f}] (n={value['n']})"


def _case_detail(model: str, item: str, run: Path) -> str:
    batches = read(run / "batches.json")
    batch = next(b for b in batches if item in b["problem_ids"])
    raw = read(run / "generation_batches" / (batch["batch_id"] + ".json"))
    generated = next(r for r in raw["rows"] if r["problem_id"] == item)
    score = read(run / "scores" / (digest(item) + ".json"))
    outcome = read(run / "outcomes" / (digest(item) + ".json"))
    lines = [f"### {model} · {item}", "", f"过程状态：{score['status']}；答案状态：{outcome['status']}。", ""]
    for index, step in enumerate(generated["parse"]["steps"], start=1):
        scored = next((entry for entry in score["steps"] if entry["index"] == index - 1), None)
        value = f"g={scored['score']['g']:.6f}" if scored else "g=N/A (首步或过程未评分)"
        lines.extend([f"步骤 {index}（{value}）：", "", step["text"], ""])
    answer = generated["parse"]["answer"]
    lines.extend(["最终答案：", "", answer["text"] if answer["valid"] else "N/A", ""])
    return "\n".join(lines)


def create_report(analysis_dir: str | Path, output: str | Path) -> dict:
    analysis_dir, output = Path(analysis_dir), Path(output)
    analysis = read(analysis_dir / "analysis.json")
    require(analysis["schema_version"] == "pals_e3_analysis_v1" and
            analysis["scientific_evidence"] is True, "only audited formal analyses may be reported")
    require(not output.exists(), "report output already exists")
    output.mkdir(parents=True, mode=0o700)
    lines = ["# E3 原生轨迹实验报告", "", "每题每模型只生成一次。G/M/W 为模型对自身完整推理轨迹的步骤级条件支持摘要；W 描述轨迹内部，不是重复间 D。", "",
             "## 表一：正式范围与主结果", "",
             "| 数据集 | 模型 | 原题 | 过程完整 | G 有效 | W 有效 | 答案可判 | 单次准确率 | G | M | W | full NLL |",
             "|---|---|---:|---:|---:|---:|---:|---|---|---|---|---|"]
    code_unknown = 0
    for row in analysis["primary"]:
        code_unknown += row["outcome_statuses"].get("harness_unsupported", 0)
        lines.append("| " + " | ".join([
            row["benchmark"], row["model"], str(row["planned"]),
            str(row["complete_process"]), str(row["G_valid"]), str(row["W_valid"]),
            str(row["outcome_decided"]), _stat(row["one_generation_accuracy"]),
            _stat(row["G"]), _stat(row["M"]), _stat(row["W"]), _stat(row["NLL"]),
        ]) + " |")
    lines.extend(["", "## 表二：在相同答题结果内看过程分数", "",
                  "| 数据集 | 模型 | 答题结果 | 同时有过程分数的题数 | G | M | W | full NLL |",
                  "|---|---|---|---:|---|---|---|---|"])
    for row in analysis["conditional"]:
        lines.append("| " + " | ".join([row["benchmark"], row["model"],
            "正确" if row["correct"] else "错误", str(row["n"]),
            *(_stat(row[key]) for key in ("G", "M", "W", "NLL"))]) + " |")
    lines.extend(["", "## 表三：固定 Qwen3 共同评分者（预先抽取 10% 原题）", "",
                  "| 数据集 | 生成模型 | 预选题 | G 有效 | W 有效 | G | M | W |",
                  "|---|---|---:|---:|---:|---|---|---|"])
    for row in analysis["common_scorer"]:
        lines.append("| " + " | ".join([row["benchmark"], row["model"], str(row["selected"]),
            str(row["G_valid"]), str(row["W_valid"]),
            *(_stat(row[key]) for key in ("G", "M", "W"))]) + " |")
    lines.extend(["", "## 边界与不利结果", "",
                  "所有格式失败、一步轨迹、原题歧义及运行错误均保留原状态，不补零、不替换。", ""])
    if code_unknown:
        lines.append(f"代码题共有 {code_unknown} 个答案未获安全且协议匹配的代码评测器判分；代码通过率为 N/A，本报告不能作为完整最终结果。")
        lines.append("")
    lines.extend(["模型间配对比较及其题级区间见 `analysis.json` 的 `paired`；每题状态见 `items.csv`。", ""])
    (output / "报告.md").write_text("\n".join(lines), encoding="utf-8")
    items = read(analysis_dir / "items.json")
    columns = ["model", "benchmark", "problem_id", "process_valid", "process_reason",
               "answer_valid", "finish_reason", "step_count", "G", "M", "W", "NLL",
               "outcome", "correct", "raw_sha256"]
    with (output / "items.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for model, rows in sorted(items.items()):
            for item, row in sorted(rows.items()):
                summary = row.get("summary") or {}
                writer.writerow({name: ({"model": model, **row, **summary}).get(name) for name in columns})
    examples = ["# 预先规定的案例（保留完整步骤和逐步 g）", ""]
    for entry in analysis["cases"]:
        examples.append(f"## {entry['benchmark']} / {entry['model']} / {entry['selector']}")
        examples.append("")
        item = entry["problem_id"]
        examples.append(_case_detail(entry["model"], item, Path(analysis["run_paths"][entry["model"]]))
                         if item else "N/A：该类没有可选样本。")
        examples.append("")
    (output / "案例.md").write_text("\n".join(examples), encoding="utf-8")
    return {"report": str(output / "报告.md"), "items": str(output / "items.csv"),
            "cases": str(output / "案例.md"), "code_outcomes_unavailable": code_unknown}
