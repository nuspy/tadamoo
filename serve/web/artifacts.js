/* Artifacts panel — renders generated code artifacts and files. */
(function () {
  "use strict";

  async function fetchChanges() {
    try {
      const res = await fetch("/api/changes?t=" + Date.now(), { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch {
      return null;
    }
  }

  async function renderArtifacts() {
    const root = document.getElementById("artifacts-list");
    if (!root) return;

    const data = await fetchChanges();
    if (!data || !Array.isArray(data.edits) || data.edits.length === 0) {
      root.replaceChildren();
      const empty = document.createElement("span");
      empty.className = "dim";
      empty.textContent = "Nessun artefatto generato";
      root.appendChild(empty);
      return;
    }

    root.replaceChildren();
    for (const edit of data.edits) {
      const item = document.createElement("div");
      item.className = "artifact-item";
      item.style.fontSize = "0.75em";
      item.style.padding = "2px 0";
      item.textContent = `📄 ${edit.file} (+${edit.added ?? 0}/-${edit.removed ?? 0})`;
      root.appendChild(item);
    }
  }

  if (typeof window !== "undefined") {
    window.basaltArtifacts = { renderArtifacts };
    document.addEventListener("DOMContentLoaded", () => {
      renderArtifacts();
      setInterval(renderArtifacts, 3000);
    });
  }
})();
