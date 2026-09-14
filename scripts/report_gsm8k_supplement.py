#!/usr/bin/env python3
"""Render coverage and per-item provenance for a GSM8K DAG supplementation run."""

import argparse
import html
import json
from collections import Counter
from pathlib import Path


BASELINE_MODES = (
    ("independent_generation", "独立解题"),
    ("answer_conditioned_generation", "给定答案生成推理"),
    ("official_rationale", "官方 CoT"),
)
SUPPLEMENT_MODES = (
    ("canonical_official_rationale", "规范化官方 CoT"),
    ("diagnostic_repair_generation", "诊断重写"),
)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def result_or_state(root, item_id):
    directory = root / "items" / item_id
    result = directory / "result.json"
    if result.exists():
        return load(result)
    errors = sorted(directory.glob("*/attempt-*/error.json"))
    if errors:
        error = load(errors[-1])
        return {
            "item_id": item_id,
            "status": "uncertain_remote_state",
            "stage": errors[-1].parent.parent.name,
            "reason": error.get("category", "request failed before a response"),
        }
    return {
        "item_id": item_id,
        "status": "not_started",
        "stage": None,
        "reason": "no request was made",
    }


def mode_rows(roots, item_id):
    return {name: result_or_state(root, item_id) for name, root in roots.items()}


def status_count(rows):
    return dict(Counter(row["status"] for row in rows.values()))


