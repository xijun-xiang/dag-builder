"""Private all-outcome export; presentation never grants human approval."""

import json
from collections import Counter
from pathlib import Path

from .revision_source import seed_candidate
from .storage import digest, read_json, write_bytes_once, write_once

ACCEPTED = {"model_accepted", "repaired_model_accepted", "model_accepted_diagnostic"}


def outcome(root, item_id):
    root = Path(root)
    directory = root / "items" / item_id
    result_path = directory / "result.json"
    dag, candidate = None, None
    if result_path.exists():
        result = read_json(result_path)
        if result.get("status") in ACCEPTED:
            dag = read_json(directory / "dag.json")
            if digest(dag) != result["dag_sha256"]:
                raise ValueError("DAG digest mismatch")
    elif (directory / "verification.json").exists():
        result_path = directory / "verification.json"
        verification = read_json(result_path)
        review = verification.get("output", {})
        decision = review.get("decision", verification.get("audit_decision"))
        status = (
            "model_accepted_diagnostic"
            if decision == "accept"
            else "revisable"
            if decision == "revisable"
            else "source_disputed"
            if decision == "source_disputed"
            else "diagnostic_not_accepted"
        )
        result = dict(
            verification,
            status=status,
            reason=review.get(
                "reason",
                verification.get("audit_reason", verification.get("reason", "")),
            ),
        )
    elif (root / "phase-result.json").exists():
        result_path = root / "phase-result.json"
        result = next(
            (r for r in read_json(result_path)["results"] if r["item_id"] == item_id),
            None,
        )
        if result is None:
            return None
    elif (root / "completion.json").exists():
        completion = read_json(root / "completion.json")
        if completion.get("item_id") != item_id:
            return None
        result_path = root / "completion.json"
        if completion.get("status") != "audit_completed":
            return (
                None  # Unknown transport outcomes do not replace a scientific verdict.
            )
        result = dict(
            completion,
            status="model_accepted_diagnostic"
            if completion.get("decision") == "accept"
            else "diagnostic_not_accepted",
        )
    else:
        return None
    # Preserve latest candidate even for failed audits, but do not call it accepted.
    candidates = sorted(directory.glob("round-*/candidate.json"))
    if candidates:
        candidate = read_json(candidates[-1])
    else:
        audit_inputs = sorted(directory.glob("round-*-audit/input.json"))
        if audit_inputs:
            candidate = read_json(audit_inputs[-1])["input"].get("candidate")
        else:
            candidate, _ = seed_candidate(directory)
    if result["status"] == "model_accepted_diagnostic" and candidate is None:
        raise ValueError("diagnostic acceptance without candidate")
    return {
        "source_root": str(root),
        "result_path": str(result_path),
        "result_sha256": digest(read_json(result_path)),
        "result": result,
        "dag": dag,
        "candidate": candidate,
    }


def record(item, events, exclusions, pilot_ids):
    latest = events[-1] if events else None
    status = (
        latest["result"]["status"]
        if latest
        else ("source_entry_review" if item["item_id"] in exclusions else "pending")
    )
    return {
        "schema_version": "gpqa_all_outcomes_v1",
        "item_id": item["item_id"],
        "cohort_role": "pilot_80" if item["item_id"] in pilot_ids else "full_extension",
        "source": item,
        "status": status,
        "model_accepted": status in ACCEPTED,
        "human_approved": False,
        "reason": latest["result"].get("reason", "")
        if latest
        else exclusions.get(item["item_id"], "not processed"),
        "dag": latest["dag"] if latest and status in ACCEPTED else None,
        "candidate": latest["candidate"] if latest else None,
        "history": events,
        "limitation": "Current-protocol outcomes, not proof of DAG impossibility or human-certified gold. Pilot and extension protocols may differ.",
    }


