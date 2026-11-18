"use strict";
/* The book icon of the composer (Chat and Agentic coding share it): a dialog that attaches or drops one or more knowledge archives of this
   conversation, filtered by project. The archive's own id is sent (never its name), so there is never a guess between two archives called the
   same. Everything the server returns enters the page as text. */
(() => {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };

  async function api(path) {
    try {
      const r = await fetch(path, { cache: "no-store", headers: authHeaders() });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }

  async function open(chatApi) {
    if (!chatApi) return;
    const [cat, projects] = await Promise.all([api("api/rag/catalog"), api("api/rag/projects")]);
    const archives = (cat && cat.archives) || [];
    const attached = new Set(chatApi.knowledgeIds());
    const picked = new Set(attached);

    const dlg = el("dialog", "rg-dlg kp-dlg");
    const filter = el("select");
    const all = el("option", "", T("kp.allprojects")); all.value = ""; filter.append(all);
    ((projects && projects.projects) || []).forEach(p => { const o = el("option", "", p.chat ? T("rg.proj.chat") : p.name); o.value = p.key; filter.append(o); });
    const list = el("div", "kp-list");
    const note = el("p", "rg-help", archives.length ? "" : T("kp.none"));

    function row(a) {
      const li = el("label", "kp-row"), box = el("input"); box.type = "checkbox";
      box.checked = picked.has(a.id);
      box.onchange = () => { if (box.checked) picked.add(a.id); else picked.delete(a.id); };
      li.append(box, el("span", "kp-name", a.name), el("span", "kp-project", a.project_name || a.project || ""));
      return li;
    }
    function drawList() {
      const want = filter.value;
      list.replaceChildren(...archives.filter(a => !want || a.project === want).map(row));
    }
    filter.onchange = drawList;
    drawList();

    const status = el("p", "rg-note"); status.setAttribute("role", "status"); status.hidden = true;
    const save = btn(T("kp.save"), "primary", async () => {
      save.disabled = cancel.disabled = true; status.hidden = false; status.textContent = T("kp.saving");
      for (const id of picked) if (!attached.has(id)) await chatApi.knowledge("attach", id);
      for (const id of attached) if (!picked.has(id)) await chatApi.knowledge("detach", id);
      dlg.close();
    });
    const cancel = btn(T("rg.cancel"), "", () => dlg.close());
    dlg.append(el("h3", "rg-h2", T("kp.title")), el("p", "rg-help", T("kp.help")), filter, note, list, status, save, cancel);
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  }

  window.KnowledgeAttach = { open };
})();
