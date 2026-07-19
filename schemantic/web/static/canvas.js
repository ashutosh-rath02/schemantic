// Schemantic infinite canvas.
// Our own vector rendering built from the connectivity graph. Topology is
// exact (every drawn segment connects electrically-connected pins); the
// specific wire paths are synthetic, not the original drawn routes.

const SVG_NS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("schematic-svg");
const panel = document.getElementById("side-panel");

// ---- persistence (localStorage; board switches are full reloads, so all
// UI state that should survive them lives here) ----
function stGet(key, fallback) {
  try {
    const raw = localStorage.getItem("schemantic." + key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch { return fallback; }
}
function stSet(key, value) {
  try { localStorage.setItem("schemantic." + key, JSON.stringify(value)); } catch { /* full/blocked */ }
}
let activeBoardId = null;  // set once workspace info arrives
function boardKey(suffix) { return `board.${activeBoardId || "default"}.${suffix}`; }

// distinct hues for a selected component's nets, so you can tell which
// wire is which; assigned per selection, mirrored as dots in the panel
const NET_COLORS = ["#3fb950", "#58a6ff", "#d29922", "#f85149", "#39c5cf",
                    "#bc8cff", "#ff7b72", "#7ee787", "#ffa657", "#79c0ff"];

let schematic = null;
let comps = {};
let selection = null;        // {type:"comp"|"net", key}
let hoverNet = null;         // net row hovered in panel -> isolate that net
let netColorMap = {};        // netKey -> color, for current comp selection

// ---- view state ----
let view = { x: 0, y: 0, w: 842, h: 595 };
let fitView = { ...view };

function applyView() {
  svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
  svg.classList.toggle("zoomed", view.w < 380);   // passive labels
  svg.classList.toggle("zoomed2", view.w < 240);  // pin numbers + net names
  if (activeBoardId) stSet(boardKey("view"), view);
}

function clientToSvg(cx, cy) {
  const r = svg.getBoundingClientRect();
  return {
    x: view.x + ((cx - r.left) / r.width) * view.w,
    y: view.y + ((cy - r.top) / r.height) * view.h,
  };
}

function flyTo(box, minW = 160) {
  const cx = (box.x0 + box.x1) / 2;
  const cy = (box.y0 + box.y1) / 2;
  const r = svg.getBoundingClientRect();
  const aspect = r.height / r.width;
  const w = Math.max((box.x1 - box.x0) * 5, minW);
  view = { x: cx - w / 2, y: cy - (w * aspect) / 2, w, h: w * aspect };
  applyView();
}

svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const factor = e.deltaY > 0 ? 1.18 : 1 / 1.18;
  const pt = clientToSvg(e.clientX, e.clientY);
  view.x = pt.x - (pt.x - view.x) * factor;
  view.y = pt.y - (pt.y - view.y) * factor;
  view.w *= factor;
  view.h *= factor;
  applyView();
}, { passive: false });

let panState = null;
svg.addEventListener("mousedown", (e) => {
  panState = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
});
window.addEventListener("mousemove", (e) => {
  if (!panState) return;
  const r = svg.getBoundingClientRect();
  if (Math.abs(e.clientX - panState.sx) + Math.abs(e.clientY - panState.sy) > 4) panState.moved = true;
  view.x = panState.vx - ((e.clientX - panState.sx) / r.width) * view.w;
  view.y = panState.vy - ((e.clientY - panState.sy) / r.height) * view.h;
  applyView();
});
window.addEventListener("mouseup", () => {
  if (panState) setTimeout(() => { panState = null; }, 0);
});

document.getElementById("reset-view").addEventListener("click", () => {
  view = { ...fitView };
  applyView();
});

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") { if (e.key === "Escape") e.target.blur(); return; }
  if (e.key === "Escape") { clearSelection(true); }
  if (e.key === "+" || e.key === "=") zoomCenter(1 / 1.3);
  if (e.key === "-") zoomCenter(1.3);
});

function zoomCenter(factor) {
  const cx = view.x + view.w / 2, cy = view.y + view.h / 2;
  view.w *= factor; view.h *= factor;
  view.x = cx - view.w / 2; view.y = cy - view.h / 2;
  applyView();
}

// ---- init ----
let workspaceInfo = { boards: [], mates: [] };

