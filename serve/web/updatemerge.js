"use strict";
/* The update of an installed skill, plugin or MCP server that keeps what the person changed. One window for Settings > Imported, Settings > Skills and Connectors:
   what the update does in plain words, the three versions of every file that differs (installed, new, merged), a choice for each file both sides changed in the same
   place, the person's instructions to the assistant for how to change the merge, and the three ways out: apply the merge, replace with the new version, cancel.
   The owner of the item gives the window two calls (`load(instructions)` reads the update, `apply(body)` does it); everything the server says is put in as text. */
(() => {
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = fn; return b; };
  const MARKERS = /^(<<<<<<< installed|=======|>>>>>>> new)$/m;
  const VERSIONS = [["installed", "um.v.installed"], ["new", "um.v.new"], ["merged", "um.v.merged"]];

  /* one version of one file, as a block of text */
  function pane(file, which) {
    const text = file[which];
    const box = h("div", "um-pane");
    box.dataset.version = which;
    if (!file.text) box.append(h("p", "setnote", file["in_" + (which === "merged" ? "merge" : which)] ? T("um.v.binary") : T("um.v.none")));
    else if (text == null) box.append(h("p", "setnote", T("um.v.none")));
    else box.append(h("pre", "um-text", text));
    return box;
  }

  function open(o) {
    const dlg = h("dialog", "im-dialog um-dlg");
    const head = h("h2", "", T("um.title", o.title)); head.id = "um-title";
    dlg.setAttribute("aria-labelledby", head.id);
    const content = h("div", "im-body um-body"), actions = h("div", "im-actions"), status = h("p", "setnote im-status");
    status.setAttribute("role", "status");
    dlg.append(h("div", "im-box um-box", head, content, status, actions));
    const close = () => { if (dlg.open) dlg.close(); dlg.remove(); };
    dlg.addEventListener("cancel", e => { e.preventDefault(); give("cancel"); });
    document.body.append(dlg);
    dlg.showModal();
    const say = (t, bad) => { status.textContent = t || ""; status.dataset.bad = bad ? "1" : ""; };
    let plan = null, resolutions = {}, tab = "merged", overwriteArmed = false, notesDraft = [];

    async function give(choice) {
      if (plan && plan.token && choice === "cancel") await o.apply({ token: plan.token, choice: "cancel" });
      close();
    }

    async function read(instructions) {
      content.replaceChildren(h("p", "setnote", T(instructions && instructions.length ? "um.instr.working" : "um.loading")));
      actions.replaceChildren(btn(T("um.close"), "ghost", () => give("cancel")));
      const r = await o.load(instructions || []);
      if (!r.ok) { content.replaceChildren(h("p", "setnote warnnote", o.errText(r))); return; }
      if (r.data.status === "unchanged") { content.replaceChildren(h("p", "", T("um.same"))); return; }
      plan = r.data; resolutions = {}; overwriteArmed = false;
      notesDraft = (plan.notes || []).slice();
      paint();
    }

    const unitKey = (u, f) => (plan.units.length > 1 ? u.key + "::" : "") + f.path;

    function counts() {
      const c = { upstream: 0, kept: 0, merged: 0, conflict: 0, same: 0 };
      plan.units.forEach(u => Object.keys(c).forEach(k => { c[k] += (u.counts || {})[k] || 0; }));
      return c;
    }

    function fileRow(u, f) {
      const key = unitKey(u, f), det = h("details", "um-file um-" + f.outcome);
      det.dataset.path = f.path; det.dataset.outcome = f.outcome;
      if (f.needs_choice) det.open = true;
      det.append(h("summary", "", h("span", "um-path", f.path), h("span", "um-tag", T("um.tag." + f.outcome))));
      const tabs = h("div", "um-tabs");
      tabs.setAttribute("role", "tablist");
      const panes = h("div", "um-panes");
      VERSIONS.forEach(([which, label]) => {
        const t = h("button", "um-tab" + (tab === which ? " on" : ""), T(label)); t.type = "button"; t.dataset.version = which; t.setAttribute("role", "tab"); t.setAttribute("aria-selected", tab === which ? "true" : "false");
        t.onclick = () => { tab = which; det.parentElement.querySelectorAll(".um-file").forEach(x => x.dataset.tab = tab); document.querySelectorAll(".um-dlg .um-tab").forEach(b => { const on = b.dataset.version === tab; b.classList.toggle("on", on); b.setAttribute("aria-selected", on ? "true" : "false"); }); };
        tabs.append(t);
        const p = pane(f, which); p.prepend(h("p", "um-vname", T(label)));
        panes.append(p);
      });
      det.dataset.tab = tab;
      det.append(tabs, panes);
      if (f.outcome === "conflict") {
        const pick = h("div", "um-pick");
        ["ours", "theirs", "merged"].forEach(choice => {
          const usable = choice !== "merged" || (f.text && f.merged != null && !MARKERS.test(f.merged) && !f.needs_choice);
          const b = btn(T("um.pick." + choice), resolutions[key] === choice ? "primary" : "", () => { resolutions[key] = choice; paint(); });
          b.dataset.choice = choice; b.disabled = !usable; b.setAttribute("aria-pressed", resolutions[key] === choice ? "true" : "false");
          pick.append(b);
        });
        det.append(pick);
      }
      return det;
    }

    function openChoices() {
      return plan.units.flatMap(u => u.files.filter(f => f.outcome === "conflict" && f.needs_choice && !resolutions[unitKey(u, f)])).length;
    }

    function paint() {
      const c = counts(), box = h("div", "um-sum");
      if (plan.description && plan.description.text) box.append(h("p", "um-words", plan.description.text));
      else box.append(h("p", "setnote", T("um.desc.none")));
      box.append(h("p", "setnote um-counts", T("um.counts", c.upstream, c.kept, c.merged, c.conflict, c.same)));
      if (!plan.has_base) box.append(h("p", "setnote warnnote", T("um.nobase")));
      if (o.extra) { const x = o.extra(plan); if (x) box.append(x); }
      plan.units.forEach(u => {
        if (!u.files.length) return;
        const sec = h("section", "um-unit");
        if (plan.units.length > 1) sec.append(h("h3", "um-unith", u.label));
        u.files.forEach(f => sec.append(fileRow(u, f)));
        box.append(sec);
      });
      (plan.parts || []).filter(x => x.state && x.state !== "same" && x.state !== "own").forEach(x => box.append(h("p", "setnote", `${x.key}: ${T("im.part." + x.state)}`)));

      /* what the person tells the assistant to change in the merge, and what is kept for the next updates */
      const ins = h("section", "um-instr");
      const ta = h("textarea", "um-ta"); ta.id = "um-instr"; ta.rows = 3; ta.placeholder = T("um.instr.ph"); ta.setAttribute("aria-label", T("um.instr.title"));
      ins.append(h("h3", "um-unith", T("um.instr.title")), h("p", "setnote", T("um.instr.help")), ta,
        btn(T("um.instr.go"), "", async () => { const t = ta.value.trim(); if (!t) return; await read([...notesDraft, t]); }));
      if ((plan.notes || []).length) {
        ins.append(h("p", "setnote", h("b", "", T("um.notes.title") + ": ")), h("ul", "um-notes", plan.notes.map(n => h("li", "", n))));
      }
      const res = Object.values(plan.notes_result || {}).flatMap(x => Object.entries(x));
      if (res.length) ins.append(h("ul", "um-res setnote", res.map(([p, s]) => h("li", "", `${p}: ${/^(revised|unchanged)$/.test(s) ? T("um.res." + s) : T("um.res.left")}`))));
      box.append(ins);
      content.replaceChildren(box);

      const go = btn(T("um.apply"), "primary", async () => {
        go.disabled = true; say(T("im.working"));
        const body = { token: plan.token, choice: "merge", resolutions };
        const r = await o.apply(body);
        if (r.ok) { say(""); close(); if (o.done) await o.done(T("um.done", o.title), r.data); } else { say(o.errText(r), true); go.disabled = false; }
      });
      go.id = "um-apply";
      const pending = openChoices();
      go.disabled = pending > 0;
      if (pending > 0) say(T("um.nores"));
      else say("");
      const ow = btn(overwriteArmed ? T("um.overwrite2") : T("um.overwrite"), "ghost im-danger", async () => {
        if (!overwriteArmed) { overwriteArmed = true; ow.textContent = T("um.overwrite2"); return; }
        ow.disabled = true; say(T("im.working"));
        const r = await o.apply({ token: plan.token, choice: "overwrite" });
        if (r.ok) { close(); if (o.done) await o.done(T("um.replaced", o.title), r.data); } else { say(o.errText(r), true); ow.disabled = false; }
      });
      ow.id = "um-overwrite";
      actions.replaceChildren(btn(T("um.cancel"), "ghost", () => give("cancel")), ow, go);
    }

    read([]);
    return { close };
  }

  window.UpdateMerge = { open };
})();
