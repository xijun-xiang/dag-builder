#!/usr/bin/env python3
"""Render an offline, searchable HTML reviewer for a GSM8K DAG JSONL cohort."""

import argparse
import json
from collections import Counter
from pathlib import Path

from dag_builder.storage import read_json, write_bytes_once


SOURCE_LABELS = {
    "independent_generation": "独立解题",
    "answer_conditioned_generation": "给定答案生成推理",
    "official_rationale": "官方 CoT",
    "canonical_official_initial": "规范化官方 CoT（首轮）",
    "canonical_official_recovery": "规范化官方 CoT（恢复批次）",
    "diagnostic_repair_initial": "诊断重写（首轮）",
    "diagnostic_repair_isolated": "诊断重写（隔离批次）",
    "independent_generation_full_run": "全量独立解题补入",
}

ROLE_LABELS = {
    "fixed_sample": "固定样本",
    "deterministic_replacement": "确定性补入",
}


def read_jsonl(path):
    rows = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("dag"), dict):
            raise ValueError(f"invalid cohort JSONL row {line_number}")
        rows.append(value)
    if not rows or len({row["item_id"] for row in rows}) != len(rows):
        raise ValueError("cohort must contain non-duplicate records")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = read_jsonl(args.cohort)
    manifest = read_json(args.manifest)
    source_counts = Counter(row["source_name"] for row in rows)
    role_counts = Counter(row["cohort_role"] for row in rows)
    payload = json.dumps(
        {
            "rows": rows,
            "manifest": manifest,
            "sourceLabels": SOURCE_LABELS,
            "roleLabels": ROLE_LABELS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    page = f"""<!doctype html>
<html lang="zh">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>GSM8K DAG 100 条查看器</title>
<style>
:root{{--ink:#18212a;--muted:#5f6b76;--line:#d6dce0;--paper:#fff;--wash:#f4f7f8;--teal:#0b6b68;--orange:#b75817;--blue:#1f5f99;--green:#267542;--red:#a73e35}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--wash);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{background:#18383a;color:#fff;padding:22px max(20px,calc((100vw - 1500px)/2));border-bottom:4px solid #d5a13b}} h1{{font-size:24px;margin:0 0 5px;letter-spacing:0}} header p{{margin:0;color:#e3eeee}}
.layout{{max-width:1500px;margin:20px auto;padding:0 18px;display:grid;grid-template-columns:300px minmax(0,1fr);gap:18px}} .sidebar,.viewer{{background:var(--paper);border:1px solid var(--line);border-radius:6px}}
.sidebar{{padding:16px;height:calc(100vh - 150px);position:sticky;top:14px;overflow:auto}} label{{display:block;color:var(--muted);font-size:13px;margin:13px 0 5px}} input,select{{width:100%;font:inherit;padding:8px;border:1px solid #aeb8bf;border-radius:4px;background:#fff;color:var(--ink)}}
.counts{{margin:16px 0;padding:10px;background:#eef5f4;border-left:3px solid var(--teal);font-size:13px}} .list{{margin:10px -8px 0;padding:0;list-style:none}} .list button{{width:100%;border:0;border-left:3px solid transparent;background:transparent;text-align:left;padding:9px 10px;cursor:pointer;color:var(--ink);font:inherit}} .list button:hover{{background:#f1f5f5}} .list button[aria-current="true"]{{border-left-color:var(--orange);background:#fff3db;font-weight:600}}
.viewer{{min-width:0;padding:22px}} .topline{{display:flex;justify-content:space-between;align-items:start;gap:15px;border-bottom:1px solid var(--line);padding-bottom:14px}} h2{{font-size:19px;margin:0 0 4px}} .controls{{display:flex;gap:8px;white-space:nowrap}} .controls button{{border:1px solid #9aa7ad;background:#fff;border-radius:4px;padding:6px 10px;cursor:pointer;color:var(--ink)}} .controls button:hover{{background:#edf3f3}}
.badges{{display:flex;flex-wrap:wrap;gap:7px;margin:11px 0}} .badge{{font-size:12px;border-radius:12px;padding:2px 8px;border:1px solid currentColor}} .role{{color:var(--blue);background:#eef6ff}} .source{{color:var(--teal);background:#eaf8f6}} .pending{{color:var(--orange);background:#fff6e9}}
.meta{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:13px 0}} .meta div{{background:#f5f7f8;border:1px solid #e2e6e8;padding:8px;overflow-wrap:anywhere}} .meta b{{display:block;color:var(--muted);font-size:12px;font-weight:600}}
section{{margin-top:22px}} h3{{font-size:15px;margin:0 0 8px;color:#24343b}} pre{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:13px;background:#f7f8f9;border:1px solid #e0e4e6;border-radius:4px;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}}
.node{{border:1px solid var(--line);border-left:4px solid #81919b;border-radius:4px;padding:11px 12px;margin:10px 0}} .node.given{{border-left-color:var(--blue)}} .node.knowledge{{border-left-color:var(--orange)}} .node.derived{{border-left-color:var(--teal)}} .node.answer{{border-left-color:var(--green)}} .node-head{{display:flex;justify-content:space-between;gap:10px;font-size:12px;color:var(--muted);margin-bottom:7px}} .statement{{font-weight:600;margin-bottom:9px}} .quote{{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#35444c;background:#f2f5f6;padding:7px;border-radius:3px}} details{{margin-top:8px;color:#3b4c55}} summary{{cursor:pointer;font-size:13px}} .empty{{padding:40px;color:var(--muted);text-align:center}}
.legend{{font-size:12px;color:var(--muted);padding-top:14px;border-top:1px solid var(--line)}} @media(max-width:900px){{.layout{{grid-template-columns:1fr}}.sidebar{{position:static;height:auto}}.meta{{grid-template-columns:1fr}}.topline{{flex-direction:column}}}}
</style>
<header><h1>GSM8K DAG 100 条查看器</h1><p>CoT、DAG 节点、依赖关系、来源与补入关系均来自冻结的 JSONL。所有记录仍待人工审核。</p></header>
<main class="layout"><aside class="sidebar">
<label for="search">检索题目 / CoT / ID</label><input id="search" type="search" placeholder="输入关键词">
<label for="sourceFilter">来源</label><select id="sourceFilter"><option value="">全部来源</option></select>
<label for="roleFilter">角色</label><select id="roleFilter"><option value="">全部角色</option></select>
<div class="counts" id="counts"></div><ul class="list" id="list"></ul>
<p class="legend"><b>角色：</b>固定样本来自原始 100 条；确定性补入是原样本缺失 DAG 时，从全量已通过运行中按固定哈希顺序加入的非重叠样本。<br><br><b>来源：</b>表示 CoT/DAG 的构造路径，并不等于人工认可的质量等级。</p>
</aside><article class="viewer" id="viewer"></article></main>
<script id="cohort-data" type="application/json">{payload}</script>
<script>
(() => {{
  const data = JSON.parse(document.getElementById('cohort-data').textContent);
  const search = document.getElementById('search'), sourceFilter = document.getElementById('sourceFilter'), roleFilter = document.getElementById('roleFilter');
  const list = document.getElementById('list'), viewer = document.getElementById('viewer'), counts = document.getElementById('counts');
  let visible = data.rows, selectedId = data.rows[0].item_id;
  const label = (map, key) => map[key] || key;
  for (const key of [...new Set(data.rows.map(r => r.source_name))].sort()) {{ const o=document.createElement('option');o.value=key;o.textContent=label(data.sourceLabels,key);sourceFilter.append(o); }}
  for (const key of [...new Set(data.rows.map(r => r.cohort_role))].sort()) {{ const o=document.createElement('option');o.value=key;o.textContent=label(data.roleLabels,key);roleFilter.append(o); }}
  function node(tag, text, className) {{ const el=document.createElement(tag); if (className) el.className=className; el.textContent=text; return el; }}
  function addText(parent, tag, text, className) {{ parent.append(node(tag,text,className)); }}
  function badge(text, kind) {{ return node('span', text, 'badge '+kind); }}
  function renderRecord(record) {{
    viewer.replaceChildren(); if (!record) {{ viewer.append(node('div','没有匹配的样本','empty')); return; }}
    const dag=record.dag, source=dag.source, solution=dag.reference_solution || {{}};
    const head=document.createElement('div');head.className='topline'; const titles=document.createElement('div');addText(titles,'h2',record.item_id);addText(titles,'div','源行 '+(source.row ?? '未知')+' · 标准答案 '+(source.gold_answer ?? solution.answer ?? '未知'));head.append(titles);
    const controls=document.createElement('div');controls.className='controls';for(const [text,delta] of [['上一条',-1],['下一条',1]]) {{ const b=node('button',text);b.onclick=()=>{{const at=visible.findIndex(r=>r.item_id===selectedId);const next=visible[(at+delta+visible.length)%visible.length];selectedId=next.item_id;render();}};controls.append(b); }}head.append(controls);viewer.append(head);
    const badges=document.createElement('div');badges.className='badges';badges.append(badge(label(data.roleLabels,record.cohort_role),'role'),badge(label(data.sourceLabels,record.source_name),'source'),badge('待人工审核','pending'));viewer.append(badges);
    const meta=document.createElement('div');meta.className='meta';for(const [k,v] of [['构造方式',dag.solution_source || record.source_name],['模型答案',solution.answer || '未知'],['DAG 哈希',record.source_dag_sha256 || '未知'],['替代关系',record.replaces_unresolved_fixed_item_id || '无']]) {{ const box=document.createElement('div');addText(box,'b',k);addText(box,'span',String(v));meta.append(box); }}viewer.append(meta);
    const q=document.createElement('section');addText(q,'h3','题目');q.append(node('pre',source.question || ''));viewer.append(q);
    const cot=document.createElement('section');addText(cot,'h3','CoT / Reference Rationale');cot.append(node('pre',solution.rationale || ''));viewer.append(cot);
    const graph=document.createElement('section');addText(graph,'h3','DAG 节点（parents 为直接前置节点）');for (const n of dag.nodes || []) {{ const card=document.createElement('div');card.className='node '+n.kind;const nh=document.createElement('div');nh.className='node-head';addText(nh,'span','#'+n.node_id+' · '+n.kind);addText(nh,'span','parents: '+(n.parents?.length ? n.parents.join(', ') : 'root'));card.append(nh);addText(card,'div',n.statement || '','statement');addText(card,'div','引用：'+(n.source_field || '未知'),'node-head');card.append(node('div',n.source_quote || '','quote'));const d=document.createElement('details');const s=node('summary','推导说明');d.append(s,node('div',n.justification || ''));card.append(d);graph.append(card); }}viewer.append(graph);
    const review=document.createElement('section');addText(review,'h3','模型审核摘要');review.append(node('pre',JSON.stringify({{solution_review:dag.solution_review,dag_review:dag.dag_review,calculation_check:dag.calculation_check,limitation:dag.limitation}},null,2)));viewer.append(review);
  }}
  function render() {{
    const term=search.value.trim().toLowerCase(), source=sourceFilter.value, role=roleFilter.value;
    visible=data.rows.filter(r=>{{const text=[r.item_id,r.source_name,r.cohort_role,r.dag.source.question,r.dag.reference_solution?.rationale,...(r.dag.nodes||[]).flatMap(n=>[n.statement,n.source_quote,n.justification])].filter(Boolean).join('\\n').toLowerCase();return (!term||text.includes(term))&&(!source||r.source_name===source)&&(!role||r.cohort_role===role);}});
    if(!visible.some(r=>r.item_id===selectedId)) selectedId=visible[0]?.item_id; counts.textContent=`显示 ${{visible.length}} / ${{data.rows.length}} 条；固定样本 ${{data.rows.filter(r=>r.cohort_role==='fixed_sample').length}}，确定性补入 ${{data.rows.filter(r=>r.cohort_role==='deterministic_replacement').length}}。`;
    list.replaceChildren();for(const r of visible){{const li=document.createElement('li'),b=node('button',`${{r.item_id}} · ${{label(data.sourceLabels,r.source_name)}}`);b.setAttribute('aria-current',String(r.item_id===selectedId));b.onclick=()=>{{selectedId=r.item_id;render();}};li.append(b);list.append(li);}} renderRecord(visible.find(r=>r.item_id===selectedId));
  }}
  search.addEventListener('input',render);sourceFilter.addEventListener('change',render);roleFilter.addEventListener('change',render);render();
}})();
</script></html>"""
    write_bytes_once(args.output, page.encode("utf-8"))
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "records": len(rows),
                "source_counts": dict(source_counts),
                "role_counts": dict(role_counts),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