async function init() {
  const res = await fetch("/api/schematic");
  schematic = await res.json();
  comps = Object.fromEntries(schematic.components.map((c) => [c.ref_token, c]));
  try {
    workspaceInfo = await (await fetch("/api/workspace")).json();
  } catch { /* workspace optional */ }
  const active = workspaceInfo.boards.find((b) => b.active);
  activeBoardId = active ? active.id : null;
  renderBoardSwitcher();

  const pad = 20;
  fitView = { x: -pad, y: -pad, w: schematic.page_width + pad * 2, h: schematic.page_height + pad * 2 };
  view = { ...fitView };

  buildScene();
  buildSearch();
  renderRegionChips();
  applyView();
  applyStateFromUrl(false);

  // no explicit URL intent -> restore this board's last selection and view
  const params = new URLSearchParams(location.search);
  const hasUrlIntent = ["select", "net", "ask", "q"].some((k) => params.get(k));
  if (!hasUrlIntent) {
    const savedSel = stGet(boardKey("selection"), null);
    if (savedSel && savedSel.type === "comp" && comps[savedSel.key]) {
      selectComponent(savedSel.key, { pushUrl: false });
    } else if (savedSel && savedSel.type === "net" && schematic.nets[savedSel.key]) {
      selectNet(savedSel.key, { pushUrl: false });
    }
    const savedView = stGet(boardKey("view"), null);
    if (savedView && savedView.w > 0) { view = savedView; applyView(); }
  }
  if (!selection) renderOverview();
}

function applyStateFromUrl(fromPop) {
  const params = new URLSearchParams(location.search);
  const sel = params.get("select");
  const netParam = params.get("net");
  if (sel) {
    const m = schematic.components.find(
      (c) => c.display_label === sel || c.encoded_refdes === sel || c.ref_token === sel
    );
    if (m) { selectComponent(m.ref_token, { pushUrl: false, fly: !fromPop }); return; }
  }
  if (netParam) {
    const key = Object.keys(schematic.nets).find(
      (k) => k === netParam || schematic.nets[k].names.includes(netParam)
    );
    if (key) { selectNet(key, { pushUrl: false }); return; }
  }
  if (fromPop) clearSelection(false);
}

window.addEventListener("popstate", () => applyStateFromUrl(true));

function pushUrl(query) {
  history.pushState(null, "", query ? `?${query}` : location.pathname);
}

// ---- component type styling ----
function typeOf(c) {
  const p = (c.encoded_refdes.match(/^[A-Za-z]+/) || [""])[0].toUpperCase();
  if (["U", "TB", "AMS", "M"].includes(p)) return "ic";
  if (["P", "H", "J", "TYPE", "DC"].includes(p)) return "conn";
  if (["Q", "VT"].includes(p)) return "trans";
  if (["D", "LED", "TVS"].includes(p)) return "diode";
  return "passive";
}

function isNoteworthy(c) { return typeOf(c) !== "passive"; }

// ---- scene ----
function buildScene() {
  svg.innerHTML = "";
  const gRegions = document.createElementNS(SVG_NS, "g");
  const gWires = document.createElementNS(SVG_NS, "g");
  const gComps = document.createElementNS(SVG_NS, "g");
  const gFlags = document.createElementNS(SVG_NS, "g");
  gFlags.id = "net-flags";
  svg.append(gRegions, gWires, gComps, gFlags);

  // faint region outlines + titles for orientation
  for (const [name, info] of Object.entries(schematic.regions)) {
    const b = info.bounds;
    if (!b || b.x0 === undefined) continue;
    const pad = 10;
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", b.x0 - pad); rect.setAttribute("y", b.y0 - pad);
    rect.setAttribute("width", b.x1 - b.x0 + pad * 2);
    rect.setAttribute("height", b.y1 - b.y0 + pad * 2);
    rect.setAttribute("rx", 5);
    rect.setAttribute("class", "region-outline");
    gRegions.appendChild(rect);
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", b.x0 - pad + 3);
    label.setAttribute("y", b.y0 - pad - 2.5);
    label.setAttribute("class", "region-title");
    label.textContent = name;
    gRegions.appendChild(label);
  }

  for (const [key, net] of Object.entries(schematic.nets)) {
    const g = document.createElementNS(SVG_NS, "g");
    g.dataset.net = key;
    g.setAttribute("class", "net" + (net.is_power ? " net-power" : ""));
    for (const [x1, y1, x2, y2] of net.segments) {
      const wire = document.createElementNS(SVG_NS, "line");
      wire.setAttribute("x1", x1); wire.setAttribute("y1", y1);
      wire.setAttribute("x2", x2); wire.setAttribute("y2", y2);
      wire.setAttribute("class", "wire");
      g.appendChild(wire);
      const hit = document.createElementNS(SVG_NS, "line");
      hit.setAttribute("x1", x1); hit.setAttribute("y1", y1);
      hit.setAttribute("x2", x2); hit.setAttribute("y2", y2);
      hit.setAttribute("class", "wire-hit");
      g.appendChild(hit);
    }
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${net.names.join(" / ")} — ${net.members.length} components`;
    g.appendChild(title);
    gWires.appendChild(g);
  }

  for (const c of schematic.components) {
    const g = document.createElementNS(SVG_NS, "g");
    g.dataset.comp = c.ref_token;
    g.setAttribute("class", `comp t-${typeOf(c)}`);

    const b = c.box;
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", b.x0); rect.setAttribute("y", b.y0);
    rect.setAttribute("width", b.x1 - b.x0); rect.setAttribute("height", b.y1 - b.y0);
    rect.setAttribute("rx", 2);
    rect.setAttribute("class", "comp-body");
    g.appendChild(rect);

    for (const p of c.pins) {
      const pin = document.createElementNS(SVG_NS, "circle");
      pin.setAttribute("cx", p.x); pin.setAttribute("cy", p.y);
      pin.setAttribute("r", 1.1);
      pin.setAttribute("class", "pin");
      g.appendChild(pin);
      if (p.net) {
        // pin detail, visible only at deep zoom
        const pl = document.createElementNS(SVG_NS, "text");
        pl.setAttribute("x", p.x + 1.8);
        pl.setAttribute("y", p.y - 1.2);
        pl.setAttribute("class", "pin-label");
        pl.textContent = `${parseInt(p.pin_id, 10) || p.pin_id}·${p.net.split("/")[0]}`;
        g.appendChild(pl);
      }
    }

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", (b.x0 + b.x1) / 2);
    label.setAttribute("y", b.y0 - 2);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", isNoteworthy(c) ? "comp-label" : "comp-label label-passive");
    label.textContent = c.display_label || c.encoded_refdes;
    g.appendChild(label);

    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${c.display_label || c.encoded_refdes} — ${c.region || "unlabeled area"}`;
    g.appendChild(title);
    gComps.appendChild(g);
  }
}

