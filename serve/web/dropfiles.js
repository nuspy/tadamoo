"use strict";
/* Files and folders dropped on the Chat: in a conversation with the Agent they are attached to it (a folder file by file, each named by its
   path inside the folder); in the model-only chat pictures and videos are attached to the message. The + and ++ buttons keep working. */
(() => {
  const zone = document.getElementById("view-chat");
  if (!zone) return;
  const status = t => { const s = document.getElementById("chat-status"); if (s) s.textContent = t; };
  const hasFiles = e => [...((e.dataTransfer || {}).types || [])].includes("Files");
  let depth = 0;

  const mark = on => { if (on) zone.setAttribute("data-drop", T("ch.drop.hint")); zone.classList.toggle("dropping", on); };
  zone.addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); depth++; mark(true); });
  zone.addEventListener("dragover", e => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  zone.addEventListener("dragleave", e => { if (hasFiles(e) && --depth <= 0) { depth = 0; mark(false); } });
  window.addEventListener("dragend", () => { depth = 0; mark(false); });

  const readEntries = reader => new Promise(resolve => {
    const all = [];
    const next = () => reader.readEntries(batch => { if (!batch.length) resolve(all); else { all.push(...batch); next(); } }, () => resolve(all));
    next();
  });
  const fileOf = entry => new Promise(resolve => entry.file(resolve, () => resolve(null)));
  // every file under an entry, each with its path inside the dropped folder (kept as webkitRelativePath, as the folder picker does)
  async function walk(entry, out) {
    if (entry.isFile) {
      const f = await fileOf(entry);
      if (f) { const path = entry.fullPath.replace(/^\//, ""); if (path.includes("/")) Object.defineProperty(f, "webkitRelativePath", { value: path }); out.push(f); }
    } else if (entry.isDirectory) {
      for (const child of await readEntries(entry.createReader())) await walk(child, out);
    }
  }

  zone.addEventListener("drop", async e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0; mark(false);
    const items = [...((e.dataTransfer || {}).items || [])];
    // the entries must be taken now: the list is emptied when the handler returns
    const entries = items.filter(i => i.kind === "file").map(i => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null));
    const plain = [...((e.dataTransfer || {}).files || [])];
    const files = [];
    if (entries.length && entries.every(Boolean)) { for (const en of entries) await walk(en, files); } else files.push(...plain);
    if (!files.length) return;
    const nested = files.filter(f => f.webkitRelativePath && f.webkitRelativePath.includes("/"));
    const agent = window.chatUi && window.chatUi.agentActive && window.chatUi.agentActive() && window.agentAttach;
    if (agent) {
      const loose = files.filter(f => !nested.includes(f));
      if (loose.length) await window.agentAttach.files(loose);
      if (nested.length) await window.agentAttach.dir(nested);                // the folders of tools and history are left out there, and what is past the limit
      return;
    }
    if (nested.length) { status(T("ch.drop.nofolder")); return; }
    if (window.chatUi && window.chatUi.addMedia) window.chatUi.addMedia(files);
  });
})();
