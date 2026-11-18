"use strict";
/* Knowledge > Images: where the images of the documents are kept. A folder of files (the default: it can be moved from here), or MinIO: one that is already on this PC (the page asks for
   its address and keys) or a separate one that TaDaMoo installs, starts and stops. Nothing is downloaded until a button is pressed. The secret key is sent once and goes to the vault;
   the page never gets it back. Everything the server says is put in the page as text. */
(() => {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("ri.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const field = (id, label, help, input) => {
    const wrap = el("div", "rg-field"), lab = el("label", "", label); lab.htmlFor = id; input.id = id;
    wrap.append(lab, input); if (help) wrap.append(el("p", "rg-help", help));
    return wrap;
  };
  const text = (value, max) => { const i = el("input"); i.type = "text"; i.value = value || ""; i.maxLength = max || 200; i.autocomplete = "off"; i.spellcheck = false; return i; };
  const mb = n => `${(n / 1e6).toFixed(1)} MB`;

  let view = null, note = "", bad = false, busy = "", form = { path: "", dpath: "", endpoint: "", access: "", secret: "", bucket: "", open: false }, repaint = () => {};

  async function load() {
    const r = await api("GET", "api/rag/images");
    if (r.ok) view = r.data; else { note = why(r); bad = true; }
    repaint();
  }
  async function act(name, path, body) {
    busy = name; note = ""; repaint();
    const r = await api("POST", path, body);
    busy = "";
    if (r.ok) { view = r.data; note = T(name === "move" && r.data.moved != null ? "ri.moved" : "ri.done", r.data.moved); bad = false; form.secret = ""; form.open = false; }
    else { note = why(r); bad = true; }
    repaint();
  }

  function build() {
    const box = el("details", "rg-settings ri-box");
    const sum = el("summary", "rg-sum", T("ri.title")); box.append(sum);
    box.addEventListener("toggle", () => { form.open = box.open; });
    box.open = form.open;
    if (!view) { box.append(el("p", "rg-help", T("ri.loading"))); return box; }
    const where = view.store === "s3" ? T("ri.where.s3", view.s3.endpoint, view.s3.bucket) : T("ri.where.folder", view.folder, view.count);
    box.append(el("p", "rg-line", where), el("p", "rg-help", T("ri.help")));

    if (view.store === "folder") {
      const path = text(form.path, 500); path.oninput = () => { form.path = path.value; };
      const move = btn(T(busy === "move" ? "ri.running" : "ri.move"), "", () => act("move", "api/rag/images/folder", { path: form.path.trim() }));
      move.disabled = !!busy || !form.path.trim();
      const row = el("div", "rg-row"); row.append(path, move);
      box.append(field("ri-path", T("ri.path"), T("ri.path.help"), row));
    } else {
      box.append(btn(T("ri.usefolder"), "", () => act("folder", "api/rag/images/use-folder", {})));
    }

    const m = view.minio, own = m.own;
    const mbox = el("div", "ri-minio");
    mbox.append(el("h4", "rg-h2", T("ri.minio")));
    if (m.found.programs.length) mbox.append(el("p", "rg-help", T("ri.found", m.found.programs.map(p => p.path).join(", "))));
    if (m.found.listening.length) mbox.append(el("p", "rg-help", T("ri.listening", m.found.listening.join(", "))));
    if (m.found.broken.length) mbox.append(el("p", "rg-help", T("ri.broken", m.found.broken.map(p => p.path).join(", "))));
    if (!m.found.programs.length && !m.found.listening.length) mbox.append(el("p", "rg-help", T("ri.none")));
    m.options.forEach(o => { if (o.id !== "skip") mbox.append(el("p", "rg-dmeta", "• " + T("ri.opt." + o.id))); });

    if (own.installed) {
      mbox.append(el("p", "rg-line", T(own.running ? "ri.own.running" : "ri.own.stopped", own.endpoint || "")));
      mbox.append(btn(T(own.running ? "ri.stop" : "ri.start"), "", () => act("own", `api/rag/images/minio/${own.running ? "stop" : "start"}`, {})));
      const dpath = text(form.dpath || own.data_dir, 500); dpath.oninput = () => { form.dpath = dpath.value; };
      const dmove = btn(T(busy === "dmove" ? "ri.running" : "ri.move"), "", () => act("dmove", "api/rag/images/minio/folder", { path: dpath.value.trim() }));
      dmove.disabled = !!busy || !dpath.value.trim() || dpath.value.trim() === own.data_dir;
      const drow = el("div", "rg-row"); drow.append(dpath, dmove);
      mbox.append(field("ri-dpath", T("ri.own.datadir"), T("ri.own.datadir.help"), drow));
    } else {
      const inst = btn(T(busy === "install" ? "ri.running" : "ri.install", mb(m.release.bytes)), "primary", () => act("install", "api/rag/images/minio/install", {}));
      inst.disabled = !!busy;
      mbox.append(el("p", "rg-help", T("ri.install.help", m.release.tag, mb(m.release.bytes))), inst);
    }
    const ex = [["endpoint", "ri.f.endpoint", "127.0.0.1:9000"], ["access", "ri.f.access", ""], ["secret", "ri.f.secret", ""], ["bucket", "ri.f.bucket", "tadamoo-images"]];
    const fb = el("div", "ri-existing");
    fb.append(el("h4", "rg-h2", T("ri.existing")), el("p", "rg-help", T("ri.existing.help")));
    const inputs = {};
    ex.forEach(([k, key, ph]) => {
      const i = text(form[k], 200); if (ph) i.placeholder = ph;
      if (k === "secret") { i.type = "password"; i.autocomplete = "new-password"; }
      i.oninput = () => { form[k] = i.value; };
      inputs[k] = i; fb.append(field("ri-" + k, T(key), "", i));
    });
    const use = btn(T(busy === "existing" ? "ri.running" : "ri.use"), "", () => act("existing", "api/rag/images/minio/existing", { endpoint: form.endpoint.trim(), access_key: form.access.trim(), secret_key: form.secret, bucket: form.bucket.trim() || undefined }));
    use.disabled = !!busy || !form.endpoint.trim() || !form.access.trim() || !form.secret;
    fb.append(use);
    mbox.append(fb);
    box.append(mbox);
    const status = el("p", "rg-note", note); status.setAttribute("role", "status"); status.dataset.bad = bad ? "1" : ""; status.hidden = !note;
    box.append(status);
    return box;
  }

  window.RagImages = {
    block(again) { repaint = again || (() => {}); if (!view) load(); return build(); },
  };
})();