// ---- selection ----
svg.addEventListener("click", (e) => {
  if (panState && panState.moved) return;
  const compG = e.target.closest("[data-comp]");
  if (compG) { selectComponent(compG.dataset.comp); return; }
  const netG = e.target.closest("[data-net]");
  if (netG) { selectNet(netG.dataset.net); return; }
  clearSelection(true);
});

function clearSelection(push) {
  selection = null;
  hoverNet = null;
  netColorMap = {};
  if (activeBoardId) stSet(boardKey("selection"), null);
  updateHighlights();
  renderOverview();
  if (push) pushUrl("");
}

function selectComponent(token, opts = {}) {
  const { pushUrl: push = true, fly = false } = opts;
  selection = { type: "comp", key: token };
  if (activeBoardId) stSet(boardKey("selection"), selection);
  hoverNet = null;
  const c = comps[token];
  netColorMap = {};
  c.nets.filter((n) => !n.is_power).forEach((n, i) => {
    netColorMap[n.key] = NET_COLORS[i % NET_COLORS.length];
  });
  updateHighlights();
  renderComponentPanel(c);
  if (fly) flyTo(c.box, 220);
  if (push) pushUrl(`select=${encodeURIComponent(c.display_label || c.encoded_refdes)}`);
}

function selectNet(key, opts = {}) {
  const { pushUrl: push = true } = opts;
  selection = { type: "net", key };
  if (activeBoardId) stSet(boardKey("selection"), selection);
  hoverNet = null;
  netColorMap = { [key]: NET_COLORS[0] };
  updateHighlights();
  renderNetPanel(key);
  if (push) pushUrl(`net=${encodeURIComponent(schematic.nets[key].names[0])}`);
}

