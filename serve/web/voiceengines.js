"use strict";
/* The Avatar engines card of Settings > Voice: for each kind of screen (this PC, the web page, the Android app, the ESP32) the engines that run there,
   with what each costs (size, speed note, licence, what it needs), whether it can be used for the avatar of the chosen personality, a radio to choose
   it and the output height to draw at. The engines and their availability come from the voice service (GET api/voice/avatar/engines, which the server
   completes with the choice kept in voice.json); a choice is POST api/voice/avatar/engines/select. Everything the server says enters the page as text.
   voicetab.js puts the card under the Avatar output card; the Avatar card of the Voice tab shows webLine(). */
(function () {
  const TARGETS = ["pc", "web", "android", "esp32"];
  const REFRESH_MS = 30000;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); return node; };
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("ve.e.offline") : `HTTP ${r.status}`);
  };
  const words = o => (o && (o[LANG] || o.en)) || "";

  let data = null, down = "", note = { text: "", bad: false }, root = null, at = 0, inflight = null;

  function weightText(i) {
    if (i.weight_mb == null) return T("ve.weight.nm");
    if (i.weight_mb === 0) return T("ve.weight.none");
    return T("ve.weight", i.weight_mb >= 1000 ? `${(i.weight_mb / 1000).toFixed(1)} GB` : `${i.weight_mb} MB`);
  }
  function trainText(t) {
    if (!t.needed) return "";
    if (t.video_minutes) return T("ve.train.min", t.video_minutes[0], t.video_minutes[1]) + (t.gpu ? " " + T("ve.train.gpu") : "");
    return T("ve.train.long");
  }
  const badge = (text, kind, id) => { const b = el("span", "ve-badge" + (kind ? " ve-" + kind : ""), text); if (id) b.id = id; return b; };

  function row(target, i, chosen, api) {
    const li = el("li", "ve-row"), pick = el("label", "ve-pick"), input = el("input");
    li.dataset.engine = i.id; li.dataset.chosen = chosen ? "1" : "0";
    input.type = "radio"; input.name = `ve-${target}`; input.value = i.id; input.id = `ve-${target}-${i.id}`; input.checked = chosen;
    input.onchange = () => { if (input.checked) choose(api, target, i.id); };
    pick.append(input, el("strong", "ve-name", i.name));
    const badges = el("div", "ve-badges"), lic = i.license, st = i.status || {};
    badges.append(badge(T(i.kind === "personalized" ? "ve.kind.personalized" : "ve.kind.generic"), i.kind === "personalized" ? "train" : ""), badge(weightText(i)));
    if (lic.noncommercial) badges.append(badge(T("ve.lic.nc"), "warn"));
    if (lic.verify) badges.append(badge(T("ve.lic.verify"), "warn"));
    if (!lic.noncommercial && !lic.verify) badges.append(badge(T("ve.lic", lic.id)));
    badges.append(badge(st.available ? T("ve.avail.yes") : T("ve.avail.no"), st.available ? "ok" : "off"));
    li.append(pick, badges);
    const more = el("details", "ve-more"), summary = el("summary", "", T("ve.more"));       // the detail of the engine in use is open, the others fold away
    more.open = chosen;
    more.append(summary, el("p", "ve-text", words(i.performance)), el("p", "ve-text ve-lic", words(lic.note)));
    if (!st.available) more.append(Object.assign(el("p", "ve-text ve-why", (LANG === "it" && st.reason_it) || st.reason || ""), { id: `ve-${target}-${i.id}-why` }));
    const train = trainText(i.training);
    if (train) more.append(el("p", "ve-text", train));
    if (i.needs && i.needs.length) {
      const ul = el("ul", "ve-list");
      more.append(el("p", "ve-text ve-needs-h", T("ve.needs")), ul);
      i.needs.forEach(n => ul.append(el("li", "", words(n))));
    }
    li.append(more);
    return li;
  }

  function resolutionField(target, current, api) {
    const wrap = el("div", "vt-field ve-res"), label = el("label", "", T("ve.res")), sel = el("select");
    const info = data.engines.find(e => e.id === data.selection.engines[target]);
    label.htmlFor = sel.id = `ve-res-${target}`;
    const heights = data.resolutions[target] || [];
    heights.forEach(h => {
      const o = el("option", "", info && info.native_resolution === h ? T("ve.res.native", h) : T("ve.res.opt", h));
      o.value = String(h); o.selected = h === current; sel.append(o);
    });
    sel.onchange = () => choose(api, target, data.selection.engines[target], Number(sel.value));
    wrap.append(label, sel, el("p", "vt-help", T("ve.res.help")));
    if (info && info.native_resolution && current > info.native_resolution) wrap.append(el("p", "vt-help ve-up", T("ve.res.up", current, info.native_resolution)));
    return wrap;
  }

  function paintTarget(target, api) {
    const set = el("fieldset", "ve-set"), legend = el("legend", "ve-legend", T("ve.t." + target));
    set.id = `ve-set-${target}`;
    set.append(legend, el("p", "vt-help", T("ve.t." + target + ".help")));
    const list = el("ul", "ve-rows"), chosen = data.selection.engines[target];
    data.engines.filter(i => i.runs_on.includes(target)).forEach(i => list.append(row(target, i, i.id === chosen, api)));
    set.append(list, resolutionField(target, data.selection.resolution[target], api));
    return set;
  }

  function paint(api) {
    if (!root) return;
    const active = document.activeElement && document.activeElement.id, body = clear(root.body);
    if (data) {
      body.append(el("p", "vt-help", data.avatar ? T("ve.avatar", data.avatar) : T("ve.avatar.none")));
      TARGETS.forEach(t => body.append(paintTarget(t, api)));
    } else body.append(el("p", "vt-help", down || T("ve.loading")));
    root.note.textContent = note.text; root.note.hidden = !note.text; root.note.dataset.bad = note.bad ? "1" : "0";
    const again = active && document.getElementById(active);
    if (again && root.box.contains(again) && !again.disabled) again.focus();
  }

  async function refresh(api) {
    if (inflight) return inflight;
    inflight = (async () => {
      const r = await api("GET", "api/voice/avatar/engines");
      if (r.ok) { data = r.data; down = ""; } else { data = null; down = msgOf(r); }
      at = Date.now(); inflight = null;
      document.dispatchEvent(new CustomEvent("tadamoo:avatar-engines"));
    })();
    return inflight;
  }

  async function choose(api, target, engine, resolution) {
    note = { text: "", bad: false };
    const body = { target, engine };
    if (resolution != null) body.resolution = resolution;
    const r = await api("POST", "api/voice/avatar/engines/select", body);
    if (r.ok) {
      data = { ...data, selection: { ...data.selection, ...r.data.selection }, resolutions: r.data.resolutions };
      note = { text: T("ve.saved"), bad: false };
      document.dispatchEvent(new CustomEvent("tadamoo:avatar-engines"));
    } else note = { text: msgOf(r), bad: true };
    paint(api);
  }

  function settingsCard(api) {
    const box = el("section", "vt-card ve-card"), h = el("h3", "vt-h", T("ve.title"));
    h.id = "vb-h-engines"; box.setAttribute("aria-labelledby", h.id);
    const body = el("div", "ve-body"), n = el("p", "vt-note"), again = el("button", "btn", T("ve.refresh"));
    n.id = "ve-note"; n.setAttribute("role", "status"); n.hidden = true;
    again.type = "button"; again.id = "ve-refresh";
    again.onclick = async () => { note = { text: "", bad: false }; await refresh(api); paint(api); };
    box.append(h, el("p", "vt-help", T("ve.intro")), body, n, again);
    root = { box, body, note: n };
    paint(api);
    refresh(api).then(() => paint(api));
    return box;
  }

  /* One line for the Voice tab: the engine and height the web preview uses. Reads again at most every 30 seconds. */
  function webLine(api) {
    if (api && (!data || Date.now() - at > REFRESH_MS) && !inflight) refresh(api);
    if (!data) return "";
    const info = data.engines.find(e => e.id === data.selection.engines.web);
    return info ? T("ve.preview", info.name, data.selection.resolution.web) : "";
  }

  window.VoiceEngines = { settingsCard, refresh, webLine };
})();
