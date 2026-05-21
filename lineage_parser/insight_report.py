"""Offline task-insight workbench renderer."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from .insight_builder import build_task_insight
from .scope_serializer import to_dict, to_profile_dict
from .scope_types import ScopeLineageResult


def write_task_insight_report(
    result: ScopeLineageResult,
    output_dir: str | Path,
    *,
    business_doc: str | None = None,
    business_doc_index: dict[str, Any] | None = None,
    business_knowledge: dict[str, Any] | None = None,
) -> Path:
    """Write ``task_insight.json`` and ``task_insight.html`` for a result."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    insight = build_task_insight(
        lineage=to_dict(result),
        profile=to_profile_dict(result),
        business_doc=business_doc,
        business_doc_index=business_doc_index,
        business_knowledge=business_knowledge,
    )
    write_task_insight_files(insight, output_dir)
    return output_dir


def write_task_insight_files(insight: dict[str, Any], output_dir: str | Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "task_insight.json").write_text(
        json.dumps(insight, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (output_dir / "task_insight.html").write_text(render_task_insight_html(insight), encoding="utf-8")
    return output_dir


def write_task_insight_report_from_dir(
    input_dir: str | Path,
    out_path: str | Path | None = None,
    *,
    business_doc_path: str | Path | None = None,
    business_doc_index_path: str | Path | None = None,
    business_knowledge_path: str | Path | None = None,
) -> Path:
    input_dir = Path(input_dir)
    lineage = _load_json(input_dir / "lineage.json")
    profile = _load_json(input_dir / "profile.json")
    diagnostics_path = input_dir / "diagnostics.json"
    diagnostics = _load_json(diagnostics_path) if diagnostics_path.exists() else None
    business_doc = Path(business_doc_path).read_text(encoding="utf-8") if business_doc_path else None
    business_doc_index = _load_json(Path(business_doc_index_path)) if business_doc_index_path else None
    business_knowledge = _load_json(Path(business_knowledge_path)) if business_knowledge_path else None
    insight = build_task_insight(
        lineage=lineage,
        profile=profile,
        diagnostics=diagnostics,
        business_doc=business_doc,
        business_doc_index=business_doc_index,
        business_knowledge=business_knowledge,
    )
    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_task_insight_html(insight), encoding="utf-8")
        return out_path
    return write_task_insight_files(insight, input_dir)


def render_task_insight_html(insight: dict[str, Any]) -> str:
    raw_payload = json.dumps(insight, ensure_ascii=False, default=str)
    raw_payload = raw_payload.replace("http://", "http:\\/\\/").replace("https://", "https:\\/\\/")
    payload = html.escape(raw_payload, quote=False)
    title = html.escape((insight.get("task") or {}).get("task_name") or "SQL Task Insight")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - SQL Task Insight</title>
<style>
{_CSS}
</style>
</head>
<body>
<script id="task-insight-data" type="application/json">{payload}</script>
<div class="app">
  <header class="topbar">
    <div>
      <h1 id="taskTitle">SQL Task Insight</h1>
      <div id="taskSubtitle" class="muted"></div>
    </div>
    <div id="summaryChips" class="chips"></div>
  </header>
  <main class="layout">
    <section class="panel sections">
      <div class="panel-head">
        <h2>业务阶段</h2>
        <input id="sectionSearch" placeholder="搜索阶段/规则/字段">
      </div>
      <div id="sectionsList" class="list"></div>
    </section>
    <section class="panel graph">
      <div class="panel-head">
        <h2>Scope DAG</h2>
        <div class="toolbar">
          <input id="scopeSearch" placeholder="搜索 scope">
          <select id="graphMode" title="切换 Scope DAG 展示模式">
            <option value="business">业务视图</option>
            <option value="full">完整模式</option>
          </select>
          <button id="zoomScopeOut" type="button" title="缩小 Scope 图">-</button>
          <button id="zoomScopeIn" type="button" title="放大 Scope 图">+</button>
          <button id="resetScopeView" type="button" title="重置 Scope 图视图">重置</button>
          <button id="clearSelection" type="button">清除选择</button>
        </div>
      </div>
      <div id="graphNotice" class="graph-notice"></div>
      <svg id="scopeSvg" role="img" aria-label="Scope DAG"></svg>
    </section>
    <aside class="panel detail">
      <div class="panel-head"><h2>详情与证据链</h2></div>
      <div id="detailBox" class="detail-box muted">点击业务阶段、scope、规则或字段。</div>
    </aside>
    <section class="panel fields">
      <div class="panel-head">
        <h2>字段血缘</h2>
        <div class="toolbar">
          <input id="columnSearch" placeholder="搜索字段">
          <select id="traceFilter">
            <option value="ALL">全部</option>
            <option value="COMPLETE">完整追溯</option>
            <option value="INCOMPLETE">追溯不完整</option>
          </select>
          <button id="zoomFieldOut" type="button" title="缩小字段血缘图">-</button>
          <button id="zoomFieldIn" type="button" title="放大字段血缘图">+</button>
          <button id="resetFieldView" type="button" title="重置字段血缘图视图">重置</button>
        </div>
      </div>
      <div class="field-grid">
        <div class="table-wrap">
          <table>
            <thead><tr><th>字段</th><th>语义</th><th>转换</th><th>追溯</th></tr></thead>
            <tbody id="columnsBody"></tbody>
          </table>
        </div>
        <svg id="fieldSvg" role="img" aria-label="Focused field lineage"></svg>
      </div>
    </section>
  </main>
</div>
<script>
{_JS}
</script>
</body>
</html>
"""


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


_CSS = r"""
:root {
  --bg: #f5f7fa;
  --panel: #fff;
  --text: #172033;
  --muted: #667085;
  --line: #d7dde8;
  --blue: #0969da;
  --green: #1f7a4d;
  --yellow: #9a6700;
  --red: #c9353f;
  --chip: #eef4ff;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; background: var(--bg); color: var(--text); }
.app { min-width: 1180px; }
.topbar { display: flex; justify-content: space-between; gap: 20px; padding: 16px 20px; background: #111827; color: #fff; }
h1, h2, h3, p { margin: 0; }
h1 { font-size: 20px; }
h2 { font-size: 15px; }
h3 { font-size: 14px; margin: 12px 0 6px; }
.muted { color: var(--muted); font-size: 12px; line-height: 1.45; }
.topbar .muted { color: #cbd5e1; margin-top: 4px; }
.chips { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 8px; max-width: 680px; }
.chip { display: inline-flex; align-items: center; padding: 3px 9px; min-height: 24px; border-radius: 999px; background: var(--chip); color: #184b8f; font-size: 12px; font-weight: 650; }
.chip.red { color: var(--red); background: #ffebe9; }
.chip.yellow { color: var(--yellow); background: #fff4ce; }
.chip.green { color: var(--green); background: #e7f6ec; }
.layout { display: grid; grid-template-columns: 360px minmax(560px, 1fr) 380px; grid-template-rows: 480px minmax(420px, auto); gap: 14px; padding: 14px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; box-shadow: 0 1px 2px rgba(16,24,40,.04); min-width: 0; }
.sections { grid-column: 1; grid-row: 1 / 3; }
.graph { grid-column: 2; grid-row: 1; }
.detail { grid-column: 3; grid-row: 1 / 3; }
.fields { grid-column: 2; grid-row: 2; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 12px; border-bottom: 1px solid var(--line); background: #fbfcfe; }
.toolbar { display: flex; align-items: center; gap: 8px; }
input, select, button { height: 30px; border: 1px solid #cfd6e0; border-radius: 6px; background: #fff; color: var(--text); font-size: 13px; padding: 0 8px; }
button { cursor: pointer; font-weight: 650; }
button:active { transform: translateY(1px); }
.list { height: calc(100% - 53px); overflow: auto; padding: 10px; }
.section-card, .rule-card { border: 1px solid var(--line); border-radius: 7px; padding: 10px; margin-bottom: 9px; cursor: pointer; background: #fff; }
.section-card:hover, .rule-card:hover, tr:hover { border-color: #9cc7ff; background: #f7fbff; }
.section-card.selected, .rule-card.selected { border-color: var(--blue); background: #eef6ff; }
.tag-row { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.tag { font-size: 11px; padding: 2px 6px; border-radius: 999px; color: #344054; background: #f2f4f7; }
#scopeSvg { width: 100%; height: calc(100% - 53px); display: block; background: #fff; cursor: grab; touch-action: none; }
#fieldSvg { width: 100%; min-height: 360px; display: block; background: #fff; border-left: 1px solid var(--line); cursor: grab; touch-action: none; }
#scopeSvg.dragging, #fieldSvg.dragging { cursor: grabbing; }
.node rect { fill: #fff; stroke: #9aa8bb; stroke-width: 1.2; rx: 7; }
.node.scope-root rect { fill: #111827; stroke: #111827; }
.node.scope-root text { fill: #fff; }
.node.table rect { fill: #e7f6ec; stroke: #8fd0a5; }
.node.union rect { fill: #eef6ff; stroke: #8bbdf5; }
.node.selected rect, .node.highlight rect { stroke: var(--blue); stroke-width: 2.5; }
.edge { stroke: #9aa8bb; stroke-width: 1.2; fill: none; marker-end: url(#arrow); }
.edge.highlight { stroke: var(--blue); stroke-width: 2.4; }
.node text { font-size: 12px; fill: var(--text); pointer-events: none; }
.graph-notice { padding: 7px 12px; border-bottom: 1px solid var(--line); background: #fffdf5; color: #7a4d00; font-size: 12px; line-height: 1.35; }
.detail-box { padding: 12px; overflow: auto; height: calc(100% - 45px); }
.detail-box dl { display: grid; grid-template-columns: 110px 1fr; gap: 6px 10px; margin: 8px 0; }
.detail-box dt { color: var(--muted); }
.detail-box dd { margin: 0; word-break: break-word; }
.evidence { margin-top: 12px; padding: 8px; background: #f8fafc; border: 1px solid var(--line); border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; white-space: pre-wrap; }
.field-grid { display: grid; grid-template-columns: 48% 52%; min-height: 420px; }
.table-wrap { max-height: 520px; overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 7px 8px; border-bottom: 1px solid #edf1f6; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #f8fafc; z-index: 1; color: #475467; }
tr { cursor: pointer; }
tr.selected { background: #eef6ff; }
code { background: #f2f4f7; border-radius: 4px; padding: 1px 4px; }
"""


_JS = r"""
const insight = JSON.parse(document.getElementById("task-insight-data").textContent);
const objects = insight.objects || {};
const links = insight.links || [];
const state = {
  selectedId: null,
  selectedType: null,
  selectedColumn: null,
  views: {
    scope: { x: 0, y: 0, k: 1 },
    field: { x: 0, y: 0, k: 1 },
  },
  dragging: null,
  nodeDragging: null,
  nodeOffsets: {},
  suppressClick: false,
  graphMode: "business",
};

const byId = {};
for (const group of Object.values(objects)) {
  for (const [id, item] of Object.entries(group || {})) byId[id] = item;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function linked(id, type) {
  return links.filter(l => l.from === id && (!type || l.type === type)).map(l => l.to)
    .concat(links.filter(l => l.to === id && (!type || l.type === type)).map(l => l.from));
}

function renderSummary() {
  const task = insight.task || {};
  document.getElementById("taskTitle").textContent = task.task_name || "SQL Task Insight";
  document.getElementById("taskSubtitle").textContent = `${task.target_table || ""}${task.target_table_label ? " · " + task.target_table_label : ""}`;
  const chips = [
    ["输入表", task.input_table_count],
    ["输出字段", task.output_column_count],
    ["完整scope", task.lineage_scope_count || task.scope_count],
    ["展示scope", task.visible_scope_count],
    ["隐藏scope", task.hidden_scope_count],
    ["DAG节点", task.dag_node_count],
    ["完整追溯", task.trace_complete_count],
    ["不完整", task.trace_incomplete_count],
    ["warning", task.warning_count],
    ["风险", task.risk_level],
  ];
  document.getElementById("summaryChips").innerHTML = chips.filter(([,v]) => v !== undefined && v !== null)
    .map(([k,v]) => `<span class="chip ${String(v).toLowerCase()}">${esc(k)} ${esc(v)}</span>`).join("");
}

function renderSections() {
  const q = (document.getElementById("sectionSearch").value || "").toLowerCase();
  const sections = Object.values(objects.sections || {});
  const rules = Object.values(objects.rules || {});
  const sectionHtml = sections.filter(s => JSON.stringify(s).toLowerCase().includes(q)).map(s => `
    <div class="section-card ${state.selectedId === s.id ? "selected" : ""}" data-id="${esc(s.id)}" data-type="section">
      <strong>${esc(s.title)}</strong>
      <p class="muted">${esc(s.body || "")}</p>
      <div class="tag-row">${(s.scope_ids || []).map(x => `<span class="tag">${esc(x)}</span>`).join("")}</div>
    </div>`).join("");
  const ruleHtml = rules.filter(r => JSON.stringify(r).toLowerCase().includes(q)).map(r => `
    <div class="rule-card ${state.selectedId === r.id ? "selected" : ""}" data-id="${esc(r.id)}" data-type="rule">
      <strong>${esc(r.title)}</strong>
      <p class="muted">${esc(r.condition_summary || r.condition_expression || "")}</p>
      <div class="tag-row">${(r.scope_ids || []).map(x => `<span class="tag">${esc(x)}</span>`).join("")}</div>
    </div>`).join("");
  document.getElementById("sectionsList").innerHTML = sectionHtml + (rules.length ? `<h3>业务规则</h3>${ruleHtml}` : "");
  document.querySelectorAll("[data-id]").forEach(el => el.addEventListener("click", () => selectObject(el.dataset.id, el.dataset.type)));
}

function graphNodes() {
  const scopes = Object.values(objects.scopes || {}).filter(s => state.graphMode === "full" || !s.hidden_in_business_view);
  const tables = Object.values(objects.tables || {}).filter(t => t.role === "input");
  return scopes.concat(tables);
}

function renderGraphNotice() {
  const task = insight.task || {};
  const diagnostics = insight.graph_diagnostics || {};
  const hidden = task.hidden_scope_count || 0;
  const dangling = (diagnostics.dangling_scope_ids || []).length;
  const text = state.graphMode === "business"
    ? `业务视图：隐藏 ${hidden} 个 lineage-only/无下游 scope，用于突出主要加工链路。完整血缘仍在 lineage.json，可切换完整模式审计。疑似孤立 scope ${dangling} 个。`
    : `完整模式：展示工作台模型中的全部 ${task.full_graph_scope_count || task.visible_scope_count || 0} 个 scope。无下游/孤立 scope 通常较少，若存在应检查 SQL 是否未使用该 CTE，或解析器是否漏连。`;
  document.getElementById("graphNotice").textContent = text;
}

function layoutDag(nodes) {
  const ids = new Set(nodes.map(n => n.id));
  const sourceOrder = Object.fromEntries(nodes.map((node, index) => [node.id, index]));
  const rank = Object.fromEntries(nodes.map(n => [n.id, 0]));
  const feedEdges = links.filter(l => l.type === "feeds" && ids.has(l.from) && ids.has(l.to));
  for (let i = 0; i < nodes.length + 1; i += 1) {
    let changed = false;
    for (const edge of feedEdges) {
      const nextRank = (rank[edge.from] || 0) + 1;
      if (nextRank > (rank[edge.to] || 0)) {
        rank[edge.to] = nextRank;
        changed = true;
      }
    }
    if (!changed) break;
  }
  const levels = new Map();
  for (const node of nodes) {
    const level = rank[node.id] || 0;
    if (!levels.has(level)) levels.set(level, []);
    levels.get(level).push(node);
  }
  const sortedLevels = [...levels.keys()].sort((a, b) => a - b);
  const levelIndex = Object.fromEntries(sortedLevels.map((level, index) => [level, index]));
  const cellW = 190;
  const cellH = 104;
  let orderById = new Map();
  for (const level of sortedLevels) {
    levels.get(level).sort((a, b) => compareGraphNodes(a, b, sourceOrder)).forEach((node, index) => orderById.set(node.id, index));
  }
  const connected = new Map(nodes.map(n => [n.id, []]));
  for (const edge of feedEdges) {
    connected.get(edge.to)?.push(edge.from);
    connected.get(edge.from)?.push(edge.to);
  }
  for (let pass = 0; pass < 3; pass += 1) {
    for (const level of sortedLevels.slice(1)) {
      levels.get(level).sort((a, b) => weightedOrder(a.id, connected, orderById, sourceOrder) - weightedOrder(b.id, connected, orderById, sourceOrder) || compareGraphNodes(a, b, sourceOrder));
      levels.get(level).forEach((node, index) => orderById.set(node.id, index));
    }
    for (const level of sortedLevels.slice(0, -1).reverse()) {
      levels.get(level).sort((a, b) => weightedOrder(a.id, connected, orderById, sourceOrder) - weightedOrder(b.id, connected, orderById, sourceOrder) || compareGraphNodes(a, b, sourceOrder));
      levels.get(level).forEach((node, index) => orderById.set(node.id, index));
    }
  }
  const positions = {};
  let maxRows = 1;
  for (const level of sortedLevels) {
    const levelNodes = levels.get(level);
    maxRows = Math.max(maxRows, levelNodes.length);
    levelNodes.forEach((node, row) => {
      const offset = state.nodeOffsets[node.id] || { x: 0, y: 0 };
      positions[node.id] = { x: 24 + levelIndex[level] * cellW + offset.x, y: 32 + row * cellH + offset.y };
    });
  }
  return {
    positions,
    cellW,
    width: Math.max(700, 70 + sortedLevels.length * cellW),
    height: Math.max(360, 80 + maxRows * cellH),
  };
}

function compareGraphNodes(a, b, sourceOrder = {}) {
  const aTable = a.type === "table" ? 0 : 1;
  const bTable = b.type === "table" ? 0 : 1;
  return aTable - bTable || (sourceOrder[a.id] ?? 0) - (sourceOrder[b.id] ?? 0) || String(a.name || a.label || a.id).localeCompare(String(b.name || b.label || b.id));
}

function neighborAverage(id, graph, orderById) {
  const neighbors = graph.get(id) || [];
  const rows = neighbors.map(n => orderById.get(n)).filter(v => v !== undefined);
  if (!rows.length) return orderById.get(id) ?? 0;
  return rows.reduce((sum, value) => sum + value, 0) / rows.length;
}

function weightedOrder(id, graph, orderById, sourceOrder) {
  const neighbor = neighborAverage(id, graph, orderById);
  const original = sourceOrder[id] ?? orderById.get(id) ?? 0;
  return neighbor * 0.45 + original * 0.55;
}

function viewportTransform(name) {
  const v = state.views[name];
  return `translate(${v.x} ${v.y}) scale(${v.k})`;
}

function applyViewport(name) {
  const group = document.getElementById(`${name}Viewport`);
  if (group) group.setAttribute("transform", viewportTransform(name));
}

function clampZoom(value) {
  return Math.max(0.25, Math.min(4, value));
}

function svgPoint(svg, event) {
  const rect = svg.getBoundingClientRect();
  const box = svg.viewBox.baseVal;
  return {
    x: box.x + ((event.clientX - rect.left) / Math.max(rect.width, 1)) * box.width,
    y: box.y + ((event.clientY - rect.top) / Math.max(rect.height, 1)) * box.height,
  };
}

function zoomGraph(name, factor, centerEvent) {
  const svg = document.getElementById(name === "scope" ? "scopeSvg" : "fieldSvg");
  const view = state.views[name];
  const oldK = view.k;
  const newK = clampZoom(oldK * factor);
  const box = svg.viewBox.baseVal;
  const point = centerEvent ? svgPoint(svg, centerEvent) : { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  view.x = point.x - ((point.x - view.x) / oldK) * newK;
  view.y = point.y - ((point.y - view.y) / oldK) * newK;
  view.k = newK;
  applyViewport(name);
}

function resetGraphView(name) {
  if (name === "scope") {
    fitGraphView(name);
    return;
  }
  state.views[name] = { x: 0, y: 0, k: 1 };
  applyViewport(name);
}

function fitGraphView(name) {
  const svg = document.getElementById(name === "scope" ? "scopeSvg" : "fieldSvg");
  const box = svg.viewBox.baseVal;
  const rect = svg.getBoundingClientRect();
  const sx = rect.width / Math.max(box.width, 1);
  const sy = rect.height / Math.max(box.height, 1);
  const k = clampZoom(Math.min(sx, sy) * 0.94);
  const viewportW = Math.max(rect.width, 1) / k;
  const viewportH = Math.max(rect.height, 1) / k;
  state.views[name] = {
    x: Math.max(0, (viewportW - box.width) / 2),
    y: Math.max(0, (viewportH - box.height) / 2),
    k,
  };
  applyViewport(name);
}

function setupGraphPanZoom(svgId, name) {
  const svg = document.getElementById(svgId);
  svg.addEventListener("wheel", event => {
    event.preventDefault();
    zoomGraph(name, event.deltaY < 0 ? 1.15 : 1 / 1.15, event);
  }, { passive: false });
  svg.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    const node = event.target.closest?.("[data-id]");
    if (node && name === "scope") {
      svg.setPointerCapture(event.pointerId);
      const point = svgPoint(svg, event);
      state.nodeDragging = {
        id: node.dataset.id,
        type: node.dataset.type,
        pointerId: event.pointerId,
        x: point.x,
        y: point.y,
        moved: false,
      };
      return;
    }
    if (node) return;
    svg.setPointerCapture(event.pointerId);
    svg.classList.add("dragging");
    state.dragging = { name, pointerId: event.pointerId, x: event.clientX, y: event.clientY };
  });
  svg.addEventListener("pointermove", event => {
    const nodeDrag = state.nodeDragging;
    if (nodeDrag && nodeDrag.pointerId === event.pointerId && name === "scope") {
      const point = svgPoint(svg, event);
      const view = state.views.scope;
      const dx = (point.x - nodeDrag.x) / Math.max(view.k, 0.01);
      const dy = (point.y - nodeDrag.y) / Math.max(view.k, 0.01);
      if (Math.abs(dx) + Math.abs(dy) > 0.5) {
        const offset = state.nodeOffsets[nodeDrag.id] || { x: 0, y: 0 };
        state.nodeOffsets[nodeDrag.id] = { x: offset.x + dx, y: offset.y + dy };
        nodeDrag.x = point.x;
        nodeDrag.y = point.y;
        nodeDrag.moved = true;
        renderScopeGraph();
      }
      return;
    }
    const drag = state.dragging;
    if (!drag || drag.name !== name || drag.pointerId !== event.pointerId) return;
    const rect = svg.getBoundingClientRect();
    const box = svg.viewBox.baseVal;
    state.views[name].x += ((event.clientX - drag.x) / Math.max(rect.width, 1)) * box.width;
    state.views[name].y += ((event.clientY - drag.y) / Math.max(rect.height, 1)) * box.height;
    drag.x = event.clientX;
    drag.y = event.clientY;
    applyViewport(name);
  });
  svg.addEventListener("pointerup", event => {
    const nodeDrag = state.nodeDragging;
    if (nodeDrag && nodeDrag.pointerId === event.pointerId) {
      state.suppressClick = nodeDrag.moved;
      if (!nodeDrag.moved) selectObject(nodeDrag.id, nodeDrag.type);
      state.nodeDragging = null;
      return;
    }
    if (state.dragging?.pointerId === event.pointerId) {
      state.dragging = null;
      svg.classList.remove("dragging");
    }
  });
  svg.addEventListener("pointercancel", () => {
    state.dragging = null;
    state.nodeDragging = null;
    svg.classList.remove("dragging");
  });
  if (name === "scope") {
    svg.addEventListener("click", event => {
      if (state.suppressClick) {
        state.suppressClick = false;
        return;
      }
      const target = event.target.closest?.("[data-id]");
      if (target) selectObject(target.dataset.id, target.dataset.type);
    });
  }
}

function renderScopeGraph() {
  renderGraphNotice();
  const svg = document.getElementById("scopeSvg");
  const nodes = graphNodes();
  const q = (document.getElementById("scopeSearch").value || "").toLowerCase();
  const visible = nodes.filter(n => !q || JSON.stringify(n).toLowerCase().includes(q));
  const { positions, cellW, width, height } = layoutDag(visible);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const highlight = scopeHighlightInfo();
  let body = "";
  for (const link of links.filter(l => l.type === "feeds")) {
    const a = positions[link.from], b = positions[link.to];
    if (!a || !b) continue;
    const h = highlight.edges.has(`${link.from}->${link.to}`) ? " highlight" : "";
    body += `<path class="edge${h}" d="M${a.x+145},${a.y+24} C${a.x+cellW/2},${a.y+24} ${b.x-cellW/2},${b.y+24} ${b.x},${b.y+24}"/>`;
  }
  for (const n of visible) {
    const p = positions[n.id];
    const cls = ["node", n.type === "table" ? "table" : "", n.kind === "root" || n.name === "ROOT" ? "scope-root" : "", n.kind === "union" ? "union" : "", state.selectedId === n.id ? "selected" : "", highlight.nodes.has(n.id) ? "highlight" : ""].join(" ");
    body += `<g class="${cls}" data-id="${esc(n.id)}" data-type="${esc(n.type || "scope")}" transform="translate(${p.x},${p.y})">
      <rect width="150" height="48"></rect>
      <text x="10" y="19">${esc((n.name || n.label || n.id).slice(0, 22))}</text>
      <text x="10" y="36">${esc(n.kind || n.role || n.type || "")}</text>
    </g>`;
  }
  svg.innerHTML = `<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#9aa8bb"></path></marker></defs><g id="scopeViewport" transform="${viewportTransform("scope")}">${body}</g>`;
  if (!state.views.scope.fitted) {
    fitGraphView("scope");
    state.views.scope.fitted = true;
  }
}

function renderColumns() {
  const q = (document.getElementById("columnSearch").value || "").toLowerCase();
  const filter = document.getElementById("traceFilter").value;
  const columns = Object.values(objects.columns || {}).filter(c => c.type === "output_column");
  const rows = columns.filter(c => {
    if (q && !JSON.stringify(c).toLowerCase().includes(q)) return false;
    if (filter === "COMPLETE" && !c.trace_complete) return false;
    if (filter === "INCOMPLETE" && c.trace_complete) return false;
    return true;
  }).map(c => `<tr data-id="${esc(c.id)}" data-type="column" class="${state.selectedId === c.id ? "selected" : ""}">
    <td><code>${esc(c.name)}</code></td><td>${esc(c.label || "")}</td><td>${esc(c.transform || "")}</td><td>${c.trace_complete ? "完整" : "不完整"}</td>
  </tr>`).join("");
  document.getElementById("columnsBody").innerHTML = rows;
  document.querySelectorAll("#columnsBody tr").forEach(row => row.addEventListener("click", () => {
    state.selectedColumn = row.dataset.id;
    selectObject(row.dataset.id, "column");
  }));
  renderFieldGraph();
}

function renderFieldGraph() {
  const svg = document.getElementById("fieldSvg");
  const col = byId[state.selectedColumn || state.selectedId];
  if (!col || col.type !== "output_column") {
    svg.setAttribute("viewBox", "0 0 480 260");
    svg.innerHTML = `<g id="fieldViewport" transform="${viewportTransform("field")}"><text x="18" y="32" fill="#667085">选择 ROOT 输出字段查看字段血缘</text></g>`;
    return;
  }
  const sources = (col.physical_sources || []).slice(0, 8);
  const width = Math.max(svg.clientWidth || 480, 480);
  const height = Math.max(260, 80 + sources.length * 52);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  let body = `<g class="node selected" transform="translate(${width-190},${Math.max(24, height/2-24)})"><rect width="165" height="48"></rect><text x="10" y="20">${esc(col.name)}</text><text x="10" y="37">${esc(col.label || col.transform || "")}</text></g>`;
  sources.forEach((s, i) => {
    const y = 24 + i * 52;
    body += `<g class="node table" transform="translate(18,${y})"><rect width="210" height="42"></rect><text x="9" y="18">${esc((s.table || "").slice(0, 30))}</text><text x="9" y="34">${esc(s.column || "")} · ${esc(s.transform || "")}</text></g>`;
    body += `<path class="edge" marker-end="url(#fieldArrow)" d="M228,${y+21} C${width/2},${y+21} ${width/2},${height/2} ${width-190},${height/2}"/>`;
  });
  svg.innerHTML = `<defs><marker id="fieldArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#9aa8bb"></path></marker></defs><g id="fieldViewport" transform="${viewportTransform("field")}">${body}</g>`;
}

function highlightSet() {
  const ids = new Set();
  if (!state.selectedId) return ids;
  ids.add(state.selectedId);
  for (const id of linked(state.selectedId)) ids.add(id);
  const item = byId[state.selectedId] || {};
  for (const id of item.scope_ids || []) ids.add(id);
  for (const id of item.rule_ids || []) ids.add(id);
  for (const id of item.column_ids || []) ids.add(id);
  return ids;
}

function scopeHighlightInfo() {
  const nodes = new Set();
  const edges = new Set();
  if (!state.selectedId) return { nodes, edges };
  const selected = byId[state.selectedId];
  if (!selected || !["scope", "table"].includes(selected.type)) return { nodes, edges };
  nodes.add(state.selectedId);
  const incident = links.filter(link => link.type === "feeds" && (link.from === state.selectedId || link.to === state.selectedId));
  if (incident.length <= 6) {
    for (const link of incident) {
      nodes.add(link.from);
      nodes.add(link.to);
      edges.add(`${link.from}->${link.to}`);
    }
  }
  return { nodes, edges };
}

function selectObject(id, type) {
  state.selectedId = id;
  state.selectedType = type;
  if (type === "column") state.selectedColumn = id;
  renderSections();
  renderScopeGraph();
  renderColumns();
  renderDetail(id);
}

function renderDetail(id) {
  const item = byId[id];
  const box = document.getElementById("detailBox");
  if (!item) {
    box.innerHTML = `<span class="muted">未找到对象：${esc(id)}</span>`;
    return;
  }
  const evidence = (item.evidence || []).map(e => `${e.source}: ${e.path || ""}`).join("\n");
  const linkedItems = linked(id).slice(0, 20).map(x => `<span class="tag">${esc(x)}</span>`).join("");
  box.innerHTML = `<h3>${esc(item.title || item.label || item.name || item.id)}</h3>
    <dl>
      <dt>ID</dt><dd><code>${esc(item.id)}</code></dd>
      <dt>类型</dt><dd>${esc(item.type || item.kind || "")}</dd>
      <dt>说明</dt><dd>${esc(item.body || item.summary || item.description || item.condition_summary || "")}</dd>
      <dt>条件</dt><dd>${esc(item.condition_expression || item.condition_summary || "")}</dd>
      <dt>转换</dt><dd>${esc(item.transform || "")}</dd>
      <dt>追溯</dt><dd>${item.trace_complete === undefined ? "" : (item.trace_complete ? "完整" : "不完整")}</dd>
      <dt>关联对象</dt><dd><div class="tag-row">${linkedItems}</div></dd>
    </dl>
    ${renderLogic(item)}
    ${evidence ? `<div class="evidence">${esc(evidence)}</div>` : ""}`;
}

function renderLogic(item) {
  const logic = item.logic;
  if (!logic) return "";
  const parts = [];
  for (const key of ["filters", "joins", "window_functions", "case_when", "aggregations"]) {
    const value = logic[key] || [];
    if (!value.length) continue;
    parts.push(`<h3>${esc(key)}</h3><pre class="evidence">${esc(JSON.stringify(value, null, 2))}</pre>`);
  }
  return parts.join("");
}

document.getElementById("sectionSearch").addEventListener("input", renderSections);
document.getElementById("scopeSearch").addEventListener("input", renderScopeGraph);
document.getElementById("graphMode").addEventListener("change", event => {
  state.graphMode = event.target.value;
  state.views.scope.fitted = false;
  renderScopeGraph();
});
document.getElementById("columnSearch").addEventListener("input", renderColumns);
document.getElementById("traceFilter").addEventListener("change", renderColumns);
document.getElementById("clearSelection").addEventListener("click", () => selectObject(null, null));
document.getElementById("zoomScopeIn").addEventListener("click", () => zoomGraph("scope", 1.2));
document.getElementById("zoomScopeOut").addEventListener("click", () => zoomGraph("scope", 1 / 1.2));
document.getElementById("resetScopeView").addEventListener("click", () => resetGraphView("scope"));
document.getElementById("zoomFieldIn").addEventListener("click", () => zoomGraph("field", 1.2));
document.getElementById("zoomFieldOut").addEventListener("click", () => zoomGraph("field", 1 / 1.2));
document.getElementById("resetFieldView").addEventListener("click", () => resetGraphView("field"));
setupGraphPanZoom("scopeSvg", "scope");
setupGraphPanZoom("fieldSvg", "field");

renderSummary();
renderSections();
renderScopeGraph();
renderColumns();
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render task_insight.html from an output directory")
    parser.add_argument("input", help="Directory containing lineage.json/profile.json")
    parser.add_argument("--out", help="Output HTML path. Defaults to <input>/task_insight.html")
    args = parser.parse_args(argv)
    path = write_task_insight_report_from_dir(args.input, args.out)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