function updateHighlights() {
  const compGroups = svg.querySelectorAll("[data-comp]");
  const netGroups = svg.querySelectorAll("[data-net]");
  const flags = svg.querySelector("#net-flags");
  flags.innerHTML = "";

  if (!selection) {
    compGroups.forEach((g) => { g.classList.remove("selected", "peer", "dimmed"); });
    netGroups.forEach((g) => {
      g.classList.remove("active", "dimmed");
      g.querySelectorAll(".wire").forEach((w) => (w.style.stroke = ""));
    });
    return;
  }

  let activeNets = new Set();
  let activeComps = new Set();

  if (selection.type === "comp") {
    activeComps.add(selection.key);
    for (const n of comps[selection.key].nets) {
      if (!n.is_power) {
        activeNets.add(n.key);
        for (const m of schematic.nets[n.key].members) activeComps.add(m);
      }
    }
  } else {
    activeNets.add(selection.key);
    for (const m of schematic.nets[selection.key].members) activeComps.add(m);
  }

  // hovering a net row in the panel isolates just that net
  const isolating = hoverNet && activeNets.has(hoverNet);

  compGroups.forEach((g) => {
    const token = g.dataset.comp;
    g.classList.remove("selected", "peer", "dimmed");
    let on = activeComps.has(token);
    if (isolating) on = token === selection.key || schematic.nets[hoverNet].members.includes(token);
    if (selection.type === "comp" && token === selection.key) g.classList.add("selected");
    else if (on) g.classList.add("peer");
    else g.classList.add("dimmed");
  });

  netGroups.forEach((g) => {
    const key = g.dataset.net;
    g.classList.remove("active", "dimmed");
    let on = activeNets.has(key);
    if (isolating) on = key === hoverNet;
    if (on) {
      g.classList.add("active");
      const color = netColorMap[key] || NET_COLORS[0];
      g.querySelectorAll(".wire").forEach((w) => (w.style.stroke = color));
    } else {
      g.classList.add("dimmed");
      g.querySelectorAll(".wire").forEach((w) => (w.style.stroke = ""));
    }
  });

  // name flag on the traced net (single-net selection only)
  if (selection.type === "net") {
    const net = schematic.nets[selection.key];
    let longest = null, len = -1;
    for (const s of net.segments) {
      const l = (s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2;
      if (l > len) { len = l; longest = s; }
    }
    if (longest) {
      const t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("x", (longest[0] + longest[2]) / 2);
      t.setAttribute("y", (longest[1] + longest[3]) / 2 - 2);
      t.setAttribute("class", "net-flag");
      t.textContent = net.names[0];
      flags.appendChild(t);
    }
  }
}

// ---- search ----
function buildSearch() {
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");

  function run(q) {
    results.innerHTML = "";
    if (!q || q.length < 1) { results.classList.remove("open"); return; }
    const ql = q.toLowerCase();
    const hits = [];
    for (const c of schematic.components) {
      const label = c.display_label || c.encoded_refdes;
      const part = c.identity && c.identity.likely_part_number;
      if (label.toLowerCase().includes(ql) || (part && part.toLowerCase().includes(ql))) {
        hits.push({ kind: "comp", key: c.ref_token, main: label, sub: part || c.region || "unlabeled area" });
      }
      if (hits.length > 14) break;
    }
    for (const [key, net] of Object.entries(schematic.nets)) {
      if (net.names.some((n) => n.toLowerCase().includes(ql))) {
        hits.push({ kind: "net", key, main: net.names.join(" / "), sub: `${net.members.length} components` });
      }
      if (hits.length > 18) break;
    }
    if (!hits.length) { results.classList.remove("open"); return; }
    for (const h of hits.slice(0, 12)) {
      const row = document.createElement("div");
      row.className = "search-row";
      row.innerHTML = `<span class="mono">${esc(h.main)}</span><span class="search-sub">${esc(h.sub || "")}</span>`;
      row.onclick = () => {
        results.classList.remove("open");
        input.value = "";
        if (h.kind === "comp") { selectComponent(h.key, { fly: true }); }
        else selectNet(h.key);
      };
      results.appendChild(row);
    }
    results.classList.add("open");
  }

  input.addEventListener("input", () => run(input.value.trim()));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const first = results.querySelector(".search-row");
      if (first) first.click();
    }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) results.classList.remove("open");
  });

  // ?q= deep link (also how automated screenshots verify search)
  const q = new URLSearchParams(location.search).get("q");
  if (q) { input.value = q; run(q); }
}

// ---- panel ----
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function compChip(token) {
  const c = comps[token];
  const name = c ? (c.display_label || c.encoded_refdes) : token;
  return `<span class="chip" data-comp="${esc(token)}">${esc(name)}</span>`;
}

function renderOverview() {
  const regions = Object.entries(schematic.regions);
  const counts = {};
  for (const c of schematic.components) counts[c.region] = (counts[c.region] || 0) + 1;
  const unlabeledCount = schematic.components.filter((c) => !c.region).length;
  panel.innerHTML = `
    <div class="panel-section">
      <div class="panel-title">Board overview</div>
      <div class="panel-sub">${schematic.components.length} components · ${Object.keys(schematic.nets).length} nets · click a section to zoom to it</div>
      <div class="link-row" style="margin-top:8px;">
        <a class="link-btn" href="/api/hardwaremap" download>Export hardware map (.md)</a>
        <a class="link-btn" href="/api/hardwaremap?format=json" target="_blank" rel="noopener">.json</a>
      </div>
      <span class="hint">Pin tables, buses, rails, datasheet links — drop it into a firmware repo
      so a coding agent knows what's actually wired where.</span>
    </div>
    ${regions.map(([name, info]) => `
      <div class="panel-section overview-region" data-region="${esc(name)}">
        <h3>${esc(name)} <span class="hint">${counts[name] || 0} components</span></h3>
        <p style="font-size:12.5px; margin:0;">${esc(info.explanation)}</p>
      </div>`).join("")}
    ${unlabeledCount ? `
      <div class="panel-section">
        <h3>Unlabeled areas <span class="hint">${unlabeledCount} components</span></h3>
        <p style="font-size:12.5px; margin:0;" class="text-dim">This schematic draws no section title
        near these components, so no section is claimed for them — an honest gap, not an error.
        Use the "unlabeled" chip above the canvas to see them.</p>
      </div>` : ""}
    ${boardLinksHtml()}
    <p class="hint" style="padding: 0 4px;">Section summaries are AI-inferred from the identified parts within each region.</p>
  `;
}

