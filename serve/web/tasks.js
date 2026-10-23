// Tasks panel — renders agent tasks and execution status.
//
// Shows open, running, completed, and failed tasks with hierarchical dependencies.

(function () {
  const TASKS_URL = "/web/tasks.json";
  const POLL_INTERVAL_MS = 2500;

  async function fetchTasks() {
    try {
      const res = await fetch(`${TASKS_URL}?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch {
      return null;
    }
  }

  function renderTaskRow(task) {
    const row = document.createElement("div");
    row.className = `task-row task-${task.status || "pending"}`;
    row.style.padding = "6px 10px";
    row.style.margin = "4px 0";
    row.style.borderRadius = "4px";
    row.style.fontSize = "0.75em";
    row.style.background = task.status === "done" ? "rgba(46, 160, 67, 0.1)" :
                           task.status === "running" ? "rgba(9, 105, 218, 0.1)" :
                           task.status === "failed" ? "rgba(207, 34, 46, 0.1)" : "rgba(0, 0, 0, 0.04)";
    row.style.borderLeft = `4px solid ${
      task.status === "done" ? "#2ea043" :
      task.status === "running" ? "#0969da" :
      task.status === "failed" ? "#cf222e" : "#8c959f"
    }`;

    const titleLine = document.createElement("div");
    titleLine.style.display = "flex";
    titleLine.style.justifyContent = "space-between";
    titleLine.style.fontWeight = "600";

    const idSpan = document.createElement("span");
    idSpan.textContent = task.id;

    const statusBadge = document.createElement("span");
    statusBadge.textContent = (task.status || "idle").toUpperCase();
    statusBadge.style.fontSize = "0.85em";
    statusBadge.style.opacity = "0.8";

    titleLine.appendChild(idSpan);
    titleLine.appendChild(statusBadge);
    row.appendChild(titleLine);

    if (task.result) {
      const resDiv = document.createElement("div");
      resDiv.style.marginTop = "4px";
      resDiv.style.opacity = "0.85";
      resDiv.textContent = task.result;
      row.appendChild(resDiv);
    }

    return row;
  }

  async function refresh() {
    const root = document.getElementById("task-root");
    if (!root) return;

    const data = await fetchTasks();
    if (!data || !Array.isArray(data.tasks) || data.tasks.length === 0) {
      root.replaceChildren();
      const empty = document.createElement("div");
      empty.className = "dim";
      empty.style.fontSize = "0.75em";
      empty.style.padding = "8px";
      empty.textContent = "Nessun task attivo al momento.";
      root.appendChild(empty);
      return;
    }

    root.replaceChildren();
    for (const task of data.tasks) {
      root.appendChild(renderTaskRow(task));
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    refresh();
    setInterval(refresh, POLL_INTERVAL_MS);
  });
})();
