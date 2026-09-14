#!/usr/bin/env python3
"""Build a self-contained comparison of three GSM8K solution-source runs."""

import argparse
import html
import json
import statistics
from collections import Counter
from pathlib import Path


MODES = (
    ("independent_generation", "独立解题"),
    ("answer_conditioned_generation", "给定答案生成推理"),
    ("official_rationale", "直接使用官方推理"),
)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def reason_category(result):
    reason = result.get("reason", "")
    if reason.startswith("answer_label_mismatch"):
        return "答案不匹配"
    if "not one JSON object" in reason:
        return "JSON 格式错误"
    if "source quote" in reason:
        return "引用无法验证"
    if "root" in reason or "connected" in reason or "premise" in reason:
        return "DAG 结构错误"
    if result["status"] == "model_accepted":
        return "通过"
    return result.get("stage", "其他") + " 其他"


def mode_data(root, item_ids):
    rows, status, stages, reasons = {}, Counter(), Counter(), Counter()
    attempts = reported_tokens = 0
    rationale_lengths, accepted_node_counts, kinds = [], [], Counter()
    for item_id in item_ids:
        directory = root / "items" / item_id
        result = load(directory / "result.json")
        solution_path = directory / "solve" / "output.json"
        if solution_path.exists():
            solution = load(solution_path)
        else:
            raw_response = next(directory.glob("solve/attempt-*/response.json"), None)
            content = None
            if raw_response:
                body = load(raw_response).get("body", {})
                choices = body.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content")
            solution = {
                "answer": None,
                "rationale": content or "[没有有效的 solve 输出]",
                "valid_output": False,
            }
        dag_path = directory / "dag.json"
        dag = load(dag_path) if dag_path.exists() else None
        rows[item_id] = {"result": result, "solution": solution, "dag": dag}
        status[result["status"]] += 1
        stages[f'{result["status"]}:{result["stage"]}'] += 1
        reasons[reason_category(result)] += 1
        if solution.get("valid_output", True):
            rationale_lengths.append(len(solution.get("rationale", "")))
        if dag:
            accepted_node_counts.append(len(dag["nodes"]))
            kinds.update(node["kind"] for node in dag["nodes"])
        for response in directory.glob("*/attempt-*/response.json"):
            attempts += 1
            usage = load(response)["body"].get("usage", {})
            if type(usage.get("total_tokens")) is int:
                reported_tokens += usage["total_tokens"]
    return {
        "root": str(root),
        "status": dict(status),
        "stages": dict(stages),
        "reason_categories": dict(reasons),
        "api_responses": attempts,
        "reported_total_tokens": reported_tokens,
        "mean_rationale_chars": round(statistics.mean(rationale_lengths), 1),
        "mean_nodes_for_accepted": (
            round(statistics.mean(accepted_node_counts), 2)
            if accepted_node_counts
            else None
        ),
        "accepted_node_kinds": dict(kinds),
        "rows": rows,
    }


