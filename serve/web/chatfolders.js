"use strict";
/* Chat's own folders: a pure name, no code workspace (that is the Agentic coding tab's project, projects.js). The list the
   server keeps, added, renamed and removed straight from the Chat column - no window of its own, since there is no path to
   type or browse for. Everything shown enters the page as text. */
(function () {
  let folders = [];
  const listeners = [];

  async function api(method, tail, body) {
    try {
      const r = await fetch("api/agent/chat-folders" + tail, { method, cache: "no-store",
        headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let j = {};
      try { j = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json: j };
    } catch (e) { return { ok: false, status: 0, json: {} }; }
  }
  const errText = r => (r.json && r.json.error && r.json.error.message) || (r.status ? `HTTP ${r.status}` : T("ag.add.e.unreachable"));

  async function load() {
    const r = await api("GET", "");
    if (r.ok) folders = r.json.folders || [];
    return folders;
  }
  const changed = () => listeners.forEach(f => f(folders));

  async function add(name) {
    const r = await api("POST", "", { name });
    if (!r.ok) return { ok: false, message: errText(r) };
    await load();
    changed();
    return { ok: true, folder: r.json.folder };
  }
  async function rename(id, name) {
    const r = await api("POST", "/rename", { id, name });
    if (!r.ok) return { ok: false, message: errText(r) };
    await load();
    changed();
    return { ok: true, folder: r.json.folder };
  }
  async function remove(id) {
    const r = await api("POST", "/remove", { id });
    if (!r.ok) return { ok: false, message: errText(r) };
    await load();
    changed();
    return { ok: true, deleted_sessions: r.json.deleted_sessions || [] };
  }

  window.chatFolders = { load, list: () => folders, add, rename, remove, onChange: f => listeners.push(f) };
})();