def render_html(rows, manifest):
    payload = (
        json.dumps({"rows": rows, "manifest": manifest}, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (
        """<!doctype html><html lang="zh"><meta charset="utf-8"><title>GPQA-Diamond 全量审核</title>
<style>body{font:16px system-ui;margin:32px;max-width:1300px;background:#f6f7fb;color:#172238}header,article{background:white;padding:24px;margin:16px 0;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px system-ui}table{width:100%;border-collapse:collapse}td,th{padding:10px;border:1px solid #ddd;text-align:left;vertical-align:top}input,select{padding:10px;margin:8px}summary{cursor:pointer;font-weight:600}small{color:#596578}</style>
<header><h1>GPQA-Diamond 全量结果</h1><p>包含通过、失败、争议及入口待检查样本。模型审核不等于人工认证；先导批与扩展批保留各自历史。</p>
<p><a href="all_results.jsonl">全部结果 JSONL</a> · <a href="model_accepted.jsonl">仅模型通过 JSONL</a> · <a href="manifest.json">清单</a></p>
<pre id="summary"></pre><input id="query" placeholder="搜索题目、ID、领域"><select id="status"><option value="">全部状态</option></select><small id="count"></small></header><main id="list"></main>
<script type="application/json" id="data">"""
        + payload
        + """</script><script>
const data=JSON.parse(document.getElementById('data').textContent),rows=data.rows;
const el=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n};
document.getElementById('summary').textContent=JSON.stringify(data.manifest.counts,null,2);
const status=document.getElementById('status'),query=document.getElementById('query');
[...new Set(rows.map(r=>r.status))].sort().forEach(s=>{const o=el('option',s);o.value=s;status.append(o)});
function draw(){const list=document.getElementById('list');list.replaceChildren();let selected=rows.filter(r=>(!status.value||r.status===status.value)&&JSON.stringify([r.item_id,r.source.question,r.source.domain]).toLowerCase().includes(query.value.toLowerCase()));document.getElementById('count').textContent=selected.length+' / '+rows.length;
for(const r of selected){const a=document.createElement('article'),d=document.createElement('details');d.append(el('summary',r.item_id+' · '+r.source.domain+' · '+r.status));d.append(el('p',r.reason));d.append(el('h3','题目与选项'));d.append(el('pre',r.source.question+'\\n\\n'+r.source.choices.map((c,i)=>'ABCD'[i]+'. '+c).join('\\n')));d.append(el('p','来源答案：'+r.source.gold_answer));d.append(el('h3','官方 Explanation（原文）'));d.append(el('pre',r.source.official_explanation));
const nodes=r.dag?.nodes||r.candidate?.nodes||[];if(nodes.length){d.append(el('h3',r.model_accepted?'模型通过候选（待人工）':'未通过/不完整候选'));let t=document.createElement('table'),head=document.createElement('tr');['节点','类型','断言','父节点','Justification','来源引文'].forEach(x=>head.append(el('th',x)));t.append(head);for(const n of nodes){let tr=document.createElement('tr'),parents=n.parents||r.candidate?.parents?.find(p=>p.node_id===n.node_id)?.parents||[],j=n.justification||r.candidate?.justifications?.find(p=>p.node_id===n.node_id)?.text||'';[n.node_id,n.kind,n.statement,parents.join(', '),j,n.source_quote].forEach(x=>tr.append(el('td',String(x??''))));t.append(tr)}d.append(t)}else d.append(el('p','尚无完整候选图。'));
let h=document.createElement('details');h.append(el('summary','完整结果与历史记录'));h.append(el('pre',JSON.stringify(r,null,2)));d.append(h);a.append(d);list.append(a)}}status.onchange=draw;query.oninput=draw;draw();</script></html>"""
    )


def export_campaign(root, label):
    root = Path(root)
    manifest = read_json(root / "campaign.json")
    items = read_json(root / "items.json")
    pilot = read_json(root / "pilot_history.json")
    exclusions = manifest["source_entry_reviews"]
    rows = []
    for item in items:
        events = list(pilot.get(item["item_id"], []))
        for stage in ("construction", "revision"):
            event = outcome(root / stage, item["item_id"])
            if event:
                events.append(event)
        rows.append(record(item, events, exclusions, set(manifest["pilot_ids"])))
    if len(rows) != len({r["item_id"] for r in rows}) or len(rows) != 198:
        raise ValueError("full export must contain every unique Diamond item")
    summary = {
        "schema_version": "gpqa_all_outcomes_v1",
        "total": len(rows),
        "counts": dict(Counter(r["status"] for r in rows)),
        "model_accepted": sum(r["model_accepted"] for r in rows),
        "human_approved": 0,
        "records_sha256": digest(rows),
        "campaign_sha256": digest(manifest),
        "label": label,
        "complete": all(r["status"] != "pending" for r in rows),
    }
    destination = root / "exports" / (label + "-" + digest(summary)[:12])
    for name, subset in (
        ("all_results", rows),
        ("model_accepted", [r for r in rows if r["model_accepted"]]),
    ):
        payload = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in subset
        ).encode()
        write_bytes_once(destination / (name + ".jsonl"), payload)
    write_once(destination / "manifest.json", summary)
    write_bytes_once(
        destination / "dag_viewer.html", render_html(rows, summary).encode()
    )
    return str(destination), summary