function renderComponentPanel(c) {
  const identity = c.identity;
  const regionInfo = schematic.regions[c.region];

  let identityHtml = "";
  if (identity) {
    const confClass = identity.confidence < 0.6 ? "low" : "";
    const badgeClass = identity.likely_part_number ? "badge-verified" : "badge-inferred";
    const badgeText = identity.likely_part_number ? "AI-identified" : "AI-inferred";
    identityHtml = `
      <div class="panel-section">
        <h3>What it is</h3>
        <span class="badge ${badgeClass}">${badgeText}</span>
        <div class="fact-row"><span class="k">Part number</span><span class="v">${esc(identity.likely_part_number || "unknown")}</span></div>
        <div class="fact-row"><span class="k">Package</span><span class="v">${esc(identity.package_type || "unknown")}</span></div>
        <div class="fact-row"><span class="k">Manufacturer</span><span class="v">${esc(identity.manufacturer || "unknown")}</span></div>
        <div class="fact-row"><span class="k">Confidence</span><span class="v">${Math.round(identity.confidence * 100)}%</span></div>
        <div class="confidence-bar"><div class="confidence-fill ${confClass}" style="width:${identity.confidence * 100}%"></div></div>
        ${c.pin_count_mismatch ? `
        <div class="mismatch-warning">⚠ The claimed package doesn't match the ${c.pin_count} pins
        parsed from the schematic — mechanically checked, worth verifying before trusting.</div>` : ""}
        <p style="font-size:13px; margin-top:10px;">${esc(identity.function)}</p>
        <details class="reasoning-details">
          <summary>Why the AI thinks so</summary>
          <div class="reasoning-block">${esc(identity.reasoning)}</div>
        </details>
        <p class="hint">AI guess grounded in visible schematic text — check against the real part:</p>
        <div class="link-row">
          ${Object.entries(c.lookup_links).map(([name, url]) =>
            `<a class="link-btn" href="${esc(url)}" target="_blank" rel="noopener">${esc(name.replace("_", " "))}</a>`
          ).join("")}
        </div>
      </div>`;
  }

  let datasheetHtml = "";
  if (c.datasheet) {
    const ds = c.datasheet;
    datasheetHtml = `
      <div class="panel-section">
        <h3>From the datasheet</h3>
        <p style="font-size:13px;">${esc(ds.summary)}</p>
        ${ds.facts.map((f) => `
          <div class="ds-fact">
            <div class="ds-fact-text">${esc(f.fact)}</div>
            <div class="ds-quote">“${esc(f.quote)}” <span class="ds-cite">p.${f.page} <span class="hash-ok">✓ verified</span></span></div>
          </div>`).join("")}
        ${ds.facts_dropped_unverified ? `<p class="hint">${ds.facts_dropped_unverified} extracted fact(s) failed quote verification and were dropped.</p>` : ""}
        <p class="hint">Quotes are checked mechanically against the fetched PDF — facts that fail are never shown.</p>
        <div class="link-row">
          <a class="link-btn" href="${esc(ds.url)}" target="_blank" rel="noopener">open datasheet (${esc(ds.source.replace("web-search:", ""))})</a>
        </div>
      </div>`;
  }

  const signal = c.nets.filter((n) => !n.is_power);
  const power = c.nets.filter((n) => n.is_power);

  const signalHtml = signal.map((n) => {
    const peers = schematic.nets[n.key].members.filter((m) => m !== c.ref_token);
    const color = netColorMap[n.key] || "#3fb950";
    return `
      <div class="net-row" data-hovernet="${esc(n.key)}">
        <div class="net-name mono" data-net="${esc(n.key)}">
          <span class="net-dot" style="background:${color}"></span>${esc(n.names.join(" / "))}
        </div>
        <div class="peer-chips">${peers.map(compChip).join("") || '<span class="hint">no other components</span>'}</div>
      </div>`;
  }).join("");

  const powerHtml = power.map((n) =>
    `<div class="net-item" data-net="${esc(n.key)}">
       <span class="mono">${esc(n.names.join(" / "))}</span>
       <span class="peers">power rail · ${n.peer_count} others</span>
     </div>`
  ).join("");

  let regionHtml = "";
  if (regionInfo) {
    regionHtml = `
      <div class="panel-section">
        <h3>Section: ${esc(c.region)}</h3>
        <p style="font-size:13px;">${esc(regionInfo.explanation)}</p>
        <span class="hint">AI-inferred, confidence ${Math.round(regionInfo.confidence * 100)}%</span>
      </div>`;
  }

  panel.innerHTML = `
    <div class="panel-section">
      <div class="panel-title">${esc(c.display_label || c.encoded_refdes)}</div>
      <div class="panel-sub mono">${esc(c.encoded_refdes)} &middot; ${c.pin_count} pin${c.pin_count === 1 ? "" : "s"} &middot; ${esc(c.region || "unlabeled area")}</div>
      <span class="hint">Each wire color below matches a net on the canvas. Hover a net to isolate it.</span>
    </div>
    ${identityHtml}
    ${datasheetHtml}
    <div class="panel-section">
      <h3>Connected to (signal nets)</h3>
      ${signalHtml || '<span class="hint">only power connections</span>'}
    </div>
    <div class="panel-section">
      <h3>Power rails (click to trace anyway)</h3>
      ${powerHtml || '<span class="hint">none</span>'}
    </div>
    ${regionHtml}
  `;
}

