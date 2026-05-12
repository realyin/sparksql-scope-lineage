"""Offline HTML report renderer for scope-lineage output.

The generated report is a single self-contained HTML file. It does not load
CDN assets, fonts, scripts, images, or local sidecar files, so it works in
offline intranet environments and can be archived with the lineage output.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from .scope_serializer import to_dict
from .scope_types import ScopeLineageResult


def write_html_report(result: ScopeLineageResult, output_dir: str | Path) -> Path:
    """Write a self-contained ``report.html`` for a parsed statement."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data = to_dict(result)
    path = output_dir / "report.html"
    path.write_text(render_html(data), encoding="utf-8")
    return path


def write_html_report_from_dir(input_dir: str | Path, out_path: str | Path | None = None) -> Path:
    """Render ``report.html`` from an existing directory with lineage.json."""
    input_dir = Path(input_dir)
    lineage_path = input_dir / "lineage.json"
    diagnostics_path = input_dir / "diagnostics.json"
    if not lineage_path.exists():
        raise FileNotFoundError(f"lineage.json not found: {lineage_path}")

    data = json.loads(lineage_path.read_text(encoding="utf-8"))
    if diagnostics_path.exists():
        data["diagnostics"] = json.loads(diagnostics_path.read_text(encoding="utf-8"))

    path = Path(out_path) if out_path else input_dir / "report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(data), encoding="utf-8")
    return path


def render_html(data: dict[str, Any]) -> str:
    raw_payload = json.dumps(data, ensure_ascii=False, default=_json_default)
    raw_payload = raw_payload.replace("http://", "http:\\/\\/").replace("https://", "https:\\/\\/")
    payload = html.escape(raw_payload, quote=False)
    title = html.escape(data.get("task_id") or "scope-lineage report")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Scope Lineage</title>
<style>
{_CSS}
</style>
</head>
<body>
<script id="lineage-data" type="application/json">{payload}</script>
<div class="app">
  <header class="topbar">
    <div>
      <h1 id="taskTitle">Scope Lineage Report</h1>
      <div class="muted" id="targetTitle"></div>
    </div>
    <div class="summary" id="summary"></div>
  </header>
  <main class="grid">
    <section class="panel graph-panel">
      <div class="panel-head">
        <div>
          <h2>Scope DAG</h2>
          <p class="muted">Scope-level structure only. Field edges are shown after selecting a ROOT column.</p>
        </div>
        <div class="toolbar">
          <input id="scopeSearch" placeholder="Search scope">
          <button id="fitScope" type="button">Fit</button>
        </div>
      </div>
      <div class="canvas-wrap">
        <svg id="scopeSvg" role="img" aria-label="Scope DAG"></svg>
      </div>
    </section>

    <aside class="panel detail-panel">
      <h2>Details</h2>
      <div id="detailBox" class="detail-box muted">Click a scope or ROOT column.</div>
      <h2>Diagnostics</h2>
      <div id="diagnostics"></div>
    </aside>

    <section class="panel table-panel">
      <div class="panel-head">
        <div>
          <h2>ROOT Columns</h2>
          <p class="muted">Search, filter by risk, then click a column to focus its lineage.</p>
        </div>
        <div class="toolbar">
          <input id="columnSearch" placeholder="Search column">
          <select id="riskFilter">
            <option value="ALL">ALL</option>
            <option value="RED">RED</option>
            <option value="YELLOW">YELLOW</option>
            <option value="GREEN">GREEN</option>
          </select>
        </div>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Risk</th>
              <th>Column</th>
              <th>Transform</th>
              <th>Direct Sources</th>
              <th>Physical / Unknown Sources</th>
            </tr>
          </thead>
          <tbody id="columnsBody"></tbody>
        </table>
      </div>
    </section>

    <section class="panel field-panel">
      <div class="panel-head">
        <div>
          <h2>Focused Column Lineage</h2>
          <p class="muted" id="fieldHint">Select a ROOT column to draw its upstream path.</p>
        </div>
        <div class="toolbar">
          <button id="fitField" type="button">Fit</button>
        </div>
      </div>
      <div class="canvas-wrap field-wrap">
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


