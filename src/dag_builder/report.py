"""Escaped, self-contained human review reports; all selected items remain visible."""

import html
import json
from collections import Counter

from .run_status import paused_items
from .storage import digest, read_json, write_bytes_once, write_once

REPORT_TEMPLATE_VERSION = "7"


def report_title(items, selection):
    """Return a dataset-specific title without changing the stored sample data."""
    task_types = {item.get("task_type", "mmlu") for item in items}
    if task_types == {"gsm8k"}:
        is_full_dataset = selection.get("selected_count") == len(
            items
        ) and selection.get("candidate_count") == len(items)
        return "GSM8K DAG 构造全量审查" if is_full_dataset else "GSM8K DAG 构造审查"
    if task_types == {"mmlu"}:
        return "MMLU DAG 构造试点审查"
    if task_types == {"gpqa"}:
        return "GPQA-Diamond DAG 构造审查"
    if task_types == {"humaneval"}:
        return "HumanEval 参考代码解释与 DAG 审查（未执行代码）"
    if task_types == {"livecodebench"}:
        return "LiveCodeBench v6 参考程序候选（需独立执行验收）"
    return "DAG 构造审查"


def overview(root):
    items = read_json(root / "items.json")
    pauses = paused_items(root)
    rows, counts = [], Counter()
    for item in items:
        directory = root / "items" / item["item_id"]
        result = (
            read_json(directory / "result.json")
            if (directory / "result.json").exists()
            else pauses.get(item["item_id"])
            or {
                "item_id": item["item_id"],
                "status": (
                    "not_started"
                    if (directory / "baseline.json").exists()
                    and not any(directory.glob("*/attempt-*/request.json"))
                    else "incomplete"
                    if directory.exists()
                    else "not_started"
                ),
            }
        )
        result = dict(
            result,
            completed_stages=sorted(
                p.parent.name for p in directory.glob("*/output.json")
            ),
        )
        counts[result["status"]] += 1
        rows.append(result)
    usage, reported, attempts = 0, 0, 0
    returned_models = Counter()
    for request in root.glob("items/*/*/attempt-*/request.json"):
        attempts += 1
        response = request.parent / "response.json"
        if response.exists():
            body = read_json(response)["body"]
            returned_models[str(body.get("model", "not_reported"))] += 1
            tokens = body.get("usage", {}).get("total_tokens")
            if type(tokens) is int and tokens >= 0:
                usage += tokens
                reported += 1
    return {
        "selected": len(items),
        "counts": dict(counts),
        "items": rows,
        "api_attempts": attempts,
        "requests_with_reported_usage": reported,
        "reported_total_tokens": usage,
        "reported_usage_complete": reported == attempts,
        "returned_models": dict(returned_models),
        "monetary_cost": None,
        "cost_note": "No verified unit price configured; missing usage is not zero cost.",
        "claim": "engineering pilot; same-model synthesis/review, no PALS validation claim",
    }