def esc(value):
    return html.escape(str(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--independent", required=True, type=Path)
    parser.add_argument("--conditioned", required=True, type=Path)
    parser.add_argument("--official", required=True, type=Path)
    parser.add_argument("--canonical-initial", required=True, type=Path)
    parser.add_argument("--canonical-recovery", required=True, type=Path)
    parser.add_argument("--diagnostic-initial", required=True, type=Path)
    parser.add_argument("--diagnostic-individual-base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    items = load(args.items)
    baseline_roots = {
        "independent_generation": args.independent.resolve(),
        "answer_conditioned_generation": args.conditioned.resolve(),
        "official_rationale": args.official.resolve(),
    }
    canonical_roots = [
        args.canonical_initial.resolve(),
        args.canonical_recovery.resolve(),
    ]
    diagnostic_initial = args.diagnostic_initial.resolve()
    diagnostic_individual_base = args.diagnostic_individual_base.resolve()
    rows, counts = [], Counter()
    for item in items:
        baseline = mode_rows(baseline_roots, item["item_id"])
        canonical_candidates = [
            result_or_state(root, item["item_id"]) for root in canonical_roots
            if (root / "items" / item["item_id"]).exists()
        ]
        canonical = next(
            (row for row in canonical_candidates if row["status"] != "not_started"),
            {"status": "not_started", "stage": None, "reason": "not selected"},
        )
        diagnostic_root = (
            diagnostic_initial
            if (diagnostic_initial / "items" / item["item_id"]).exists()
            else diagnostic_individual_base / item["item_id"]
        )
        diagnostic = result_or_state(diagnostic_root, item["item_id"])
        expanded = {**baseline, "canonical_official_rationale": canonical, "diagnostic_repair_generation": diagnostic}
        covered_before = any(row["status"] == "model_accepted" for row in baseline.values())
        covered_after = any(row["status"] == "model_accepted" for row in expanded.values())
        counts["baseline_covered"] += covered_before
        counts["supplement_covered"] += covered_after and not covered_before
        counts["remaining_uncovered"] += not covered_after
        rows.append({"item": item, "modes": expanded, "covered_before": covered_before, "covered_after": covered_after})

    summary = {
        "sample_count": len(items),
        "baseline_complete_dag_count": counts["baseline_covered"],
        "supplemented_complete_dag_count": counts["supplement_covered"],
        "complete_dag_union_count": counts["baseline_covered"] + counts["supplement_covered"],
        "remaining_without_complete_dag": counts["remaining_uncovered"],
        "mode_status_counts": {
            label: status_count({row["item"]["item_id"]: row["modes"][name] for row in rows})
            for name, label in BASELINE_MODES + SUPPLEMENT_MODES
        },
        "remaining_item_ids": [row["item"]["item_id"] for row in rows if not row["covered_after"]],
        "boundary": "model_accepted means same-model reviewed pending human review; it is not independent mathematical validation.",
    }
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = [
        "# GSM8K DAG 缺口补齐实验",
        "",
        "本实验仅处理三条基线路径均未得到完整 DAG 的固定样本。所有新运行目录独立保存，原始题目、`raw_answer` 和三条基线结果均未修改。",
        "",
        "| 指标 | 数量 |",
        "|---|---:|",
        f"| 初始缺口 | {summary['sample_count']} |",
        f"| 基线已有完整 DAG | {summary['baseline_complete_dag_count']} |",
        f"| 新补齐完整 DAG | {summary['supplemented_complete_dag_count']} |",
        f"| 补齐后完整 DAG 并集 | {summary['complete_dag_union_count']} |",
        f"| 仍无完整 DAG | {summary['remaining_without_complete_dag']} |",
        "",
        "## 路径与边界",
        "",
        "- `canonical_official_rationale`：不调用 solve；仅把 GSM8K `<<expression>>result` 标记变为 `expression`，同时保存原始答案哈希和规范化文本哈希。",
        "- `diagnostic_repair_generation`：请求包含 gold answer、上一轮给定答案的草稿和结构性失败诊断；模型只生成替代 rationale，框架单独记录答案来自数据集。",
        "- `uncertain_remote_state`：请求已发出但没有收到响应，按幂等与计费约束不在原目录自动重试。",
        "",
        "> `model_accepted` 仅表示同模型审核通过、待人工审核，不是独立的数学正确性证明。",
        "",
        "## 各路径状态",
        "",
        "| 路径 | 状态分布 |",
        "|---|---|",
    ]
    for name, label in BASELINE_MODES + SUPPLEMENT_MODES:
        md.append(f"| {label} | `{summary['mode_status_counts'][label]}` |")
    (output / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    blocks = [
        "<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">",
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        "<title>GSM8K DAG 缺口补齐实验</title>",
        "<style>body{max-width:1450px;margin:28px auto;padding:0 18px;font:15px/1.55 sans-serif;color:#202124}table{border-collapse:collapse;width:100%;margin:16px 0 28px}th,td{border:1px solid #c9cdd2;padding:8px;vertical-align:top;text-align:left}th{background:#f1f3f4}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f7f8;padding:12px;margin:7px 0}.grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.mode{border:1px solid #c9cdd2;padding:8px}.model_accepted{color:#137333}.rejected{color:#b3261e}.needs_review,.uncertain_remote_state{color:#b06000}@media(max-width:1100px){.grid{grid-template-columns:1fr}}</style>",
        "<h1>GSM8K DAG 缺口补齐实验</h1>",
        "<p>固定处理三条基线路径均未得到完整 DAG 的 16 条样本。新路径与旧产物隔离，所有来源和超时状态可追溯。</p>",
        "<table><tr><th>指标</th><th>数量</th></tr>" + "".join(
            f"<tr><td>{esc(key)}</td><td>{esc(value)}</td></tr>"
            for key, value in summary.items() if key.endswith("count") or key == "remaining_without_complete_dag"
        ) + "</table>",
        "<p><strong>边界：</strong>model_accepted 仅表示同模型审核通过，仍需人工审核；它不是独立数学真值证明。</p>",
    ]
    for index, row in enumerate(rows, 1):
        item, modes = row["item"], row["modes"]
        blocks.append(f"<section><h2>{index}. {esc(item['item_id'])}</h2><pre>{esc(item['question'])}</pre><p>标准答案：<strong>{esc(item['gold_answer'])}</strong>；补齐后覆盖：<strong>{esc(row['covered_after'])}</strong></p><div class=grid>")
        for name, label in BASELINE_MODES + SUPPLEMENT_MODES:
            state = modes[name]
            blocks.append(
                f"<div class=mode><h3>{esc(label)}</h3><p class={esc(state['status'])}>{esc(state['status'])} · {esc(state.get('stage'))}</p><small>{esc(state.get('reason', ''))}</small></div>"
            )
        blocks.append("</div></section>")
    blocks.append("</html>")
    (output / "review.html").write_text("\n".join(blocks), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