function renderNetPanel(key) {
  const net = schematic.nets[key];
  panel.innerHTML = `
    <div class="panel-section">
      <div class="panel-title mono">${esc(net.names.join(" / "))}</div>
      <div class="panel-sub">${net.is_power ? "power rail" : "signal net"} &middot; ${net.pins.length} pins &middot; ${net.members.length} components</div>
      <span class="hint">Everything highlighted on the canvas is on this net.</span>
    </div>
    <div class="panel-section">
      <h3>Components on this net</h3>
      <div class="peer-chips">${net.members.map(compChip).join("")}</div>
    </div>
  `;
}

panel.addEventListener("click", (e) => {
  const proposeBtn = e.target.closest("#propose-mates-btn");
  if (proposeBtn) { proposeMates(proposeBtn); return; }
  const mateBtn = e.target.closest("[data-mate]");
  if (mateBtn) { decideMate(mateBtn.dataset.mate, mateBtn.dataset.decision); return; }
  const regionEl = e.target.closest("[data-region]");
  if (regionEl) {
    const name = regionEl.dataset.region;
    const b = schematic.regions[name] && schematic.regions[name].bounds;
    if (b) flyTo({ x0: b.x0 - 15, y0: b.y0 - 15, x1: b.x1 + 15, y1: b.y1 + 15 });
    return;
  }
  const compEl = e.target.closest("[data-comp]");
  if (compEl) { selectComponent(compEl.dataset.comp, { fly: true }); return; }
  const netEl = e.target.closest("[data-net]");
  if (netEl) selectNet(netEl.dataset.net);
});

panel.addEventListener("mouseover", (e) => {
  const row = e.target.closest("[data-hovernet]");
  const next = row ? row.dataset.hovernet : null;
  if (next !== hoverNet) { hoverNet = next; updateHighlights(); }
});
panel.addEventListener("mouseleave", () => {
  if (hoverNet) { hoverNet = null; updateHighlights(); }
});

// ---- multi-board workspace ----
function renderBoardSwitcher() {
  const el = document.getElementById("board-switcher");
  if (!el || workspaceInfo.boards.length < 2) return;
  el.innerHTML = workspaceInfo.boards.map((b) =>
    `<span class="board-chip ${b.active ? "active" : ""}" data-board="${esc(b.id)}">${esc(b.name)}</span>`
  ).join("");
  el.querySelectorAll("[data-board]").forEach((chip) => {
    chip.onclick = async () => {
      await fetch("/api/workspace/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ board_id: chip.dataset.board }),
      });
      location.href = "/";  // full reload: new payload, new canvas
    };
  });
}