def render(root):
    summary = overview(root)
    item_status = {row["item_id"]: row for row in summary["items"]}
    items = read_json(root / "items.json")
    title = report_title(items, read_json(root / "selection.json"))
    # Include the renderer version so a template fix creates a new immutable report.
    version = digest({"template": REPORT_TEMPLATE_VERSION, "summary": summary})[:16]
    destination = root / "reports" / version
    write_once(destination / "summary.json", summary)
    blocks = [
        '<!doctype html><html lang="zh"><meta charset="utf-8">',
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        "<title>"
        + html.escape(title)
        + "</title><style>body{max-width:1100px;margin:35px auto;padding:0 20px;font:16px/1.6 sans-serif}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f6f8;padding:16px}section{border-top:1px solid #ccc;margin:30px 0}h3{margin-top:24px}</style>",
        "<h1>"
        + html.escape(title)
        + "</h1><p>构图与模型自审不构成独立验证。待人工审查，不自动发布 gold 数据。原始题目仅供本地私密审查。</p>",
        "<pre>" + html.escape(str(summary["counts"])) + "</pre>",
    ]
    for item in items:
        blocks.append(
            "<section><h2>"
            + html.escape(item["item_id"])
            + " · 源行 "
            + str(item["row"])
            + "</h2>"
        )
        rendered_question = item["question"]
        if item.get("task_type") not in ("gsm8k", "humaneval", "livecodebench"):
            rendered_question += "\n" + "\n".join(
                f"{label}. {choice}" for label, choice in zip("ABCD", item["choices"])
            )
        blocks.append("<pre>" + html.escape(rendered_question) + "</pre>")
        if item.get("task_type") == "humaneval":
            blocks.append("<h3>官方参考 completion（未执行）</h3><pre>"
                          + html.escape(item["canonical_solution"]) + "</pre>")
        elif item.get("task_type") == "livecodebench":
            blocks.append("<p>模型参考程序候选；不是官方 gold，也未自动通过测试。</p>")
        else:
            blocks.append("<p>数据集答案：" + html.escape(item["gold_answer"]) + "</p>")
        if item.get("task_type") == "gpqa":
            blocks.append(
                "<h3>GPQA 官方专家 Explanation（未重新生成）</h3><pre>"
                + html.escape(item["official_explanation"])
                + "</pre><p>来源字段："
                + html.escape(item["source_fields"]["Explanation"])
                + "；领域："
                + html.escape(item["domain"])
                + "</p>"
            )
        if item.get("task_type") == "gsm8k":
            blocks.append(
                "<h3>GSM8K 原始解答</h3><pre>"
                + html.escape(item["raw_answer"])
                + "</pre>"
            )
        directory = root / "items" / item["item_id"]
        blocks.append(
            "<h3>验收状态（含有证据的通信暂停）</h3><pre>"
            + html.escape(json.dumps(item_status[item["item_id"]], ensure_ascii=False))
            + "</pre>"
        )
        for path in sorted(directory.glob("round-*/*.json")):
            if path.name not in (
                "output.json",
                "diff.json",
                "validation.json",
                "normalization.json",
            ):
                continue
            blocks.append(
                "<h3>"
                + html.escape(str(path.relative_to(directory)))
                + "</h3><pre>"
                + html.escape(json.dumps(read_json(path), ensure_ascii=False, indent=2))
                + "</pre>"
            )
        for label, path in [
            ("LiveCodeBench 参考程序候选", directory / "reference_code/output.json"),
            ("修复前原始状态与产物", directory / "baseline.json"),
            ("确定性拓扑排序", directory / "topology.json"),
            ("模型复核与修复提案", directory / "repair/output.json"),
            ("修改前后与修复依据", directory / "repair_audit.json"),
            ("修复后候选结构", directory / "candidate.json"),
            ("原始参考解答", directory / "solve/output.json"),
            (
                "整理后的参考解答（原生思考模式）",
                directory / "structure_solution/output.json",
            ),
            ("解答审核", directory / "review_solution/output.json"),
            ("原子节点", directory / "atomize/output.json"),
            ("直接依赖", directory / "dependencies/output.json"),
            ("推导解释", directory / "justify/output.json"),
            ("完整审核", directory / "review_dag/output.json"),
            ("新上下文最终审核（仍为同模型）", directory / "review_repair/output.json"),
        ]:
            if path.exists():
                blocks.append(
                    "<h3>"
                    + label
                    + "</h3><pre>"
                    + html.escape(path.read_text())
                    + "</pre>"
                )
        for path in sorted(directory.glob("*/validation.json")):
            blocks.append(
                "<h3>结构/格式问题 · "
                + html.escape(path.parent.name)
                + "</h3><pre>"
                + html.escape(path.read_text())
                + "</pre>"
            )
        blocks.append("</section>")
    blocks.append("</html>")
    write_bytes_once(destination / "review.html", "\n".join(blocks).encode())
    return destination
