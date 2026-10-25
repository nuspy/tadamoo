"use strict";
/* Wallet: what the agent may use for purchases and bookings (cards, personal data, logins, secrets in the vault) and how much it may spend
   on its own (the payment systems and their limits). A value goes in and never comes out: the server shows which fields are set and the
   last four digits of a card. Turning on automatic payments is the three dialogs of the design, in order, and the server checks the
   declaration again. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("wl-root"), dlg = document.getElementById("wl-wizard");
  if (!root || !dlg) return;
  const live = document.getElementById("wl-live");              // the one place a screen reader is told of a result; the page itself is not a live region
  let data = null, editing = null, sysForm = null, note = "", noteBad = false, wizard = null, vaultFilter = "all", vaultSearch = "", shown = "";
  const again = new Map();                                    // press-again buttons: key -> timer

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = fn; return b; };
  const label = (key, prefix) => { const t = T(prefix + key); return t === prefix + key ? key : t; };
  const when = ts => new Date(ts * 1000).toLocaleDateString(LANG === "it" ? "it-IT" : "en-GB", { day: "numeric", month: "short", year: "numeric" });

  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let body = {};
      try { body = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data: body };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => (r.status === 0 ? T("wl.e.offline") : ((r.data.error || {}).message || `HTTP ${r.status}`));
  function say(text, bad) {
    note = text; noteBad = !!bad;
    const n = root.querySelector(".wl-note"); if (n) { n.textContent = note; n.dataset.bad = noteBad ? "1" : ""; n.hidden = !note; }
    if (live) { live.textContent = ""; setTimeout(() => { live.textContent = note; }, 50); }
  }
  const field = (labelText, input, help) => {
    const id = input.id || (input.id = "wl-f-" + Math.random().toString(36).slice(2, 8));
    const l = h("label", "", labelText); l.htmlFor = id;
    return h("div", "field", l, input, help ? h("p", "help", help) : null);
  };
  const textInput = (value, opts = {}) => {
    const i = h("input"); i.type = opts.type || "text"; i.value = value || ""; i.autocomplete = "off"; i.spellcheck = false;
    if (opts.mode) i.inputMode = opts.mode;
    if (opts.placeholder) i.placeholder = opts.placeholder;
    if (opts.disabled) i.disabled = true;
    return i;
  };
  const select = (options, value, disabled) => {
    const s = h("select", "", options.map(([v, t]) => Object.assign(h("option", "", t), { value: v })));
    s.value = value; s.disabled = !!disabled; return s;
  };
  function pressAgain(key, button, text, again_text, run) {
    button.onclick = () => {
      if (again.has(key)) { clearTimeout(again.get(key)); again.delete(key); run(); return; }
      button.textContent = again_text;
      again.set(key, setTimeout(() => { again.delete(key); button.textContent = text; }, 4000));
    };
    return button;
  }

  /* ---- the banner: automatic payments are on, and the kill switch ---- */
  function banner() {
    const on = (data.systems || []).filter(s => s.waiver);
    if (!on.length) return null;
    const box = h("section", "sec wl-sec wl-on", h("div", "sh", h("b", "", T("wl.stop.t"))));
    const body = h("div", "wl-body", on.map(s => h("p", "", T("wl.stop.line", s.name, s.waiver.per_payment, s.waiver.cumulative, s.waiver.spent, s.waiver.remaining, when(s.waiver.expires_at)))));
    body.append(btn(T("wl.stop.btn"), "primary", async () => {
      const r = await call("POST", "api/wallet/revoke", {});
      if (r.ok) { say(T("wl.stopped")); await load(); } else say(why(r), true);
    }));
    box.append(body);
    return box;
  }

  /* ---- the vault ---- */
  async function revealed(id, field) {
    const r = await call("POST", "api/wallet/reveal", { id, field });
    if (!r.ok) { say(why(r), true); return null; }
    return r.data.value;
  }
  async function copy(id, field) {
    const v = await revealed(id, field);
    if (v === null) return;
    try { await navigator.clipboard.writeText(v); say(T("wl.copied")); } catch (e) { say(T("wl.copy.fail"), true); }
  }
  function generated(length = 20) {                                  // random from the browser's own source, with every kind of character
    const sets = ["abcdefghijkmnopqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789", "!@#$%^&*-_=+?"];
    const all = sets.join("");
    const pick = s => s[crypto.getRandomValues(new Uint32Array(1))[0] % s.length];
    const out = sets.map(pick);
    while (out.length < length) out.push(pick(all));
    for (let i = out.length - 1; i > 0; i--) { const j = crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1); [out[i], out[j]] = [out[j], out[i]]; }
    return out.join("");
  }
  function entryCard(en) {
    const lines = [];
    const order = data.kinds[en.kind] || [], rank = n => (order.includes(n) ? order.indexOf(n) : order.length);
    lines.push(h("ul", "wl-chips", Object.entries(en.fields).sort((a, b) => rank(a[0]) - rank(b[0])).map(([n, hint]) => h("li", "", label(n, "wl.field."), hint === "set" ? "" : ` ${hint}`))));
    const sites = en.hosts.length ? en.hosts.join(", ") : T("wl.local");
    const facts = [`${T("wl.hosts")}: ${sites}`];
    if (en.kind === "login") facts.push(`${T("wl.sf")}: ${T("wl.sf." + (en.second_factor || "none"))}`);
    if (en.kind === "card") facts.push(`${T("wl.cardkind." + en.card_kind)}${en.currency ? " · " + en.currency : ""}`, T(en.guard ? "wl.guard.on" : "wl.guard.off"));
    const del = pressAgain("del:" + en.id, btn(T("wl.delete"), "ghost", () => {}), T("wl.delete"), T("wl.delete.again"), async () => {
      const r = await call("POST", "api/wallet/entry/delete", { id: en.id });
      if (r.ok) { say(T("wl.deleted")); await load(); } else say(why(r), true);
    });
    return h("li", "wl-card",
      h("div", "wl-head", h("b", "", en.label), h("span", "wl-tag", T("wl.kind." + en.kind)), h("code", "wl-id", en.id)),
      lines, h("p", "wl-facts", facts.join(" · ")), ...entryRisks(en).map(([, text]) => riskLabel(text)),
      h("div", "wl-actions", btn(T("wl.edit"), "", () => { editing = { id: en.id, kind: en.kind, isNew: false, en }; draw(); }),
        ...(en.kind === "login" ? [btn(T("wl.copy.user"), "ghost", () => copy(en.id, "username")), btn(T("wl.copy.pw"), "ghost", () => copy(en.id, "password"))] : []), del));
  }
  /* the settings of an entry that raise a risk: each is a label wherever it is on, and is asked about twice when it is set */
  const entryRisks = e => {
    const out = [];
    if (e.kind === "card" && e.guard === false) out.push(["guard", T("wl.risk.guard")]);
    if ((e.hosts || []).includes("*")) out.push(["anysite", T("wl.risk.anysite")]);
    if (e.kind === "login" && e.second_factor === "automatic") out.push(["totp", T("wl.risk.totp")]);
    return out;
  };
  const SECRET_FIELDS = new Set(["cvc", "password", "totp_secret", "value"]);
  function editor() {
    const e = editing, en = e.en || { fields: {}, hosts: [] };
    const form = h("form", "wl-form"); form.noValidate = true;
    const id = textInput(e.id || "", { disabled: !e.isNew }), lab = textInput(e.label !== undefined ? e.label : (en.label || ""));
    const kind = select(Object.keys(data.kinds).map(k => [k, T("wl.kind." + k)]), e.kind, !e.isNew);
    kind.onchange = () => { e.id = id.value; e.label = lab.value; e.kind = kind.value; draw(); };
    form.append(field(T("wl.f.id"), id), field(T("wl.f.label"), lab), field(T("wl.f.kind"), kind));

    const names = [...new Set([...(data.kinds[e.kind] || []), ...Object.keys(en.fields)])];
    const inputs = {}, removes = {};
    const addField = (name) => {
      const set = name in en.fields;
      const input = textInput("", { type: SECRET_FIELDS.has(name) ? "password" : "text", mode: name === "number" ? "numeric" : undefined, placeholder: set ? T("wl.f.keep") : "" });
      inputs[name] = input;
      const row = field(label(name, "wl.field.") + (set && en.fields[name] !== "set" ? ` (${en.fields[name]})` : ""), input);
      if (set) {
        const rm = h("input"); rm.type = "checkbox"; rm.id = "wl-rm-" + name; removes[name] = rm;
        const l = h("label", "wl-rm", rm, h("span", "", T("wl.f.remove"))); l.htmlFor = rm.id;
        const show = btn(T("wl.show"), "ghost", async () => {
          if (input.dataset.shown) { input.value = ""; input.type = SECRET_FIELDS.has(name) ? "password" : "text"; delete input.dataset.shown; show.textContent = T("wl.show"); return; }
          const v = await revealed(e.id, name);
          if (v === null) return;
          input.value = v; input.type = "text"; input.dataset.shown = "1"; show.textContent = T("wl.hide");
        });
        row.append(h("div", "wl-actions", show, btn(T("wl.copy"), "ghost", () => copy(e.id, name))), l);
      }
      if (name === "password") row.append(btn(T("wl.gen"), "ghost", () => { input.value = generated(); input.type = "text"; input.dataset.shown = "1"; }));
      form.append(row);
    };
    names.forEach(addField);
    let extra = null;
    if (e.kind === "identity") {                                 // personal data takes any field the person names
      const n = textInput("", { placeholder: T("wl.custom.name") }), v = textInput("", { placeholder: T("wl.custom.value") });
      extra = [n, v];
      form.append(h("div", "wl-extra", h("b", "", T("wl.custom")), n, v));
    }
    let cardKind, currency, guard, second, hosts;
    if (e.kind === "card") {
      cardKind = select(data.card_kinds.map(k => [k, T("wl.cardkind." + k)]), en.card_kind || "virtual");
      currency = textInput(en.currency || "", { placeholder: "EUR" });
      guard = h("input"); guard.type = "checkbox"; guard.id = "wl-guard"; guard.checked = en.guard !== false;
      const gl = h("label", "wl-rm", guard, h("span", "", T("wl.guard"))); gl.htmlFor = "wl-guard";
      form.append(field(T("wl.cardkind"), cardKind), field(T("wl.currency"), currency), h("div", "field", gl, h("p", "help", T("wl.guard.help"))));
    }
    if (e.kind === "login") {
      const fs = h("fieldset", "wl-sf", h("legend", "", T("wl.sf")));
      second = en.second_factor || "none";
      data.second_factors.forEach(level => {
        const r = h("input"); r.type = "radio"; r.name = "wl-sf"; r.value = level; r.checked = level === second; r.onchange = () => { second = level; };
        fs.append(h("label", "radio", r, h("span", "", h("b", "", T("wl.sf." + level)), h("small", "", T("wl.sf." + level + ".d")))));
      });
      form.append(fs);
    }
    hosts = h("textarea"); hosts.rows = 3; hosts.spellcheck = false; hosts.value = e.hosts !== undefined ? e.hosts : (en.hosts || []).join("\n");
    form.append(field(T("wl.hosts"), hosts, T("wl.hosts.help")));
    const out = h("p", "wl-msg", ""); out.setAttribute("role", "alert");
    const save = h("button", "btn primary", T("wl.save")); save.type = "submit";
    form.append(out, h("div", "wl-actions", save, btn(T("wl.cancel"), "ghost", () => { editing = null; draw(); })));
    form.onsubmit = async ev => {
      ev.preventDefault();
      const fields = {};
      Object.entries(inputs).forEach(([n, i]) => { if (removes[n] && removes[n].checked) fields[n] = ""; else if (i.value) fields[n] = i.value; });
      if (extra && extra[0].value.trim()) fields[extra[0].value.trim()] = extra[1].value;
      const body = { id: id.value.trim(), kind: e.kind, label: lab.value.trim(), fields, hosts: hosts.value };
      if (e.kind === "card") Object.assign(body, { card_kind: cardKind.value, currency: currency.value.trim(), guard: guard.checked });
      if (e.kind === "login") body.second_factor = second;
      const was = new Set(entryRisks(en).map(r => r[0]));
      const now = entryRisks({ kind: e.kind, guard: body.guard, hosts: body.hosts.replace(/,/g, " ").split(/\s+/), second_factor: body.second_factor });
      const added = now.filter(r => !was.has(r[0])).map(r => r[1]);
      if (added.length && !(await riskConfirm(added))) return;
      save.disabled = true;
      const r = await call("POST", "api/wallet/entry", body);
      save.disabled = false;
      if (r.ok) { editing = null; say(T("wl.saved")); await load(); } else out.textContent = why(r);
    };
    return form;
  }
  async function importLogins(file) {
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { say(T("wl.import.big"), true); return; }
    const r = await call("POST", "api/wallet/import", { csv: await file.text() });
    if (!r.ok) { say(T("wl.import.bad", why(r)), true); return; }
    const skipped = Object.values(r.data.skipped || {}).reduce((a, b) => a + b, 0);
    say(T("wl.import.done", r.data.added, (r.data.skipped || {}).duplicate || 0, skipped - ((r.data.skipped || {}).duplicate || 0) + (r.data.refused || []).length));
    await load();
  }
  function vaultSection() {
    const pick = h("input"); pick.type = "file"; pick.accept = ".csv,text/csv"; pick.hidden = true; pick.onchange = () => { importLogins(pick.files[0]); pick.value = ""; };
    const sec = h("section", "sec wl-sec", h("div", "sh", h("b", "", T("wl.vault")), h("span", "grow"),
      btn(T("wl.import"), "", () => pick.click()), btn(T("wl.add"), "primary", () => { editing = { id: "", kind: "card", isNew: true }; draw(); }), pick));
    const body = h("div", "wl-body", h("p", "help", T("wl.vault.help")), h("p", "help", T("wl.import.help")));
    if (editing) body.append(editor());
    const entries = data.entries || [];
    if (!entries.length) { body.append(h("p", "dim", T("wl.vault.none"))); sec.append(body); return sec; }
    const kinds = select([["all", T("wl.filter.all")], ...Object.keys(data.kinds).map(k => [k, T("wl.kind." + k)])], vaultFilter);
    const find = textInput(vaultSearch, { placeholder: T("wl.search") }); find.type = "search"; find.setAttribute("aria-label", T("wl.search"));
    const list = h("ul", "wl-list"), none = h("p", "dim", T("wl.nomatch"));
    const paint = () => {
      const q = vaultSearch.trim().toLowerCase();
      const shown = entries.filter(en => (vaultFilter === "all" || en.kind === vaultFilter)
        && (!q || [en.label, en.id, en.kind, ...(en.hosts || [])].some(t => String(t).toLowerCase().includes(q))));
      list.replaceChildren(...shown.map(entryCard));
      none.hidden = shown.length > 0;
    };
    kinds.setAttribute("aria-label", T("wl.filter"));
    kinds.onchange = () => { vaultFilter = kinds.value; paint(); };
    find.oninput = () => { vaultSearch = find.value; paint(); };
    paint();
    body.append(h("div", "wl-tools", kinds, find), list, none);
    sec.append(body);
    return sec;
  }

  /* ---- the payment systems ---- */
  function systemCard(s) {
    const w = s.waiver;
    const status = w ? T("wl.sys.auto", w.per_payment, w.cumulative, w.spent, w.remaining, when(w.expires_at)) : T("wl.sys.manual");
    const actions = h("div", "wl-actions");
    actions.append(btn(T("wl.sys.go"), w ? "ghost" : "primary", () => openWizard(s)));
    if (w) actions.append(btn(T("wl.sys.stop"), "", async () => {
      const r = await call("POST", "api/wallet/revoke", { id: s.id });
      if (r.ok) { say(T("wl.stopped")); await load(); } else say(why(r), true);
    }));
    actions.append(pressAgain("sys:" + s.id, btn(T("wl.sys.remove"), "ghost", () => {}), T("wl.sys.remove"), T("wl.sys.remove.again"), async () => {
      const r = await call("POST", "api/wallet/system/remove", { id: s.id });
      if (r.ok) { await load(); } else say(why(r), true);
    }));
    const pays = s.vault_entry ? T("wl.sys.with", s.vault_entry) : label(s.credential, "wl.credkind.");
    return h("li", "wl-card", h("div", "wl-head", h("b", "", s.name), h("span", "wl-tag", s.currency), h("code", "wl-id", s.id)),
      h("p", "wl-facts", pays), h("p", "wl-status", status), actions);
  }
  function systemForm() {
    const f = sysForm, form = h("form", "wl-form"); form.noValidate = true;
    const kinds = data.credential_kinds;
    const credential = select(kinds.map(k => [k, label(k, "wl.credkind.")]), f.credential_kind);
    credential.onchange = () => { Object.assign(f, read()); f.credential_kind = credential.value; draw(); };
    const id = textInput(f.id), name = textInput(f.name), currency = textInput(f.currency, { placeholder: "EUR" });
    const terms = textInput(f.terms_url, { placeholder: "https://" }), checked = textInput(f.terms_checked_on); checked.type = "date";
    const revoke = textInput(f.how_to_revoke);
    const wantKind = f.credential_kind === "vault_card" ? "card" : f.credential_kind === "vault_login" ? "login" : "";
    const entries = (data.entries || []).filter(e => e.kind === wantKind);
    const entry = wantKind ? select(entries.map(e => [e.id, `${e.label} (${e.id})`]), f.vault_entry || (entries[0] || {}).id || "", !entries.length) : null;
    const read = () => ({ id: id.value.trim(), name: name.value.trim(), currency: currency.value.trim(), terms_url: terms.value.trim(), terms_checked_on: checked.value,
      how_to_revoke: revoke.value.trim(), vault_entry: entry ? entry.value : "" });
    form.append(field(T("wl.sys.name"), name), field(T("wl.sys.id"), id), field(T("wl.sys.pays"), credential));
    if (entry) form.append(field(T("wl.sys.entry"), entry, entries.length ? "" : T("wl.sys.noentry")));
    form.append(field(T("wl.currency"), currency), field(T("wl.sys.terms"), terms), field(T("wl.sys.checked"), checked), field(T("wl.sys.revoke"), revoke));
    const out = h("p", "wl-msg", ""); out.setAttribute("role", "alert");
    const save = h("button", "btn primary", T("wl.save")); save.type = "submit";
    form.append(out, h("div", "wl-actions", save, btn(T("wl.cancel"), "ghost", () => { sysForm = null; draw(); })));
    form.onsubmit = async ev => {
      ev.preventDefault();
      save.disabled = true;
      const r = await call("POST", "api/wallet/system", { ...read(), credential_kind: credential.value, kind: "payment system" });
      save.disabled = false;
      if (r.ok) { sysForm = null; say(T("wl.saved")); await load(); } else out.textContent = why(r);
    };
    return form;
  }
  function paySection() {
    const sec = h("section", "sec wl-sec", h("div", "sh", h("b", "", T("wl.pay")), h("span", "grow"),
      btn(T("wl.sys.add"), "primary", () => { sysForm = { id: "", name: "", currency: "EUR", terms_url: "", terms_checked_on: new Date().toISOString().slice(0, 10), how_to_revoke: "", vault_entry: "", credential_kind: "vault_card" }; draw(); })));
    const body = h("div", "wl-body", h("p", "help", T("wl.pay.help")));
    if (sysForm) body.append(systemForm());
    const list = h("ul", "wl-list");
    (data.systems || []).forEach(s => list.append(systemCard(s)));
    body.append((data.systems || []).length ? list : h("p", "dim", T("wl.pay.none")));
    sec.append(body);
    return sec;
  }

  /* ---- the browser the agent uses ---- */
  function browserSection() {
    const b = data.browser || { found: false, enabled: false, headless: false };
    const on = h("input"); on.type = "checkbox"; on.id = "wl-br-on"; on.checked = b.enabled; on.disabled = !b.found;
    const hide = h("input"); hide.type = "checkbox"; hide.id = "wl-br-hide"; hide.checked = b.headless; hide.disabled = !b.found;
    const save = async () => {
      const r = await call("POST", "api/wallet/browser", { enabled: on.checked, headless: hide.checked, private: priv.checked, persistent: keep.checked });
      if (r.ok) { data.browser = r.data.browser; say(T("wl.saved")); draw(); } else say(why(r), true);
    };
    const priv = h("input"); priv.type = "checkbox"; priv.id = "wl-br-priv"; priv.checked = !!b.private; priv.disabled = !b.found;
    const keep = h("input"); keep.type = "checkbox"; keep.id = "wl-br-keep"; keep.checked = !!b.persistent; keep.disabled = !b.found;
    keep.onchange = async () => { if (keep.checked && !(await riskConfirm([T("wl.risk.persist")]))) { keep.checked = false; return; } save(); };
    on.onchange = save; hide.onchange = save;
    priv.onchange = async () => { if (priv.checked && !(await riskConfirm([T("wl.risk.private")]))) { priv.checked = false; return; } save(); };
    const row = (input, text) => { const l = h("label", "wl-rm", input, h("span", "", text)); l.htmlFor = input.id; return l; };
    return h("section", "sec wl-sec", h("div", "sh", h("b", "", T("wl.br"))), h("div", "wl-body", h("p", "help", T("wl.br.help")),
      b.found ? null : h("p", "wl-note", T("wl.br.none")), row(on, T("wl.br.on")), row(hide, T("wl.br.hide")), row(priv, T("wl.br.private")),
      ...(b.private ? [riskLabel(T("wl.risk.private"))] : []), row(keep, T("wl.br.keep")), ...(b.persistent ? [riskLabel(T("wl.risk.persist"))] : []),
      ...(b.persistent ? [h("p", "help", T("wl.br.signin.help")), h("div", "wl-actions", btn(T("wl.br.signin"), "", async () => {
        const r = await call("POST", "api/wallet/browser/signin", {});
        say(r.ok ? T("wl.br.opened") : why(r), !r.ok);
      }), pressAgain("forget", btn(T("wl.br.forget"), "ghost", () => {}), T("wl.br.forget"), T("wl.br.forget.again"), async () => {
        const r = await call("POST", "api/wallet/browser/forget", {});
        say(r.ok ? T("wl.br.forgot") : why(r), !r.ok);
      }))] : []), h("p", "help", T("wl.br.next"))));
  }

  /* ---- the history ---- */
  function histSection() {
    const rows = (data.ledger || []).map(r => {
      const d = r.details || {};
      const text = d.amount ? `${d.amount} → ${d.recipient || ""} (${d.reason || ""}) · ${d.system || ""}` + (d.why ? ` · ${d.why}` : "")
        : d.per_payment_cents !== undefined ? `${d.system} · ${(d.per_payment_cents / 100).toFixed(2)} / ${(d.cumulative_cents / 100).toFixed(2)} ${d.currency || ""}` : (d.system || "");
      return h("li", "wl-row", h("span", "wl-when", new Date(r.at * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB")), h("b", "", label(r.action, "wl.act.")), h("span", "dim", text));
    });
    return h("section", "sec wl-sec", h("div", "sh", h("b", "", T("wl.hist"))), h("div", "wl-body", rows.length ? h("ul", "wl-hist", rows) : h("p", "dim", T("wl.hist.none"))));
  }

  /* ---- automatic payments: the three dialogs, in order ---- */
  const norm = t => t.replace(/[’‘]/g, "'").replace(/[“”]/g, '"').replace(/\s+/g, " ").trim().toLowerCase();
  function openWizard(system) {
    wizard = { step: 1, system, name: "", per: "", cum: "", days: "30", typed: "", pasted: false, intervals: [], last: 0, phrase: "", seen2: false, seen3: false, ack2: false, ack3: false, n2: "", n3: "", error: "", timer: 0 };
    drawWizard();
    if (!dlg.open) dlg.showModal();
  }
  async function refreshPhrase() {
    const w = wizard;
    if (!w.name.trim() || !w.per.trim()) { w.phrase = ""; return paintPhrase(); }
    const r = await call("POST", "api/wallet/phrase", { name: w.name, amount: w.per, lang: LANG });
    w.phrase = r.ok ? r.data.phrase : "";
    w.phraseError = r.ok ? "" : why(r);
    paintPhrase();
  }
  function paintPhrase() {
    const w = wizard, box = dlg.querySelector(".wl-phrase"), next = dlg.querySelector("[data-next]"), miss = dlg.querySelector(".wl-mismatch");
    if (!box) return;
    box.textContent = w.phrase || T("wl.w.fill");
    const ok = !!w.phrase && norm(w.typed) === norm(w.phrase);
    miss.textContent = w.typed && w.phrase && !ok ? T("wl.w.mismatch") : (w.phraseError || "");
    next.disabled = !(ok && w.cum.trim());
  }
  function reader(text, onEnd) {                              // a box to read to the end before the next button works
    const box = h("div", "wl-read", h("p", "", text)); box.tabIndex = 0; box.setAttribute("role", "region");
    const check = () => { if (box.scrollTop + box.clientHeight >= box.scrollHeight - 4) onEnd(); };
    box.addEventListener("scroll", check); setTimeout(check, 0);
    return box;
  }
  function drawWizard() {
    const w = wizard, texts = (data.texts || {})[LANG] || (data.texts || {}).en || {};
    const body = h("div", "wl-wbody");
    body.append(h("h2", "", T("wl.w.title")), h("p", "dim", T("wl.w." + w.step)), h("p", "dim", w.system.name));
    const back = btn(T("wl.w.back"), "ghost", () => { w.step -= 1; w.error = ""; drawWizard(); });
    const close = btn(T("wl.cancel"), "ghost", () => { dlg.close(); wizard = null; });
    const next = h("button", "btn primary", T("wl.w.next")); next.type = "button"; next.dataset.next = "1";
    const msg = h("p", "wl-msg", w.error); msg.setAttribute("role", "alert");
    if (w.step === 1) {
      body.append(h("p", "", texts.waiver || ""));
      const name = textInput(w.name), per = textInput(w.per, { mode: "decimal" }), cum = textInput(w.cum, { mode: "decimal" }), days = textInput(w.days, { mode: "numeric" });
      const typed = h("textarea"); typed.rows = 4; typed.spellcheck = false; typed.value = w.typed; typed.autocomplete = "off";
      name.oninput = () => { w.name = name.value; clearTimeout(w.timer); w.timer = setTimeout(refreshPhrase, 250); };
      per.oninput = () => { w.per = per.value; clearTimeout(w.timer); w.timer = setTimeout(refreshPhrase, 250); };
      cum.oninput = () => { w.cum = cum.value; paintPhrase(); };
      days.oninput = () => { w.days = days.value; };
      typed.oninput = () => { w.typed = typed.value; paintPhrase(); };
      typed.addEventListener("paste", () => { w.pasted = true; });
      typed.addEventListener("keydown", e => { const t = performance.now(); if (w.last && w.intervals.length < 2000) w.intervals.push(Math.round(t - w.last)); w.last = t; });
      const copy = btn(T("wl.w.copy"), "ghost", async () => {
        try { await navigator.clipboard.writeText(w.phrase); copy.textContent = T("wl.w.copied"); } catch (e) { const r = document.createRange(); r.selectNodeContents(dlg.querySelector(".wl-phrase")); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
      });
      body.append(field(T("wl.w.name"), name), field(T("wl.w.per") + (w.system.currency ? ` (${w.system.currency})` : ""), per), field(T("wl.w.cum") + (w.system.currency ? ` (${w.system.currency})` : ""), cum),
        field(T("wl.w.days"), days), h("div", "field", h("b", "", T("wl.w.phrase")), h("p", "wl-phrase", T("wl.w.fill")), copy), field(T("wl.w.typed"), typed), h("p", "wl-mismatch dim", ""));
      next.onclick = () => { w.step = 2; drawWizard(); };
    } else {
      const two = w.step === 2, key = two ? "2" : "3";
      body.append(reader(texts[two ? "release" : "acceptance"] || "", () => { if (!w["seen" + key]) { w["seen" + key] = true; const c = dlg.querySelector("[data-ack]"); if (c) c.disabled = false; sync(); } }));
      const ack = h("input"); ack.type = "checkbox"; ack.id = "wl-ack"; ack.dataset.ack = "1"; ack.checked = w["ack" + key]; ack.disabled = !w["seen" + key];
      const al = h("label", "wl-rm", ack, h("span", "", T("wl.w.read"))); al.htmlFor = "wl-ack";
      const again2 = textInput(w["n" + key]);
      function sync() { next.disabled = !(w["seen" + key] && ack.checked && norm(again2.value) === norm(w.name)); }
      ack.onchange = () => { w["ack" + key] = ack.checked; sync(); };
      again2.oninput = () => { w["n" + key] = again2.value; sync(); };
      body.append(h("p", "dim", T("wl.w.scroll")), al, field(T("wl.w.retype"), again2));
      if (two) next.onclick = () => { w.step = 3; w.error = ""; drawWizard(); };
      else {
        next.textContent = T("wl.w.give");
        next.onclick = async () => {
          next.disabled = true;
          const r = await call("POST", "api/wallet/grant", { system_id: w.system.id, user_name: w.name, typed_text: w.typed, per_payment: w.per, cumulative: w.cum,
            read_to_end: w.seen2 && w.seen3, release_ack: w.ack2, retyped_names: { release: w.n2, acceptance: w.n3 }, pasted: w.pasted, ttl_days: Number(w.days) || 30,
            lang: LANG, typing_intervals_ms: w.intervals });
          if (r.ok) { dlg.close(); say(T("wl.w.done", w.system.name)); wizard = null; await load(); }
          else { w.error = why(r); next.disabled = false; msg.textContent = w.error; }
        };
      }
      next.disabled = true; setTimeout(sync, 0);
    }
    const row = h("div", "wl-actions", close);
    if (w.step > 1) row.prepend(back);
    row.append(next);
    body.append(msg, row);
    dlg.replaceChildren(body);
    if (w.step === 1) { next.disabled = true; refreshPhrase(); }
  }
  dlg.addEventListener("cancel", () => { wizard = null; });

  /* ---- the page ---- */
  const pill = document.getElementById("wl-pill"), wrap = document.getElementById("wl-pillwrap"), pop = document.getElementById("wl-pop");
  function closePop(focus) {
    if (!pop || pop.hidden) return;
    pop.hidden = true; pill.setAttribute("aria-expanded", "false");
    if (focus) pill.focus();
  }
  function paintPop(on) {                                          // the detail behind the pill: the limits and the expiry of each system, as text
    pop.setAttribute("aria-label", T("wl.pill.panel"));
    pop.replaceChildren(...on.map(s => h("p", "", T("wl.stop.line", s.name, s.waiver.per_payment, s.waiver.cumulative, s.waiver.spent, s.waiver.remaining, when(s.waiver.expires_at)))),
      h("div", "wl-actions", btn(T("wl.pill.open"), "primary", () => { closePop(false); view("wallet", true); }), btn(T("wl.pill.close"), "ghost", () => closePop(true))));
  }
  function paintPill() {                                          // while automatic payments are on, every screen says so; the pill opens the detail
    if (!pill || !data) return;
    const on = (data.systems || []).filter(s => s.waiver);
    wrap.hidden = !on.length;
    if (!on.length) { closePop(false); return; }
    const short = on.length === 1 ? `${on[0].waiver.per_payment} / ${on[0].waiver.cumulative}` : T("wl.pill.many", on.length);
    pill.textContent = `${T("wl.pill")} · ${short}`;
    if (!pop.hidden) paintPop(on);
  }
  if (pill) {
    pill.onclick = () => {
      if (!pop.hidden) { closePop(false); return; }
      paintPop((data.systems || []).filter(s => s.waiver));
      pop.hidden = false; pill.setAttribute("aria-expanded", "true");
    };
    wrap.addEventListener("keydown", e => { if (e.key === "Escape" && !pop.hidden) { e.stopPropagation(); closePop(true); } });
    wrap.addEventListener("focusout", e => { if (!pop.hidden && e.relatedTarget && !wrap.contains(e.relatedTarget)) closePop(false); });
    document.addEventListener("pointerdown", e => { if (!pop.hidden && !wrap.contains(e.target)) closePop(false); });
  }
  function draw() {
    paintPill();
    if (!data) return;
    const n = h("p", "wl-note", note); n.hidden = !note; n.dataset.bad = noteBad ? "1" : "";
    const problems = (data.problems || []).map(p => h("p", "wl-note", p));
    problems.forEach(p => { p.dataset.bad = "1"; });
    root.replaceChildren(...[banner(), h("div", "wl-top", h("p", "wl-lead", T("wl.lead")), n, ...problems), vaultSection(), paySection(), browserSection(), histSection()].filter(Boolean));
  }
  const inField = () => { const a = document.activeElement; return !!a && root.contains(a) && a.matches("input, select, textarea"); };
  /* quiet: a periodic or view refresh. It redraws only when the data changed, and never while a field of the page has the focus (the next one does),
     so what is being typed, and the cursor, stay where they are. A load after an action always redraws. */
  async function load(quiet) {
    const r = await call("GET", "api/wallet");
    if (!r.ok) { if (!document.getElementById("view-wallet").hidden) root.replaceChildren(h("p", "wl-note", why(r))); shown = ""; return; }
    data = r.data;
    const sig = LANG + JSON.stringify(data);
    if (editing || sysForm || wizard) { if (document.getElementById("view-wallet").hidden) { draw(); shown = sig; } else paintPill(); return; }
    if (quiet && (sig === shown || inField())) { paintPill(); return; }
    draw(); shown = sig;
  }
  load();
  setInterval(() => { if (!document.hidden) load(true); }, 30000);
  document.addEventListener("basalt:view", e => { if (e.detail === "wallet") load(true); });
  document.addEventListener("basalt:key", () => { if (!document.getElementById("view-wallet").hidden) load(true); });
  applyT(document.getElementById("view-wallet"));
})();
