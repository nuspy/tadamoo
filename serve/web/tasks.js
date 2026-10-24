"use strict";
/* Agent tasks panel (Monitor view): what the agent is doing, from GET api/tasks. It polls only while
   the Monitor is on screen and only redraws when something changed. */
(function () {
  const POLL_MS = 3000;
  let last = "";
  let timer = null;

  function row(task) {
    const status = ["running", "done", "failed", "closed", "pending", "blocked"].includes(task.status) ? task.status : "pending";
    const el = document.createElement("li");
    el.className = `tk-row tk-${status}`;
    const head = document.createElement("div");
    head.className = "tk-head";
    const id = document.createElement("span");
    id.className = "tk-id";
    id.textContent = task.id;
    const badge = document.createElement("span");
    badge.className = "tk-badge";
    badge.textContent = T("tk.status." + status);
    head.append(id, badge);
    el.appendChild(head);
    if (task.result) {
      const res = document.createElement("div");
      res.className = "tk-result";
      res.textContent = task.result;
      el.appendChild(res);
    }
    if (Array.isArray(task.children) && task.children.length) {
      const kids = document.createElement("div");
      kids.className = "tk-kids";
      kids.textContent = T("tk.children", task.children.join(", "));
      el.appendChild(kids);
    }
    return el;
  }

  async function refresh() {
    const root = document.getElementById("task-root");
    if (!root) return;
    let data = null;
    try {
      const res = await fetch("api/tasks?t=" + Date.now(), { cache: "no-store", headers: authHeaders() });
      if (res.ok) data = await res.json();
    } catch (e) { /* offline: keep what is shown */ }
    if (!data) return;
    const sig = JSON.stringify(data.tasks || []);
    if (sig === last) return;
    last = sig;
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    if (!tasks.length) {
      const empty = document.createElement("p");
      empty.className = "tk-empty dim";
      empty.textContent = T("tk.empty");
      root.replaceChildren(empty);
      return;
    }
    const list = document.createElement("ul");
    list.className = "tk-list";
    tasks.forEach(t => list.appendChild(row(t)));
    root.replaceChildren(list);
  }

  function schedule() {
    clearInterval(timer);
    timer = null;
    if (document.body.dataset.view === "stats" && !document.hidden) {
      refresh();
      timer = setInterval(refresh, POLL_MS);
    }
  }

  document.addEventListener("basalt:view", schedule);
  document.addEventListener("visibilitychange", schedule);
  document.addEventListener("DOMContentLoaded", schedule);
})();
