"use strict";
/* MCP Apps in the page (docs/mcp-apps.md): the host side of the extension io.modelcontextprotocol/ui.

   A tool of an MCP server may be tied to a page. The conversation shows a compact placeholder in the tool's bubble (the name of the app and a button) and the app itself in a
   window: a panel pinned beside the conversation (the default) or a separate resizable dialog; an icon in the window's header switches between the two. On a phone both are full
   screen. The app runs in two nested iframes: the outer one comes from a second port of this server (a different origin, with the Content-Security-Policy of what the app declared as an
   HTTP header; serve/mcp_sandbox.py) and passes the messages on; the inner one holds the app's HTML. Host and app talk JSON-RPC 2.0 over postMessage: ui/initialize and the
   notifications from the host; tools/call, resources/read and the ui/* requests from the app. What needs the MCP server goes to the conversation's process
   (POST api/agent/sessions/<id>/app); what is the page's own (opening a link, a message, the display mode, a download) is done here. Everything the server or an app says is
   put in the page as text. */
(() => {
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const PROTOCOL = "2026-01-26", SUPPORTED = [PROTOCOL];
  const MODES = ["inline", "fullscreen", "pip"];                       // the specification's display modes: inline = the panel, pip = the dialog, fullscreen = the dialog maximised
  const READY_MS = 8000, TEARDOWN_MS = 2000, MAX_DOWNLOAD = 20 * 1024 * 1024;
  const views = new Map();                                             // app id -> AppView
  let cfg = null, cfgPromise = null, backend = null;
  const phone = () => window.matchMedia("(max-width: 720px)").matches;
  const text = (k, ...a) => T(k, ...a);

  async function config() {
    if (cfg) return cfg;
    if (!cfgPromise) cfgPromise = fetch("api/mcp-apps/config", { cache: "no-store", headers: authHeaders() }).then(r => (r.ok ? r.json() : null)).catch(() => null).then(c => { cfgPromise = null; if (c) cfg = c; return c || { sandbox_port: 0, settings: {} }; });
    return cfgPromise;
  }
  const settings = () => (cfg && cfg.settings) || {};
  const isOn = key => (settings().on || []).includes(key);

  /* what a view asks of the server: through the conversation's process; tests put their own */
  async function realBackend(sid, op, params) {
    try {
      const r = await fetch(`api/agent/sessions/${encodeURIComponent(sid)}/app`, { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify({ op, params }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) return { error: { code: -32000, message: (j.error && j.error.message) || `HTTP ${r.status}` } };
      return j;
    } catch (e) { return { error: { code: -32000, message: String(e.message || e) } }; }
  }
  const ask = (sid, op, params) => (backend || realBackend)(sid, op, params);

  async function template(hash) {
    const r = await fetch(`api/mcp-apps/template/${encodeURIComponent(hash)}`, { cache: "no-store", headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }

  /* the CSS variables of the specification, from this page's own look; light-dark() pairs so a light theme works as well */
  function hostStyles() {
    const css = getComputedStyle(document.documentElement), v = n => (css.getPropertyValue(n) || "").trim();
    const pair = (light, dark) => `light-dark(${light}, ${dark})`;
    return {
      "--color-background-primary": pair("#ffffff", v("--surface") || "#1a2128"), "--color-background-secondary": pair("#f1f3f5", v("--panel") || "#151a1f"),
      "--color-background-tertiary": pair("#e4e8eb", v("--raised") || "#2a333c"), "--color-background-inverse": pair("#171717", "#ffffff"),
      "--color-background-danger": pair("#fdecea", "#3a1a22"), "--color-background-success": pair("#e6f4ea", "#17301f"), "--color-background-warning": pair("#fff4d6", "#3a3210"),
      "--color-background-info": pair("#e8f0fe", "#14263a"),
      "--color-text-primary": pair("#171717", v("--fg") || "#e9ecee"), "--color-text-secondary": pair("#555b61", v("--fg-2") || "#9aa4ad"), "--color-text-tertiary": pair("#7a8188", v("--fg-3") || "#8a95a0"),
      "--color-text-inverse": pair("#ffffff", "#171717"), "--color-text-danger": pair("#b3261e", v("--err") || "#d93a5b"), "--color-text-warning": pair("#8a5a00", v("--warn") || "#e9c341"),
      "--color-text-success": pair("#1b6e34", "#7ad596"), "--color-text-info": pair("#0b57d0", "#8ab4f8"),
      "--color-border-primary": pair("#cfd4d9", v("--line") || "#28313a"), "--color-border-secondary": pair("#e1e5e8", v("--line-soft") || "#222a32"),
      "--color-ring-primary": pair("#ff7a3d", v("--accent") || "#ff7a3d"),
      "--font-sans": v("--font-sans") ? "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif" : "system-ui, sans-serif",
      "--font-mono": "ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
      "--font-weight-normal": "400", "--font-weight-medium": "500", "--font-weight-semibold": "600", "--font-weight-bold": "700",
      "--font-text-sm-size": "14px", "--font-text-md-size": "16px", "--font-text-lg-size": "18px", "--font-heading-md-size": "20px", "--font-heading-lg-size": "24px",
      "--font-text-md-line-height": "1.5", "--font-heading-md-line-height": "1.3",
      "--border-radius-sm": "6px", "--border-radius-md": "8px", "--border-radius-lg": "12px", "--border-radius-full": "9999px", "--border-width-regular": "1px",
    };
  }
  const theme = () => {
    const scheme = getComputedStyle(document.documentElement).colorScheme || "";
    return /\blight\b/.test(scheme) && !/\bdark\b/.test(scheme) ? "light" : "dark";
  };
  function safeInsets() {
    const p = mk("div"); p.style.cssText = "position:fixed;visibility:hidden;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)";
    document.body.append(p);
    const s = getComputedStyle(p), n = x => parseFloat(x) || 0, out = { top: n(s.paddingTop), right: n(s.paddingRight), bottom: n(s.paddingBottom), left: n(s.paddingLeft) };
    p.remove();
    return out;
  }
  const grantable = perms => { const out = {}; for (const k of ["camera", "microphone", "geolocation", "clipboardWrite"]) if (perms && perms[k]) out[k] = {}; return out; };
  const allowAttr = perms => [["camera", "camera"], ["microphone", "microphone"], ["geolocation", "geolocation"], ["clipboardWrite", "clipboard-write"]].filter(([k]) => perms && perms[k]).map(p => p[1]).join("; ");

  class AppView {
    constructor(rec, sid) {
      this.rec = rec; this.sid = sid; this.state = "idle";                // idle (not running), starting, ready
      this.kind = null; this.max = false;                                // where it is shown: "panel", "dialog", or null (parked / not shown)
      this.frame = null; this.origin = ""; this.pending = new Map(); this.nextId = 1;
      this.appInfo = null; this.appCaps = {}; this.initialized = false; this.sentInput = false; this.last = {}; this.calls = [];
      this.win = null; this.stateEl = null; this.callsEl = null; this.listeners = []; this.readyTimer = 0; this.placeholders = new Set();
    }
    get key() { return this.rec.key; }
    get name() { return this.rec.name || this.rec.uri; }
    get running() { return this.state !== "idle"; }
    get shown() { return !!this.kind && !!this.win && !this.win.hidden; }

    /* ---- the window ---- */
    build() {
      if (this.win) return;
      const w = mk("section", "mcpwin"); w.hidden = true; w.tabIndex = -1;
      const head = mk("header", "mcpwin-head"), title = mk("span", "mcpwin-title", this.name), sub = mk("span", "mcpwin-sub", text("ma.from", this.rec.server_name || ""));
      const grow = mk("span", "mcpwin-grow");
      const sw = mk("button", "mcpwin-b mcpwin-switch"); sw.type = "button";
      const mx = mk("button", "mcpwin-b mcpwin-max"); mx.type = "button";
      const cl = mk("button", "mcpwin-b mcpwin-close"); cl.type = "button"; cl.textContent = "×"; cl.setAttribute("aria-label", text("ma.close")); cl.title = text("ma.close");
      sw.onclick = () => this.show(this.kind === "panel" ? "dialog" : "panel", true);
      mx.onclick = () => this.setMax(!this.max);
      cl.onclick = () => this.close();
      head.append(title, sub, grow, sw, mx, cl);
      const body = mk("div", "mcpwin-body"), st = mk("div", "mcpwin-state"), calls = mk("div", "mcpwin-calls");
      st.setAttribute("role", "status"); calls.setAttribute("aria-live", "polite");
      body.append(st);
      w.append(head, body, calls);
      this.win = w; this.stateEl = st; this.callsEl = calls; this.bodyEl = body; this.switchBtn = sw; this.maxBtn = mx; this.titleEl = title;
      this.dragHead(head);
      w.addEventListener("keydown", e => { if (e.key === "Escape" && this.kind === "dialog") { e.stopPropagation(); this.close(); } });
      new ResizeObserver(() => this.sizeChanged()).observe(w);
      dock().append(w);
    }
    paintWindow() {
      if (!this.win) return;
      const dialog = this.kind === "dialog";
      this.win.dataset.display = this.kind || ""; this.win.dataset.max = this.max ? "1" : "0";
      this.win.setAttribute("role", dialog ? "dialog" : "complementary"); this.win.setAttribute("aria-label", this.name);
      this.switchBtn.textContent = dialog ? "◫" : "⧉";
      const label = text(dialog ? "ma.pin" : "ma.window"); this.switchBtn.setAttribute("aria-label", label); this.switchBtn.title = label;
      this.maxBtn.hidden = !dialog; this.maxBtn.textContent = this.max ? "❐" : "□";
      const m = text(this.max ? "ma.restore" : "ma.maximise"); this.maxBtn.setAttribute("aria-label", m); this.maxBtn.title = m;
      this.stateEl.hidden = this.state === "ready";
      this.callsEl.hidden = !this.calls.length;
      this.callsEl.replaceChildren(...this.calls.slice(-3).map(c => mk("div", "mcpwin-call", text("ma.call." + c.state, c.tool))));
    }
    setStateText(s) { if (this.stateEl) { this.stateEl.textContent = s; this.stateEl.hidden = !s; } }
    dragHead(head) {                                                      // a dialog moves by its header, with the pointer (mouse, pen or finger)
      let from = null;
      head.addEventListener("pointerdown", e => {
        if (this.kind !== "dialog" || this.max || phone() || e.target.closest("button")) return;
        const r = this.win.getBoundingClientRect();
        from = { x: e.clientX - r.left, y: e.clientY - r.top }; head.setPointerCapture(e.pointerId);
      });
      head.addEventListener("pointermove", e => {
        if (!from) return;
        const w = this.win, x = Math.min(Math.max(0, e.clientX - from.x), innerWidth - 80), y = Math.min(Math.max(0, e.clientY - from.y), innerHeight - 40);
        w.style.left = x + "px"; w.style.top = y + "px"; w.style.right = "auto"; w.style.bottom = "auto";
      });
      const end = () => { from = null; };
      head.addEventListener("pointerup", end); head.addEventListener("pointercancel", end);
    }

    /* where it is shown: "panel" pinned beside the conversation, "dialog" a separate window; null parks it (still running if the person keeps it on) */
    show(kind, focus) {
      this.build();
      kind = kind === "dialog" ? "dialog" : "panel";
      if (kind === "panel") for (const other of views.values()) if (other !== this && other.kind === "panel" && other.shown) other.release("replaced");
      const changed = this.kind !== kind || this.win.hidden;
      this.kind = kind; if (kind === "panel") this.max = false;
      this.win.hidden = false;
      this.paintWindow(); updateDock();
      if (this.state === "idle") this.start();
      else if (changed) { this.notifyContext(); }
      if (focus) this.win.focus({ preventScroll: true });                 // only when the person asked for it: an app that opens by itself does not take the keyboard
      repaintAll();
    }
    setMax(on) {
      if (this.kind !== "dialog") { this.show("dialog"); }
      this.max = !!on; this.paintWindow(); this.notifyContext(); repaintAll();
    }
    /* nothing shows it any more: an app that is on keeps running parked, any other one is stopped */
    release(reason) {
      if (this.win) this.win.hidden = true;
      this.kind = null; updateDock();
      if (!isOn(this.key)) this.stop(reason || "closed");
      repaintAll();
    }
    close() { this.release("closed"); }

    /* ---- starting and stopping ---- */
    async start() {
      if (this.state !== "idle") return;
      this.state = "starting"; this.initialized = false; this.sentInput = false; this.sentResult = false; this.paintWindow(); this.setStateText(text("ma.loading"));
      try {
        const c = await config();
        if (!c.sandbox_port) throw new Error(text("ma.nosandbox"));
        const page = await template(this.rec.template);
        const u = new URL(location.href); u.port = String(c.sandbox_port); u.pathname = "/sandbox"; u.search = ""; u.hash = "";
        u.searchParams.set("host", location.origin);
        const meta = page.meta || this.rec.ui || {};
        if (meta.csp) u.searchParams.set("csp", JSON.stringify(meta.csp));
        this.meta = meta; this.html = page.html; this.origin = u.origin;
        const f = document.createElement("iframe");
        f.setAttribute("sandbox", "allow-scripts allow-same-origin"); f.title = this.name; f.src = u.href; f.className = "mcpwin-frame";
        const allow = allowAttr(meta.permissions); if (allow) f.setAttribute("allow", allow);
        if (meta.prefersBorder === false) this.win.dataset.border = "0"; else if (meta.prefersBorder === true) this.win.dataset.border = "1";
        this.frame = f; this.bodyEl.append(f);
        const on = ev => this.onMessage(ev);
        window.addEventListener("message", on); this.listeners.push(() => window.removeEventListener("message", on));
        this.readyTimer = setTimeout(() => { if (this.state === "starting" && !this.initialized) this.failed(text("ma.noanswer")); }, READY_MS);
      } catch (e) { this.failed(String(e.message || e)); }
    }
    failed(why) {
      this.stopQuiet();
      this.state = "idle"; this.setStateText(text("ma.failed", why)); if (this.win) { this.stateEl.hidden = false; this.stateEl.dataset.bad = "1"; }
      repaintAll();
    }
    stopQuiet() {
      clearTimeout(this.readyTimer);
      this.listeners.forEach(f => f()); this.listeners = [];
      if (this.frame) { this.frame.remove(); this.frame = null; }
      for (const w of this.pending.values()) w.reject(new Error("stopped"));
      this.pending.clear();
    }
    /* the app is told before it goes (ui/resource-teardown) and given a moment to answer, so it can save what it holds */
    async stop(reason) {
      if (this.state === "idle") return;
      if (this.initialized && this.frame) {
        try { await Promise.race([this.hostRequest("ui/resource-teardown", { reason: reason || "closed" }), new Promise(r => setTimeout(r, TEARDOWN_MS))]); } catch (e) { /* it did not answer: it goes anyway */ }
      }
      this.stopQuiet();
      this.state = "idle"; this.initialized = false;
      if (this.stateEl) { this.stateEl.dataset.bad = ""; this.setStateText(""); }
      this.paintWindow(); repaintAll();
    }

    /* ---- JSON-RPC over postMessage, through the sandbox ---- */
    post(message) { if (this.frame && this.frame.contentWindow) this.frame.contentWindow.postMessage({ jsonrpc: "2.0", ...message }, this.origin); }
    notify(method, params) { if (this.initialized) this.post({ method, params: params || {} }); }    // nothing goes to the app before it said it is initialized
    hostRequest(method, params) {
      return new Promise((resolve, reject) => { const id = `h${this.nextId++}`; this.pending.set(id, { resolve, reject }); this.post({ id, method, params: params || {} }); });
    }
    reply(id, result) { this.post({ id, result }); }
    fail(id, code, message) { this.post({ id, error: { code, message } }); }

    onMessage(ev) {
      if (!this.frame || ev.source !== this.frame.contentWindow || ev.origin !== this.origin) return;
      const m = ev.data;
      if (!m || m.jsonrpc !== "2.0") return;
      if (m.method === undefined) {                                       // an answer to a request of ours
        const w = this.pending.get(m.id);
        if (w) { this.pending.delete(m.id); if (m.error) w.reject(new Error(m.error.message || "error")); else w.resolve(m.result); }
        return;
      }
      if (m.method === "ui/notifications/sandbox-proxy-ready") {
        this.post({ method: "ui/notifications/sandbox-resource-ready", params: { html: this.html, csp: this.meta.csp, permissions: grantable(this.meta.permissions) } });
        return;
      }
      if (m.id !== undefined) { this.request(m).catch(e => this.fail(m.id, e.code || -32000, String(e.message || e))); return; }
      this.notification(m);
    }
    notification(m) {
      const p = m.params || {};
      if (m.method === "ui/notifications/initialized") { this.initialized = true; this.state = "ready"; clearTimeout(this.readyTimer); this.setStateText(""); this.paintWindow(); repaintAll(); this.sendData(); }
      else if (m.method === "ui/notifications/size-changed") { this.size = { width: Number(p.width) || 0, height: Number(p.height) || 0 }; }
      else if (m.method === "ui/notifications/request-teardown") this.close();
      else if (m.method === "notifications/message") ask(this.sid, "log", { app: this.rec.app, server: this.rec.server, level: p.level, data: typeof p.data === "string" ? p.data : JSON.stringify(p.data) });
      else if (m.method === "notifications/tools/list_changed") this.appTools = null;
    }
    err(code, message) { const e = new Error(message); e.code = code; return e; }

    async request(m) {
      const p = m.params || {}, id = m.id;
      switch (m.method) {
        case "ui/initialize": {
          this.appInfo = p.appInfo || null; this.appCaps = p.appCapabilities || {};
          this.reply(id, { protocolVersion: SUPPORTED.includes(p.protocolVersion) ? p.protocolVersion : PROTOCOL, hostInfo: { name: "TaDaMoo", version: "1.0.0" },
                           hostCapabilities: this.hostCapabilities(), hostContext: this.context() });
          return;
        }
        case "ping": this.reply(id, {}); return;
        case "tools/call": case "tools/list": case "resources/read": {
          const params = m.method === "tools/call" ? { name: p.name, arguments: p.arguments } : m.method === "resources/read" ? { uri: p.uri } : {};
          const a = await ask(this.sid, m.method, { ...params, app: this.rec.app, server: this.rec.server });
          if (a.error) throw this.err(a.error.code || -32000, a.error.message);
          this.reply(id, a.result || {}); return;
        }
        case "ui/update-model-context": {
          const a = await ask(this.sid, "context", { app: this.rec.app, server: this.rec.server, name: this.name, content: p.content, structuredContent: p.structuredContent });
          if (a.error) throw this.err(a.error.code || -32000, a.error.message);
          this.reply(id, {}); return;
        }
        case "ui/message": {
          const c = p.content;
          if (p.role !== "user" || !c || c.type !== "text" || typeof c.text !== "string" || !c.text.trim()) throw this.err(-32602, "Invalid message format");
          const sent = window.agentChat && window.agentChat.sendFromApp ? await window.agentChat.sendFromApp(c.text, this.sid) : false;
          if (!sent) throw this.err(-32000, "Message sending denied");
          this.reply(id, {}); return;
        }
        case "ui/open-link": {
          let url; try { url = new URL(String(p.url)); } catch (e) { throw this.err(-32000, "Invalid URL"); }
          if (url.protocol !== "https:" && url.protocol !== "http:") throw this.err(-32000, "Policy violation");
          window.open(url.href, "_blank", "noopener,noreferrer");
          this.reply(id, {}); return;
        }
        case "ui/request-display-mode": this.reply(id, { mode: this.requestMode(p.mode) }); return;
        case "ui/download-file": await this.download(p); this.reply(id, {}); return;
        default: throw this.err(-32601, `Method not found: ${m.method}`);
      }
    }

    /* inline is the panel, fullscreen the dialog maximised, pip the dialog; a mode the app did not declare is not switched to: the answer is the mode it is in */
    mode() { return this.kind === "dialog" ? (this.max ? "fullscreen" : "pip") : "inline"; }
    requestMode(want) {
      const declared = this.appCaps && Array.isArray(this.appCaps.availableDisplayModes) ? this.appCaps.availableDisplayModes : null;
      if (!MODES.includes(want) || (declared && !declared.includes(want))) return this.mode();
      if (want === "inline") this.show("panel");
      else { this.show("dialog"); this.setMax(want === "fullscreen"); }
      return this.mode();
    }
    hostCapabilities() {
      const meta = this.meta || {};
      return { openLinks: {}, downloadFile: {}, serverTools: {}, serverResources: {}, logging: {}, updateModelContext: { text: {}, structuredContent: {} }, message: { text: {} },
               sandbox: { permissions: grantable(meta.permissions), csp: meta.csp || {} } };
    }
    context() {
      const r = this.win ? this.win.getBoundingClientRect() : { width: 0, height: 0 }, head = this.win && this.win.querySelector(".mcpwin-head");
      return { toolInfo: { id: this.rec.call_id || undefined, tool: this.rec.tool }, theme: theme(), styles: { variables: hostStyles() }, displayMode: this.mode(), availableDisplayModes: MODES.slice(),
               containerDimensions: { width: Math.round(r.width), height: Math.max(0, Math.round(r.height - (head ? head.getBoundingClientRect().height : 0))) },
               locale: LANG === "it" ? "it-IT" : "en-US", timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone, userAgent: "TaDaMoo", platform: phone() ? "mobile" : "web",
               deviceCapabilities: { touch: window.matchMedia("(pointer: coarse)").matches, hover: window.matchMedia("(hover: hover)").matches }, safeAreaInsets: safeInsets() };
    }
    /* what changed in the host's context since the app last got it, as host-context-changed (only the fields that differ) */
    notifyContext() {
      if (!this.initialized) return;
      const now = this.context(), change = {};
      for (const k of Object.keys(now)) if (JSON.stringify(now[k]) !== JSON.stringify(this.last[k])) change[k] = now[k];
      this.last = now;
      if (Object.keys(change).length) this.notify("ui/notifications/host-context-changed", change);
    }
    sizeChanged() { clearTimeout(this.sizeTimer); this.sizeTimer = setTimeout(() => this.notifyContext(), 120); }

    /* ---- what the host sends once the app is ready: the input, then the result or the cancellation ---- */
    sendData() {
      this.last = this.context();
      this.notify("ui/notifications/tool-input", { arguments: this.rec.input || {} });
      this.sentInput = true;
      this.sendOutcome();
    }
    sendOutcome() {
      if (!this.sentInput) return;
      if (this.rec.state === "done" && this.rec.result && !this.sentResult) { this.sentResult = true; this.notify("ui/notifications/tool-result", this.rec.result); }
      else if (this.rec.state === "cancelled" && !this.sentResult) { this.sentResult = true; this.notify("ui/notifications/tool-cancelled", { reason: this.rec.reason || "" }); }
    }
    result(result) { this.rec.state = "done"; this.rec.result = result; this.sendOutcome(); repaintAll(); }
    cancelled(reason) { this.rec.state = "cancelled"; this.rec.reason = reason; this.sendOutcome(); repaintAll(); }
    called(e) {
      this.calls.push({ tool: e.tool, state: e.state }); if (this.calls.length > 20) this.calls.shift();
      this.paintWindow(); repaintAll();
    }

    /* a file the app offers: the person is asked, then it is saved; a name is only a name */
    async download(p) {
      const items = Array.isArray(p.contents) ? p.contents : [];
      if (!items.length) throw this.err(-32000, "Invalid content");
      for (const item of items) {
        let name = "download", blob;
        if (item && item.type === "resource" && item.resource) {
          const r = item.resource;
          name = String(r.uri || "").split(/[\\/]/).filter(Boolean).pop() || name;
          if (typeof r.text === "string") blob = new Blob([r.text], { type: r.mimeType || "text/plain" });
          else if (typeof r.blob === "string") { try { const bin = atob(r.blob), a = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i); blob = new Blob([a], { type: r.mimeType || "application/octet-stream" }); } catch (e) { throw this.err(-32000, "Invalid content"); } }
        } else if (item && item.type === "resource_link" && typeof item.uri === "string") {
          name = item.name || String(item.uri).split("/").filter(Boolean).pop() || name;
          try { const resp = await fetch(item.uri); if (!resp.ok) throw new Error("status"); blob = await resp.blob(); } catch (e) { throw this.err(-32000, "Invalid content"); }
        }
        if (!blob || blob.size > MAX_DOWNLOAD) throw this.err(-32000, "Invalid content");
        name = name.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").replace(/^\.+/, "").slice(0, 120) || "download";
        if (!window.confirm(text("ma.dl.confirm", this.name, name))) throw this.err(-32000, "Download denied by user");
        const a = mk("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      }
    }
  }

  /* ---- the dock: the window of every app lives in one place so an iframe is never moved (moving one reloads it) ---- */
  function dock() {
    let d = document.getElementById("appdock");
    if (!d) {
      d = mk("aside", "appdock"); d.id = "appdock"; d.setAttribute("aria-label", text("ma.dock"));
      const row = document.querySelector("#view-chat .chatrow");
      (row || document.body).append(d);
    }
    return d;
  }
  function updateDock() {
    const d = dock(), any = [...views.values()].some(v => v.kind === "panel" && v.shown);
    d.dataset.panel = any ? "1" : "0";
  }

  /* ---- the placeholder in the tool's bubble: the name of the app, its state and a button that brings it up ---- */
  const painters = new Set();
  function repaintAll() { painters.forEach(f => f()); }
  function placeholder(view) {
    const row = mk("div", "mcpph"), name = mk("b", "mcpph-name", view.name), sub = mk("span", "mcpph-sub"), go = mk("button", "mcpph-go"), calls = mk("div", "mcpph-calls");
    go.type = "button"; row.setAttribute("role", "group"); row.setAttribute("aria-label", text("ma.app", view.name));
    go.onclick = () => (view.shown ? (view.win.focus(), view.kind === "dialog" || phone() ? null : view.win.scrollIntoView({ block: "nearest" })) : view.show(settings().display === "dialog" ? "dialog" : "panel", true));
    row.append(mk("span", "mcpph-ico", "▦"), name, sub, go, calls);
    const paint = () => {
      const s = view.rec.state === "cancelled" ? text("ma.state.cancelled") : view.state === "idle" ? text(view.rec.state === "running" ? "ma.state.running" : "ma.state.ready") : view.state === "starting" ? text("ma.loading") : view.shown ? text("ma.state.shown") : text("ma.state.parked");
      sub.textContent = ` · ${view.rec.server_name || ""} · ${s}`;
      go.textContent = view.shown ? text("ma.focus") : text("ma.open");
      calls.replaceChildren(...view.calls.slice(-2).map(c => mk("div", "mcpph-call", text("ma.call." + c.state, c.tool))));
      calls.hidden = !view.calls.length;
    };
    painters.add(paint); paint();
    return row;
  }

  /* ---- what the conversation page calls ---- */
  const api = {
    /* a record of an app (from an event or from the saved history). A live one opens at once where the person chose; one read from the history waits for its button. */
    attach(sid, rec, live) {
      let v = views.get(rec.app);
      if (!v) { v = new AppView(rec, sid); views.set(rec.app, v); }
      else { v.rec = { ...v.rec, ...rec }; }
      if (live) config().then(() => { if (!v.shown) v.show(settings().display === "dialog" ? "dialog" : "panel"); });
      return v;
    },
    view: id => views.get(id) || null,
    paint(el, block, sid) {                                              // the placeholder of a tool's bubble
      const rec = block.app;
      if (!rec) return;
      let row = el.querySelector(".mcpph");
      if (!row) { const view = api.attach(sid, rec, false); row = placeholder(view); el.querySelector("summary").after(row); }
    },
    result(id, result) { const v = views.get(id); if (v) v.result(result); },
    cancelled(id, reason) { const v = views.get(id); if (v) v.cancelled(reason); },
    called(e) { const v = views.get(e.app); if (v) v.called(e); },
    /* the server of some apps was stopped: those nobody keeps on stop with it */
    serverStopped(server) { for (const v of views.values()) if (v.rec.server === server && v.running && !isOn(v.key)) v.release("server stopped"); },
    /* the person went to another conversation: its apps are no longer shown; the ones that are on stay running, hidden */
    left(sid) { for (const v of views.values()) if (v.sid !== sid) { if (v.shown) v.release("conversation left"); else if (v.running && !isOn(v.key)) v.stop("conversation left"); } },
    running: () => [...views.values()].filter(v => v.running).map(v => v.key),
    all: () => [...views.values()],
    setBackend(fn) { backend = fn; },
    reset() { for (const v of views.values()) { v.stopQuiet(); if (v.win) v.win.remove(); } views.clear(); painters.clear(); },
    reloadConfig() { cfg = null; return config(); },
    config,
  };
  window.mcpApps = api;
  document.addEventListener("tadamoo:conversation", () => { const cur = window.agentChat && window.agentChat.current ? window.agentChat.current() : ""; api.left(cur); });
  window.addEventListener("resize", () => { for (const v of views.values()) if (v.running) v.sizeChanged(); });
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  if (dark.addEventListener) dark.addEventListener("change", () => views.forEach(v => v.notifyContext()));
  new MutationObserver(() => views.forEach(v => v.notifyContext())).observe(document.documentElement, { attributes: true, attributeFilter: ["class", "style", "data-theme"] });
})();
