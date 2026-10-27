"use strict";
/* Personal skills: what the person did in a conversation, kept as a Python script they can run again with other values.
   Here: the list (run, edit, delete, export, import), the form that asks for the values (dates may be written in words), making a skill from
   the requests the person chose (the values that change, then the script), and editing one. The server does the work (api/personal-skills,
   api/agent/sessions/<id>/skill*); everything shown comes in as text. */
(() => {
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const A = () => window.agentChat;
  const TYPES = ["text", "number", "date", "datetime", "choice", "list", "path", "bool"];
  const ITEMS = ["number", "text", "mixed"];

  async function api(method, path, body, signal) {
    try {
      const r = await fetch(path, { method, cache: "no-store", signal, headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let json = {};
      try { json = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json };
    } catch (e) { return { ok: false, status: 0, json: {}, aborted: e && e.name === "AbortError" }; }
  }
  const errText = r => (r.json && r.json.error && r.json.error.message) || (r.status ? `HTTP ${r.status}` : T("psk.err.net"));
  const say = text => { if (A() && A().active()) A().note(text); };

  /* ---- a window ---- */
  function windowOf(title, wide) {
    const dialog = mk("dialog", "psk" + (wide ? " wide" : ""));
    dialog.setAttribute("aria-labelledby", "psk-title");
    const head = mk("div", "psk-head"), h = mk("h2", "", title), x = mk("button", "btn ghost", T("psk.close"));
    h.id = "psk-title"; x.type = "button";
    const body = mk("div", "psk-body"), foot = mk("div", "psk-foot");
    head.append(h, x);
    dialog.append(head, body, foot);
    const close = () => { try { dialog.close(); } catch (e) { /* closed */ } dialog.remove(); };
    x.onclick = close;
    dialog.addEventListener("cancel", e => { e.preventDefault(); close(); });
    document.body.append(dialog);
    dialog.showModal();
    return { dialog, head, h, body, foot, close };
  }
  const button = (label, cls, onclick) => { const b = mk("button", "btn " + (cls || ""), label); b.type = "button"; b.onclick = onclick; return b; };
  const field = (label, input, help) => {
    const row = mk("div", "psk-field"), l = mk("label", "", label);
    const id = "psk-f-" + Math.random().toString(36).slice(2, 8);
    input.id = id; l.htmlFor = id;
    row.append(l, input);
    if (help) row.append(mk("p", "psk-help", help));
    return row;
  };
  const input = (value, cls) => { const i = mk("input", cls); i.type = "text"; i.autocomplete = "off"; i.spellcheck = false; i.value = value == null ? "" : String(value); return i; };
  const select = (options, value, label) => { const s = mk("select"); options.forEach(o => { const op = mk("option", "", label(o)); op.value = o; s.append(op); }); s.value = value; return s; };
  const typeName = t => T("psk.type." + t);

  /* ---- a parameter: the card that defines it (making and editing a skill) ---- */
  function paramCard(spec, o = {}) {
    const card = mk("fieldset", "psk-param");
    const legend = mk("legend", "", spec.label || spec.name || T("psk.pf.new"));
    card.append(legend);
    let keep = null;
    if (o.keep) {
      keep = mk("input"); keep.type = "checkbox"; keep.checked = true;
      const l = mk("label", "psk-keep"); l.append(keep, mk("span", "", T("psk.pf.keep")));
      card.append(l);
    }
    const ident = input(spec.name || "", "psk-mono"), label = input(spec.label || ""), explain = input(spec.explanation || "");
    const type = select(TYPES, TYPES.includes(spec.type) ? spec.type : "text", typeName);
    const choices = input((spec.choices || []).join(", ")), items = select(ITEMS, ITEMS.includes(spec.item_type) ? spec.item_type : "text", t => T("psk.item." + t));
    const usual = input(spec.default == null ? "" : spec.default);
    const choicesRow = field(T("psk.pf.choices"), choices), itemsRow = field(T("psk.pf.listitems"), items);
    const sync = () => { choicesRow.hidden = type.value !== "choice"; itemsRow.hidden = type.value !== "list"; };
    type.onchange = sync; sync();
    label.addEventListener("input", () => { legend.textContent = label.value || ident.value || T("psk.pf.new"); });
    card.append(field(T("psk.pf.ident"), ident), field(T("psk.pf.label"), label), field(T("psk.pf.explain"), explain), field(T("psk.pf.type"), type), choicesRow, itemsRow,
                field(T("psk.pf.default"), usual, spec.type === "date" || spec.type === "datetime" ? T("psk.pf.dateHelp") : ""));
    if (o.seen != null && o.seen !== "") card.append(mk("p", "psk-help", T("psk.make.seen", String(o.seen))));
    if (o.used) card.append(mk("p", "psk-help", T("psk.make.used", o.used)));
    if (o.unverified) card.append(mk("p", "psk-warn", T("psk.make.unverified")));
    if (o.remove) card.append(button(T("psk.pf.remove"), "ghost", () => { card.remove(); o.remove(card); }));
    return {
      el: card, kept: () => !keep || keep.checked,
      read() {
        const name = ident.value.trim().toLowerCase();
        if (!/^[a-z_][a-z0-9_]{0,39}$/.test(name)) throw new Error(T("psk.pf.badname", name));
        const out = { name, label: label.value.trim() || name, explanation: explain.value.trim(), type: type.value };
        if (type.value === "choice") out.choices = choices.value.split(",").map(s => s.trim()).filter(Boolean);
        if (type.value === "list") out.item_type = items.value;
        if (usual.value.trim()) out.default = usual.value.trim();
        if (spec.value_seen != null) out.value_seen = spec.value_seen;
        return out;
      },
    };
  }

  /* ---- the form that asks for the values of a run ---- */
  function valueField(spec, preset) {
    const wrap = mk("div", "psk-field");
    const id = "psk-v-" + spec.name;
    const l = mk("label", "", spec.label || spec.name);
    l.htmlFor = id;
    wrap.append(l);
    if (spec.explanation) wrap.append(mk("p", "psk-help", spec.explanation));
    wrap.append(mk("p", "psk-help psk-type", T("psk.pf.type.is", typeName(spec.type) + (spec.type === "list" ? " (" + T("psk.item." + (spec.item_type || "text")) + ")" : ""))));
    let control, get;
    const start = preset != null ? preset : (spec.default != null ? spec.default : "");
    if (spec.type === "choice") {
      control = select(spec.choices || [], (spec.choices || []).includes(start) ? start : (spec.choices || [""])[0], x => x);
      get = () => control.value;
    } else if (spec.type === "bool") {
      control = select(["true", "false"], String(start) === "false" ? "false" : "true", x => T(x === "true" ? "psk.yes" : "psk.no"));
      get = () => control.value;
    } else if (spec.type === "text" || spec.type === "list") {
      control = mk("textarea", "psk-text"); control.rows = spec.type === "list" ? 4 : 3; control.value = Array.isArray(start) ? start.join("\n") : String(start);
      get = () => control.value;
      if (spec.type === "list") wrap.append(mk("p", "psk-help", T("psk.pf.list")));
    } else {
      control = input(start);
      get = () => control.value;
      if (spec.type === "date" || spec.type === "datetime") {
        const picker = mk("input", "psk-picker"); picker.type = spec.type === "date" ? "date" : "datetime-local";
        picker.setAttribute("aria-label", T("psk.pf.picker", spec.label || spec.name));
        picker.onchange = () => { if (picker.value) { control.value = picker.value; control.dispatchEvent(new Event("input")); } };
        const row = mk("div", "psk-row"); row.append(control, picker);
        wrap.append(row, mk("p", "psk-help", T("psk.pf.dateHelp")));
        control.id = id;
        const hint = mk("p", "psk-hint"); hint.setAttribute("aria-live", "polite"); wrap.append(hint);
        return { el: wrap, get, control, hint };
      }
    }
    control.id = id;
    wrap.append(control);
    const hint = mk("p", "psk-hint"); hint.setAttribute("aria-live", "polite"); wrap.append(hint);
    return { el: wrap, get, control, hint };
  }

  async function runForm(skill, preset = {}) {
    const w = windowOf(T("psk.run.title", skill.name));
    if (skill.description) w.body.append(mk("p", "psk-lead", skill.description));
    if (skill.status !== "approved") {
      const box = mk("div", "psk-pending");
      box.append(mk("p", "", T("psk.run.pending")));
      const code = mk("pre", "psk-code-view"); code.textContent = (await api("GET", `api/personal-skills/${skill.slug}`)).json.skill?.script || "";
      const ok = button(T("psk.approve"), "primary", async () => {
        const r = await api("POST", `api/personal-skills/${skill.slug}/approve`, {});
        if (!r.ok) { status.textContent = errText(r); return; }
        w.close(); const fresh = await api("GET", `api/personal-skills/${skill.slug}`); if (fresh.ok) runForm(fresh.json.skill, preset);
      });
      box.append(code, ok);
      w.body.append(box);
    }
    const fields = (skill.params || []).map(p => ({ spec: p, ...valueField(p, preset[p.name]) }));
    fields.forEach(f => w.body.append(f.el));
    if (!fields.length) w.body.append(mk("p", "psk-help", T("psk.run.noparams")));
    const status = mk("p", "psk-status"); status.setAttribute("role", "status");
    w.foot.append(status);
    const values = () => { const out = {}; fields.forEach(f => { const v = f.get(); if (String(v).trim() !== "") out[f.spec.name] = v; }); return out; };
    let timer = 0;
    const preview = async () => {
      const r = await api("POST", "api/personal-skills/preview", { params: skill.params || [], values: values() });
      if (!r.ok) return;
      fields.forEach(f => {
        const e = (r.json.errors || {})[f.spec.name], v = (r.json.values || {})[f.spec.name];
        f.hint.className = "psk-hint" + (e ? " bad" : "");
        f.hint.textContent = e ? e : (f.spec.type === "date" || f.spec.type === "datetime") && v ? T("psk.pf.becomes", v) : "";
      });
    };
    fields.forEach(f => f.control.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(preview, 300); }));
    preview();
    const go = button(T("psk.run.go"), "primary", async () => {
      go.disabled = true; status.textContent = "";
      if (!A() || !A().active()) { status.textContent = T("psk.run.needchat"); go.disabled = false; return; }
      if (!(await A().ensure())) { status.textContent = T("psk.run.needchat"); go.disabled = false; return; }
      const r = await api("POST", `api/agent/sessions/${A().current()}/skill`, { slug: skill.slug, values: values() });
      if (!r.ok) { status.textContent = errText(r); go.disabled = false; return; }
      w.close();
      A().refresh();
    });
    if (skill.status === "approved") w.foot.append(go);
    else go.disabled = true;
    w.foot.append(button(T("psk.close"), "ghost", w.close));
    const first = fields[0] && fields[0].control; if (first) first.focus();
  }

  /* ---- the list ---- */
  async function skillBy(text) {
    const r = await api("GET", "api/personal-skills");
    if (!r.ok) return { error: errText(r) };
    const want = text.trim().toLowerCase();
    return { skills: r.json.skills || [], found: (r.json.skills || []).find(s => s.slug === want || s.name.toLowerCase() === want) || null };
  }

  async function openList() {
    const w = windowOf(T("psk.list.title"), true);
    const bar = mk("div", "psk-bar"), file = mk("input"); file.type = "file"; file.accept = ".pskill"; file.hidden = true;
    const msg = mk("p", "psk-status"); msg.setAttribute("role", "status");
    bar.append(button(T("psk.import"), "", () => file.click()), file);
    w.body.append(bar, msg);
    const list = mk("div", "psk-list"); w.body.append(list);
    let armed = "";
    const draw = async () => {
      const r = await api("GET", "api/personal-skills");
      if (!r.ok) { msg.textContent = errText(r); return; }
      const skills = r.json.skills || [];
      list.replaceChildren(...(skills.length ? skills.map(row) : [mk("p", "psk-help", T("psk.list.empty"))]));
    };
    const row = s => {
      const el = mk("section", "psk-item"), top = mk("div", "psk-itemhead");
      top.append(mk("b", "", s.name));
      if (s.status !== "approved") top.append(mk("span", "psk-badge warn", T("psk.pending")));
      el.append(top);
      if (s.description) el.append(mk("p", "psk-help", s.description));
      el.append(mk("p", "psk-help", T("psk.meta", (s.params || []).length, s.uses || 0)));
      const acts = mk("div", "psk-acts");
      const del = button(armed === s.slug ? T("psk.delete.sure") : T("psk.delete"), "ghost" + (armed === s.slug ? " sure" : ""), async () => {
        if (armed !== s.slug) { armed = s.slug; draw(); setTimeout(() => { if (armed === s.slug) { armed = ""; draw(); } }, 4000); return; }
        armed = "";
        const d = await api("POST", `api/personal-skills/${s.slug}/delete`, {});
        msg.textContent = d.ok ? "" : errText(d);
        draw();
      });
      acts.append(button(T("psk.run"), "primary", async () => { const f = await api("GET", `api/personal-skills/${s.slug}`); if (f.ok) { w.close(); runForm(f.json.skill); } else msg.textContent = errText(f); }),
                  button(T("psk.edit"), "", async () => { const f = await api("GET", `api/personal-skills/${s.slug}`); if (f.ok) editor(f.json.skill, draw); else msg.textContent = errText(f); }),
                  button(T("psk.export"), "", async () => {
                    try {
                      const resp = await fetch(`api/personal-skills/${s.slug}/export`, { headers: authHeaders(), cache: "no-store" });
                      if (!resp.ok) throw new Error("HTTP " + resp.status);
                      await A().saveBlob(await resp.blob(), s.slug + ".pskill");
                    } catch (e) { msg.textContent = T("psk.err", String(e.message || e)); }
                  }), del);
      el.append(acts);
      return el;
    };
    file.onchange = async () => {
      const f = file.files[0]; file.value = "";
      if (!f) return;
      const bytes = new Uint8Array(await f.arrayBuffer());
      let bin = ""; for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      const r = await api("POST", "api/personal-skills/import", { data: btoa(bin) });
      msg.textContent = r.ok ? T("psk.imported", r.json.skill.name) : errText(r);
      draw();
    };
    w.foot.append(button(T("psk.close"), "ghost", w.close));
    draw();
  }

  /* ---- the script and the definition: making a skill (step two) and editing one ---- */
  function editor(skill, done, fresh) {
    const w = windowOf(fresh ? T("psk.make.title") : T("psk.edit.title", skill.name), true);
    const name = input(skill.name || ""), desc = input(skill.description || "");
    const cards = [];
    const paramBox = mk("div", "psk-params");
    const addCard = (spec, o) => { const c = paramCard(spec, { ...o, remove: () => { const i = cards.indexOf(c); if (i >= 0) cards.splice(i, 1); } }); cards.push(c); paramBox.append(c.el); return c; };
    (skill.params || []).forEach(p => addCard(p, {}));
    const code = mk("textarea", "psk-code"); code.rows = 20; code.spellcheck = false; code.value = skill.script || "";
    const limit = input(skill.timeout || ""); limit.inputMode = "numeric";
    const out = mk("div", "psk-out"), status = mk("p", "psk-status");
    status.setAttribute("role", "status");
    w.body.append(field(T("psk.field.name"), name), field(T("psk.field.desc"), desc), mk("h3", "", T("psk.make.params")), paramBox,
                  button(T("psk.make.add"), "", () => addCard({ type: "text" }, {})), mk("h3", "", T("psk.field.script")), code, out,
                  field(T("psk.field.limit"), limit, T("psk.limit.help")));
    if (skill.unused && skill.unused.length) out.append(mk("p", "psk-warn", T("psk.unused", skill.unused.join(", "))));
    const collect = () => {
      const params = [];
      for (const c of cards) params.push(c.read());
      return { name: name.value.trim(), description: desc.value.trim(), timeout: Math.max(0, parseInt(limit.value, 10) || 0), params, ...(skill.source ? { source: skill.source } : {}) };
    };
    const check = async () => {
      out.replaceChildren();
      const r = await api("POST", "api/personal-skills/check", { script: code.value });
      if (!r.ok) { status.textContent = errText(r); return false; }
      const problems = r.json.problems || [];
      if (problems.length) {
        out.append(mk("p", "psk-warn", T("psk.problems")), ...problems.slice(0, 8).map(p => mk("p", "psk-help", T("psk.line", p.line, p.message))));
        return false;
      }
      out.append(mk("p", "psk-ok", T("psk.check.ok")), ...(r.json.tools && r.json.tools.length ? [mk("p", "psk-help", T("psk.tools", r.json.tools.join(", ")))] : []));
      return true;
    };
    const save = button(T("psk.save"), "primary", async () => {
      status.textContent = "";
      let spec;
      try { spec = collect(); } catch (e) { status.textContent = e.message; return; }
      if (!spec.name) { status.textContent = T("psk.name.need"); return; }
      save.disabled = true;
      const r = await api("POST", "api/personal-skills", { skill: spec, script: code.value, slug: fresh ? undefined : skill.slug });
      save.disabled = false;
      if (!r.ok) { status.textContent = errText(r); return; }
      w.close();
      if (fresh) say(T("psk.saved", r.json.skill.name, r.json.skill.slug));
      if (done) done(r.json.skill);
    });
    w.foot.append(status, button(T("psk.check"), "", check), save);
    if (!fresh && skill.status !== "approved") w.foot.append(button(T("psk.approve"), "", async () => { const r = await api("POST", `api/personal-skills/${skill.slug}/approve`, {}); status.textContent = r.ok ? T("psk.approved") : errText(r); if (r.ok && done) done(r.json.skill); }));
    if (!fresh && (skill.versions || []).length) {
      const versions = select((skill.versions || []).map(String), String(skill.versions[skill.versions.length - 1]), v => T("psk.version", v));
      w.foot.append(versions, button(T("psk.restore"), "ghost", async () => {
        const r = await api("POST", `api/personal-skills/${skill.slug}/restore`, { version: Number(versions.value) });
        if (r.ok) { w.close(); editor(r.json.skill, done); if (done) done(r.json.skill); } else status.textContent = errText(r);
      }));
    }
    w.foot.append(button(T("psk.close"), "ghost", w.close));
    if (fresh) check();
  }

  /* ---- making a skill from the requests the person chose ---- */
  async function make(sid, turns) {
    const w = windowOf(T("psk.make.title"), true);
    const wait = (text, abort) => { w.body.replaceChildren(mk("p", "psk-lead", text)); w.foot.replaceChildren(...(abort ? [button(T("psk.sel.cancel"), "ghost", abort)] : [])); };
    const controller = new AbortController();
    wait(T("psk.make.finding"), () => { controller.abort(); w.close(); });
    const r = await api("POST", `api/agent/sessions/${sid}/skill-propose`, { turns }, controller.signal);
    if (r.aborted) return;
    if (!r.ok) { w.body.replaceChildren(mk("p", "psk-warn", T("psk.err", errText(r)))); w.foot.replaceChildren(button(T("psk.close"), "ghost", w.close)); return; }
    const proposal = r.json;
    w.body.replaceChildren();
    const asked = mk("details", "psk-asked"); asked.append(mk("summary", "", T("psk.make.requests")), ...proposal.requests.map(q => mk("p", "psk-help", `${q.n}. ${q.raw}`)));
    const name = input(proposal.name || ""), desc = input(proposal.description || "");
    const cards = [], box = mk("div", "psk-params");
    const addCard = (spec, o) => { const c = paramCard(spec, { ...o, remove: () => { const i = cards.indexOf(c); if (i >= 0) cards.splice(i, 1); } }); cards.push(c); box.append(c.el); return c; };
    const cand = value => (proposal.candidates || []).find(c => String(c.value) === String(value));
    (proposal.params || []).forEach(p => {
      const c = cand(p.value_seen);
      addCard(p, { keep: true, seen: p.value_seen, unverified: p.unverified, used: c ? c.used_in.map(u => `${u.tool}.${u.argument}`).slice(0, 4).join(", ") : "" });
    });
    w.body.append(asked, field(T("psk.field.name"), name), field(T("psk.field.desc"), desc), mk("h3", "", T("psk.make.params")), mk("p", "psk-help", T("psk.make.params.help")));
    if (proposal.note) w.body.append(mk("p", "psk-warn", proposal.note));
    if (!(proposal.params || []).length) w.body.append(mk("p", "psk-help", T("psk.make.none")));
    w.body.append(box, button(T("psk.make.add"), "", () => addCard({ type: "text" }, { keep: true })));
    const status = mk("p", "psk-status"); status.setAttribute("role", "status");
    const write = button(T("psk.make.write"), "primary", async () => {
      status.textContent = "";
      let params;
      try { params = cards.filter(c => c.kept()).map(c => c.read()); } catch (e) { status.textContent = e.message; return; }
      if (new Set(params.map(p => p.name)).size !== params.length) { status.textContent = T("psk.pf.dup"); return; }
      const ctl = new AbortController();
      wait(T("psk.make.writing"), () => { ctl.abort(); w.close(); });
      const s = await api("POST", `api/agent/sessions/${sid}/skill-script`, { turns, name: name.value.trim(), description: desc.value.trim(), params }, ctl.signal);
      if (s.aborted) return;
      if (!s.ok) { w.body.replaceChildren(mk("p", "psk-warn", T("psk.err", errText(s)))); w.foot.replaceChildren(button(T("psk.close"), "ghost", w.close)); return; }
      w.close();
      editor({ ...s.json.skill, script: s.json.script, unused: s.json.unused }, null, true);
    });
    w.foot.append(status, write, button(T("psk.sel.cancel"), "ghost", w.close));
  }

  window.pskills = {
    openList, make, edit: editor, runForm, skillBy, valueField,
    use: async text => {                                             // /skilluse name  [key=value ...]
      const m = /^(?:"([^"]+)"|(\S+))(?:\s+(.*))?$/.exec(text.trim());
      if (!m) { openList(); return; }
      const found = await skillBy(m[1] || m[2]);
      if (found.error) { say(T("psk.err", found.error)); return; }
      if (!found.found) { say(T("psk.nosuch", m[1] || m[2])); return; }
      const preset = {};
      (m[3] || "").replace(/(\w+)=("([^"]*)"|(\S+))/g, (_, k, v, q, p) => { preset[k] = q != null ? q : p; return ""; });
      const f = await api("GET", `api/personal-skills/${found.found.slug}`);
      if (f.ok) runForm(f.json.skill, preset);
    },
  };
  const open = $("#pskills-open");
  if (open) open.onclick = openList;
})();
