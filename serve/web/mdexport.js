"use strict";
/* The question a Markdown file the agent made asks when it is saved: only the Markdown file, only the file already converted (Word, PDF ...), or both
   in one zip. The formats come from the server (`api/export/formats`): what this PC can make. The owner of the file gives the window two calls:
   `original()` saves the Markdown as it is, `converted(mode, format)` saves the converted one (mode "converted" or "both"). The last choice is kept in this browser. */
(() => {
  const KEY = "tadamoo.mdexport";
  const MODES = ["md", "converted", "both"];
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = fn; return b; };
  const kept = () => { try { const v = JSON.parse(localStorage.getItem(KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch (e) { return {}; } };
  const keep = v => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* kept for this page only */ } };
  let known = null;
  async function formats() {
    if (known) return known;
    try {
      const r = await fetch("api/export/formats?lang=" + encodeURIComponent(LANG), { headers: authHeaders() });
      known = r.ok ? ((await r.json()).formats || []) : [];
    } catch (e) { known = []; }
    return known;
  }
  const isMarkdown = name => /\.(md|markdown)$/i.test(String(name || ""));

  async function ask(o) {
    const list = await formats();
    if (!list.length) { await o.original(); return; }                   // a server that cannot convert: the file as it always was
    const last = kept();
    let mode = MODES.includes(last.mode) ? last.mode : "md";
    const dlg = h("dialog", "im-dialog mx-dlg");
    const head = h("h2", "", T("mx.title", o.name)); head.id = "mx-title";
    dlg.setAttribute("aria-labelledby", head.id);
    const select = h("select", "mx-format", list.map(f => Object.assign(h("option", "", f.label), { value: f.id })));
    select.id = "mx-format";
    select.value = list.some(f => f.id === last.format) ? last.format : list[0].id;
    const label = h("label", "mx-format-l", T("mx.format"), select); label.htmlFor = "mx-format";
    const rows = MODES.map(m => {
      const r = h("input"); r.type = "radio"; r.name = "mx-mode"; r.value = m; r.checked = m === mode;
      r.onchange = () => { mode = m; select.disabled = mode === "md"; };
      return h("label", "im-check mx-mode", r, h("span", "", T("mx." + m)));
    });
    select.disabled = mode === "md";
    const status = h("p", "setnote im-status"); status.setAttribute("role", "status");
    const close = () => { if (dlg.open) dlg.close(); dlg.remove(); };
    const go = btn(T("mx.go"), "primary", async () => {
      keep({ mode, format: select.value });
      go.disabled = true; status.textContent = "";
      close();
      try { if (mode === "md") await o.original(); else await o.converted(mode, select.value); }
      catch (e) { if (o.fail) o.fail(e); }
    });
    go.id = "mx-go";
    dlg.append(h("div", "im-box mx-box", head, h("div", "mx-modes", rows), label, status, h("div", "im-actions", btn(T("mx.cancel"), "ghost", close), go)));
    dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
    document.body.append(dlg);
    dlg.showModal();
    (rows[MODES.indexOf(mode)].querySelector("input")).focus();
  }

  window.mdExport = { ask, isMarkdown };
})();