async function decideMate(mateId, status) {
  await fetch(`/api/mates/${mateId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  workspaceInfo = await (await fetch("/api/workspace")).json();
  renderOverview();
}

async function proposeMates(btn) {
  btn.textContent = "Analyzing connectors… (~30s)";
  btn.disabled = true;
  try {
    const res = await fetch("/api/workspace/propose-mates", { method: "POST" });
    const data = await res.json();
    workspaceInfo = await (await fetch("/api/workspace")).json();
    renderOverview();
    if (data.error) alert(data.error);
  } catch {
    btn.textContent = "Propose board links (AI)";
    btn.disabled = false;
  }
}

function boardLinksHtml() {
  if (workspaceInfo.boards.length < 2) return "";
  const mates = workspaceInfo.mates;
  const cards = mates.map((m) => {
    const pins = m.pin_matches.map((pm) =>
      `<tr><td class="mono">${esc(m.board_a_connector)}.${esc(pm.a_pin)}</td><td class="mono">${esc(pm.a_net)}</td>
       <td>↔</td><td class="mono">${esc(m.board_b_connector)}.${esc(pm.b_pin)}</td><td class="mono">${esc(pm.b_net)}</td></tr>`
    ).join("");
    const statusBadge = m.status === "confirmed"
      ? '<span class="badge badge-verified">confirmed — traversable</span>'
      : m.status === "rejected"
        ? '<span class="badge">rejected</span>'
        : '<span class="badge badge-inferred">AI-proposed — needs your call</span>';
    const buttons = m.status === "proposed"
      ? `<div class="label-buttons" style="margin-top:8px;">
           <button class="btn btn-good" data-mate="${esc(m.id)}" data-decision="confirmed">Confirm link</button>
           <button class="btn btn-bad" data-mate="${esc(m.id)}" data-decision="rejected">Reject</button>
         </div>`
      : "";
    return `
      <div class="panel-section mate-card ${m.status}">
        <h3>${esc(m.board_a_name)}:${esc(m.board_a_connector)} ↔ ${esc(m.board_b_name)}:${esc(m.board_b_connector)}</h3>
        ${statusBadge}
        <p style="font-size:12.5px; margin:8px 0 6px;">${esc(m.rationale)}</p>
        <table class="claim-facts"><tbody>${pins}</tbody></table>
        ${buttons}
      </div>`;
  }).join("");
  return `
    <div class="panel-section">
      <h3>Board-to-board links</h3>
      <p class="hint" style="margin-bottom:8px;">Connector matings aren't in the schematics — they're
      physical harness decisions. The AI proposes candidates with pin-level evidence (each one
      mechanically validated against the real connectors); only links YOU confirm become
      traversable in cross-board answers.</p>
      <button class="btn" id="propose-mates-btn">Propose board links (AI)</button>
    </div>
    ${cards}`;
}

// ---- region chips ----
function renderRegionChips() {
  const bar = document.getElementById("region-filter");
  bar.innerHTML = "";
  const regions = [...new Set(schematic.components.map((c) => c.region).filter(Boolean))].sort();
  const hasUnlabeled = schematic.components.some((c) => !c.region);
  const zoomToMembers = (members) => {
    const xs = members.flatMap((c) => [c.box.x0, c.box.x1]);
    const ys = members.flatMap((c) => [c.box.y0, c.box.y1]);
    flyTo({ x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) }, 200);
  };
  for (const region of regions) {
    const chip = document.createElement("span");
    chip.className = "region-chip";
    chip.textContent = region;
    chip.onclick = () => zoomToMembers(schematic.components.filter((c) => c.region === region));
    bar.appendChild(chip);
  }
  if (hasUnlabeled) {
    const chip = document.createElement("span");
    chip.className = "region-chip chip-unlabeled";
    chip.textContent = "unlabeled";
    chip.title = "Components in areas where this schematic draws no section title";
    chip.onclick = () => zoomToMembers(schematic.components.filter((c) => !c.region));
    bar.appendChild(chip);
  }
}

// ---- chat: grounded Q&A over the connection graph ----
// Session id + transcript persist in localStorage: the server keeps the
// conversation memory keyed by session id, so surviving a reload (board
// switches are reloads) keeps both the visible transcript AND the agent's
// memory of it.
let chatSessionId = stGet("chat.sessionId", null);
let chatLog = stGet("chat.log", []);  // [{cls, html}], final messages only
const CHAT_LOG_MAX = 40;
let chatBusy = false;

const chatMessages = document.getElementById("chat-messages");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const chatSend = document.getElementById("chat-send");

const chatDrawer = document.getElementById("chat-drawer");
if (stGet("chat.collapsed", false)) chatDrawer.classList.add("collapsed");
document.getElementById("chat-toggle").addEventListener("click", () => {
  chatDrawer.classList.toggle("collapsed");
  stSet("chat.collapsed", chatDrawer.classList.contains("collapsed"));
});

function addChatMsg(cls, html, persist = false) {
  const div = document.createElement("div");
  div.className = `chat-msg ${cls}`;
  div.innerHTML = html;
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  if (persist) {
    chatLog.push({ cls, html });
    if (chatLog.length > CHAT_LOG_MAX) chatLog = chatLog.slice(-CHAT_LOG_MAX);
    stSet("chat.log", chatLog);
  }
  return div;
}

// restore transcript: localStorage is only a display cache -- the system
// of record is the server's persistent memory (SQLite), so an empty cache
// with a known session rebuilds from /api/chat/history (survives cleared
// browser data and server restarts alike)
function renderChatEntry(entry) {
  const div = document.createElement("div");
  div.className = `chat-msg ${entry.cls}`;
  div.innerHTML = entry.html;
  chatMessages.appendChild(div);
}
async function restoreTranscript() {
  if (chatLog.length) {
    chatLog.forEach(renderChatEntry);
  } else if (chatSessionId) {
    try {
      const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(chatSessionId)}`);
      const data = await res.json();
      for (const ex of data.exchanges || []) {
        const q = { cls: "chat-msg-user", html: esc(ex.question) };
        let botHtml = esc(ex.answer);
        if (ex.tool_trace) botHtml += `<div class="chat-provenance">checked: ${esc(ex.tool_trace)}</div>`;
        const a = { cls: "chat-msg-bot", html: botHtml };
        chatLog.push(q, a);
        renderChatEntry(q); renderChatEntry(a);
      }
      if (chatLog.length) stSet("chat.log", chatLog);
    } catch { /* history unavailable -- start fresh */ }
  }
  if (chatLog.length) chatMessages.scrollTop = chatMessages.scrollHeight;
}
restoreTranscript();

