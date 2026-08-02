// Project switcher: fetches /api/projects and renders chips into the
// header, same visual pattern as canvas.js's board switcher. Kept separate
// from canvas.js on purpose -- it must work on an empty project's page too,
// which loads none of canvas.js's schematic-specific setup. Wrapped in an
// IIFE so its local `esc` doesn't collide with canvas.js's own when both
// scripts are on the same (non-empty-project) page.
(function () {
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  async function render() {
    const el = document.getElementById("project-switcher");
    if (!el) return;
    let data;
    try {
      data = await (await fetch("/api/projects")).json();
    } catch {
      return; // project list is a nice-to-have; don't break the page over it
    }
    const chips = data.projects
      .map((p) => `<span class="board-chip${p.id === PROJECT_ID ? " active" : ""}" data-project="${esc(p.id)}">${esc(p.name)}</span>`)
      .join("");
    el.innerHTML = chips + `<span class="board-chip" id="new-project-chip" title="Create a new project">+ New</span>`;
    el.querySelectorAll("[data-project]").forEach((chip) => {
      chip.onclick = () => {
        if (chip.dataset.project !== PROJECT_ID) location.href = "/p/" + chip.dataset.project + "/";
      };
    });
    const newBtn = document.getElementById("new-project-chip");
    newBtn.onclick = async () => {
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const project = await res.json();
      location.href = "/p/" + project.id + "/";
    };
  }

  render();
})();
