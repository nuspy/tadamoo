"use strict";
/* Addresses for a person who is not at the machine the server runs on (docs/remote-devices.md). A page opened at an address that is not this machine's own (a phone or another PC
   over Tailscale or the LAN) shows answers whose links may name this machine as the model or a tool wrote them: localhost:PORT, 127.0.0.1, 0.0.0.0, a file route of this server as an
   address, a path on the server's disk. None of those works where the person is. `NetAddr.fix` turns them into what does, in the text of an answer, before it is shown:
     - an address of this machine becomes the host the page was reached at (the port and the rest as written);
     - a file route of this server written as an address (api/outputs/<session>/<id>/file, api/agent/sessions/<session>/file?path=) becomes the reference the page resolves with its own key
       (tadamoo-file:<id>, tadamoo-path:<path>), so a picture loads from the network address instead of staying a dead link;
     - a path on the server's disk inside the conversation's folder, as the target of a link or a picture, becomes tadamoo-path:<path in the folder>.
   At the machine itself (a loopback page) the text is returned as it is. The same rules are in tadamoo/netaddr.py (server side) and mobile/lib/src/net_address.dart (the app). */
(() => {
  const LOOP_NAME = /^(localhost|[^.]+\.localhost)$/i;
  const isLoopback = host => {
    host = String(host || "").toLowerCase().replace(/^\[|\]$/g, "");
    if (!host) return false;
    if (LOOP_NAME.test(host)) return true;
    return /^127(\.\d{1,3}){3}$/.test(host) || host === "::1" || host === "0.0.0.0" || host === "::";
  };
  let forced = null;                                                   // a test says where the page is
  const where = () => forced || { remote: !isLoopback(location.hostname), host: location.hostname, base: location.origin };
  const LOOP = String.raw`(?:localhost|[A-Za-z0-9-]+\.localhost|127(?:\.\d{1,3}){3}|\[::1\]|0\.0\.0\.0|\[::\])`;
  const URL_RE = new RegExp(String.raw`\b(https?|wss?):\/\/(${LOOP})(:\d{1,5})?((?:[\/?#][^\s<>()\[\]"'` + "`" + String.raw`]*)?)`, "gi");
  const OUTPUT_ROUTE = new RegExp(String.raw`(!?\[[^\]\n]*\]\()https?:\/\/${LOOP}(?::\d{1,5})?\/api\/outputs\/[A-Za-z0-9_-]+\/([a-f0-9]{12})\/file[^)\s]*\)`, "gi");
  const SESSION_FILE = new RegExp(String.raw`(!?\[[^\]\n]*\]\()https?:\/\/${LOOP}(?::\d{1,5})?\/api\/agent\/sessions\/[A-Za-z0-9_-]+\/file\?([^)\s]*)\)`, "gi");
  const DISK_TARGET = /(!?\[[^\]\n]*\]\()(?:file:\/\/\/?)?([A-Za-z]:[\\/][^)\s]+|\/[^)\s]+)\)/g;
  const normal = p => String(p).replace(/\\/g, "/").replace(/\/+$/, "");

  /* the path of a file inside the conversation's folder, relative to it, or "" */
  function inside(path, folder) {
    const p = normal(decodeURI(path)), f = normal(folder || "");
    if (!f) return "";
    const win = /^[A-Za-z]:/.test(f);
    const a = win ? p.toLowerCase() : p, b = win ? f.toLowerCase() : f;
    return a.startsWith(b + "/") ? p.slice(f.length + 1) : "";
  }
  const folderNow = () => { try { return (window.agentChat && window.agentChat.record && (window.agentChat.record() || {}).workspace) || ""; } catch (e) { return ""; } };

  function fix(text, opts) {
    const here = where();
    if (!here.remote || typeof text !== "string" || !text) return text;
    const folder = (opts && opts.workspace) || folderNow();
    let out = text.replace(OUTPUT_ROUTE, (m, pre, id) => `${pre}tadamoo-file:${id})`);
    out = out.replace(SESSION_FILE, (m, pre, query) => {
      let path = "";
      try { path = new URLSearchParams(query).get("path") || ""; } catch (e) { /* not a query */ }
      return path ? `${pre}tadamoo-path:${encodeURI(path).replace(/\)/g, "%29")})` : m;
    });
    if (folder) out = out.replace(DISK_TARGET, (m, pre, path) => { const rel = inside(path, folder); return rel ? `${pre}tadamoo-path:${encodeURI(rel).replace(/\)/g, "%29")})` : m; });
    return out.replace(URL_RE, (m, scheme, host, port, rest) => `${scheme}://${here.host.includes(":") ? "[" + here.host + "]" : here.host}${port || ""}${rest}`);
  }
  window.NetAddr = {
    fix, isLoopback,
    remote: () => where().remote, base: () => where().base,
    force(state) { forced = state ? { remote: !!state.remote, host: state.host || "", base: state.base || "" } : null; },        // for the tests
  };
})();