def esc(value):
    return html.escape(str(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--independent", required=True, type=Path)
    parser.add_argument("--conditioned", required=True, type=Path)
    parser.add_argument("--official", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    items = load(args.items)
    item_ids = [item["item_id"] for item in items]
    roots = (args.independent, args.conditioned, args.official)
    modes = {
        key: mode_data(root.resolve(), item_ids)
        for (key, _), root in zip(MODES, roots)
    }
    patterns = Counter(
        tuple(modes[key]["rows"][item_id]["result"]["status"] for key, _ in MODES)
        for item_id in item_ids
    )
    summary = {
        "sample_count": len(items),
        "same_item_ids": all(set(mode["rows"]) == set(item_ids) for mode in modes.values()),
        "modes": {
            key: {k: v for k, v in mode.items() if k != "rows"}
            for key, mode in modes.items()
        },
        "status_patterns": {" | ".join(key): value for key, value in patterns.items()},
    }

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown = [
        "# GSM8K 三种推理来源对照",
        "",
        f"固定样本数：{summary['sample_count']}；三路 item_id 一致：{summary['same_item_ids']}。",
        "",
        "> `model_accepted` 表示模型审核通过，仍需人工审核；它不是独立数学真值证明。",
        "",
        "| 方式 | 通过 | 拒绝 | 待审 | API 响应 | tokens | 平均推理字符 | 通过样本平均节点 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in MODES:
        mode = modes[key]
        markdown.append(
            f"| {label} | {mode['status'].get('model_accepted', 0)} "
            f"| {mode['status'].get('rejected', 0)} | {mode['status'].get('needs_review', 0)} "
            f"| {mode['api_responses']} | {mode['reported_total_tokens']} "
            f"| {mode['mean_rationale_chars']} | {mode['mean_nodes_for_accepted']} |"
        )
    markdown.extend([
        "",
        "## 结论",
        "",
        "1. 独立解题最能反映模型是否能自行得到标准答案，但 100 条中有较多样本在 solve 阶段因答案不匹配停止。",
        "2. 给定标准答案后，完整 DAG 通过数最高；它适合构造答案对齐的推理数据，但答案已由数据集提供，不能再用最终答案匹配衡量模型解题能力。",
        "3. 官方 rationale 不需要 solve 请求，API 成本最低；但官方文本中的 GSM8K 计算标记和简写会增加 source_quote/atomize 校验失败，需要增加专门的官方 rationale 清洗或引用策略。",
        "4. 三种方式都可能在 dependencies 阶段失败，因此后续质量瓶颈已经从‘算对答案’转移到‘拆成可验证、最小且闭合的 DAG’。",
        "",
        "## 产物",
        "",
        "- `comparison.html`：逐条展示三种方式的状态、答案、推理和 DAG 节点。",
        "- `summary.json`：机器可读统计。",
    ])
    (args.output / "comparison.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    blocks = [
        '<!doctype html><html lang="zh"><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
        "<title>GSM8K 三种推理来源对照</title>",
        "<style>body{max-width:1500px;margin:28px auto;padding:0 18px;font:15px/1.55 sans-serif;color:#202124}table{border-collapse:collapse;width:100%;margin:16px 0 28px}th,td{border:1px solid #c9cdd2;padding:8px;vertical-align:top;text-align:left}th{background:#f1f3f4}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f7f8;padding:12px;margin:7px 0}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.mode{border:1px solid #c9cdd2;padding:10px}.model_accepted{color:#137333}.rejected{color:#b3261e}.needs_review{color:#b06000}section{border-top:2px solid #5f6368;padding-top:18px;margin-top:34px}small{color:#5f6368}@media(max-width:950px){.grid{grid-template-columns:1fr}}</style>",
        "<h1>GSM8K 三种推理来源对照</h1>",
        "<p>同一批 100 条固定样本；独立解题复用全量运行结果，另外两种模式为本次运行。model_accepted 仍只表示同模型审核通过，未经过独立数学验证或人工发布。</p>",
        "<table><thead><tr><th>方式</th><th>通过</th><th>拒绝</th><th>待审</th><th>API 响应</th><th>Reported tokens</th><th>平均推理字符</th><th>通过样本平均节点</th></tr></thead><tbody>",
    ]
    for key, label in MODES:
        mode = modes[key]
        blocks.append(
            "<tr><td>" + label + "</td>"
            + f'<td>{mode["status"].get("model_accepted", 0)}</td>'
            + f'<td>{mode["status"].get("rejected", 0)}</td>'
            + f'<td>{mode["status"].get("needs_review", 0)}</td>'
            + f'<td>{mode["api_responses"]}</td>'
            + f'<td>{mode["reported_total_tokens"]}</td>'
            + f'<td>{mode["mean_rationale_chars"]}</td>'
            + f'<td>{mode["mean_nodes_for_accepted"]}</td></tr>'
        )
    blocks.append("</tbody></table><h2>失败类型</h2><table><tr><th>方式</th><th>分布</th></tr>")
    for key, label in MODES:
        blocks.append(f"<tr><td>{label}</td><td><pre>{esc(modes[key]['reason_categories'])}</pre></td></tr>")
    blocks.append("</table><h2>逐条对照</h2>")

    for index, item in enumerate(items, 1):
        item_id = item["item_id"]
        blocks.append(f"<section><h2>{index}. {esc(item_id)}</h2>")
        blocks.append(f"<pre>{esc(item['question'])}</pre><p>标准答案：<strong>{esc(item['gold_answer'])}</strong></p><div class=grid>")
        for key, label in MODES:
            row = modes[key]["rows"][item_id]
            result, solution, dag = row["result"], row["solution"], row["dag"]
            blocks.append(
                f'<div class="mode"><h3>{label}</h3>'
                f'<p class="{esc(result["status"])}">{esc(result["status"])} · {esc(result["stage"])}</p>'
                f'<small>{esc(result.get("reason", ""))}</small>'
                f'<p>输出答案：{esc(solution.get("answer"))}</p>'
                f'<pre>{esc(solution.get("rationale", ""))}</pre>'
            )
            if dag:
                blocks.append("<details><summary>DAG 节点</summary><pre>")
                blocks.append(
                    esc("\n".join(
                        f'{node["node_id"]} [{node["kind"]}] parents={node["parents"]}: {node["statement"]}'
                        for node in dag["nodes"]
                    ))
                )
                blocks.append("</pre></details>")
            blocks.append("</div>")
        blocks.append("</div></section>")
    blocks.append("</html>")
    (args.output / "comparison.html").write_text("\n".join(blocks), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