def _json_default(obj: Any) -> Any:
    converted = to_dict(obj)
    if converted is obj:
        return str(obj)
    return converted


_CSS = r"""
:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #18202a;
  --muted: #667085;
  --line: #d7dde6;
  --green: #238636;
  --green-bg: #e8f5ec;
  --yellow: #9a6700;
  --yellow-bg: #fff4ce;
  --red: #cf222e;
  --red-bg: #ffebe9;
  --blue: #0969da;
  --blue-bg: #ddf4ff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
}
.app { min-width: 1100px; }
.topbar {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  padding: 18px 22px;
  background: #111827;
  color: #fff;
  border-bottom: 1px solid #0b1220;
}
h1, h2, p { margin: 0; }
h1 { font-size: 20px; font-weight: 700; }
h2 { font-size: 15px; font-weight: 700; }
.muted { color: var(--muted); font-size: 12px; line-height: 1.45; }
.topbar .muted { color: #cbd5e1; margin-top: 4px; }
.summary {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
  max-width: 560px;
}
.chip {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 650;
  border: 1px solid transparent;
  white-space: nowrap;
}
.chip.green { color: var(--green); background: var(--green-bg); border-color: #b7dfc0; }
.chip.yellow { color: var(--yellow); background: var(--yellow-bg); border-color: #f1d178; }
.chip.red { color: var(--red); background: var(--red-bg); border-color: #ffb8b3; }
.chip.blue { color: var(--blue); background: var(--blue-bg); border-color: #b6e3ff; }
.grid {
  display: grid;
  grid-template-columns: minmax(680px, 1fr) 380px;
  grid-template-rows: 430px minmax(360px, auto) 420px;
  gap: 14px;
  padding: 14px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-width: 0;
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
}
.graph-panel { grid-column: 1; grid-row: 1; }
.detail-panel { grid-column: 2; grid-row: 1 / 4; padding: 14px; overflow: auto; }
.table-panel { grid-column: 1; grid-row: 2; }
.field-panel { grid-column: 1; grid-row: 3; }
.panel-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: #fbfcfe;
}
.toolbar { display: flex; align-items: center; gap: 8px; }
input, select, button {
  height: 30px;
  border: 1px solid #cfd6e0;
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  font-size: 13px;
  padding: 0 9px;
}
button { cursor: pointer; font-weight: 650; }
.canvas-wrap {
  height: calc(100% - 64px);
  min-height: 280px;
  background:
    linear-gradient(#eef2f7 1px, transparent 1px),
    linear-gradient(90deg, #eef2f7 1px, transparent 1px);
  background-size: 24px 24px;
}
svg { width: 100%; height: 100%; display: block; user-select: none; }
.table-scroll { max-height: 500px; overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 9px 10px; border-bottom: 1px solid #edf0f4; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #f8fafc; z-index: 1; color: #475467; font-size: 11px; }
tr { cursor: pointer; }
tr:hover { background: #f6faff; }
tr.selected { background: #eaf4ff; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.sources { max-width: 360px; color: #475467; line-height: 1.4; }
.detail-box { margin: 10px 0 18px; padding: 10px; border: 1px solid var(--line); border-radius: 6px; background: #fbfcfe; }
.detail-box h3 { margin: 0 0 8px; font-size: 14px; }
.detail-box dl { display: grid; grid-template-columns: 90px 1fr; gap: 6px 8px; margin: 0; font-size: 12px; }
.detail-box dt { color: var(--muted); }
.detail-box dd { margin: 0; word-break: break-word; }
.warn-list { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
.warn-item { padding: 8px; border-radius: 6px; border: 1px solid var(--line); font-size: 12px; line-height: 1.4; }
.warn-item.red { border-color: #ffb8b3; background: var(--red-bg); }
.warn-item.yellow { border-color: #f1d178; background: var(--yellow-bg); }
.node rect { stroke-width: 1.4; rx: 8; }
.node text { pointer-events: none; }
.edge { fill: none; stroke: #98a2b3; stroke-width: 1.5; }
.edge.hot { stroke: var(--blue); stroke-width: 2.4; }
.node.hot rect { stroke: var(--blue); stroke-width: 2.6; }
.node.dim { opacity: .28; }
.node-label { font-size: 12px; font-weight: 700; fill: #1f2937; }
.node-sub { font-size: 10px; fill: #667085; }
.edge-label { font-size: 10px; fill: #667085; paint-order: stroke; stroke: #fff; stroke-width: 3px; }
"""


