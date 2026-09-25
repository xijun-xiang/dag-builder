"""One offline HTML viewer for every pals_dag_unified_v1 cohort."""

import json


TEMPLATE = '''<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PALS DAG 统一数据查看器</title>
<style>
body{font:15px/1.6 system-ui,-apple-system,sans-serif;background:#f5f7f9;color:#172b36;margin:0}
header{background:#173442;color:white;padding:24px max(20px,calc((100vw - 1200px)/2))}
h1{margin:0 0 6px;font-size:24px}h2{font-size:20px;margin:0 0 12px}h3{font-size:16px;margin:16px 0 6px}
.layout{max-width:1200px;margin:20px auto;padding:0 20px;display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px}
.panel{background:white;border:1px solid #dce4e8;border-radius:8px;padding:18px;min-width:0}
input{box-sizing:border-box;width:100%;padding:10px;border:1px solid #9fb1bc;border-radius:5px;font:inherit}
button{width:100%;border:0;background:none;text-align:left;padding:10px;border-bottom:1px solid #e5ebee;cursor:pointer;color:inherit}
button:hover,button.active{background:#e9f2f3}.muted{color:#52707c}.small{font-size:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6f7;border:1px solid #e1e8eb;padding:12px;border-radius:5px}
.node{border-left:4px solid #487c8c;padding:10px 14px;margin:10px 0;background:#f5f8f9}
.answer{border-left-color:#8b6288}.badge{display:inline-block;background:#e5edef;padding:1px 6px;border-radius:4px;font-size:12px}
details{margin:8px 0}summary{cursor:pointer}#list{max-height:70vh;overflow:auto}
@media(max-width:800px){.layout{display:block}.panel{margin-bottom:16px}#list{max-height:250px}}
</style></head><body>
<header><h1>PALS DAG 统一数据查看器</h1><div id="summary"></div></header>
<main class="layout"><aside class="panel"><label for="search">搜索题号、题面或步骤</label>
<input id="search" type="search"><div id="count" class="muted small"></div><div id="list"></div></aside>
<article class="panel" id="detail"></article></main>
<script id="cohort-data" type="application/json">__PAYLOAD__</script>
<script>
(() => {
  'use strict';
  const payload=JSON.parse(document.getElementById('cohort-data').textContent);
  const rows=payload.rows;
  const list=document.getElementById('list'),detail=document.getElementById('detail');
  const search=document.getElementById('search'),count=document.getElementById('count');
  const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node};
  document.getElementById('summary').textContent=`${payload.benchmark} · ${rows.length} 题 · ${payload.schema_version}`;
  const labels={given:'已知',knowledge:'知识',derived:'推导',answer:'答案'};
  const index=rows.map(row=>[row.item_id,row.provenance.source_id,row.problem.question,
    ...row.dag.nodes.map(node=>node.statement)].join('\\n').toLowerCase());
  let selected=0;
  function show(row){
    selected=rows.indexOf(row);detail.replaceChildren();
    detail.append(el('h2',`${row.provenance.source_id} · ${row.benchmark}`),
      el('div',`item_id: ${row.item_id} · 来源审核: ${row.review.source_status}`,'muted small'),
      el('h3','题目'),el('pre',row.problem.question));
    if(row.problem.choices)detail.append(el('pre',row.problem.choices.map((value,i)=>`${'ABCD'[i]}. ${value}`).join('\\n')));
    detail.append(el('h3',`推理 DAG · ${row.dag.nodes.length} 个节点`));
    for(const node of row.dag.nodes){
      const card=el('section',undefined,`node ${node.kind==='answer'?'answer':''}`);
      card.append(el('div',`#${node.node_id} · ${labels[node.kind]} · 父节点: ${node.parents.length?node.parents.map(id=>'#'+id).join(', '):'无'}`,'badge'),
        el('pre',node.statement));
      const audit=el('details');audit.append(el('summary','来源与依赖说明'),
        el('div',`来源字段: ${node.source_field}`,'small'),el('pre',node.source_quote),
        el('pre',node.justification));card.append(audit);detail.append(card);
    }
    const edges=row.dag.nodes.flatMap(node=>node.parents.map(parent=>`#${parent} → #${node.node_id}`));
    const extra=el('details');extra.append(el('summary',`全部 ${edges.length} 条边与来源哈希`),
      el('pre',edges.join('\\n')),el('pre',JSON.stringify(row.provenance,null,2)));
    detail.append(extra);
    for(const button of list.children)button.classList.toggle('active',button.dataset.itemId===row.item_id);
  }
  function draw(){
    const q=search.value.trim().toLowerCase();list.replaceChildren();
    rows.forEach((row,i)=>{if(q&&!index[i].includes(q))return;
      const button=el('button',`${row.provenance.source_id} · ${row.item_id}`);
      button.dataset.itemId=row.item_id;button.onclick=()=>show(row);list.append(button)});
    count.textContent=`${list.children.length} / ${rows.length}`;
    if(list.children.length)show(rows.find(row=>row.item_id===list.children[0].dataset.itemId));
    else detail.replaceChildren(el('p','没有匹配的题目。','muted'));
  }
  search.oninput=draw;draw();
})();
</script></body></html>'''


def render(rows, benchmark):
    payload = {"schema_version": "pals_dag_unified_view_v1", "benchmark": benchmark, "rows": rows}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    encoded = encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__PAYLOAD__", encoded).encode("utf-8")
