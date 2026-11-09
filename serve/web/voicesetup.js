"use strict";
/* The "Set up" panel of an engine on the Avatar engines card (Settings > Voice): for the PC engines that run upstream code in an environment of their own
   (Wav2Lip v2, SadTalker, ER-NeRF) it lists each prerequisite one by one (the Python of the engine environment, ffmpeg, each file, and for ER-NeRF PyTorch3D and the
   four CUDA modules) with its state, lets the person choose the Python, install a file (the server's download flow: plan, licence acknowledgement, confirm; nothing
   downloads by itself), and press Check, which loads the engine once in its environment. Training an ER-NeRF is the plan, confirm, progress and cancel of the Avatar
   card's "Train a personalised model" section. The panel asks nothing until it is opened. Everything the server says enters the page as text.
   voiceengines.js gives each engine row a node: window.VoiceEngineSetup.panel(engine, api). */
(function () {
  const ENGINES = ["wav2lip_v288", "sadtalker", "er_nerf"];
  const POLL_MS = 1500;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); return node; };
  const words = o => (o && (o[LANG] || o.en)) || "";
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("vse.e.offline") : `HTTP ${r.status}`);
  };
  const mb = n => (n / 1e6 >= 10 ? Math.round(n / 1e6) : (n / 1e6).toFixed(1)) + " MB";
  const svc = (api, method, path, body) => api("POST", "api/voice/service/call", { method, path, ...(body ? { body } : {}) });

  function panel(engine, api) {
    if (!ENGINES.includes(engine)) return document.createDocumentFragment();
    const box = el("details", "vse"), summary = el("summary", "vse-sum", T("vse.title"));
    box.id = `vse-${engine}`;
    const body = el("div", "vse-body");
    box.append(summary, body);
    let setup = null, catalog = {}, plan = null, planning = "", ack = false, nc = false, note = { text: "", bad: false }, timer = 0, opened = false;

    async function load() {
      const [s, m] = await Promise.all([svc(api, "GET", `/avatar/engines/${engine}/setup`), api("GET", "api/voice/models")]);
      if (s.ok) setup = s.data; else note = { text: msgOf(s), bad: true };
      if (m.ok && m.data) {
        const avatar = (m.data.services || []).find(x => x.id === "avatar");
        catalog = {};
        ((avatar && avatar.models) || []).forEach(x => { catalog[x.id] = x; });
      }
      paint();
      const running = setup && setup.check && setup.check.state === "running";
      clearTimeout(timer);
      if (running && box.isConnected) timer = setTimeout(load, POLL_MS);
    }
    async function savePython(path) {
      const r = await svc(api, "POST", "/avatar/engines/python", { path });
      note = r.ok ? { text: T("vse.saved"), bad: false } : { text: msgOf(r), bad: true };
      await load();
    }
    async function check() {
      note = { text: "", bad: false };
      const r = await svc(api, "POST", `/avatar/engines/${engine}/check`);
      if (!r.ok) note = { text: msgOf(r), bad: true };
      await load();
    }
    async function ask(id) {
      planning = id; ack = nc = false; paint();
      const r = await api("GET", "api/voice/download/plan?id=" + encodeURIComponent(id));
      planning = "";
      if (r.ok) { plan = { id, data: r.data }; note = { text: "", bad: false }; } else { plan = null; note = { text: msgOf(r), bad: true }; }
      paint();
    }
    async function install() {
      const p = plan.data;
      const r = await api("POST", "api/voice/download", { id: plan.id, token: p.token, confirm: true, ack, noncommercial: nc });
      if (!r.ok) { note = { text: msgOf(r), bad: true }; if (r.data && r.data.plan) plan.data = r.data.plan; paint(); return; }
      plan = null; note = { text: T("vse.installing"), bad: false }; paint();
      watchDownload();
    }
    async function watchDownload() {
      const r = await api("GET", "api/voice/download/status");
      if (r.ok && ["running", "verifying"].includes(r.data.phase)) { note = { text: T("vse.installing") + " " + (r.data.bytes_total ? Math.floor(100 * r.data.bytes_done / r.data.bytes_total) + "%" : ""), bad: false }; paint(); setTimeout(watchDownload, POLL_MS); return; }
      note = r.ok && r.data.phase === "failed" ? { text: words(r.data.error), bad: true } : { text: "", bad: false };
      await load();
    }

    function item(c) {
      const li = el("li", "vse-item");
      li.dataset.ok = c.ok ? "1" : "0"; li.dataset.id = c.id;
      li.append(el("strong", "vse-mark", c.ok ? T("vse.ok") : T("vse.no")), document.createTextNode(" " + (LANG === "it" ? c.it : c.en)));
      if (c.scope === "training") li.append(el("span", "vse-tag", " " + T("vse.training")));
      if (c.model && !c.ok) {
        const m = catalog[c.model];
        if (m && m.downloadable) {
          const b = el("button", "btn", planning === c.model ? T("vse.planning") : T("vse.install"));
          b.type = "button"; b.id = `vse-install-${c.model}`; b.disabled = !!planning; b.onclick = () => ask(c.model);
          li.append(b);
        } else if (m) li.append(el("p", "vt-help", T("vse.byhand", m.folder || "")));
      }
      return li;
    }

    function planView() {
      const p = plan.data, v = el("div", "vse-plan");
      v.append(el("p", "vt-help vse-strong", T("vse.plan", (catalog[plan.id] || {}).name || plan.id)),
        el("p", "vt-help", T("vse.plan.files", (p.files || []).length, mb(p.remaining_bytes || 0), p.folder || "")));
      const lic = p.license || {};
      v.append(el("p", "vt-help", T("vse.licence", lic.name || "") + " " + words(lic)));
      (p.problems || []).forEach(x => v.append(el("p", "vt-help vse-bad", (LANG === "it" && x.text_it) || x.text)));
      const ready = !!p.ok;
      const row = (id, text, get, set) => {
        const l = el("label", "vse-ack"), i = el("input");
        i.type = "checkbox"; i.id = id; i.checked = get(); i.disabled = !ready; i.onchange = () => { set(i.checked); paint(); };
        l.append(i, el("span", "", text));
        return l;
      };
      v.append(row("vse-ack", T("vse.ack"), () => ack, x => { ack = x; }));
      if (p.noncommercial) v.append(row("vse-nc", T("vse.nc"), () => nc, x => { nc = x; }));
      const go = el("button", "btn", T("vse.confirm")), no = el("button", "btn", T("vse.cancel"));
      go.type = no.type = "button"; go.id = "vse-confirm"; no.id = "vse-cancel";
      go.disabled = !ready || !ack || (p.noncommercial && !nc);
      go.onclick = install; no.onclick = () => { plan = null; paint(); };
      v.append(go, no);
      return v;
    }

    function paint() {
      if (!opened) return;
      clear(body);
      if (!setup) { body.append(el("p", "vt-help", note.text || T("vse.loading"))); return; }
      body.append(el("p", "vt-help", T("vse.help")));
      const pf = el("div", "vt-field"), lab = el("label", "", T("vse.python")), input = el("input"), save = el("button", "btn", T("vse.python.save"));
      input.type = "text"; input.id = `vse-python-${engine}`; input.value = setup.python.path || ""; input.autocomplete = "off"; input.spellcheck = false;
      input.placeholder = T("vse.python.ph"); lab.htmlFor = input.id; save.type = "button"; save.id = `vse-python-save-${engine}`;
      save.onclick = () => savePython(input.value.trim());
      pf.append(lab, input, save, el("p", "vt-help", T("vse.python.src." + setup.python.source)));
      body.append(pf);
      const ul = el("ul", "vse-list");
      setup.checks.forEach(c => ul.append(item(c)));
      body.append(ul);
      if (plan) body.append(planView());
      const st = setup.check || { state: "idle" }, go = el("button", "btn", st.state === "running" ? T("vse.check.running") : T("vse.check"));
      go.type = "button"; go.id = `vse-check-${engine}`; go.disabled = st.state === "running"; go.onclick = check;
      body.append(go, el("p", "vt-help", T("vse.check.help")));
      if (st.state === "failed") body.append(Object.assign(el("p", "vt-help vse-bad", T("vse.check.failed", st.error || "")), { id: `vse-check-err-${engine}` }));
      if (engine === "er_nerf") body.append(el("p", "vt-help", T("vse.train")));
      const n = el("p", "vt-help vse-note", note.text || ""); n.hidden = !note.text; n.dataset.bad = note.bad ? "1" : "0"; n.setAttribute("role", "status");
      body.append(n);
    }

    box.addEventListener("toggle", () => {
      if (box.open && !opened) { opened = true; paint(); load(); }
      else if (box.open) load();
      else clearTimeout(timer);
    });
    return box;
  }

  window.VoiceEngineSetup = { panel };
})();
