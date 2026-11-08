"use strict";
/* Knowledge: the archives of documents a personality can draw on. Make an archive, add files or a path, see how each is indexed, try a question,
   and choose which personalities use it. Every archive is searched by words; with an embedding model installed it is searched by meaning too.
   Everything the server returns enters the page as text. */
(function () {
  const host = document.getElementById("rg-root");
  if (!host) return;

  const ARM_MS = 4000, TOP_K = 4;
  let S = null, list = [], chosen = "", creating = false, detail = null, note = "", noteBad = false, armed = "", armTimer = 0, question = "", results = null, asking = false, working = false;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("rg.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const size = n => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const say = (text, bad) => { note = text; noteBad = !!bad; };
  const knowledgeOn = () => document.body.dataset.view === "rag";

  async function api(method, path, body, raw) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: raw || (body ? JSON.stringify(body) : undefined) });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  function arm(key) {
    armed = key; clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = ""; paint(); }, ARM_MS);
    paint();
  }

  async function refresh() {
    const r = await api("GET", "api/rag");
    if (!r.ok) { S = null; say(why(r), true); paint(); return; }
    S = r.data; list = r.data.archives || [];
    if (chosen && !list.some(a => a.id === chosen)) { chosen = ""; detail = null; }
    if (chosen) await reloadDetail(); else paint();
  }
  async function reloadDetail() {
    const r = await api("GET", `api/rag/archives/${encodeURIComponent(chosen)}`);
    if (!r.ok) { say(why(r), true); detail = null; if (r.status === 404) chosen = ""; paint(); return; }
    detail = r.data;
    paint();
  }
  async function open(id) {
    chosen = id; creating = false; armed = ""; detail = null; results = null; question = ""; say("", false);
    await reloadDetail();
  }
  async function reloadAll() {
    const r = await api("GET", "api/rag");
    if (r.ok) { S = r.data; list = r.data.archives || []; }
    if (chosen) await reloadDetail(); else paint();
  }

  /* ---- actions ---- */
  async function create(name, description) {
    const r = await api("POST", "api/rag/archives", { name, description });
    if (!r.ok) { say(why(r), true); paint(); return; }
    creating = false; chosen = r.data.archive.id; say(T("rg.created"), false); results = null; question = "";
    await reloadAll();
  }
  async function save(body) {
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/update`, body);
    say(r.ok ? T("rg.saved") : why(r), !r.ok);
    await reloadAll();
  }
  async function remove() {
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/delete`, { confirm: true });
    armed = "";
    if (!r.ok) { say(why(r), true); paint(); return; }
    chosen = ""; detail = null; results = null; say(T("rg.deleted"), false);
    await reloadAll();
  }
  async function addFiles(files) {
    if (!files.length || working) return;
    working = true;
    const lines = []; let bad = false;
    for (const f of files) {
      say(T("rg.add.reading", f.name), false); paint();
      const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/upload?name=${encodeURIComponent(f.name)}`, null, f);
      if (r.ok) lines.push(T("rg.add.done", f.name, r.data.document.chunks) + (r.data.note ? " " + r.data.note : ""));
      else { bad = true; lines.push(T("rg.add.fail", f.name, why(r))); }
    }
    working = false;
    say(lines.join("\n"), bad);
    await reloadAll();
  }
  async function addPath(path) {
    if (!path.trim() || working) return;
    working = true; say(T("rg.add.reading", path), false); paint();
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/documents`, { path: path.trim() });
    working = false;
    say(r.ok ? T("rg.add.done", r.data.document.name, r.data.document.chunks) + (r.data.note ? " " + r.data.note : "") : why(r), !r.ok);
    await reloadAll();
  }
  async function removeDoc(doc) {
    armed = "";
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/documents/${encodeURIComponent(doc.id)}/remove`, {});
    say(r.ok ? T("rg.doc.removed", doc.name) : why(r), !r.ok);
    results = null;
    await reloadAll();
  }
  async function reindex() {
    working = true; say(T("rg.index.working"), false); paint();
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/reindex`, {});
    working = false;
    say(r.ok ? T("rg.index.done", r.data.chunks) + (r.data.note ? " " + r.data.note : "") : why(r), !r.ok);
    await reloadAll();
  }
  async function ask() {
    if (!question.trim() || asking) return;
    asking = true; results = null; paint();
    const r = await api("POST", "api/rag/search", { query: question.trim(), archives: [chosen], top_k: TOP_K });
    asking = false;
    if (!r.ok) { say(why(r), true); results = null; } else { say("", false); results = r.data; }
    paint();
  }
  async function link(slug, name, linked) {
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/link`, { persona: slug, linked });
    if (r.ok) { detail.personas = r.data.personas; say(T(linked ? "rg.use.linked" : "rg.use.unlinked", name), false); } else say(why(r), true);
    paint();
  }

  /* ---- the page ---- */
  function field(id, labelText, help, control, target) {
    const wrap = el("div", "rg-field"), lab = el("label", "", labelText);
    lab.htmlFor = id; (target || control).id = id;
    wrap.append(lab, control);
    if (help) wrap.append(el("p", "rg-help", help));
    return wrap;
  }
  function textInput(value, max) {
    const i = el("input"); i.type = "text"; i.value = value || ""; i.maxLength = max; i.autocomplete = "off"; i.spellcheck = false;
    return i;
  }
  function banner() {
    const on = S.embedding && S.embedding.available, box = el("div", "rg-banner");
    box.setAttribute("role", "status"); box.dataset.mode = on ? "hybrid" : "text";
    box.append(el("p", "rg-banner-t", on ? T("rg.hybrid", S.embedding.model) : T("rg.textonly")));
    if (!on) box.append(el("p", "rg-help", T("rg.textonly.help")));
    if (!S.pdf) box.append(el("p", "rg-help", T("rg.nopdf")));
    return box;
  }
  function sideList() {
    const side = el("nav", "rg-side"), ul = el("ul", "rg-names");
    side.setAttribute("aria-label", T("rg.list.title"));
    list.forEach(a => {
      const li = el("li"), on = a.id === chosen && !creating;
      const b = btn("", "rg-pick" + (on ? " on" : ""), () => open(a.id));
      b.setAttribute("aria-current", String(on));
      b.append(el("span", "rg-pname", a.name), el("span", "rg-pcount", T("rg.count", a.documents)));
      li.append(b); ul.append(li);
    });
    side.append(el("h3", "rg-h2", T("rg.list.title")), list.length ? ul : el("p", "rg-help", T("rg.list.none")),
      btn(T("rg.new"), "primary rg-newbtn", () => { creating = true; chosen = ""; detail = null; results = null; say("", false); paint(); }));
    return side;
  }
  function createForm() {
    const form = el("form", "rg-form"), name = textInput("", S.limits.name), desc = el("textarea");
    desc.rows = 3; desc.maxLength = S.limits.description;
    form.noValidate = true;
    form.append(field("rg-new-name", T("rg.f.name"), "", name), field("rg-new-desc", T("rg.f.desc"), "", desc));
    const bar = el("div", "rg-bar"), go = btn(T("rg.create"), "primary", () => create(name.value, desc.value));
    go.type = "submit";
    bar.append(go, btn(T("rg.cancel"), "", () => { creating = false; say("", false); paint(); }));
    form.append(bar);
    form.onsubmit = e => { e.preventDefault(); create(name.value, desc.value); };
    return form;
  }
  function editForm(a) {
    const form = el("form", "rg-form"), name = textInput(a.name, S.limits.name), desc = el("textarea"), cloud = el("input");
    desc.rows = 3; desc.maxLength = S.limits.description; desc.value = a.description;
    cloud.type = "checkbox"; cloud.checked = !!a.cloud_ok;
    form.noValidate = true;
    const check = el("div", "rg-field rg-check"), lab = el("label", "");
    cloud.id = "rg-cloud";
    lab.htmlFor = "rg-cloud"; lab.append(cloud, el("span", "", T("rg.f.cloud")));
    check.append(lab, el("p", "rg-help", T("rg.f.cloud.help")));
    form.append(field("rg-name", T("rg.f.name"), "", name), field("rg-desc", T("rg.f.desc"), "", desc), check);
    const bar = el("div", "rg-bar"), go = btn(T("rg.save"), "primary", () => {});
    go.type = "submit";
    bar.append(go);
    form.append(bar);
    form.onsubmit = e => { e.preventDefault(); save({ name: name.value, description: desc.value, cloud_ok: cloud.checked }); };
    return form;
  }
  function docsBlock(a) {
    const box = el("section", "rg-block"), ul = el("ul", "rg-docs");
    box.setAttribute("aria-labelledby", "rg-docs-h");
    const h = el("h4", "rg-h2", T("rg.docs.title")); h.id = "rg-docs-h";
    box.append(h);
    a.document_list.forEach(d => {
      const li = el("li", "rg-doc"), text = el("div", "rg-doc-t");
      text.append(el("span", "rg-dname", d.name), el("span", "rg-dmeta", T("rg.doc.meta", size(d.bytes), d.chunks)));
      const key = "d" + d.id, sure = armed === key;
      const b = btn(sure ? T("rg.doc.remove.sure") : T("rg.doc.remove"), sure ? "rg-warn" : "", () => { if (!sure) arm(key); else removeDoc(d); });
      b.id = "rg-rm-" + d.id;
      li.append(text, b); ul.append(li);
    });
    box.append(a.document_list.length ? ul : el("p", "rg-help", T("rg.docs.none")));
    const pick = el("input"); pick.type = "file"; pick.multiple = true; pick.accept = S.formats.join(","); pick.disabled = working;
    pick.onchange = () => addFiles([...pick.files]);
    const path = textInput("", 500); path.inputMode = "url";
    const addp = btn(T("rg.add.path.btn"), "", () => addPath(path.value)); addp.disabled = working;
    const row = el("div", "rg-row");
    row.append(path, addp);
    box.append(field("rg-file", T("rg.add.file"), T("rg.add.help", S.formats.join(" "), Math.round(S.limits.document_bytes / (1 << 20))), pick), field("rg-path", T("rg.add.path"), "", row, path));
    path.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); addPath(path.value); } };
    return box;
  }
  function indexBlock(a) {
    const box = el("section", "rg-block");
    box.setAttribute("aria-labelledby", "rg-index-h");
    const h = el("h4", "rg-h2", T("rg.index.title")); h.id = "rg-index-h";
    box.append(h, el("p", "rg-line", T("rg.index.line", a.chunks, a.documents)),
      el("p", "rg-line", a.mode === "hybrid" ? T("rg.index.hybrid", a.embed_model) : T("rg.index.text")));
    if (a.reindex) box.append(el("p", "rg-help", T("rg.index.hint")));
    const go = btn(T("rg.index.again"), "", reindex); go.disabled = working || !a.chunks;
    box.append(go, el("p", "rg-help", T("rg.index.again.help")));
    return box;
  }
  function tryBlock(a) {
    const box = el("section", "rg-block");
    box.setAttribute("aria-labelledby", "rg-try-h");
    const h = el("h4", "rg-h2", T("rg.try.title")); h.id = "rg-try-h";
    const form = el("form", "rg-row"), q = textInput(question, 500);
    q.placeholder = T("rg.try.ph"); q.setAttribute("aria-label", T("rg.try.title")); q.id = "rg-q";
    q.oninput = () => { question = q.value; };
    const go = btn(T("rg.try.go"), "primary", ask); go.type = "submit"; go.disabled = asking || !a.chunks;
    form.append(q, go);
    form.onsubmit = e => { e.preventDefault(); ask(); };
    box.append(h, form);
    if (asking) box.append(el("p", "rg-help", T("rg.try.working")));
    if (results) {
      box.append(el("p", "rg-help", results.mode === "hybrid" ? T("rg.try.mode.hybrid") : T("rg.try.mode.lexical") + (results.note ? " " + results.note : "")));
      if (!results.hits.length) box.append(el("p", "rg-line", T("rg.try.none")));
      const ul = el("ol", "rg-hits");
      results.hits.forEach(hit => {
        const li = el("li", "rg-hit"), head = el("div", "rg-hit-h");
        head.append(el("strong", "", T("rg.try.from", hit.archive, hit.document)),
          el("span", "rg-tag", hit.mode === "hybrid" ? T("rg.tag.hybrid") : T("rg.tag.lexical")), el("span", "rg-tag", T("rg.try.score", hit.score)));
        li.append(head, el("p", "rg-hit-text", hit.text)); ul.append(li);
      });
      box.append(ul);
    }
    return box;
  }
  function usesBlock() {
    const box = el("section", "rg-block");
    box.setAttribute("aria-labelledby", "rg-use-h");
    const h = el("h4", "rg-h2", T("rg.use.title")); h.id = "rg-use-h";
    box.append(h);
    if (!detail.personas.length) { box.append(el("p", "rg-help", T("rg.use.none"))); return box; }
    box.append(el("p", "rg-help", T("rg.use.help")));
    const ul = el("ul", "rg-uses");
    detail.personas.forEach(p => {
      const li = el("li"), lab = el("label", "rg-use"), c = el("input");
      c.type = "checkbox"; c.checked = p.linked; c.id = "rg-use-" + p.slug;
      c.onchange = () => link(p.slug, p.name, c.checked);
      lab.append(c, el("span", "", p.name)); li.append(lab); ul.append(li);
    });
    box.append(ul);
    return box;
  }
  function detailView() {
    const a = detail.archive, main = el("div", "rg-main");
    main.append(el("h3", "rg-h", a.name), editForm(a), docsBlock(a), indexBlock(a), tryBlock(a), usesBlock());
    const sure = armed === "delete", bar = el("div", "rg-bar");
    const del = btn(sure ? T("rg.delete.sure") : T("rg.delete"), "rg-warn", () => { if (!sure) arm("delete"); else remove(); });
    del.id = "rg-delete";
    bar.append(del);
    main.append(bar);
    return main;
  }
  function paint() {
    if (!S) { host.replaceChildren(el("p", "rg-help", note || T("rg.loading"))); return; }
    const focus = document.activeElement && document.activeElement.id;
    const status = el("p", "rg-note", note); status.setAttribute("role", "status"); status.dataset.bad = noteBad ? "1" : ""; status.hidden = !note;
    let main;
    if (creating) { main = el("div", "rg-main"); main.append(el("h3", "rg-h", T("rg.new")), createForm()); }
    else if (detail) main = detailView();
    else { main = el("div", "rg-main"); main.append(el("p", "rg-help", T("rg.pick"))); }
    const layout = el("div", "rg-layout");
    layout.append(sideList(), main);
    host.replaceChildren(el("p", "rg-intro", T("rg.intro")), banner(), status, layout);
    const again = focus && document.getElementById(focus);
    if (again && again !== document.activeElement) again.focus();
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "rag") refresh(); });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  if (knowledgeOn()) refresh(); else paint();
})();