function resolveRef(ref) {
  const rl = String(ref).trim().toLowerCase();
  return schematic.components.find(
    (c) => (c.display_label || "").toLowerCase() === rl
        || c.encoded_refdes.toLowerCase() === rl
        || c.ref_token.toLowerCase() === rl
  );
}

function applyChatCanvasCommands(reply) {
  // trace_net wins (it already highlights all its members); otherwise
  // highlight the referenced components using the selection machinery
  if (reply.trace_net) {
    const key = Object.keys(schematic.nets).find(
      (k) => k === reply.trace_net || schematic.nets[k].names.includes(reply.trace_net)
    );
    if (key) selectNet(key, { pushUrl: false });
  } else if (reply.highlight_refs && reply.highlight_refs.length === 1) {
    const c = resolveRef(reply.highlight_refs[0]);
    if (c) selectComponent(c.ref_token, { pushUrl: false, fly: !!reply.fly_to });
  } else if (reply.highlight_refs && reply.highlight_refs.length > 1) {
    // multi-part answer: select the first, which lights its peers -- then
    // if a fly_to is named, go there instead
    const first = resolveRef(reply.highlight_refs[0]);
    if (first) selectComponent(first.ref_token, { pushUrl: false });
  }
  if (reply.fly_to) {
    const c = resolveRef(reply.fly_to);
    if (c) flyTo(c.box, 220);
  }
}

async function sendChat(message) {
  if (chatBusy || !message.trim()) return;
  chatBusy = true;
  chatSend.disabled = true;
  addChatMsg("chat-msg-user", esc(message), true);
  const thinking = addChatMsg("chat-msg-bot chat-thinking", "checking the graph…");
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: chatSessionId }),
    });
    const data = await res.json();
    thinking.remove();
    if (data.error) {
      addChatMsg("chat-msg-bot", esc(data.error));
      return;
    }
    chatSessionId = data.session_id;
    stSet("chat.sessionId", chatSessionId);
    let html = esc(data.answer);
    if (data.highlight_refs && data.highlight_refs.length) {
      html += "<div>" + data.highlight_refs.map((r) => {
        const c = resolveRef(r);
        return c ? `<span class="chip" data-comp="${esc(c.ref_token)}">${esc(r)}</span>` : "";
      }).join("") + "</div>";
    }
    if (data.tool_trace && data.tool_trace.length) {
      html += `<div class="chat-provenance">checked: ${data.tool_trace.map(esc).join(" · ")}</div>`;
    }
    addChatMsg("chat-msg-bot", html, true);
    applyChatCanvasCommands(data);
  } catch (err) {
    thinking.remove();
    addChatMsg("chat-msg-bot", "request failed — is the server still running?");
  } finally {
    chatBusy = false;
    chatSend.disabled = false;
  }
}

// component chips inside any chat message (including restored ones) jump
// to that part on the canvas
chatMessages.addEventListener("click", (e) => {
  const chipEl = e.target.closest("[data-comp]");
  if (chipEl) selectComponent(chipEl.dataset.comp, { pushUrl: false, fly: true });
});

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const message = chatInput.value;
  chatInput.value = "";
  sendChat(message);
});

init().then(() => {
  // ?ask= deep link: auto-sends one chat message on load (also the hook
  // automated end-to-end verification uses)
  const ask = new URLSearchParams(location.search).get("ask");
  if (ask) sendChat(ask);
});
