"use strict";
/* Commands that start with a slash, typed in the Chat's message box: the ones that act on the page or the conversation, the requests the
   Chat words for the model, the skills found on this PC, and the ones the conversation's own process answers (agentchat.js hands those over).
   Only the first characters of the message are looked at; a message that begins with a space is sent as it is. Everything shown comes in as text. */
(() => {
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const prompt = $("#prompt"), menu = $("#slash-menu");
  const A = () => window.agentChat;
  const goTo = view => { location.hash = view; };

  /* ---- what the model is asked to do by the commands that are requests ---- */
  const ASK = {
    init: rest => "Look through this folder and write AGENTS.md at its root: what the project is, how to build, test and run it, how the code is laid out and the conventions to follow. "
      + "State only what you checked in the files, and keep it short. If AGENTS.md exists, improve it instead." + (rest ? " " + rest : ""),
    review: rest => "Review the changes in this folder (git diff, staged changes included) for bugs, regressions and missing tests. Report by file and line, the most serious first, "
      + "and say what you did not check. Do not change any file unless I ask." + (rest ? " " + rest : ""),
    "security-review": rest => "Review the changes in this folder (git diff, staged changes included) for security problems: injection, unsafe file or network access, secrets, missing checks. "
      + "Report by file and line, the most serious first, and say what you did not check. Do not change any file unless I ask." + (rest ? " " + rest : ""),
    research: rest => "Research this by reading the sources themselves: search the web, open the most relevant pages (not only headlines or a feed), and compare what they say. "
      + "Answer with what each source says, name the pages you opened and the ones you could not open. Question: " + rest,
  };

  /* ---- the commands: name, other names, how the argument is written, and what it does ---- */
  const note = (plain, build) => A().note(plain, build);
  const needChat = () => { if (A() && A().active()) return true; note(T("sl.need.conv")); return false; };
  const server = (name, rest) => { if (!needChat()) return; note(T("sl.running")); A().runOnServer(name, rest); };
  const openSettings = () => { if ($("#chatset").hidden) $("#cs-open").click(); $("#cs-perm").scrollIntoView({ block: "nearest" }); };
  const MODE_NAMES = { plan: "plan", read: "read_only", read_only: "read_only", readonly: "read_only", ask: "default", default: "default", auto: "auto", automatic: "auto" };
  const EFFORT_NAMES = { none: "none", off: "none", low: "low", medium: "medium", high: "", auto: "", default: "" };
  const sel = $("#effort");
  const effortLabel = () => sel.options[sel.selectedIndex].text;

  const COMMANDS = [
    { name: "help", alias: ["?"], run: () => { note(T("sl.help.start"), helpNode); } },
    { name: "clear", alias: ["new", "reset"], run: () => { $("#new-chat").click(); } },
    { name: "model", run: () => { const m = $("#chat-model").textContent.trim(); note(m ? T("sl.model", m) : T("sl.model.none")); } },
    { name: "effort", args: "[none|low|medium|high|auto]", run: rest => {
      const want = rest.toLowerCase();
      if (!want || !(want in EFFORT_NAMES)) { note(T("sl.effort.usage", effortLabel())); return; }
      sel.value = EFFORT_NAMES[want];
      sel.dispatchEvent(new Event("change"));
      if (!A() || !A().active() || !A().current()) note(T("ch.effort.now", effortLabel()));        // with a conversation, its process says it
    } },
    { name: "mode", args: "[plan|read|ask|auto]", run: async rest => {
      if (!A() || !A().perm.available()) { note(T("sl.need.conv")); return; }
      const want = MODE_NAMES[rest.toLowerCase()];
      if (!want) { note(T("sl.mode.usage", T("ag.mode." + A().perm.now()))); return; }
      if (await A().perm.set(want) && !A().current()) note(T("ch.mode.now", T("ag.mode." + want)));
    } },
    { name: "plan", args: "[what to plan]", run: async rest => {
      if (!A() || !A().perm.available()) { note(T("sl.need.conv")); return; }
      if (!(await A().perm.set("plan"))) return;
      if (rest) A().send(rest);
    } },
    { name: "permissions", alias: ["allowed"], run: () => { openSettings(); if (A() && A().active() && A().current()) server("permissions", ""); } },
    { name: "settings", alias: ["config"], run: () => { openSettings(); } },
    { name: "status", run: rest => server("status", rest) },
    { name: "context", run: rest => server("context", rest) },
    { name: "compact", run: rest => server("compact", rest) },
    { name: "tools", run: rest => server("tools", rest) },
    { name: "hooks", run: rest => server("hooks", rest) },
    { name: "mcp", run: rest => server("mcp", rest) },
    { name: "memory", args: "[search]", run: rest => server("memory", rest) },
    { name: "copy", args: "[n]", run: async rest => {
      const all = A() && A().active() ? A().answers() : [];
      const n = Math.max(1, parseInt(rest, 10) || 1), text = all[all.length - n];
      if (!text) { note(T("sl.copy.none")); return; }
      try { await navigator.clipboard.writeText(text); note(T("sl.copied")); } catch (e) { note(T("sl.copy.fail")); }
    } },
    { name: "export", run: async () => {
      const text = A() && A().current() ? await A().transcript() : "";
      if (!text) { note(T("sl.export.none")); return; }
      const name = "basalt-" + A().current() + ".txt";
      await A().saveBlob(new Blob([text], { type: "text/plain;charset=utf-8" }), name);
      note(T("sl.exported", name));
    } },
    { name: "rename", args: "<name>", run: async rest => {
      if (!rest) { note(T("sl.rename.usage")); return; }
      if (!A() || !A().current()) { note(T("sl.need.sid")); return; }
      if (await A().rename(rest)) note(T("sl.renamed", rest));
    } },
    { name: "branch", alias: ["fork"], run: () => { if (!A() || !A().current()) note(T("sl.need.sid")); else A().branch(); } },
    { name: "branches", run: () => { const b = $("#branches-open"); if (b.hidden) note(T("sl.need.conv")); else b.click(); } },
    { name: "resume", alias: ["sessions"], run: () => {
      const convs = A() ? A().sessions() : [];
      if (!convs.length) { note(T("sl.resume.none")); return; }
      note(T("sl.resume.title"), () => {
        const box = mk("div", "slres"); box.append(mk("b", "", T("sl.resume.title")));
        const ul = mk("ul"); box.append(ul);
        convs.forEach(s => { const li = mk("li"), b = mk("button", "btn ghost", s.label); b.type = "button"; b.onclick = () => A().open(s.id); li.append(b); ul.append(li); });
        return box;
      });
    } },
    { name: "rewind", alias: ["undo", "checkpoint"], run: () => {
      const backs = [...document.querySelectorAll("#amsgs .aback")];
      if (!backs.length) { note(T("sl.rewind.none")); return; }
      backs[backs.length - 1].focus(); note(T("sl.rewind", T("ch.back")));
    } },
    { name: "stop", run: () => { if (A() && A().active()) A().stop(); } },
    { name: "diff", alias: ["changes"], run: () => { if (A() && A().hasCopy()) A().toggleCopy(); else note(T("sl.diff.none")); } },
    { name: "plans", alias: ["tasks"], run: () => goTo("plans") },
    { name: "wallet", run: () => goTo("wallet") },
    { name: "init", ask: true }, { name: "review", ask: true, args: "[what to look at]" }, { name: "security-review", ask: true },
    { name: "research", alias: ["search"], ask: true, args: "<question>" },
  ];
  COMMANDS.forEach(c => { c.alias = c.alias || []; c.args = c.args || ""; });
  const byName = word => COMMANDS.find(c => c.name === word || c.alias.includes(word));

  /* ---- skills found on this PC: /name asks for one ---- */
  let skills = [], skillsAt = 0;
  async function loadSkills() {
    if (Date.now() - skillsAt < 30000) return;
    skillsAt = Date.now();
    try {
      const r = await fetch("api/capabilities", { cache: "no-store", headers: authHeaders() });
      if (!r.ok) return;
      const j = await r.json();
      skills = (j.entries || []).filter(e => e.kind === "skill" && e.state !== "off").map(e => ({ id: e.id, name: e.name, description: e.description || "", state: e.state }));
    } catch (e) { /* the built-in commands are there anyway */ }
  }
  const skillOf = word => skills.find(s => s.name.toLowerCase() === word);

  /* ---- from the text of the message to what happens ---- */
  function parse(text) {
    const m = /^\/([^\s/]+)(?:\s+([\s\S]*))?$/.exec(text);
    return m ? { word: m[1].toLowerCase(), rest: (m[2] || "").trim() } : null;
  }
  function run(text) {                      // false: not a command; "keep": a command that does not exist, the text stays to be edited
    const p = parse(text);
    if (!p) return false;
    const c = byName(p.word);
    const chat = !!(A() && A().active());
    if (c) {
      if (c.ask) {
        if (!chat) { note(T("sl.need.conv")); return "keep"; }
        if (c.name === "research" && !p.rest) { note(T("sl.research.usage")); return "keep"; }
        A().send(ASK[c.name](p.rest));
      } else c.run(p.rest);
      return true;
    }
    const s = skillOf(p.word);
    if (s) {
      if (!chat) { note(T("sl.need.conv")); return "keep"; }
      if (s.state !== "approved") { note(T("sl.skill.pending", s.name)); return true; }
      A().send(`Load the skill ${s.id} with load_capability and follow it. ` + (p.rest ? "What I need: " + p.rest : "Ask me what I need if the skill requires input."));
      return true;
    }
    note(T("sl.unknown", p.word));
    return "keep";
  }

  /* ---- the menu under the message box ---- */
  let shown = [], at = 0;
  function candidates(query) {
    const q = query.toLowerCase();
    const all = [...COMMANDS.map(c => ({ name: c.name, alias: c.alias, args: c.args, text: T("sl.d." + c.name), kind: "" })),
      ...skills.filter(s => !byName(s.name.toLowerCase())).map(s => ({ name: s.name, alias: [], args: "", text: s.description, kind: "skill", pending: s.state !== "approved" }))];
    const names = c => [c.name.toLowerCase(), ...c.alias];
    const rank = c => names(c).some(n => n.startsWith(q)) ? 0 : names(c).some(n => n.includes(q)) ? 1 : c.text.toLowerCase().includes(q) ? 2 : 3;
    return all.map((c, i) => [q ? rank(c) : 0, i, c]).filter(([r]) => r < 3).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(([, , c]) => c);      // no letters yet: the order of the list
  }
  function close() { shown = []; menu.hidden = true; prompt.setAttribute("aria-expanded", "false"); prompt.removeAttribute("aria-activedescendant"); }
  function paintMenu() {
    menu.replaceChildren(...shown.map((c, i) => {
      const o = mk("div", "slashitem" + (i === at ? " on" : ""));
      o.id = "slash-o" + i; o.setAttribute("role", "option"); o.setAttribute("aria-selected", String(i === at));
      o.append(mk("b", "", "/" + c.name), mk("span", "slashargs", c.args), mk("span", "slashd", (c.pending ? T("sl.skill.pending.short") + " " : "") + c.text));
      if (c.kind) o.append(mk("span", "slashkind", T("sl.skill")));
      o.onmousedown = e => { e.preventDefault(); at = i; choose(true); };
      return o;
    }));
    const on = menu.children[at];
    if (on) { prompt.setAttribute("aria-activedescendant", on.id); on.scrollIntoView({ block: "nearest" }); }
  }
  function update() {
    const v = prompt.value;
    if (!v.startsWith("/") || /\s/.test(v)) { close(); return; }
    loadSkills();
    shown = candidates(v.slice(1)).slice(0, 40);
    if (!shown.length) { close(); return; }
    at = Math.min(at, shown.length - 1);
    menu.hidden = false; prompt.setAttribute("aria-expanded", "true");
    paintMenu();
  }
  function choose(runIt) {                  // a command without an argument runs; one that takes an argument is completed for it to be written
    const c = shown[at];
    if (!c) return;
    close();
    if (runIt && !c.args && !c.ask) { prompt.value = ""; window.chatUi.autosize(); run("/" + c.name); return; }
    window.chatUi.setPrompt("/" + c.name + " ");
  }
  prompt.setAttribute("role", "combobox"); prompt.setAttribute("aria-controls", "slash-menu"); prompt.setAttribute("aria-expanded", "false"); prompt.setAttribute("aria-autocomplete", "list");
  prompt.addEventListener("input", () => { at = 0; update(); });
  prompt.addEventListener("blur", () => close());
  prompt.addEventListener("keydown", e => {
    if (e.key === "Tab" && e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {         // Shift+Tab goes through the modes while the message is being written
      if (A() && A().perm.available()) { e.preventDefault(); e.stopImmediatePropagation(); close(); A().perm.cycle(); }
      return;
    }
    if (menu.hidden || e.isComposing) return;
    const key = e.key;
    if (key === "ArrowDown" || key === "ArrowUp") { e.preventDefault(); e.stopImmediatePropagation(); at = (at + (key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length; paintMenu(); }
    else if (key === "Tab" && !e.shiftKey) { e.preventDefault(); e.stopImmediatePropagation(); choose(false); }
    else if (key === "Enter" && !e.shiftKey) {
      if (prompt.value.slice(1).toLowerCase() === shown[at].name.toLowerCase() && !shown[at].args) { close(); return; }      // typed in full: sent as it is
      e.preventDefault(); e.stopImmediatePropagation(); choose(true);
    } else if (key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); close(); }
  }, true);

  /* ---- /help ---- */
  function helpNode() {
    const box = mk("div", "slres"), ul = mk("ul");
    box.append(mk("b", "", T("sl.help.title")), ul);
    COMMANDS.forEach(c => {
      const li = mk("li"), names = [c.name, ...c.alias].map(n => "/" + n).join(" ");
      li.append(mk("code", "", names + (c.args ? " " + c.args : "")), mk("span", "", " — " + T("sl.d." + c.name)));
      ul.append(li);
    });
    if (skills.length) {
      box.append(mk("b", "", T("sl.help.skills")));
      const us = mk("ul"); box.append(us);
      skills.forEach(s => { const li = mk("li"); li.append(mk("code", "", "/" + s.name), mk("span", "", " — " + (s.state !== "approved" ? T("sl.skill.pending.short") + " " : "") + s.description)); us.append(li); });
    }
    return box;
  }

  /* ---- what the conversation's process answers ---- */
  const list = (title, rows) => {
    const box = mk("div", "slres"), ul = mk("ul");
    box.append(mk("b", "", title), ul);
    rows.forEach(r => { const li = mk("li"); if (Array.isArray(r)) li.append(mk("span", "slk", r[0] + ": "), mk("span", "", String(r[1]))); else li.textContent = r; ul.append(li); });
    return box;
  };
  const pct = x => Math.round((x || 0) * 100);
  const SHOW = {
    status: d => list(T("sl.r.status"), [[T("sl.r.mode"), T("ag.mode." + d.mode)], [T("sl.r.effort"), d.effort ? effortName(d.effort) : effortName("")], [T("sl.r.turns"), d.turns],
      [T("sl.r.context"), T("sl.r.ctx", d.tokens, d.window, pct(d.share))], [T("sl.r.messages"), d.messages], [T("sl.r.tools"), d.tools], [T("sl.r.memories"), d.memories],
      [T("sl.r.mcp"), d.mcp_servers], [T("sl.r.spent"), d.model_tokens]]),
    context: d => list(T("sl.r.context"), [T("sl.r.ctx", d.tokens, d.window, pct(d.share)), [T("sl.r.messages"), d.messages], ...(d.summarised ? [T("sl.r.ctx.summary")] : [])]),
    tools: d => list(T("sl.r.tools.title", d.tools.length), d.tools.map(t => t.name + " · " + t.effect)),
    hooks: d => list(T("sl.r.hooks"), [d.sources.length ? T("sl.r.hooks.files", d.sources.join(", ")) : T("sl.r.hooks.none"), T("sl.r.hooks.counts", d.pre_tool, d.post_tool),
      ...Object.entries(d.events).map(([k, n]) => k + ": " + n), ...(d.errors.length ? [T("sl.r.hooks.errors", d.errors.join(" | "))] : [])]),
    mcp: d => list(T("sl.r.mcp.title"), d.servers.length ? d.servers.map(s => s.error ? T("sl.r.mcp.err", s.name, s.error) : T("sl.r.mcp.line", s.name, s.tools)) : [T("sl.r.mcp.none")]),
    memory: d => list(T("sl.r.mem.title", d.count), d.items.length ? d.items.map(m => m.category + " · " + m.content) : [d.query ? T("sl.r.mem.nomatch", d.query) : T("sl.r.mem.empty")]),
    permissions: d => list(T("sl.r.perm"), [[T("sl.r.mode"), T("ag.mode." + d.mode)], ...(d.rules.length ? d.rules.map(r => r.action + " " + r.pattern) : [T("sl.r.perm.none")]),
      T("sl.r.perm.denied", d.denied), T("sl.r.perm.edit")]),
    compact: d => d.done ? T("sl.r.compact.done", d.before, d.after) : T("sl.r.compact." + d.reason, d.tokens),
  };
  const effortName = level => { const o = [...sel.options].find(x => x.value === (level || "")); return o ? o.text : String(level); };
  document.addEventListener("basalt:command", ev => {
    const e = ev.detail || {};
    if (!e.ok) { note(T("sl.r.error", e.error || "")); return; }
    const out = (SHOW[e.name] || (() => ""))(e.data || {});
    if (typeof out === "string") note(out); else note(e.name, () => out);
  });

  document.addEventListener("basalt:view", e => { if (e.detail === "chat") loadSkills(); });
  window.slash = { run, parse, byName, candidates, commands: COMMANDS };
})();
