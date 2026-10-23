// Changes panel — displays agent code modifications GitHub-style.
//
// Additions: green (#e6ffec) with +
// Modifications: amber (#fff8c5) with ±
// Deletions: red (#ffebe9) with -
// Diff text renders at 2/3 base body font size.

(function () {
  const CHANGES_URL = "/web/changes.json";
  const POLL_INTERVAL_MS = 2500;

  const COLORS = {
    added: "#e6ffec",
    modified: "#fff8c5",
    deleted: "#ffebe9"
  };

  function determineKind(edit) {
    if (edit.kind) return edit.kind;
    if (edit.added > 0 && (!edit.removed || edit.removed === 0)) return "added";
    if (edit.removed > 0 && (!edit.added || edit.added === 0)) return "deleted";
    return "modified";
  }

  async function fetchChanges() {
    try {
      const res = await fetch(`${CHANGES_URL}?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch {
      return null;
    }
  }

  function renderRow(edit) {
    const kind = determineKind(edit);
    const color = COLORS[kind] || COLORS.modified;
    const sign = kind === "added" ? "+" : kind === "deleted" ? "-" : "±";

    const row = document.createElement("div");
    row.className = "change-row";
    row.style.background = color;
    row.style.padding = "4px 8px";
    row.style.margin = "4px 0";
    row.style.borderRadius = "4px";
    row.style.fontSize = "0.67em";
    row.style.fontFamily = "var(--font-mono, monospace)";
    row.style.border = "1px solid rgba(0,0,0,0.06)";

    const header = document.createElement("div");
    header.style.display = "flex";
    header.style.justifyContent = "space-between";
    header.style.alignItems = "center";

    const titleSpan = document.createElement("span");
    titleSpan.style.fontWeight = "600";
    titleSpan.textContent = `${sign} ${edit.file || "unknown"}`;

    const countSpan = document.createElement("span");
    countSpan.className = "change-count";
    countSpan.style.opacity = "0.75";
    countSpan.textContent = `+${edit.added ?? 0} −${edit.removed ?? 0}`;

    header.appendChild(titleSpan);
    header.appendChild(countSpan);
    row.appendChild(header);

    if (edit.diff) {
      const diffPre = document.createElement("pre");
      diffPre.style.margin = "4px 0 0 0";
      diffPre.style.whiteSpace = "pre-wrap";
      diffPre.style.fontSize = "0.9em";
      diffPre.style.maxHeight = "160px";
      diffPre.style.overflowY = "auto";
      diffPre.textContent = edit.diff;
      row.appendChild(diffPre);
    }

    return row;
  }

  async function refresh() {
    const root = document.getElementById("changes-root");
    if (!root) return;

    const data = await fetchChanges();
    if (!data || !Array.isArray(data.edits) || data.edits.length === 0) {
      root.replaceChildren();
      const emptyMsg = document.createElement("div");
      emptyMsg.className = "dim";
      emptyMsg.style.fontSize = "0.67em";
      emptyMsg.style.padding = "8px";
      emptyMsg.textContent = "Nessuna modifica recente registrata.";
      root.appendChild(emptyMsg);
      return;
    }

    root.replaceChildren();
    for (const edit of data.edits) {
      root.appendChild(renderRow(edit));
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    refresh();
    setInterval(refresh, POLL_INTERVAL_MS);
  });
})();
