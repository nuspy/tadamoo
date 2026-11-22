"use strict";
/* Skills: install new ones (from the list that comes with the program, a folder, a GitHub address or a web page), turn them on or off, update and remove them.
   One component, drawn in two places: Settings > Skills and, through the /install-skill command, inside a conversation. It only calls /api/skills; the work
   (and what an address holds, the models, the progress of a long installation) is the server's. Everything the server says is put in the page as text. */
(() => {
  const POLL_MS = 1500;
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
  const why = r => (r.status === 0 ? T("sk.e.offline") : r.status === 403 ? T("sk.e.local") : ((r.data.error || {}).message || `HTTP ${r.status}`));
  const kb = n => (n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const gbText = gb => nfmt(gb, gb % 1 ? 1 : 0);
  const pick = o => (o && (o[LANG] || o.en)) || "";
  const when = iso => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString(LANG); };
  const ICONS = { on: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12.5l3 3 5-6", off: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM10 9v6M14 9v6", none: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z" };
  function icon(name) {
    const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg"), p = document.createElementNS(NS, "path");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "sk-ico"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    p.setAttribute("d", ICONS[name]); p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    if (name === "none") p.setAttribute("stroke-dasharray", "3 3");
    s.append(p);
    return s;
  }
  const tell = origin => document.dispatchEvent(new CustomEvent("tadamoo:skills", { detail: { origin } }));

  function mount(host, opts) {
    const origin = (opts && opts.origin) || "tab";
    const S = { rows: [], job: null, plan: null, preview: "", msg: "", bad: false, remove: "", done: new Set(), dismissed: "", loaded: false, busy: false };
    let timer = 0, address = "";
    const say = (text, bad) => { S.msg = text || ""; S.bad = !!bad; };
    const running = () => !!S.job && S.job.state === "running";
    const titleOf = name => { const r = S.rows.find(x => x.name === name); return r ? pick(r.title) || name : name; };

    async function refresh() {
      const r = await call("GET", "api/skills");
      if (!r.ok) { say(why(r), true); S.loaded = true; paint(); return; }
      S.rows = r.data.skills || []; S.job = r.data.job || null; S.loaded = true;
      paint();
      if (running()) watch();
    }
    function watch() {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const r = await call("GET", "api/skills/job");
        if (r.ok) S.job = r.data.job;
        if (running()) { paint(true); watch(); return; }
        await finished();
      }, POLL_MS);
    }
    async function finished() {
      const job = S.job;
      await refresh();
      if (job && job.state === "done") (job.result.installed || []).concat(job.result.updated || [], job.result.same || []).forEach(n => S.done.add(n));
      paint();
      tell(origin);
    }
    async function act(path, payload, okText) {
      const r = await call("POST", path, payload);
      if (!r.ok) { say(why(r), true); paint(); return null; }
      say(typeof okText === "function" ? okText(r.data) : okText || "");
      await refresh();
      tell(origin);
      return r.data;
    }
    /* the update that keeps what the person changed: the window of updatemerge.js, with this tab's calls */
    function update(r) {
      const name = pick(r.title) || r.name;
      UpdateMerge.open({ title: name, errText: why,
        load: instructions => call("POST", "api/skills/update/plan", { name: r.name, instructions, language: LANG }),
        apply: body => call("POST", "api/skills/update/apply", body),
        notes: { set: notes => call("POST", "api/skills/update/notes", { name: r.name, notes }) },
        done: async text => { say(text); await refresh(); tell(origin); } });
    }
    async function start(path, payload) {
      const r = await call("POST", path, payload);
      if (!r.ok) { say(why(r), true); paint(); return; }
      S.job = r.data.job; S.dismissed = ""; S.preview = ""; say("");
      paint(); watch();
    }

    /* ---- what an address holds ---- */
    async function read(text) {
      address = text.trim();
      if (!address) return;
      say(T("sk.reading")); S.plan = null; paint();
      const r = await call("POST", "api/skills/plan", { address });
      if (!r.ok) { say(why(r), true); paint(); return; }
      say(""); S.plan = r.data; paint();
    }
    async function discard() {
      const token = S.plan && S.plan.token;
      S.plan = null; say(""); paint();
      if (token) await call("POST", "api/skills/discard", { token });
    }

    /* ---- the parts ---- */
    function addCard() {
      const input = h("input"); input.type = "text"; input.id = "sk-address-" + origin; input.value = address; input.autocomplete = "off"; input.spellcheck = false; input.placeholder = T("sk.add.ph");
      input.addEventListener("input", () => { address = input.value; });
      const form = h("form", "sk-form", h("label", "sk-field", h("span", "", T("sk.add.label")), input), btn(T("sk.add.go"), "primary", () => {}));
      form.lastChild.type = "submit";
      form.onsubmit = e => { e.preventDefault(); read(input.value); };
      return h("section", "sk-card sk-add", h("h3", "", T("sk.add.title")), h("p", "setnote", T("sk.add.help")), form);
    }

    function foundCard() {
      const p = S.plan, ok = p.candidates.filter(c => !c.error && !(c.blockers || []).length);
      const card = h("section", "sk-card sk-found", h("h3", "", p.generated ? T("sk.found.gen.title") : T("sk.found.title", p.address)));
      if (p.generated) card.append(h("p", "setnote", T("sk.found.gen")));
      const list = h("ul", "sk-list");
      p.candidates.forEach(c => {
        const li = h("li", "sk-item");
        li.append(h("div", "sk-head", h("b", "sk-name", c.name), h("span", "sk-badge", T(c.kind === "plugin" ? "sk.kind.plugin" : "sk.kind.skill"))));
        if (c.description) li.append(h("p", "sk-sum", c.description));
        if (c.files) li.append(h("p", "setnote sk-meta", T("sk.files", c.files, kb(c.bytes))));
        (c.warnings || []).forEach(w => li.append(h("p", "setnote sk-meta", w)));
        if (c.error) li.append(h("p", "setnote warnnote", c.error));
        (c.blockers || []).forEach(b => li.append(h("p", "setnote warnnote", T("sk.found.blocked", b))));
        if (c.already_installed && !S.done.has(c.name)) li.append(h("p", "setnote sk-meta", T("sk.found.replace")));
        if (c.skill_md) li.append(h("details", "sk-md", h("summary", "", T("sk.found.read")), h("pre", "", c.skill_md)));
        if (S.done.has(c.name)) li.append(h("p", "sk-state", icon("on"), h("span", "", T("sk.found.done"))));
        else if (!c.error && !(c.blockers || []).length) li.append(h("div", "sk-acts", btn(T(c.already_installed ? "sk.found.again" : "sk.install"), "primary", () => start("api/skills/install", { token: p.token, only: [c.id] }))));
        if (c.skill_md) li.querySelector("details").open = true;
        list.append(li);
      });
      card.append(list);
      const acts = h("div", "sk-acts");
      const todo = ok.filter(c => !S.done.has(c.name));
      if (todo.length > 1) acts.append(btn(T("sk.install.all", todo.length), "primary", () => start("api/skills/install", { token: p.token, only: todo.map(c => c.id) })));
      acts.append(btn(T("sk.discard"), "ghost", discard));
      card.append(acts);
      return card;
    }

    function previewCard() {
      const r = S.rows.find(x => x.name === S.preview);
      if (!r || r.state !== "not_installed") return null;
      const card = h("section", "sk-card sk-found", h("h3", "", T("sk.preview.title", pick(r.title) || r.name)));
      if (pick(r.summary)) card.append(h("p", "sk-sum", pick(r.summary)));
      const also = (r.will_install || []).filter(n => n !== r.name);
      if (also.length) card.append(h("p", "setnote sk-meta", T("sk.also", also.map(titleOf).join(", "))));
      card.append(h("p", "setnote sk-meta", r.download_gb ? T("sk.size", gbText(r.download_gb)) : r.has_models ? T("sk.size.unknown") : T("sk.size.none")));
      const go = btn(T("sk.install"), "primary", () => start("api/skills/install", { names: [r.name] }));
      go.disabled = running();
      card.append(h("div", "sk-acts", go, btn(T("sk.cancel"), "ghost", () => { S.preview = ""; paint(); })));
      return card;
    }

    function jobCard() {
      const j = S.job;
      if (!j || (j.state !== "running" && S.dismissed === j.id)) return null;
      const what = j.what === "models" ? T("sk.job.models", titleOf(j.names[0])) : T("sk.job.installing", j.names.length === 1 ? titleOf(j.names[0]) : j.names.map(titleOf).join(", ") || T("sk.job.skills"));
      const card = h("section", "sk-card sk-job", h("h3", "", what));
      card.dataset.state = j.state;
      if (j.state === "running") {
        const bar = h("progress"); bar.max = Math.max(j.steps, 1); bar.value = Math.min(j.step, bar.max); bar.setAttribute("aria-label", what);
        card.append(h("p", "setnote", T(j.phase === "models" ? "sk.job.phase.models" : "sk.job.phase.copy", j.current ? titleOf(j.current) : ""), " ", j.steps ? T("sk.job.step", Math.min(j.step + 1, j.steps), j.steps) : ""), bar,
          h("p", "setnote sk-line", j.line || ""), h("div", "sk-acts", btn(T("sk.stop"), "ghost", async () => { await call("POST", "api/skills/job/cancel", {}); })));
      } else {
        const r = j.result || {};
        if (j.state === "done") {
          card.append(h("p", "sk-state", icon("on"), h("span", "", T("sk.job.done"))));
          [["installed", "sk.res.installed"], ["updated", "sk.res.updated"], ["same", "sk.res.same"]].forEach(([k, key]) => { if ((r[k] || []).length) card.append(h("p", "setnote", T(key, r[k].map(titleOf).join(", ")))); });
          (r.models || []).forEach(m => card.append(h("p", "setnote", m.ready ? T("sk.res.models.ok", titleOf(m.name)) : T("sk.res.models.no", titleOf(m.name), (m.by_hand || []).join(" ")))));
          (r.problems || []).forEach(x => card.append(h("p", "setnote warnnote", T("sk.res.problem", x.name, x.why))));
          if (r.ready === true) card.append(h("p", "setnote", T("sk.res.models.ok", titleOf(r.name))));
          if (r.ready === false) card.append(h("p", "setnote warnnote", T("sk.res.models.no", titleOf(r.name), (r.by_hand || []).join(" "))));
        } else if (j.state === "cancelled") card.append(h("p", "setnote", T("sk.job.cancelled")));
        else card.append(h("p", "setnote warnnote", T("sk.job.failed", j.error || "")));
        card.append(h("div", "sk-acts", btn(T("sk.close"), "", () => { S.dismissed = j.id; paint(); })));
      }
      return card;
    }

    function stateBadge(r) {
      const k = r.state === "installed" ? "on" : r.state === "disabled" ? "off" : "none";
      return h("span", "sk-badge sk-state-" + k, icon(k), h("span", "", T("sk.state." + k)));
    }

    function sourceLine(r) {
      const s = r.source || {};
      if (s.type === "builtin") return T("sk.from.builtin");
      if (s.generated) return T("sk.from.generated", s.ref);
      return T("sk.from." + (s.type === "github" || s.type === "web" ? s.type : "folder"), s.ref || "");
    }

    function card(r) {
      const li = h("li", "sk-item"), installed = r.state !== "not_installed", locked = running();
      li.dataset.name = r.name; li.dataset.state = r.state;
      li.append(h("div", "sk-head", h("b", "sk-name", pick(r.title) || r.name), stateBadge(r)));
      if (pick(r.summary)) li.append(h("p", "sk-sum", pick(r.summary)));
      const meta = [];
      if (!installed) {
        const also = (r.will_install || []).filter(n => n !== r.name);
        if (also.length) meta.push(T("sk.also", also.map(titleOf).join(", ")));
        meta.push(r.download_gb ? T("sk.size", gbText(r.download_gb)) : r.has_models ? T("sk.size.unknown") : T("sk.size.none"));
      } else {
        if (r.has_models) meta.push(T("sk.models." + r.models) + (r.models !== "ready" && r.download_gb ? " " + T("sk.size", gbText(r.download_gb)) : ""));
        if ((r.needed_by || []).length) meta.push(T("sk.neededby", r.needed_by.map(titleOf).join(", ")));
        meta.push(sourceLine(r), T("sk.when", when(r.installed_at)));
      }
      meta.forEach(m => li.append(h("p", "setnote sk-meta", m)));
      const acts = h("div", "sk-acts");
      if (!installed) {
        if (r.present === false) acts.append(h("span", "setnote warnnote", T("sk.missing")));
        else { const b = btn(T("sk.install"), "primary", () => start("api/skills/install", { names: [r.name] })); b.disabled = locked; acts.append(b); }
      } else {
        acts.append(btn(T(r.state === "installed" ? "sk.off" : "sk.on"), r.state === "installed" ? "" : "primary", () => act(`api/skills/${r.state === "installed" ? "disable" : "enable"}`, { name: r.name },
          () => T(r.state === "installed" ? "sk.disabled" : "sk.enabled", pick(r.title) || r.name))));
        if (r.has_models && r.models !== "ready") { const b = btn(T("sk.models.get"), "", () => start("api/skills/models", { name: r.name })); b.disabled = locked; acts.append(b); }
        acts.append(btn(T("sk.remove"), "ghost", () => { S.remove = S.remove === r.name ? "" : r.name; paint(); }));
        const more = h("details", "sk-more", h("summary", "", T("sk.more")), h("div", "sk-acts",
          btn(T("sk.update"), "", () => update(r)),
          r.has_models ? btn(T("sk.models.check"), "", async () => {
            say(T("sk.checking")); paint();
            const d = await call("POST", "api/skills/models/check", { name: r.name });
            if (!d.ok) say(why(d), true);
            else say(d.data.ready ? T("sk.models.ok", pick(r.title) || r.name) : T("sk.models.lacking", pick(r.title) || r.name, d.data.components.filter(c => !c.present).map(c => LANG === "it" ? c.label_it : c.label).join("; ")), !d.data.ready);
            await refresh();
          }) : null));
        li.append(acts, more);
        if (S.remove === r.name) {
          const ask = h("div", "sk-ask", h("p", "", T("sk.remove.ask", pick(r.title) || r.name, T(r.has_models ? "sk.remove.models" : "sk.remove.nomodels"))));
          if ((r.needed_by || []).length) ask.append(h("p", "setnote warnnote", T("sk.remove.needed", r.needed_by.map(titleOf).join(", "))));
          ask.append(h("div", "sk-acts", btn(T("sk.remove.yes"), "sk-danger", async () => {
            S.remove = "";
            await act("api/skills/remove", { name: r.name }, d => T("sk.removed", pick(r.title) || r.name) + " " + T(d.models_kept ? "sk.remove.models" : "sk.remove.nomodels")
              + ((d.needed_by || []).length ? " " + T("sk.remove.needed", d.needed_by.map(titleOf).join(", ")) : ""));
          }), btn(T("sk.cancel"), "ghost", () => { S.remove = ""; paint(); })));
          li.append(ask);
        }
        return li;
      }
      li.append(acts);
      return li;
    }

    function section(title, rows, none) {
      return h("section", "sk-sec", h("h3", "", `${title} (${rows.length})`), rows.length ? h("ul", "sk-list", rows.map(card)) : h("p", "dim", none));
    }

    function paint(light) {
      const focused = document.activeElement && host.contains(document.activeElement) ? document.activeElement.id : "";
      const msg = h("p", "setnote sk-msg", S.msg); msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite"); msg.dataset.bad = S.bad ? "1" : ""; msg.hidden = !S.msg;
      if (light && host.querySelector(".sk-job")) {            // while a job runs only its card is redrawn, so a field being typed in is left alone
        const old = host.querySelector(".sk-job"), now = jobCard();
        if (now) old.replaceWith(now);
        return;
      }
      const installed = S.rows.filter(r => r.state !== "not_installed"), available = S.rows.filter(r => r.state === "not_installed");
      host.replaceChildren(...[opts && opts.compact ? null : h("p", "setnote sk-intro", T("sk.intro")), jobCard(), addCard(), S.plan ? foundCard() : null, previewCard(), msg,
        S.loaded ? section(T("sk.installed.title"), installed, T("sk.none.installed")) : h("p", "dim", T("sk.loading")), S.loaded ? section(T("sk.available.title"), available, T("sk.none.available")) : null].filter(Boolean));
      if (focused) { const f = document.getElementById(focused); if (f) f.focus(); }
    }

    /* ---- the words of /install-skill ---- */
    async function command(words) {
      const text = (words || "").trim();
      const m = /^(remove|delete|on|off|enable|disable|update)\s+(.+)$/i.exec(text);
      if (!text || /^(list|ls)$/i.test(text)) { paint(); return; }
      const named = n => S.rows.find(r => r.name.toLowerCase() === n.toLowerCase() || pick(r.title).toLowerCase() === n.toLowerCase());
      if (m) {
        const row = named(m[2].trim()), verb = m[1].toLowerCase();
        if (!row || row.state === "not_installed") { say(T("sk.cmd.notinstalled", m[2].trim()), true); paint(); return; }
        const name = pick(row.title) || row.name;
        if (verb === "remove" || verb === "delete") { S.remove = row.name; paint(); return; }
        if (verb === "update") { update(row); return; }
        const on = verb === "on" || verb === "enable";
        await act(`api/skills/${on ? "enable" : "disable"}`, { name: row.name }, () => T(on ? "sk.enabled" : "sk.disabled", name));
        return;
      }
      const row = named(text);
      if (row) {                                                           // a skill that comes with the program: what it brings and weighs is shown, then Install
        if (row.state !== "not_installed") { say(T("sk.cmd.already", pick(row.title) || row.name), true); paint(); return; }
        S.preview = row.name; S.plan = null; say(""); paint();
        return;
      }
      address = text;
      await read(text);
    }

    paint();
    const ready = refresh();
    return { refresh, paint, state: S, command: async words => { await ready; return command(words); } };
  }

  /* ---- Settings > Skills ---- */
  let tab = null;
  const tabHost = document.getElementById("sk-root");
  const ensureTab = () => tab || (tabHost ? (tab = mount(tabHost, { origin: "tab" })) : null);
  document.addEventListener("tadamoo:sub", e => { if (e.detail === "skills") { const t = ensureTab(); if (t) t.refresh(); } });
  document.addEventListener("tadamoo:lang", () => { if (tab) tab.paint(); });
  window.skillsUi = {
    mount,
    openTab: words => { const t = ensureTab(); if (t) t.command(words); },                    // the tab, drawn and told what to do
  };
})();
