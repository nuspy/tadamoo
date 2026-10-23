/* DAG viewer panel — renders the tool-call graph and node transitions. */
(function () {
  "use strict";

  async function fetchTasks() {
    try {
      const res = await fetch("/api/tasks?t=" + Date.now(), { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch {
      return null;
    }
  }

  async function renderDAG() {
    const root = document.getElementById("dag-nodes");
    if (!root) return;

    const data = await fetchTasks();
    if (!data || !Array.isArray(data.tasks) || data.tasks.length === 0) {
      root.replaceChildren();
      const empty = document.createElement("span");
      empty.className = "dim";
      empty.textContent = "Nessun nodo attivo nel DAG";
      root.appendChild(empty);
      return;
    }

    root.replaceChildren();
    data.tasks.forEach((task, idx) => {
      const node = document.createElement("span");
      node.className = `dag-node dag-${task.status || "pending"}`;
      node.style.padding = "2px 6px";
      node.style.borderRadius = "3px";
      node.style.fontSize = "0.75em";
      node.style.background = task.status === "done" ? "#dafbe1" : "#ddf4ff";
      node.style.border = "1px solid rgba(0,0,0,0.1)";
      node.textContent = task.id;
      root.appendChild(node);

      if (idx < data.tasks.length - 1) {
        const edge = document.createElement("span");
        edge.style.margin = "0 4px";
        edge.style.opacity = "0.5";
        edge.textContent = "→";
        root.appendChild(edge);
      }
    });
  }

  if (typeof window !== "undefined") {
    window.basaltDAG = { renderDAG };
    document.addEventListener("DOMContentLoaded", () => {
      renderDAG();
      setInterval(renderDAG, 3000);
    });
  }
})();