_JS = r"""
const lineage = JSON.parse(document.getElementById("lineage-data").textContent);
const scopes = lineage.scopes || {};
const graph = lineage.scope_graph || {nodes: [], edges: []};
const diagnostics = lineage.diagnostics || {};

const state = {
  selectedScope: null,
  selectedColumn: null,
  scopeTransform: {x: 20, y: 20, k: 1},
  fieldTransform: {x: 20, y: 20, k: 1},
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

function shortName(value, limit = 42) {
  const text = String(value ?? "");
  if (text.length <= limit) return text;
  return text.slice(0, limit - 1) + "…";
}

function scopeKind(id) {
  if (!scopes[id]) return "physical";
  const kind = scopes[id].kind || "scope";
  if (id === "ROOT") return "root";
  if (kind === "cte") return "cte";
  if (kind === "union" || id.startsWith("union:")) return "union";
  if (kind === "union_branch") return "union_branch";
  if (kind === "subquery") return "subquery";
  return kind;
}

function colorForRisk(risk) {
  if (risk === "RED") return "#cf222e";
  if (risk === "YELLOW") return "#9a6700";
  return "#238636";
}

function styleForKind(kind) {
  const map = {
    root: ["#e8f5ec", "#238636"],
    cte: ["#fff4ce", "#9a6700"],
    subquery: ["#e8f5ec", "#2da44e"],
    union: ["#f3e8ff", "#8250df"],
    union_branch: ["#ffeff7", "#bf3989"],
    physical: ["#ddf4ff", "#0969da"],
    unknown: ["#ffebe9", "#cf222e"],
  };
  return map[kind] || ["#f8fafc", "#667085"];
}

function warnings() {
  return diagnostics.warnings || [];
}

function rootColumns() {
  return ((scopes.ROOT || {}).columns || []);
}

function columnKey(scope, column) {
  return `${scope}\u0000${column}`;
}

function columnInScope(scopeId, colName) {
  const cols = ((scopes[scopeId] || {}).columns || []);
  return cols.find(col => col.name === colName) || null;
}

function traceColumn(scopeId, colName, seen = new Set(), edges = [], nodes = new Map()) {
  const key = columnKey(scopeId, colName);
  if (seen.has(key)) return {nodes, edges};
  seen.add(key);
  nodes.set(key, {scope: scopeId, column: colName, kind: scopes[scopeId] ? scopeKind(scopeId) : (scopeId === "UNKNOWN" ? "unknown" : "physical")});
  const col = columnInScope(scopeId, colName);
  if (!col) return {nodes, edges};
  for (const src of (col.sources || [])) {
    const srcKey = columnKey(src.scope, src.column);
    nodes.set(srcKey, {scope: src.scope, column: src.column, kind: scopes[src.scope] ? scopeKind(src.scope) : (src.scope === "UNKNOWN" ? "unknown" : "physical")});
    edges.push({from: srcKey, to: key, transform: col.transform || "DIRECT"});
    traceColumn(src.scope, src.column, seen, edges, nodes);
  }
  return {nodes, edges};
}

function physicalSourcesFor(col) {
  const traced = traceColumn("ROOT", col.name);
  const result = [];
  for (const node of traced.nodes.values()) {
    if (!scopes[node.scope] || node.scope === "UNKNOWN") {
      if (!(node.scope === "ROOT")) result.push(`${node.scope}.${node.column}`);
    }
  }
  return Array.from(new Set(result)).sort();
}

function columnRisk(col) {
  const traced = traceColumn("ROOT", col.name);
  for (const node of traced.nodes.values()) {
    if (node.scope === "UNKNOWN") return "RED";
  }
  if ((col.sources || []).some(src => src.column === "*" || src.scope === "UNKNOWN")) return "YELLOW";
  const warningTypes = new Set(warnings().map(w => w.type));
  if (warningTypes.has("star_not_expanded") || warningTypes.has("ambiguous_unqualified") || warningTypes.has("column_not_found")) {
    return "YELLOW";
  }
  return "GREEN";
}

function overallRisk() {
  const risks = rootColumns().map(columnRisk);
  if (risks.includes("RED")) return "RED";
  if (risks.includes("YELLOW") || warnings().length) return "YELLOW";
  return "GREEN";
}

function renderSummary() {
  document.getElementById("taskTitle").textContent = lineage.task_id || "Scope Lineage Report";
  document.getElementById("targetTitle").textContent = `Target: ${lineage.target_table || "(none)"}`;
  const risk = overallRisk();
  const summary = document.getElementById("summary");
  summary.innerHTML = [
    `<span class="chip ${risk.toLowerCase()}">${risk}</span>`,
    `<span class="chip blue">Scopes ${Object.keys(scopes).length}</span>`,
    `<span class="chip blue">Root columns ${rootColumns().length}</span>`,
    `<span class="chip blue">Warnings ${warnings().length}</span>`,
    `<span class="chip blue">Sources ${(lineage.source_tables || []).length}</span>`,
  ].join("");
}

function makeSvg(tag, attrs = {}) {
  const svgNs = "http" + "://www.w3.org/2000/svg";
  const el = document.createElementNS(svgNs, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function arrowPath(a, b) {
  const x1 = a.x + a.w;
  const y1 = a.y + a.h / 2;
  const x2 = b.x;
  const y2 = b.y + b.h / 2;
  const mid = x1 + Math.max(36, (x2 - x1) / 2);
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

function addDefs(svg) {
  const defs = makeSvg("defs");
  const marker = makeSvg("marker", {id: `${svg.id}-arrow`, markerWidth: "10", markerHeight: "10", refX: "8", refY: "3", orient: "auto", markerUnits: "strokeWidth"});
  marker.appendChild(makeSvg("path", {d: "M0,0 L0,6 L9,3 z", fill: "#98a2b3"}));
  defs.appendChild(marker);
  svg.appendChild(defs);
}

function layeredLayout(nodes, edges, nodeW = 220, nodeH = 66) {
  const incoming = new Map(nodes.map(n => [n.id, []]));
  const outgoing = new Map(nodes.map(n => [n.id, []]));
  for (const e of edges) {
    if (!incoming.has(e.to) || !outgoing.has(e.from)) continue;
    incoming.get(e.to).push(e.from);
    outgoing.get(e.from).push(e.to);
  }
  const memo = new Map();
  function depth(id, stack = new Set()) {
    if (memo.has(id)) return memo.get(id);
    if (stack.has(id)) return 0;
    stack.add(id);
    const parents = incoming.get(id) || [];
    const value = parents.length ? 1 + Math.max(...parents.map(p => depth(p, stack))) : 0;
    stack.delete(id);
    memo.set(id, value);
    return value;
  }
  const byLayer = new Map();
  for (const n of nodes) {
    const layer = n.id === "ROOT" ? Math.max(1, ...nodes.map(x => depth(x.id))) + 1 : depth(n.id);
    if (!byLayer.has(layer)) byLayer.set(layer, []);
    byLayer.get(layer).push(n);
  }
  const positioned = new Map();
  const layerGap = 190, nodeGap = 24;
  for (const [layer, layerNodes] of Array.from(byLayer.entries()).sort((a, b) => a[0] - b[0])) {
    layerNodes.sort((a, b) => a.label.localeCompare(b.label));
    layerNodes.forEach((n, idx) => {
      positioned.set(n.id, { ...n, x: 30 + layer * (nodeW + layerGap), y: 30 + idx * (nodeH + nodeGap), w: nodeW, h: nodeH });
    });
  }
  const width = Math.max(...Array.from(positioned.values()).map(n => n.x + n.w + 60), 600);
  const height = Math.max(...Array.from(positioned.values()).map(n => n.y + n.h + 60), 300);
  return {nodes: positioned, width, height};
}

function drawNode(group, n, onClick) {
  const style = styleForKind(n.kind);
  const g = makeSvg("g", {class: "node", transform: `translate(${n.x},${n.y})`, "data-id": n.id});
  g.appendChild(makeSvg("rect", {width: n.w, height: n.h, fill: style[0], stroke: style[1]}));
  const t1 = makeSvg("text", {x: 12, y: 22, class: "node-label"});
  t1.textContent = shortName(n.label, 30);
  const t2 = makeSvg("text", {x: 12, y: 42, class: "node-sub"});
  t2.textContent = shortName(n.sub || n.kind, 36);
  const t3 = makeSvg("text", {x: 12, y: 58, class: "node-sub"});
  t3.textContent = shortName(n.extra || "", 36);
  g.append(t1, t2, t3);
  g.addEventListener("click", () => onClick(n));
  group.appendChild(g);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function resetTransform(transform) {
  transform.x = 20;
  transform.y = 20;
  transform.k = 1;
}

function wheelZoomFactor(ev) {
  const sensitivity = ev.deltaMode === 1 ? 0.05 : 0.002;
  return clamp(Math.exp(-ev.deltaY * sensitivity), 0.85, 1.15);
}

function setupPanZoom(svg, viewport, transform, fitButton) {
  svg._viewport = viewport;
  svg._transform = transform;
  let dragging = false, start = null;
  function apply() {
    if (!svg._viewport) return;
    svg._viewport.setAttribute("transform", `translate(${transform.x},${transform.y}) scale(${transform.k})`);
  }
  if (svg._panZoomReady) {
    apply();
    return;
  }
  svg._panZoomReady = true;
  svg.addEventListener("wheel", ev => {
    ev.preventDefault();
    const rect = svg.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    const beforeX = (px - transform.x) / transform.k;
    const beforeY = (py - transform.y) / transform.k;
    const nextK = clamp(transform.k * wheelZoomFactor(ev), 0.15, 3);
    transform.x = px - beforeX * nextK;
    transform.y = py - beforeY * nextK;
    transform.k = nextK;
    apply();
  }, {passive: false});
  svg.addEventListener("mousedown", ev => {
    dragging = true;
    start = {x: ev.clientX - transform.x, y: ev.clientY - transform.y};
  });
  window.addEventListener("mousemove", ev => {
    if (!dragging) return;
    transform.x = ev.clientX - start.x;
    transform.y = ev.clientY - start.y;
    apply();
  });
  window.addEventListener("mouseup", () => dragging = false);
  fitButton.addEventListener("click", () => {
    const box = svg._viewport.getBBox();
    const rect = svg.getBoundingClientRect();
    const k = Math.min(rect.width / (box.width + 80), rect.height / (box.height + 80), 1.2);
    transform.k = Math.max(0.15, k);
    transform.x = 40 - box.x * transform.k;
    transform.y = 40 - box.y * transform.k;
    apply();
  });
  apply();
}

function renderScopeGraph() {
  const svg = document.getElementById("scopeSvg");
  svg.innerHTML = "";
  addDefs(svg);
  const viewport = makeSvg("g", {id: "scopeViewport"});
  svg.appendChild(viewport);
  const edgeGroup = makeSvg("g");
  const nodeGroup = makeSvg("g");
  viewport.append(edgeGroup, nodeGroup);

  const nodeIds = Array.from(new Set([...(graph.nodes || []), ...Object.keys(scopes), ...(lineage.source_tables || [])]));
  const nodes = nodeIds.map(id => {
    const s = scopes[id] || {};
    const kind = scopeKind(id);
    return {id, label: id, kind, sub: kind, extra: `${(s.columns || []).length} cols`};
  });
  const edges = (graph.edges || []).map(e => ({from: e.from, to: e.to})).filter(e => e.from && e.to);
  const layout = layeredLayout(nodes, edges);
  svg.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);

  for (const e of edges) {
    const a = layout.nodes.get(e.from), b = layout.nodes.get(e.to);
    if (!a || !b) continue;
    edgeGroup.appendChild(makeSvg("path", {class: "edge", d: arrowPath(a, b), "marker-end": `url(#${svg.id}-arrow)`}));
  }
  for (const n of layout.nodes.values()) drawNode(nodeGroup, n, selectScope);
  setupPanZoom(svg, viewport, state.scopeTransform, document.getElementById("fitScope"));
}

function selectScope(n) {
  state.selectedScope = n.id;
  const s = scopes[n.id];
  const detail = document.getElementById("detailBox");
  if (!s) {
    detail.innerHTML = `<h3>${esc(n.id)}</h3><dl><dt>Kind</dt><dd>physical table</dd></dl>`;
    return;
  }
  detail.innerHTML = `<h3>${esc(n.id)}</h3><dl>
    <dt>Kind</dt><dd>${esc(s.kind || "")}</dd>
    <dt>Role</dt><dd>${esc(s.role || "")}</dd>
    <dt>Columns</dt><dd>${(s.columns || []).length}</dd>
    <dt>Depends on</dt><dd>${esc((s.depends_on || []).join(", "))}</dd>
    <dt>Writes to</dt><dd>${esc(s.writes_to || "")}</dd>
  </dl>`;
}

function renderColumns() {
  const body = document.getElementById("columnsBody");
  const query = document.getElementById("columnSearch").value.toLowerCase();
  const risk = document.getElementById("riskFilter").value;
  body.innerHTML = "";
  for (const col of rootColumns()) {
    const colRisk = columnRisk(col);
    if (risk !== "ALL" && colRisk !== risk) continue;
    if (query && !String(col.name || "").toLowerCase().includes(query)) continue;
    const direct = (col.sources || []).map(s => `${s.scope}.${s.column}`);
    const physical = physicalSourcesFor(col);
    const tr = document.createElement("tr");
    tr.dataset.column = col.name;
    tr.innerHTML = `<td><span class="chip ${colRisk.toLowerCase()}">${colRisk}</span></td>
      <td class="mono">${esc(col.name)}</td>
      <td>${esc(col.transform || "")}</td>
      <td class="sources">${esc(shortName(direct.join(", "), 160))}</td>
      <td class="sources">${esc(shortName(physical.join(", "), 180))}</td>`;
    tr.addEventListener("click", () => selectColumn(col.name));
    body.appendChild(tr);
  }
}

function selectColumn(name) {
  const changed = state.selectedColumn !== name;
  state.selectedColumn = name;
  document.querySelectorAll("#columnsBody tr").forEach(row => row.classList.toggle("selected", row.dataset.column === name));
  const col = columnInScope("ROOT", name);
  const phys = physicalSourcesFor(col || {name});
  document.getElementById("detailBox").innerHTML = `<h3>ROOT.${esc(name)}</h3><dl>
    <dt>Risk</dt><dd><span class="chip ${columnRisk(col).toLowerCase()}">${columnRisk(col)}</span></dd>
    <dt>Transform</dt><dd>${esc(col?.transform || "")}</dd>
    <dt>Expression</dt><dd class="mono">${esc(col?.expression || "")}</dd>
    <dt>Sources</dt><dd>${esc((col?.sources || []).map(s => `${s.scope}.${s.column}`).join(", "))}</dd>
    <dt>Physical</dt><dd>${esc(phys.join(", "))}</dd>
  </dl>`;
  if (changed) resetTransform(state.fieldTransform);
  renderFieldGraph(name);
}

function renderFieldGraph(name) {
  const svg = document.getElementById("fieldSvg");
  svg.innerHTML = "";
  addDefs(svg);
  const viewport = makeSvg("g", {id: "fieldViewport"});
  svg.appendChild(viewport);
  const edgeGroup = makeSvg("g");
  const nodeGroup = makeSvg("g");
  viewport.append(edgeGroup, nodeGroup);
  const traced = traceColumn("ROOT", name);
  const nodes = Array.from(traced.nodes.entries()).map(([id, n]) => ({id, label: `${n.scope}.${n.column}`, kind: n.kind, sub: n.scope, extra: n.column}));
  const edges = traced.edges.map(e => ({from: e.from, to: e.to, transform: e.transform}));
  const layout = layeredLayout(nodes, edges, 260, 72);
  svg.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
  for (const e of edges) {
    const a = layout.nodes.get(e.from), b = layout.nodes.get(e.to);
    if (!a || !b) continue;
    edgeGroup.appendChild(makeSvg("path", {class: "edge hot", d: arrowPath(a, b), "marker-end": `url(#${svg.id}-arrow)`}));
    const label = makeSvg("text", {x: (a.x + b.x + a.w) / 2, y: (a.y + b.y) / 2 + 8, class: "edge-label"});
    label.textContent = e.transform || "";
    edgeGroup.appendChild(label);
  }
  for (const n of layout.nodes.values()) drawNode(nodeGroup, n, () => {});
  document.getElementById("fieldHint").textContent = `ROOT.${name}`;
  setupPanZoom(svg, viewport, state.fieldTransform, document.getElementById("fitField"));
}

function renderDiagnostics() {
  const box = document.getElementById("diagnostics");
  const counts = new Map();
  for (const w of warnings()) counts.set(w.type || "warning", (counts.get(w.type || "warning") || 0) + 1);
  const unknownCount = rootColumns().filter(c => columnRisk(c) === "RED").length;
  const items = [];
  if (unknownCount) items.push(`<div class="warn-item red"><b>UNKNOWN / RED columns:</b> ${unknownCount}</div>`);
  for (const [type, count] of counts) items.push(`<div class="warn-item yellow"><b>${esc(type)}</b>: ${count}</div>`);
  box.innerHTML = items.length ? `<div class="warn-list">${items.join("")}</div>` : `<p class="muted">No diagnostics warnings.</p>`;
}

function initSearch() {
  document.getElementById("columnSearch").addEventListener("input", renderColumns);
  document.getElementById("riskFilter").addEventListener("change", renderColumns);
  document.getElementById("scopeSearch").addEventListener("input", ev => {
    const q = ev.target.value.toLowerCase();
    document.querySelectorAll("#scopeSvg .node").forEach(n => {
      const hit = !q || n.dataset.id.toLowerCase().includes(q);
      n.classList.toggle("dim", !hit);
    });
  });
}

renderSummary();
renderScopeGraph();
renderColumns();
renderDiagnostics();
initSearch();
if (rootColumns()[0]) selectColumn(rootColumns()[0].name);
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an offline HTML scope-lineage report")
    parser.add_argument("--input", required=True, help="Directory containing lineage.json")
    parser.add_argument("--out", help="Output HTML path. Defaults to <input>/report.html")
    args = parser.parse_args(argv)

    path = write_html_report_from_dir(args.input, args.out)
    print(f"HTML report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
