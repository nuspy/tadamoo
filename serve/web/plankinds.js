"use strict";
/* The kinds of step of a plan on the page: an icon for each, the palette they are picked from (dragged onto the plan with a mouse or a pen, tapped or
   chosen with the keys anywhere), and the form of the fields of a kind. The table of kinds comes from the server (tadamoo/planner/nodes.py), so a kind
   added there appears here with its fields. Everything the server says is put in the page as text. */
(function (root) {
  const ICONS = {
    action: "M4 5h16v14H4z M8 12l3 3 5-6",
    sensor: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z M12 9a3 3 0 100 6 3 3 0 000-6",
    decision: "M12 3l9 9-9 9-9-9z",
    merge: "M4 4l8 8 8-8 M12 12v8",
    wait_duration: "M6 3h12 M6 21h12 M8 3v3l4 4 4-4V3 M8 21v-3l4-4 4 4v3",
    wait_until: "M4 6h16v14H4z M4 10h16 M8 3v4 M16 3v4",
    wait_condition: "M12 3a9 9 0 100 18 9 9 0 000-18z M9.5 9.5a2.5 2.5 0 115 0c0 2-2.5 2-2.5 4 M12 17v.5",
    loop: "M4 12a8 8 0 0113.5-5.8L20 8 M20 4v4h-4 M20 12a8 8 0 01-13.5 5.8L4 16 M4 20v-4h4",
    set_var: "M5 8h14 M5 16h14 M9 4l-2 16 M17 4l-2 16",
    get_var: "M4 12h12 M12 7l5 5-5 5 M20 5v14",
    input: "M4 5h16v10H9l-5 4z",
    output: "M4 19h16 M12 4v11 M7 10l5 5 5-5",
    log: "M5 4h14v16H5z M8 9h8 M8 13h8 M8 17h5",
  };
  const UNITS = [["d", 86400], ["h", 3600], ["min", 60], ["s", 1]];
  const TIME_FIELDS = new Set(["seconds", "every", "timeout_s", "poll_s"]);

  /* ---- pure parts ---- */
  const fieldsOf = (kinds, type) => ((kinds || []).find(k => k.type === type) || { fields: [] }).fields;
  const defaultsOf = fields => Object.fromEntries(fields.filter(f => f.default !== undefined).map(f => [f.name, f.default]));

  /* a length of time in the biggest unit that divides it evenly: 7200 -> {value: 2, unit: "h"} */
  function splitDuration(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n) || n <= 0) return { value: "", unit: "min" };
    const [unit, size] = UNITS.find(([, s]) => n % s === 0) || ["s", 1];
    return { value: n / size, unit };
  }
  const joinDuration = (value, unit) => Number(value) * ((UNITS.find(([u]) => u === unit) || ["s", 1])[1]);

  /* the form's raw values -> the spec the server takes; empty optional fields are left out; `errors` lists the fields that are needed or not understood */
  function readSpec(fields, raw) {
    const spec = {}, errors = [];
    for (const f of fields) {
      let v = raw[f.name];
      const empty = v === undefined || v === null || (typeof v === "string" && !v.trim()) || (Array.isArray(v) && !v.length);
      if (empty) { if (f.required) errors.push(f.name); continue; }
      if (f.kind === "number" || f.kind === "integer") {
        v = TIME_FIELDS.has(f.name) && v && typeof v === "object" ? joinDuration(v.value, v.unit) : Number(v);
        if (!Number.isFinite(v)) { errors.push(f.name); continue; }
      } else if (f.kind === "names") {
        v = (Array.isArray(v) ? v : String(v).split(/[,\n]/)).map(x => String(x).trim()).filter(Boolean);
        if (!v.length) { if (f.required) errors.push(f.name); continue; }
      } else if (f.kind === "json") {
        const text = String(v).trim();
        try { v = JSON.parse(text); } catch (e) { v = text; }
      } else {
        v = String(v).trim();
      }
      spec[f.name] = v;
    }
    return { spec, errors };
  }

  /* a few words about a step's spec for under its title */
  function brief(type, spec) {
    if (!spec) return "";
    const time = s => { const d = splitDuration(s); return `${d.value} ${d.unit}`; };
    if (type === "wait_duration") return time(spec.seconds);
    if (type === "wait_until") return String(spec.at || "");
    if (type === "wait_condition") return String(spec.expr || spec.describe || "");
    if (type === "loop") return `while ${spec.while || ""}`;
    if (type === "set_var") return `${spec.name} = ${spec.expr || JSON.stringify(spec.value)}`;
    if (type === "get_var") return `${spec.name}${spec.into ? " > " + spec.into : ""}`;
    if (type === "input") return String(spec.prompt || "");
    if (type === "output") return String(spec.name || "");
    if (type === "log") return String(spec.message || "");
    return "";
  }

  /* ---- the layouts of a whole plan (pure): {id: {x, y}} for each step, from the plan's layers and its order. `d`: the size of a step, the gaps and the margin. ---- */
  const LAYOUTS = ["lr", "tb", "compact", "lanes"];
  function arrange(kind, snap, d) {
    const nodes = snap.plan.nodes, out = {};
    const layers = snap.layers.map(ids => ids.filter(id => nodes[id])), flat = (snap.order || snap.layers.flat()).filter(id => nodes[id]);
    const at = (x, y) => ({ x: d.clamp(Math.round(x)), y: d.clamp(Math.round(y)) });
    if (kind === "tb") {
      layers.forEach((ids, i) => ids.forEach((id, j) => { out[id] = at(d.PAD + j * (d.W + d.GX), d.PAD + i * (d.H + d.GY * 1.5)); }));
    } else if (kind === "compact") {
      const cols = Math.max(1, Math.ceil(Math.sqrt(flat.length * 1.6)));
      flat.forEach((id, i) => { out[id] = at(d.PAD + (i % cols) * (d.W + d.GX / 2), d.PAD + Math.floor(i / cols) * (d.H + d.GY)); });
    } else if (kind === "lanes") {
      let top = d.PAD;
      ["agent", "user", "third_party"].filter(a => flat.some(id => nodes[id].assignee === a)).forEach(a => {
        let tallest = 1;
        layers.forEach((ids, i) => {
          const mine = ids.filter(id => nodes[id].assignee === a);
          tallest = Math.max(tallest, mine.length);
          mine.forEach((id, j) => { out[id] = at(d.PAD + i * (d.W + d.GX), top + j * (d.H + d.GY)); });
        });
        top += tallest * (d.H + d.GY) + d.GY;
      });
    } else {
      layers.forEach((ids, i) => ids.forEach((id, j) => { out[id] = at(d.PAD + i * (d.W + d.GX), d.PAD + j * (d.H + d.GY)); }));
    }
    return out;
  }
  const api = { ICONS, fieldsOf, defaultsOf, splitDuration, joinDuration, readSpec, brief, UNITS, TIME_FIELDS, arrange, LAYOUTS };
  root.PlanKinds = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.createElement) return;

  /* ---- the page parts ---- */
  const SVGNS = "http://www.w3.org/2000/svg";
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  function icon(type, size) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", String(size || 22)); s.setAttribute("height", String(size || 22));
    s.setAttribute("aria-hidden", "true"); s.setAttribute("class", "pk-icon");
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", ICONS[type] || ICONS.action);
    p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "1.8");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    s.append(p);
    return s;
  }
  /* the icon of the button that arranges the whole plan: boxes joined by lines */
  const LAYOUT_PATH = "M3 3h7v5H3z M14 3h7v5h-7z M8.5 16h7v5h-7z M6.5 8v4h11V8 M12 12v4";
  function layoutIcon() {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "20"); s.setAttribute("height", "20"); s.setAttribute("aria-hidden", "true"); s.setAttribute("class", "pk-icon");
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", LAYOUT_PATH); p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "1.8");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    s.append(p);
    return s;
  }
  /* the same icon as a group inside the plan's own svg */
  function iconGroup(type, x, y, scale) {
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("transform", `translate(${x},${y}) scale(${scale || 0.7})`);
    g.setAttribute("class", "pk-glyph"); g.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", ICONS[type] || ICONS.action);
    p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    g.append(p);
    return g;
  }

  /* The palette. A mouse or a pen drags a kind onto the plan; a tap, or Enter and Space on a button, picks it without dragging (a touch never drags,
     so the page can still be scrolled with a finger). `drop(type, event)` is called when it is let go over the plan, `pick(type)` for a tap. */
  function palette(kinds, opts) {
    const T = opts.T, families = [];
    kinds.forEach(k => { if (!families.includes(k.family)) families.push(k.family); });
    const box = h("div", "pk", ...families.map(fam => h("fieldset", "pk-fam",
      h("legend", "pk-famname", T("pl.fam." + fam)),
      h("ul", "pk-list", ...kinds.filter(k => k.family === fam).map(k => h("li", "", item(k)))))));
    let drag = null, justDragged = false;
    function item(k) {
      const b = h("button", "pk-item", icon(k.type, 22), h("span", "pk-name", T("pl.type." + k.type)));
      b.type = "button"; b.dataset.kind = k.type;
      b.title = T("pl.kind." + k.type + ".d"); b.setAttribute("aria-label", `${T("pl.type." + k.type)}. ${T("pl.kind." + k.type + ".d")}`);
      b.addEventListener("click", () => { if (justDragged) { justDragged = false; return; } opts.pick(k.type); });
      b.addEventListener("pointerdown", e => {
        if (e.pointerType === "touch" || (e.button !== undefined && e.button !== 0)) return;
        drag = { type: k.type, x: e.clientX, y: e.clientY, ghost: null };
      });
      return b;
    }
    const move = e => {
      if (!drag) return;
      if (!drag.ghost) {
        if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 6) return;
        drag.ghost = h("div", "pk-ghost", icon(drag.type, 18), h("span", "", T("pl.type." + drag.type)));
        document.body.append(drag.ghost);
      }
      drag.ghost.style.transform = `translate(${e.clientX + 10}px, ${e.clientY + 10}px)`;
      const over = opts.over(e);
      if (opts.canvas()) opts.canvas().classList.toggle("pk-over", over);
    };
    const end = (e, cancelled) => {
      if (!drag) return;
      const was = drag; drag = null;
      if (was.ghost) {
        was.ghost.remove();
        justDragged = true; setTimeout(() => { justDragged = false; }, 0);
        if (opts.canvas()) opts.canvas().classList.remove("pk-over");
        if (!cancelled && opts.over(e)) opts.drop(was.type, e);
      }
    };
    const up = e => end(e, false), cancel = e => end(e, true), esc = e => { if (e.key === "Escape" && drag) end(e, true); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", esc, true);
    box.dispose = () => {                                            // a palette drawn again (another language) lets go of the old one's listeners
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("keydown", esc, true);
    };
    return box;
  }

  /* The fields of a kind as a form. `collect()` gives {spec, errors}; ids start with `prefix`. */
  function specForm(fields, spec, prefix, T, disabled) {
    const values = { ...defaultsOf(fields), ...(spec || {}) };
    const inputs = {};
    const rows = fields.map(f => {
      const id = `${prefix}-${f.name}`;
      const label = h("label", "", T("pl.spec." + f.name) + (f.required ? " *" : ""));
      label.htmlFor = id;
      let input;
      let v = values[f.name];
      if (f.kind === "choice") {
        input = h("select", "", ...f.choices.map(c => { const o = h("option", "", T("pl.spec.opt." + c)); o.value = c; o.selected = c === v; return o; }));
      } else if (f.kind === "textarea" || f.kind === "names" || f.kind === "json") {
        input = h("textarea"); input.rows = f.kind === "textarea" ? 3 : 2;
        input.value = f.kind === "names" ? (Array.isArray(v) ? v.join(", ") : "") : f.kind === "json" ? (v === undefined ? "" : typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v)) : (v || "");
      } else if (f.kind === "datetime") {
        input = h("input"); input.type = "datetime-local"; input.value = String(v || "").replace(" ", "T").slice(0, 16);
      } else if (TIME_FIELDS.has(f.name)) {
        const d = splitDuration(v);
        const num = h("input"); num.type = "number"; num.min = "0"; num.step = "any"; num.inputMode = "decimal"; num.value = d.value === "" ? "" : String(d.value); num.id = id;
        const unit = h("select", "pk-unit", ...UNITS.map(([u]) => { const o = h("option", "", T("pl.unit." + u)); o.value = u; o.selected = u === d.unit; return o; }));
        unit.setAttribute("aria-label", T("pl.spec.unit"));
        inputs[f.name] = { get: () => (num.value === "" ? "" : { value: num.value, unit: unit.value }), els: [num, unit] };
        return h("div", "pk-row", label, h("div", "pk-dur", num, unit));
      } else {
        input = h("input"); input.type = f.kind === "number" || f.kind === "integer" ? "number" : "text";
        if (input.type === "number") { input.inputMode = "decimal"; input.step = "any"; }
        input.autocomplete = "off"; input.spellcheck = false; input.value = v === undefined ? "" : String(v);
      }
      input.id = id;
      inputs[f.name] = { get: () => input.value, els: [input] };
      return h("div", "pk-row", label, input);
    });
    if (disabled) Object.values(inputs).forEach(i => i.els.forEach(e => { e.disabled = true; }));
    return {
      el: h("div", "pk-spec", ...rows),
      collect() { return readSpec(fields, Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.get()]))); },
    };
  }
  api.icon = icon; api.iconGroup = iconGroup; api.layoutIcon = layoutIcon; api.palette = palette; api.specForm = specForm;
})(typeof window !== "undefined" ? window : globalThis);
