// Knowledge-graph view: the board as an interactive node-link diagram
// (Component / Net / Region, typed edges), separate from the schematic
// canvas. Rendered with Cytoscape.js + the fcose force-directed layout --
// the standard tool for this exact problem, not hand-rolled physics.
// Vendored locally in web/static/vendor/ (no CDN at runtime, no bundler to
// produce it -- downloaded once, checked in like any other static asset).
// `esc()` and `selectComponent()` are defined in canvas.js, loaded first.

cytoscape.use(cytoscapeFcose);

const kgToggleBtn = document.getElementById("kg-toggle-btn");
const kgPowerToggle = document.getElementById("kg-power-toggle");
const kgResetBtn = document.getElementById("reset-view");
const kgContainer = document.getElementById("kg-cy");

const SCHEM_HIDE_ON_KG = ["schematic-svg", "schem-legend", "schem-help", "schem-search-wrap", "region-filter"]
  .map((id) => document.getElementById(id))
  .filter(Boolean);
const KG_SHOW_ON_KG = [kgContainer, document.getElementById("kg-legend"), document.getElementById("kg-help"), kgPowerToggle]
  .filter(Boolean);

let kgActive = false;
let kgLoaded = false;
let kgGraph = null;    // raw {nodes, edges, counts} from the API
let kgShowPower = false;
let cy = null;

const KG_STYLE = [
  {
    selector: "node",
    style: {
      "background-color": "#8b949e",
      label: "data(label)",
      color: "#8b949e",
      "font-size": 5,
      "min-zoomed-font-size": 7,
      "text-valign": "bottom",
      "text-margin-y": 3,
      "border-width": 1,
      "border-color": "#0d1117",
      "text-outline-width": 0,
    },
  },
  { selector: "node.component", style: { "background-color": "#58a6ff", width: 10, height: 10 } },
  { selector: "node.net", style: { "background-color": "#39c5cf", width: 6, height: 6 } },
  { selector: "node.net.power", style: { "background-color": "#f85149" } },
  {
    selector: "node.region",
    style: {
      "background-color": "#d29922",
      "background-opacity": 0.25,
      "border-color": "#d29922",
      "border-width": 1.5,
      width: 22,
      height: 22,
      "font-size": 6,
      "text-valign": "top",
      "text-margin-y": -3,
    },
  },
  { selector: "edge", style: { width: 0.6, "line-color": "#30363d", "curve-style": "haystack" } },
  { selector: "edge.grouped_into", style: { "line-color": "#3d444d", "line-style": "dashed", width: 0.5 } },
  { selector: "edge.power", style: { "line-color": "rgba(248,81,73,0.3)" } },
  { selector: "node:selected", style: { "border-width": 2.5, "border-color": "#e6edf3" } },
  { selector: ".kg-dimmed", style: { opacity: 0.15 } },
];

async function kgLoad() {
  const res = await fetch("/api/knowledge-graph");
  kgGraph = await res.json();
  kgRender();
  kgLoaded = true;
}

function kgElements() {
  const nodes = kgGraph.nodes
    .filter((n) => kgShowPower || n.type !== "net" || !n.is_power)
    .map((n) => ({
      data: { ...n, label: n.label },
      classes: n.type + (n.type === "net" && n.is_power ? " power" : ""),
    }));
  const nodeIds = new Set(nodes.map((n) => n.data.id));
  const edges = kgGraph.edges
    .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
    .map((e, i) => ({
      data: { id: `e${i}`, source: e.source, target: e.target },
      classes: e.type + (e.is_power ? " power" : ""),
    }));
  return [...nodes, ...edges];
}

function kgRender() {
  if (cy) cy.destroy();
  cy = cytoscape({
    container: kgContainer,
    elements: kgElements(),
    style: KG_STYLE,
    layout: {
      name: "fcose",
      animate: true,
      randomize: true,
      nodeRepulsion: 6500,
      idealEdgeLength: 30,
      nestingFactor: 0.6,
    },
    minZoom: 0.08,
    maxZoom: 6,
    wheelSensitivity: 0.25,
  });

  cy.on("tap", "node", (evt) => kgSelectNode(evt.target));
  cy.on("tap", (evt) => { if (evt.target === cy) kgSelectNode(null); });
}

function kgSelectNode(node) {
  cy.elements().removeClass("kg-dimmed");
  const panel = document.getElementById("side-panel");
  if (!node) { renderOverview(); return; }
  const focus = node.closedNeighborhood();
  cy.elements().difference(focus).addClass("kg-dimmed");
  panel.innerHTML = kgNodePanelHtml(node.data());
}

function kgNodePanelHtml(n) {
  if (n.type === "component") {
    const ref = n.id.slice("component:".length);
    return `
      <div class="panel-section">
        <div class="panel-title">${esc(n.label)}</div>
        <div class="panel-sub">${esc(n.region)} &middot; ${n.pin_count} pins</div>
        ${n.part_number ? `
        <div class="fact-row"><span class="k">Part number</span><span class="v">${esc(n.part_number)}</span></div>
        <div class="fact-row"><span class="k">Confidence</span><span class="v">${Math.round((n.confidence || 0) * 100)}%</span></div>
        <p style="font-size:13px; margin-top:8px;">${esc(n.function || "")}</p>` : `
        <p class="hint">Not confidently identified.</p>`}
        <div class="link-row" style="margin-top:10px;">
          <button class="link-btn" id="kg-locate-btn" data-ref="${esc(ref)}" type="button">Locate on schematic</button>
        </div>
      </div>`;
  }
  if (n.type === "net") {
    return `
      <div class="panel-section">
        <div class="panel-title">${esc(n.label)}</div>
        <div class="panel-sub">${n.is_power ? "power rail" : "signal net"} &middot; ${n.member_count} member(s)</div>
      </div>`;
  }
  return `
    <div class="panel-section">
      <div class="panel-title">${esc(n.label)}</div>
      <p style="font-size:13px;">${esc(n.explanation || "No AI explanation for this region yet.")}</p>
    </div>`;
}

document.getElementById("side-panel").addEventListener("click", (e) => {
  const btn = e.target.closest("#kg-locate-btn");
  if (!btn) return;
  kgSetActive(false);
  selectComponent(btn.dataset.ref, { fly: true });
});

kgPowerToggle.addEventListener("click", () => {
  kgShowPower = !kgShowPower;
  kgPowerToggle.textContent = kgShowPower ? "Hide power nets" : "Show power nets";
  kgRender();
});

kgResetBtn.addEventListener("click", () => {
  if (kgActive && cy) cy.fit(undefined, 30);
});

window.addEventListener("keydown", (e) => {
  if (!kgActive || e.target.tagName === "INPUT") return;
  if (e.key === "Escape") kgSelectNode(null);
});

function kgSetActive(active) {
  kgActive = active;
  kgToggleBtn.classList.toggle("active", active);
  kgToggleBtn.textContent = active ? "Schematic view" : "Knowledge graph";
  for (const el of SCHEM_HIDE_ON_KG) el.classList.toggle("kg-hidden", active);
  for (const el of KG_SHOW_ON_KG) el.classList.toggle("kg-hidden", !active);
  if (active) {
    if (!kgLoaded) kgLoad();
    else cy.resize();
  }
}

kgToggleBtn.addEventListener("click", () => kgSetActive(!kgActive));
