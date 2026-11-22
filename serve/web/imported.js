"use strict";
/* Imported: skills, plugins, MCP servers and plans. TaDaMoo works on COPIES it keeps in its own folder; the original is read once, when it is imported
   or re-imported, and never changed. This is the list of what was imported (Settings > Capabilities, and the Import plan button of the Plans tab), with
   Import (a path, a Verify step that shows what will be copied), Re-import (what changed, replaced only on confirmation) and Delete (removes the copy).
   Everything the server says is put in the page as text. */
(() => {
  const KINDS = ["skill", "plugin", "mcp", "plan"];
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = fn; return b; };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => (r.status === 0 ? T("im.e.offline") : ((r.data.error || {}).message || `HTTP ${r.status}`));
  const kb = n => (n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const when = iso => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString(LANG); };
  const tell = text => document.dispatchEvent(new CustomEvent("tadamoo:imported", { detail: text }));

  /* ---- a dialog: title, a body that the caller fills, buttons ---- */
  function dialog(title, onClose) {
    const dlg = h("dialog", "im-dialog");
    const head = h("h2", "", title); head.id = "im-title-" + Math.random().toString(36).slice(2, 7);
    dlg.setAttribute("aria-labelledby", head.id);
    const content = h("div", "im-body"), actions = h("div", "im-actions"), status = h("p", "setnote im-status"); status.setAttribute("role", "status");
    dlg.append(h("div", "im-box", head, content, status, actions));
    const close = () => { if (dlg.open) dlg.close(); dlg.remove(); if (onClose) onClose(); };
    dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
    document.body.append(dlg);
    dlg.showModal();
    return { dlg, content, actions, status, close, say: (t, bad) => { status.textContent = t || ""; status.dataset.bad = bad ? "1" : ""; } };
  }
  const cancelBtn = d => btn(T("im.cancel"), "ghost", d.close);

  /* ---- Import ---- */
  function summaryOf(kind, check) {
    const s = check.snapshot, box = h("div", "im-sum");
    const dl = h("dl", "im-dl");
    const row = (k, v) => { if (v) dl.append(h("dt", "", k), h("dd", "", v)); };
    row(T("im.f.name"), s.name);
    row(T("im.f.version"), s.version);
    row(T("im.f.what"), s.description);
    row(T("im.f.from"), s.source);
    if (s.files) row(T("im.f.copies"), T("im.f.files", s.files, kb(s.bytes)));
    if (s.steps) row(T("im.f.steps"), String(s.steps));
    box.append(dl);
    if (s.file_list && s.file_list.length) box.append(h("details", "im-files", h("summary", "", T("im.f.list")), h("ul", "", s.file_list.map(f => h("li", "", f)))));
    if (s.skipped && s.skipped.length) box.append(h("p", "setnote", T("im.f.skipped", s.skipped.length)));
    if (check.parts) box.append(h("p", "setnote", h("b", "", T("im.f.parts") + ": "), check.parts.map(p => `${T("im.kind1." + p.kind)} ${p.name} (${T("im.part." + p.state)})`).join(", ")));
    if (kind === "mcp") {
      box.append(riskLabel(T("im.mcp.runs", s.runs)));
      box.append(h("p", "setnote", T(s.remote ? "im.mcp.remote" : "im.mcp.off")));
      if (s.needs_keys && s.needs_keys.length) box.append(h("p", "setnote", T("im.mcp.keys", s.needs_keys.map(k => k.entry).join(", "))));
    }
    (s.warnings || []).forEach(w => box.append(h("p", "setnote warnnote", w)));
    return box;
  }

  function importDialog(kind, done) {
    const d = dialog(T("im.imp.title." + kind));
    let verified = null, wanted = null;
    const path = h("input"); path.type = "text"; path.id = "im-path"; path.autocomplete = "off"; path.spellcheck = false; path.placeholder = T("im.ph." + kind);
    const server = h("select"); server.id = "im-server"; server.hidden = true; server.setAttribute("aria-label", T("im.f.server"));
    const serverLabel = h("label", "im-field", h("span", "", T("im.f.server")), server); serverLabel.hidden = true;
    const result = h("div", "im-result"), deps = h("div", "im-deps");
    const go = btn(T("im.imp.go"), "primary", async () => {
      if (!verified) return;
      go.disabled = true; d.say(T("im.working"));
      const r = await call("POST", `api/import/${kind}/import`, { path: path.value.trim(), server: server.value || "", with_dependencies: !!(wanted && wanted.checked) });
      if (r.ok) { tell(T("im.imp.done", r.data.imported.name)); d.close(); if (done) await done(); } else { d.say(why(r), true); go.disabled = false; }
    });
    go.disabled = true;
    const form = h("form", "im-form");
    const verify = h("button", "btn", T("im.verify")); verify.type = "submit";
    form.append(h("label", "im-field", h("span", "", T(`im.path.${kind}`)), path), serverLabel, h("div", "im-actions", verify));
    form.onsubmit = async e => {
      e.preventDefault();
      verified = null; go.disabled = true; result.replaceChildren(); deps.replaceChildren();
      if (!path.value.trim()) { d.say(T("im.e.nopath"), true); return; }
      d.say(T("im.working"));
      const r = await call("POST", `api/import/${kind}/verify`, { path: path.value.trim(), server: server.value || "" });
      if (!r.ok) {
        const e2 = r.data.error || {};
        if (e2.code === "choose_server" && (e2.servers || []).length) {
          server.replaceChildren(...e2.servers.map(s => Object.assign(h("option", "", s), { value: s })));
          server.hidden = serverLabel.hidden = false;
          d.say(T("im.mcp.choose"));
          server.focus();
        } else d.say(why(r), true);
        return;
      }
      d.say("");
      const check = r.data;
      result.replaceChildren(summaryOf(kind, check));
      if (check.already_imported) { d.say(T("im.already", check.snapshot.name), true); return; }
      if (check.blockers.length) { d.say(check.blockers.map(b => b.message).join(" "), true); return; }
      const missing = (check.snapshot.dependencies || []);
      if (missing.length) {
        deps.append(h("p", "setnote", h("b", "", T("im.dep.title") + ": ")), h("ul", "im-deplist", missing.map(x =>
          h("li", "", `${x.name} ${x.imported ? T("im.dep.have") : x.exists ? T("im.dep.can") : T("im.dep.none")}${x.why ? " - " + x.why : ""}`))));
        const can = missing.filter(x => x.exists && !x.imported);
        if (can.length) {
          wanted = h("input"); wanted.type = "checkbox"; wanted.checked = true;
          deps.append(h("label", "im-check", wanted, h("span", "", T("im.dep.bring", can.length))));
        }
      }
      verified = check; go.disabled = false; go.focus();
    };
    d.content.append(h("p", "setnote", T("im.imp.help." + kind)), form, result, deps);
    d.actions.append(cancelBtn(d), go);
    path.focus();
    return d;
  }

  /* ---- Re-import ---- */
  const diffLines = diff => [
    ...(diff.added || []).map(f => h("li", "im-add", "+ " + f)),
    ...(diff.removed || []).map(f => h("li", "im-del", "- " + f)),
    ...(diff.changed || []).map(c => h("li", "im-chg", "~ " + c.file + (c.plus != null ? ` (+${c.plus} -${c.minus})` : "")))];

  async function reimportDialog(kind, it, done) {
    if (kind !== "plan") {                                           // a skill, plugin or server: the update keeps what the person changed (updatemerge.js)
      UpdateMerge.open({ title: it.name, errText: why,
        load: instructions => call("POST", `api/import/${kind}/update-plan`, { name: it.name, instructions, language: LANG }),
        apply: body => call("POST", `api/import/${kind}/update-apply`, body),
        notes: { set: notes => call("POST", `api/import/${kind}/update-notes`, { name: it.name, notes }) },
        done: async text => { tell(text); if (done) await done(); } });
      return;
    }
    const d = dialog(T("im.re.title", it.name));
    d.content.append(h("p", "setnote", T("im.re.looking", it.source)));
    d.actions.append(cancelBtn(d));
    const r = await call("POST", `api/import/${kind}/reimport`, { name: it.name });
    if (!r.ok) { d.content.replaceChildren(h("p", "setnote warnnote", why(r))); d.actions.replaceChildren(btn(T("im.close"), "", d.close)); return; }
    const p = r.data, counts = p.diff.counts, any = counts.added + counts.removed + counts.changed > 0;
    const box = h("div", "im-sum", h("p", "setnote", T("im.re.from", p.source)));
    let ow = null, deps = null;
    if (!p.source_changed && !p.local_edited) {
      box.append(h("p", "", T("im.re.same")));
      d.content.replaceChildren(box); d.actions.replaceChildren(btn(T("im.close"), "", d.close)); return;
    }
    box.append(h("p", "", any ? T("im.re.counts", counts.added, counts.removed, counts.changed) : T("im.re.nofiles")), h("ul", "im-difflist", diffLines(p.diff)));
    (p.parts || []).forEach(x => box.append(h("p", "setnote", `${x.key}: ${T("im.part." + x.state)}`)));
    if ((p.parts_removed || []).length) box.append(h("p", "setnote warnnote", T("im.re.gone", p.parts_removed.join(", "))));
    if (kind === "plan") box.append(h("p", "setnote", T("im.re.plankeeps")), p.ran ? h("p", "setnote warnnote", T("im.re.planran")) : null);
    if (kind === "mcp") box.append(h("p", "setnote", T("im.mcp.runs", p.now_runs)), p.turns_off ? h("p", "setnote warnnote", T("im.re.turnsoff")) : null);
    const can = (p.dependencies || []).filter(x => x.exists && !x.imported);
    if (can.length) { deps = h("input"); deps.type = "checkbox"; deps.checked = true; box.append(h("label", "im-check", deps, h("span", "", T("im.dep.bring", can.length)))); }
    if (p.local_edited) {
      box.append(h("p", "setnote warnnote", T("im.re.local")));
      ow = h("input"); ow.type = "checkbox";
      box.append(h("label", "im-check", ow, h("span", "", T("im.re.overwrite"))));
    }
    const go = btn(T("im.re.go"), "primary", async () => {
      go.disabled = true; d.say(T("im.working"));
      const x = await call("POST", `api/import/${kind}/reimport`, { name: it.name, confirm: true, overwrite_local: !!(ow && ow.checked), with_dependencies: !!(deps && deps.checked) });
      if (x.ok) { tell(T("im.re.done", it.name)); d.close(); if (done) await done(); } else { d.say(why(x), true); go.disabled = false; }
    });
    if (ow) { go.disabled = true; ow.onchange = () => { go.disabled = !ow.checked; }; }
    d.content.replaceChildren(box);
    d.actions.replaceChildren(cancelBtn(d), go);
  }

  /* ---- Delete ---- */
  function deleteDialog(kind, it, done) {
    const d = dialog(T("im.del.title", it.name));
    d.content.append(h("p", "", T("im.del.what", it.name)));
    if (kind === "plugin" && (it.parts || []).length) d.content.append(h("p", "setnote", T("im.del.parts", it.parts.join(", "))));
    if (kind === "skill" && (it.forms || []).includes("personal")) d.content.append(h("p", "setnote", T("im.del.personal")));
    if (kind === "plan") d.content.append(h("p", "setnote", T("im.del.plan")));
    d.content.append(h("p", "setnote im-safe", T("im.del.source", it.source)));
    const go = btn(T("im.del.go"), "primary im-danger", async () => {
      go.disabled = true;
      const r = await call("POST", `api/import/${kind}/delete`, { name: it.name });
      if (r.ok) { tell(T("im.del.done", it.name)); d.close(); if (done) await done(); } else { d.say(why(r), true); go.disabled = false; }
    });
    d.actions.append(cancelBtn(d), go);
    d.actions.querySelector(".btn").focus();
  }

  window.ImportedUI = { importDialog };

  /* ---- the list in Settings ---- */
  const body = document.getElementById("st-imp-body");
  if (!body) return;
  let state = null, note = "", noteBad = false;
  const say = (t, bad) => { note = t; noteBad = !!bad; const n = body.querySelector(".im-note"); if (n) { n.textContent = note; n.dataset.bad = noteBad ? "1" : ""; n.hidden = !note; } };
  document.addEventListener("tadamoo:imported", e => say(e.detail));

  async function load() {
    const r = await call("GET", "api/import");
    if (r.ok) { state = r.data; draw(); } else if (!state) body.replaceChildren(h("p", "setnote warnnote", why(r)));
  }
  const reload = async () => { await load(); document.dispatchEvent(new CustomEvent("tadamoo:capabilities-changed")); };

  function itemRow(kind, it) {
    const li = h("li", "im-item");
    const badges = [it.version ? T("im.b.version", it.version) : null, it.parent ? T("im.b.part", it.parent.replace(/^plugin:/, "")) : null,
      kind === "mcp" ? T(it.enabled ? "im.b.on" : "im.b.off") : null, it.local_edited ? T("im.b.edited") : null, !it.source_exists ? T("im.b.nosource") : null,
      kind === "plan" && it.ran ? T("im.b.ran") : null, kind === "plan" && it.archived ? T("im.b.archived") : null].filter(Boolean);
    li.append(h("div", "im-head", h("b", "im-name", it.name), badges.map(b => h("span", "cp-badge", b))));
    if (it.description) li.append(h("p", "dim im-desc", it.description));
    li.append(h("p", "setnote im-meta", T("im.m.from", it.source)), h("p", "setnote im-meta", T("im.m.when", when(it.imported_at), (it.hash || "").slice(0, 12))));
    if (kind === "mcp" && it.runs) li.append(h("p", "setnote im-meta", T("im.m.runs", it.runs)));
    if (kind === "mcp" && (it.needs_keys || []).length) li.append(h("p", "setnote im-meta", T("im.mcp.keys", it.needs_keys.map(k => k.entry).join(", "))));
    if (kind === "plugin" && (it.parts || []).length) li.append(h("p", "setnote im-meta", T("im.m.parts", it.parts.join(", "))));
    if (kind === "plan" && it.adopted === "differs") li.append(h("p", "setnote im-meta", T("im.m.adopted")));
    const acts = h("div", "im-acts");
    if (it.parent) acts.append(h("span", "setnote", T("im.m.partnote", it.parent.replace(/^plugin:/, ""))));
    else {
      if (kind === "mcp") acts.append(btn(T(it.enabled ? "im.turnoff" : "im.turnon"), it.enabled ? "ghost" : "primary", async () => {
        const r = await call("POST", "api/import/mcp/enable", { name: it.name, enabled: !it.enabled });
        if (r.ok) { say(T(r.data.enabled ? "im.turnedon" : "im.turnedoff", it.name)); await reload(); } else say(why(r), true);
      }));
      acts.append(btn(T("im.reimport"), "", () => reimportDialog(kind, it, reload)), btn(T("im.delete"), "ghost", () => deleteDialog(kind, it, reload)));
    }
    li.append(acts);
    return li;
  }

  function section(kind) {
    const items = state.kinds[kind] || [];
    const sec = h("section", "im-kind");
    sec.append(h("div", "im-kindhead", h("h4", "", `${T("im.kind." + kind)} (${items.length})`), btn(T("im.imp.btn." + kind), "primary", () => importDialog(kind, reload))));
    sec.append(items.length ? h("ul", "im-list", items.map(it => itemRow(kind, it))) : h("p", "dim", T("im.none." + kind)));
    return sec;
  }

  function draw() {
    if (!state) return;
    const n = h("p", "setnote cp-note im-note", note); n.setAttribute("role", "status"); n.hidden = !note; n.dataset.bad = noteBad ? "1" : "";
    body.replaceChildren(h("p", "setnote", T("im.help")), h("p", "setnote", T("im.where", state.where.copies)), n, ...KINDS.map(section));
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
