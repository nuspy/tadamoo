"use strict";
/* The folders of the Agent tab: the list the server keeps, and the window that adds one. A folder is typed as a full path,
   or picked with the Windows dialog inside the desktop app. Adding it limits what the agent does there to that folder and
   gives the folder a `.basalt` registry of its own. Everything shown enters the page as text. */
(function () {
  const dlg = document.getElementById("afolder");
  const form = document.getElementById("af-form"), path = document.getElementById("af-path");
  const msg = document.getElementById("af-msg"), recent = document.getElementById("af-recent"), browse = document.getElementById("af-browse");
  const app = window.chrome && window.chrome.webview;
  let projects = [];
  const listeners = [];

  async function api(method, tail, body) {
    try {
      const r = await fetch("api/agent/projects" + tail, { method, cache: "no-store",
        headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let j = {};
      try { j = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json: j };
    } catch (e) { return { ok: false, status: 0, json: {} }; }
  }
  const errText = r => {
    const e = r.json && r.json.error;
    const known = e && e.code && ["folder_missing", "folder_relative", "folder_not_found", "folder_readonly"].includes(e.code);
    return known ? T("ag.add.e." + e.code) : (e && e.message) || `HTTP ${r.status}`;
  };

  async function load() {
    const r = await api("GET", "");
    if (r.ok) projects = r.json.projects || [];
    return projects;
  }
  const changed = () => listeners.forEach(f => f(projects));

  async function add(value) {
    const r = await api("POST", "", { path: value });
    if (!r.ok) return { ok: false, message: errText(r) };
    await load();
    changed();
    return { ok: true, project: r.json.project };
  }
  async function remove(value) {
    const r = await api("POST", "/remove", { path: value });
    if (!r.ok) return false;
    await load();
    changed();
    return true;
  }

  /* ---- the window ---- */
  let done = null;
  async function open() {
    msg.textContent = ""; msg.classList.remove("bad");
    path.value = "";
    await load();
    recent.replaceChildren(...projects.map(p => { const o = document.createElement("option"); o.value = p.path; return o; }));
    browse.hidden = !app;
    dlg.showModal();
    path.focus();
    return new Promise(resolve => { done = resolve; });
  }
  function close(result) {
    if (dlg.open) dlg.close();
    if (done) { const d = done; done = null; d(result || null); }
  }
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const value = path.value.trim();
    if (!value) { msg.textContent = T("ag.add.e.folder_missing"); msg.classList.add("bad"); return; }
    const r = await add(value);
    if (!r.ok) { msg.textContent = r.message; msg.classList.add("bad"); return; }
    close(r.project);
  });
  document.getElementById("af-close").addEventListener("click", () => close(null));
  dlg.addEventListener("cancel", () => close(null));
  browse.addEventListener("click", () => { if (app) app.postMessage({ type: "pickFolder", id: "af-path", path: path.value.trim(), title: T("ag.add.title") }); });
  applyT(dlg);

  window.agentProjects = { load, list: () => projects, add, remove, open, onChange: f => listeners.push(f) };
})();
