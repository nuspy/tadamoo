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
    rag: "M4 5h6a2 2 0 012 2v12a2 2 0 00-2-2H4z M20 5h-6a2 2 0 00-2 2v12a2 2 0 012-2h6z",
    start: "M7 4l13 8-13 8z",
    start_plan: "M4 5h9v6H4z M11 13h9v6h-9z M13 8h4v3 M11 16H7v-3",
  };
  const OP_WORD = { "<": "lt", "<=": "le", "=": "eq", "!=": "ne", ">=": "ge", ">": "gt", between: "between", in: "in", contains: "contains", else: "else", "": "custom" };
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

  /* a choice as a line of the form, "value | what is shown | what is shown in English"; a choice that is only a text is that text */
  const optionLine = c => (c && typeof c === "object" ? [c.value, c.label || "", c.label_en || ""].join(" | ").replace(/( \| )+$/, "") : String(c));
  function parseOption(line) {
    const [value, label, label_en] = line.split("|").map(x => x.trim());
    return label || label_en ? { value, ...(label ? { label } : {}), ...(label_en ? { label_en } : {}) } : value;
  }
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
      } else if (f.kind === "archives" || f.kind === "branches") {
        v = Array.isArray(v) ? v : [];
        if (!v.length) { if (f.required) errors.push(f.name); continue; }
      } else if (f.kind === "names") {
        v = (Array.isArray(v) ? v : String(v).split(/[,\n]/)).map(x => String(x).trim()).filter(Boolean);
        if (!v.length) { if (f.required) errors.push(f.name); continue; }
      } else if (f.kind === "options") {
        v = (Array.isArray(v) ? v.map(optionLine) : String(v).split(/\n/)).map(x => String(x).trim()).filter(Boolean).map(parseOption);
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
    if (type === "input") return `${spec.ask === "approve" ? "✓ " : ""}${String(spec.prompt || "")}`;
    if (type === "start") return `#${spec.number || 1} ${spec.launch || "manual"}${spec.words ? " " + spec.words : ""}`;
    if (type === "start_plan") return `${spec.plan || ""} #${spec.start || 1}`;
    if (type === "output") return String(spec.name || "");
    if (type === "log") return String(spec.message || "");
    if (type === "rag") return `RAG \u00D7${(spec.archives || []).length}`;
    if (type === "decision") return `${(spec.branches || []).length} \u2192`;
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
  const api = { ICONS, optionLine, parseOption, fieldsOf, defaultsOf, splitDuration, joinDuration, readSpec, brief, UNITS, TIME_FIELDS, arrange, LAYOUTS };
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

  /* The branches of a decision: an ordered list. A comparison branch is three rows, a first term, a comparison and a second term (a fourth for the upper end of
     `between`), each an operand with a source: a value, a python expression, an input of the node (or a value of the plan) or a description for the agent. A branch can also
     be one whole expression, or the Else. Buttons of 44 px move and remove a branch. `ctx` = {inputs: [names wired to the node], variables: [names of the plan's values]}.
     `get()` gives [{id?, label, left, op, right, right2?} | {id?, label, condition} | {id?, label, op: "else"}] in the order shown; a branch that was there keeps its id. */
  const KIND_OF_SLOT = { left: ["value", "python", "input", "agent"], op: ["value", "input", "agent"], right: ["value", "python", "input", "agent"], right2: ["value", "python", "input", "agent"] };
  const operandState = o => {
    const x = o && typeof o === "object" ? o : { kind: "value", value: o == null ? "" : o };
    return { kind: x.kind || "value", value: x.value == null ? "" : String(x.value), expr: x.expr || "", pick: x.from ? "from:" + x.from : x.variable ? "var:" + x.variable : "", describe: x.describe || "" };
  };
  const operandOut = st => (st.kind === "value" ? { kind: "value", value: st.value } : st.kind === "python" ? { kind: "python", expr: st.expr.trim() }
    : st.kind === "input" ? { kind: "input", ...(st.pick.startsWith("var:") ? { variable: st.pick.slice(4) } : { from: st.pick.replace(/^from:/, "") }) } : { kind: "agent", describe: st.describe.trim() });

  function branchEditor(value, prefix, T, disabled, ctx) {
    const known = ctx || { inputs: [], variables: [] };
    const mk = b => {
      const kind = b.op === "else" ? "else" : b.left !== undefined || (b.op && typeof b.op === "object") ? "compare" : b.condition !== undefined ? "expr" : "compare";
      return { id: b.id, label: b.label || "", type: kind, condition: b.condition || "", left: operandState(b.left), op: operandState(b.op || { kind: "value", value: "<" }), right: operandState(b.right), right2: operandState(b.right2) };
    };
    let rows = (Array.isArray(value) ? value : []).map(mk);
    const box = h("div", "pk-branches"), list = h("ul", "pk-brlist"), hint = h("p", "help pk-brhint"), add = h("button", "btn pk-bradd", T("pl.br.add")), addElse = h("button", "btn pk-bradd", T("pl.br.addelse"));
    add.type = "button"; addElse.type = "button";
    const hasElse = () => rows.some(r => r.type === "else");
    const move = (i, d) => { const j = i + d; if (j < 0 || j >= rows.length || rows[j].type === "else" || rows[i].type === "else") return; [rows[i], rows[j]] = [rows[j], rows[i]]; paint(); };
    const field = (el, word) => { el.setAttribute("aria-label", T(word)); el.autocomplete = "off"; el.spellcheck = false; el.disabled = !!disabled; return el; };
    /* one operand: a source select and what the source needs */
    function operand(slot, st, i) {
      const wrap = h("div", "pk-term"), id = `${prefix}-br${i}-${slot}`;
      const kind = h("select", "pk-kind"); kind.id = id + "-k"; field(kind, "pl.br.kind");
      KIND_OF_SLOT[slot].forEach(k => { const o = h("option", "", T("pl.br.kind." + k)); o.value = k; o.selected = k === st.kind; kind.append(o); });
      kind.addEventListener("change", () => { st.kind = kind.value; paint(); });
      let body;
      if (st.kind === "value" && slot === "op") {
        body = h("select"); ["<", "<=", "=", "!=", ">=", ">", "between", "in", "contains"].forEach(o => { const e = h("option", "", T("pl.br.op." + OP_WORD[o])); e.value = o; e.selected = o === st.value; body.append(e); });
        if (!st.value) st.value = "<";
        body.addEventListener("change", () => { st.value = body.value; paint(); });
      } else if (st.kind === "value") {
        body = h("input"); body.type = "text"; body.value = st.value; body.placeholder = T("pl.br.value"); body.addEventListener("input", () => { st.value = body.value; });
      } else if (st.kind === "python") {
        body = h("input"); body.type = "text"; body.value = st.expr; body.placeholder = T("pl.br.custom"); body.addEventListener("input", () => { st.expr = body.value; });
      } else if (st.kind === "input") {
        body = h("select");
        const none = h("option", "", T("pl.br.input.pick")); none.value = ""; none.selected = !st.pick; body.append(none);
        known.inputs.forEach(n => { const o = h("option", "", T("pl.br.input.wired", n)); o.value = "from:" + n; o.selected = st.pick === "from:" + n; body.append(o); });
        known.variables.forEach(n => { const o = h("option", "", T("pl.br.input.plan", n)); o.value = "var:" + n; o.selected = st.pick === "var:" + n; body.append(o); });
        if (st.pick && ![...known.inputs.map(n => "from:" + n), ...known.variables.map(n => "var:" + n)].includes(st.pick)) { const o = h("option", "", st.pick.replace(/^[a-z]+:/, "")); o.value = st.pick; o.selected = true; body.append(o); }
        body.addEventListener("change", () => { st.pick = body.value; });
      } else {
        body = h("textarea"); body.rows = 2; body.value = st.describe; body.placeholder = T("pl.br.describe"); body.addEventListener("input", () => { st.describe = body.value; });
      }
      body.id = id; field(body, "pl.br.term." + slot);
      wrap.append(h("label", "pk-termlab", T("pl.br.term." + slot)), kind, body);
      if (st.kind === "input" && !known.inputs.length && !known.variables.length) wrap.append(h("p", "help", T("pl.br.input.none")));
      return wrap;
    }
    function paint() {
      list.replaceChildren(...rows.map((r, i) => {
        const li = h("li", "pk-branch"), id = `${prefix}-br${i}`;
        const label = h("input"); label.id = id + "-l"; label.type = "text"; label.maxLength = 40; label.value = r.label; label.placeholder = T("pl.br.label");
        field(label, "pl.br.label"); label.addEventListener("input", () => { r.label = label.value; });
        const type = h("select"); type.id = id + "-t"; field(type, "pl.br.type");
        ["compare", "expr", "else"].filter(t => t !== "else" || r.type === "else").forEach(t => { const o = h("option", "", T("pl.br.type." + t)); o.value = t; o.selected = t === r.type; type.append(o); });
        type.disabled = !!disabled || r.type === "else";
        type.addEventListener("change", () => { r.type = type.value; paint(); });
        const cond = h("input"); cond.type = "text"; cond.value = r.condition; cond.placeholder = T("pl.br.custom"); field(cond, "pl.br.custom"); cond.addEventListener("input", () => { r.condition = cond.value; });
        const btn = (text, word, fn, off) => { const b = h("button", "btn pk-brbtn", text); b.type = "button"; b.title = T(word); b.setAttribute("aria-label", T(word)); b.disabled = !!disabled || !!off; b.addEventListener("click", fn); return b; };
        const last = rows.length - 1 - (hasElse() ? 1 : 0);
        li.append(label, type,
          ...(r.type === "else" ? [h("span", "pk-brelse", T("pl.br.elsehelp"))] : r.type === "expr" ? [cond]
            : [operand("left", r.left, i), operand("op", r.op, i), operand("right", r.right, i), ...(r.op.kind === "value" && r.op.value === "between" ? [operand("right2", r.right2, i)] : [])]),
          h("span", "pk-brbtns", btn("↑", "pl.br.up", () => move(i, -1), i === 0 || r.type === "else"), btn("↓", "pl.br.down", () => move(i, 1), i >= last || r.type === "else"),
            btn("×", "pl.br.remove", () => { rows.splice(i, 1); paint(); }, rows.length <= 1)));
        return li;
      }));
      hint.textContent = rows.length && !hasElse() ? T("pl.br.noelse") : "";
      hint.hidden = !hint.textContent;
      addElse.hidden = hasElse(); addElse.disabled = !!disabled; add.disabled = !!disabled || rows.length >= 12;
    }
    const blank = () => ({ label: "", type: "compare", condition: "", left: operandState({ kind: "value", value: "" }), op: operandState({ kind: "value", value: "<" }), right: operandState({ kind: "value", value: "" }), right2: operandState({ kind: "value", value: "" }) });
    add.addEventListener("click", () => { rows.splice(hasElse() ? rows.length - 1 : rows.length, 0, blank()); paint(); });
    addElse.addEventListener("click", () => { rows.push({ ...blank(), label: T("pl.br.op.else"), type: "else" }); paint(); });
    box.append(list, hint, h("div", "pk-brbar", add, addElse));
    paint();
    return {
      el: box,
      get: () => rows.map(r => ({ ...(r.id ? { id: r.id } : {}), label: r.label.trim(),
        ...(r.type === "else" ? { op: "else" } : r.type === "expr" ? { condition: r.condition.trim() }
          : { left: operandOut(r.left), op: operandOut(r.op), right: operandOut(r.right), ...(r.op.kind === "value" && r.op.value === "between" ? { right2: operandOut(r.right2) } : {}) }) })),
    };
  }

  /* The fields of a kind as a form. `collect()` gives {spec, errors}; ids start with `prefix`. */
  function specForm(fields, spec, prefix, T, disabled, ctx) {
    const values = { ...defaultsOf(fields), ...(spec || {}) };
    const inputs = {};
    const rows = fields.filter(f => !f.hidden).map(f => {
      const id = `${prefix}-${f.name}`;
      const label = h("label", "", T("pl.spec." + f.name) + (f.required ? " *" : ""));
      label.htmlFor = id;
      let input;
      let v = values[f.name];
      if (f.kind === "archives") {
        const pick = root.ragPick && root.ragPick.create ? root.ragPick.create({ T, ids: Array.isArray(v) ? v : [], disabled, label: T("pl.spec." + f.name) }) : null;
        inputs[f.name] = { get: () => (pick ? pick.get() : []), els: [] };
        if (pick) label.htmlFor = pick.findId;
        return h("div", "pk-row", label, pick ? pick.el : h("p", "help", T("rp.offline")));
      }
      if (f.kind === "branches") {
        const ed = branchEditor(v, id, T, disabled, ctx);
        inputs[f.name] = { get: () => ed.get(), els: [] };
        return h("div", "pk-row", h("span", "pk-lab", T("pl.spec." + f.name) + (f.required ? " *" : "")), ed.el);
      }
      if (f.kind === "choice") {
        input = h("select", "", ...f.choices.map(c => { const o = h("option", "", T("pl.spec.opt." + c)); o.value = c; o.selected = c === v; return o; }));
      } else if (f.kind === "textarea" || f.kind === "names" || f.kind === "json" || f.kind === "options") {
        input = h("textarea"); input.rows = f.kind === "textarea" ? 3 : f.kind === "options" ? 4 : 2;
        input.value = f.kind === "options" ? (Array.isArray(v) ? v.map(optionLine).join("\n") : "") : f.kind === "names" ? (Array.isArray(v) ? v.join(", ") : "") : f.kind === "json" ? (v === undefined ? "" : typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v)) : (v || "");
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
