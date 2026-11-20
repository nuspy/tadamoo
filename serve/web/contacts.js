"use strict";
/* Contacts: who the agent deals with when it reaches outside (today, a phone call - tadamoo/integrations/telephony_tools.py), what may
   be done or shared with them, and the dynamic list of permission types that question is answered with. Three panels: groups and
   contacts (a contact's own choice for a permission overrides its group's, and a permission nobody granted is denied); the types
   themselves (added, edited, removed, reordered - the order here is the order everywhere else they are shown); and their categories. */
(() => {
  const root = document.getElementById("ct-root");
  if (!root) return;
  let data = null, selectedGroup = null, selectedContact = null, note = "", bad = false;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const field = (tag, ph) => { const i = document.createElement(tag); if (ph) i.placeholder = ph; return i; };

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { "Content-Type": "application/json", ...authHeaders() },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      let json = {}; try { json = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, data: json };
    } catch (e) { return { ok: false, data: { error: { message: T("ct.e.offline") } } }; }
  }
  const errText = r => (r.data && r.data.error && r.data.error.message) || T("ct.e.offline");

  async function load() {
    const r = await api("GET", "api/contacts");
    if (r.ok) { data = r.data; note = ""; bad = false; } else { data = data || { permission_categories: [], permission_types: [], groups: [], contacts: [] }; note = errText(r); bad = true; }
    draw();
  }
  async function act(path, body) {
    const r = await api("POST", path, body);
    if (!r.ok) { note = errText(r); bad = true; draw(); return false; }
    data = r.data; note = ""; bad = false;
    draw();
    return true;
  }

  const categoryOf = id => data.permission_categories.find(c => c.id === id);
  const typesOf = catId => data.permission_types.filter(t => t.category === catId);

  function statusLine() {
    if (!note) return null;
    const p = el("p", "setnote", note);
    p.setAttribute("role", "status");
    p.dataset.bad = bad ? "1" : "";
    return p;
  }

  // -- Panel 1: groups and contacts ---------------------------------------------------------------------

  function permRow(t, value, onChange, tristate) {
    const row = el("div", "ct-prow");
    row.append(el("span", "ct-plabel", t.label));
    const sel = document.createElement("select");
    if (tristate) sel.append(new Option(T("ct.perm.inherit"), "inherit"), new Option(T("ct.perm.granted"), "granted"), new Option(T("ct.perm.denied"), "denied"));
    else sel.append(new Option(T("ct.perm.granted"), "granted"), new Option(T("ct.perm.denied"), "denied"));
    sel.value = value;
    sel.onchange = () => onChange(sel.value);
    row.append(sel);
    return row;
  }

  function permissionsEditor(kind, overrides, base) {
    const box = el("div", "ct-perms");
    for (const cat of data.permission_categories) {
      const types = typesOf(cat.id);
      if (!types.length) continue;
      box.append(el("h4", "ct-cath", cat.label));
      for (const t of types) {
        if (kind === "group") {
          box.append(permRow(t, base[t.id] ? "granted" : "denied", v => { overrides[t.id] = v === "granted"; }, false));
        } else {
          const has = Object.prototype.hasOwnProperty.call(overrides, t.id);
          const value = has ? (overrides[t.id] ? "granted" : "denied") : "inherit";
          const row = permRow(t, value, v => { if (v === "inherit") delete overrides[t.id]; else overrides[t.id] = v === "granted"; }, true);
          if (value === "inherit") row.append(el("span", "ct-inherited", T(base[t.id] ? "ct.perm.granted" : "ct.perm.denied")));
          box.append(row);
        }
      }
    }
    if (!data.permission_types.length) box.append(el("p", "ct-empty", T("ct.perms.none")));
    return box;
  }

  // -- voice enrollment (tadamoo/speech/voiceid.py): a few sentences read into the microphone, encoded as a 16 kHz mono WAV in the
  // browser itself - the server never needs to decode any other audio format. -------------------------------------------------

  const ENROLL_SENTENCES = {
    en: ["The weather today is quite different from yesterday.", "I would like to schedule a call for next week.",
         "Please remember to confirm this before the end of the day.", "This is a short sentence to recognise my voice."],
    it: ["Oggi il tempo è piuttosto diverso da ieri.", "Vorrei fissare una chiamata per la settimana prossima.",
         "Ricordati di confermare questo entro la fine della giornata.", "Questa è una frase breve per riconoscere la mia voce."],
  };

  function downsample(samples, inRate, outRate) {
    if (inRate === outRate) return samples;
    const ratio = inRate / outRate, outLen = Math.floor(samples.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) out[i] = samples[Math.floor(i * ratio)];
    return out;
  }

  function wavBase64(samples) {
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) { const s = Math.max(-1, Math.min(1, samples[i])); pcm[i] = s < 0 ? s * 32768 : s * 32767; }
    const bytes = pcm.length * 2, buf = new ArrayBuffer(44 + bytes), view = new DataView(buf);
    const str = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    str(0, "RIFF"); view.setUint32(4, 36 + bytes, true); str(8, "WAVE");
    str(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    str(36, "data"); view.setUint32(40, bytes, true);
    for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true);
    let binary = ""; const u8 = new Uint8Array(buf);
    for (let i = 0; i < u8.length; i++) binary += String.fromCharCode(u8[i]);
    return btoa(binary);
  }

  async function recordWav(seconds) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const chunks = [];
      proc.onaudioprocess = e => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      src.connect(proc);
      proc.connect(ctx.destination);
      await new Promise(r => setTimeout(r, seconds * 1000));
      proc.disconnect();
      src.disconnect();
      const inRate = ctx.sampleRate;
      await ctx.close();
      let total = 0;
      for (const c of chunks) total += c.length;
      const merged = new Float32Array(total);
      let off = 0;
      for (const c of chunks) { merged.set(c, off); off += c.length; }
      return wavBase64(downsample(merged, inRate, 16000));
    } finally {
      stream.getTracks().forEach(t => t.stop());
    }
  }

  function voicePanel(c) {
    const box = el("div", "ct-voice");
    box.append(el("h4", "ct-cath", T("ct.voice.title")));
    const selfRow = el("label", "ct-selfrow");
    const selfBox = document.createElement("input");
    selfBox.type = "checkbox";
    selfBox.checked = !!c.is_self;
    selfBox.onchange = () => act("api/contacts/items", { action: "update", id: c.id, is_self: selfBox.checked });
    selfRow.append(selfBox, document.createTextNode(" " + T("ct.voice.self")));
    box.append(selfRow);
    const v = c.voice || { enrolled: false, readings: {} };
    box.append(el("p", "help", v.enrolled ? T("ct.voice.readings", v.readings.mic || 0, v.readings.phone || 0) : T("ct.voice.none")));
    const progress = el("p", "ct-voiceprog", "");
    box.append(progress);
    const recordBtn = btn(T("ct.voice.record"), "", async () => {
      recordBtn.disabled = true;
      const sentences = ENROLL_SENTENCES[LANG] || ENROLL_SENTENCES.en;
      try {
        const wavs = [];
        for (const s of sentences) {
          progress.textContent = `${T("ct.voice.sentence")} "${s}"`;
          await new Promise(r => setTimeout(r, 900));
          progress.textContent = T("ct.voice.recording");
          wavs.push(await recordWav(4));
        }
        progress.textContent = T("ct.voice.saving");
        const ok = await act("api/contacts/voice", { action: "enroll", id: c.id, channel: "mic", wavs });
        if (!ok) recordBtn.disabled = false;
      } catch (e) {
        progress.textContent = T("ct.voice.needmic");
        recordBtn.disabled = false;
      }
    });
    box.append(recordBtn);
    return box;
  }

  function groupDetail(g) {
    const box = el("div", "ct-detail");
    box.append(el("h3", "ct-h3", g.name));
    const name = field("input", T("ct.group.name"));
    name.value = g.name;
    box.append(el("label", "", T("ct.group.name")), name);
    const overrides = { ...g.permissions };
    box.append(permissionsEditor("group", overrides, {}));
    const save = btn(T("ct.save"), "primary", () => act("api/contacts/groups", { action: "update", id: g.id, name: name.value, permissions: overrides }));
    const remove = btn(T("ct.remove"), "ghost", () => { if (confirm(T("ct.group.remove.confirm", g.name))) { selectedGroup = null; act("api/contacts/groups", { action: "remove", id: g.id }); } });
    const acts = el("div", "ct-acts");
    acts.append(save, remove);
    box.append(acts);
    return box;
  }

  function contactDetail(c) {
    const box = el("div", "ct-detail");
    box.append(el("h3", "ct-h3", c.name));
    const name = field("input"), org = field("input"), role = field("input"), groupSel = document.createElement("select"),
      phones = document.createElement("textarea"), emails = document.createElement("textarea"), sharing = document.createElement("textarea"),
      relation = document.createElement("textarea");
    name.value = c.name; org.value = c.organization; role.value = c.role;
    groupSel.append(new Option(T("ct.nogroup"), ""));
    for (const g of data.groups) groupSel.append(new Option(g.name, g.id));
    groupSel.value = c.group_id || "";
    phones.rows = 3; phones.value = c.phones.map(p => p.label ? `${p.number} | ${p.label}` : p.number).join("\n"); phones.placeholder = T("ct.phones.ph");
    emails.rows = 3; emails.value = c.emails.map(e => e.label ? `${e.address} | ${e.label}` : e.address).join("\n"); emails.placeholder = T("ct.emails.ph");
    sharing.rows = 4; sharing.value = c.sharing_notes; sharing.placeholder = T("ct.sharing.ph");
    relation.rows = 3; relation.value = c.relationship_notes; relation.placeholder = T("ct.relation.ph");
    const rowOf = (labelKey, input) => { const d = el("div", "ct-field"); d.append(el("label", "", T(labelKey)), input); return d; };
    box.append(rowOf("ct.name", name), rowOf("ct.org", org), rowOf("ct.role", role), rowOf("ct.group", groupSel),
      rowOf("ct.phones", phones), rowOf("ct.emails", emails));
    box.append(el("p", "help", T("ct.sharing.help")));
    box.append(rowOf("ct.sharing", sharing));
    box.append(el("p", "help", T("ct.relation.help")));
    box.append(rowOf("ct.relation", relation));
    const overrides = { ...c.overrides };
    const group = data.groups.find(g => g.id === (c.group_id || ""));
    box.append(el("h4", "ct-cath", T("ct.perms.title")), permissionsEditor("contact", overrides, group ? group.permissions : {}));
    box.append(voicePanel(c));
    const parseList = (text, key) => text.split("\n").map(l => l.trim()).filter(Boolean).map(l => { const [v, label] = l.split("|").map(s => s.trim()); return { [key]: v, label: label || "" }; });
    const save = btn(T("ct.save"), "primary", () => act("api/contacts/items", {
      action: "update", id: c.id, name: name.value, organization: org.value, role: role.value, group_id: groupSel.value,
      phones: parseList(phones.value, "number"), emails: parseList(emails.value, "address"),
      sharing_notes: sharing.value, relationship_notes: relation.value, overrides }));
    const remove = btn(T("ct.remove"), "ghost", () => { if (confirm(T("ct.contact.remove.confirm", c.name))) { selectedContact = null; act("api/contacts/items", { action: "remove", id: c.id }); } });
    const acts = el("div", "ct-acts"); acts.append(save, remove);
    box.append(acts);
    return box;
  }

  // -1-: null = nothing picked yet, "" = the "no group" row, anything else = a real group's id.
  function memberRow(c) {
    const on = selectedContact === c.id;
    const b = btn(c.name, "ghost ct-item" + (on ? " ct-on" : ""), () => { selectedContact = c.id; draw(); });
    b.setAttribute("aria-pressed", String(on));
    return b;
  }

  function groupMembers(g, groupId) {
    const box = el("div", "ct-detail");
    if (g) box.append(groupDetail(g));
    else box.append(el("h3", "ct-h3", T("ct.ungrouped")));
    box.append(el("h4", "ct-cath", T("ct.members.title")));
    const members = data.contacts.filter(c => (c.group_id || "") === groupId);
    const list = el("div", "ct-list");
    members.forEach(c => list.append(memberRow(c)));
    if (!members.length) list.append(el("p", "ct-empty", T("ct.members.none")));
    box.append(list);
    const contactName = field("input", T("ct.contact.add.ph"));
    const addContactBtn = btn(T("ct.contact.add"), "primary", () => { if (contactName.value.trim()) act("api/contacts/items", { action: "add", name: contactName.value.trim(), group_id: groupId }).then(ok => { if (ok) { const last = data.contacts[data.contacts.length - 1]; selectedContact = last.id; draw(); } }); });
    const addRow = el("div", "ct-addrow");
    addRow.append(contactName, addContactBtn);
    box.append(addRow);
    return box;
  }

  function detailPane() {
    if (selectedContact) {
      const c = data.contacts.find(c => c.id === selectedContact);
      if (c) return contactDetail(c);
      selectedContact = null;
    }
    if (selectedGroup !== null) {
      const g = selectedGroup ? data.groups.find(g => g.id === selectedGroup) : null;
      if (!selectedGroup || g) return groupMembers(g, selectedGroup || "");
      selectedGroup = null;
    }
    return el("p", "ct-empty", T("ct.pick"));
  }

  function groupButton(label, id) {
    const on = selectedGroup === id;
    const b = btn(label, "ghost ct-item" + (on ? " ct-on" : ""), () => { selectedGroup = id; selectedContact = null; draw(); });
    b.setAttribute("aria-pressed", String(on));
    return b;
  }

  function contactsPanel() {
    const box = el("div", "ct-panel");
    box.append(el("h3", "ct-h3", T("ct.groups.title")));
    const cols = el("div", "ct-cols");
    const list = el("div", "ct-list");
    list.append(groupButton(T("ct.ungrouped"), ""));
    for (const g of data.groups) list.append(groupButton(g.name, g.id));
    const groupName = field("input", T("ct.group.add.ph"));
    const addGroupBtn = btn(T("ct.group.add"), "", () => { if (groupName.value.trim()) act("api/contacts/groups", { action: "add", name: groupName.value.trim() }); });
    const addRow = el("div", "ct-addrow");
    addRow.append(groupName, addGroupBtn);
    list.append(addRow);
    cols.append(list, detailPane());
    box.append(cols);
    return box;
  }

  // -- Panel 2: permission types (CRUD + reorder) -----------------------------------------------------

  function typesPanel() {
    const box = el("div", "ct-panel");
    box.append(el("h3", "ct-h3", T("ct.types.title")), el("p", "help", T("ct.types.help")));
    if (!data.permission_categories.length) { box.append(el("p", "ct-empty", T("ct.types.needcat"))); return box; }
    const ul = el("div", "ct-typelist");
    data.permission_types.forEach((t, i) => {
      const row = el("div", "ct-typerow");
      const label = field("input"); label.value = t.label;
      const catSel = document.createElement("select");
      for (const c of data.permission_categories) catSel.append(new Option(c.label, c.id));
      catSel.value = t.category;
      const save = btn(T("ct.save"), "", () => act("api/contacts/types", { action: "update", id: t.id, label: label.value, category_id: catSel.value }));
      const up = btn("↑", "", () => moveType(i, -1)); up.disabled = i === 0; up.setAttribute("aria-label", T("ct.move.up"));
      const down = btn("↓", "", () => moveType(i, 1)); down.disabled = i === data.permission_types.length - 1; down.setAttribute("aria-label", T("ct.move.down"));
      const remove = btn(T("ct.remove"), "ghost", () => { if (confirm(T("ct.type.remove.confirm", t.label))) act("api/contacts/types", { action: "remove", id: t.id }); });
      row.append(label, catSel, up, down, save, remove);
      ul.append(row);
    });
    box.append(ul);
    const newLabel = field("input", T("ct.type.add.ph"));
    const newCat = document.createElement("select");
    for (const c of data.permission_categories) newCat.append(new Option(c.label, c.id));
    const addRow = el("div", "ct-addrow");
    addRow.append(newCat, newLabel, btn(T("ct.type.add"), "primary", () => { if (newLabel.value.trim()) act("api/contacts/types", { action: "add", category_id: newCat.value, label: newLabel.value.trim() }); }));
    box.append(addRow);
    return box;
  }

  function moveType(i, dir) {
    const order = data.permission_types.map(t => t.id);
    const j = i + dir;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    act("api/contacts/types", { action: "reorder", order });
  }

  // -- Panel 3: categories -----------------------------------------------------------------------------

  function categoriesPanel() {
    const box = el("div", "ct-panel");
    box.append(el("h3", "ct-h3", T("ct.categories.title")));
    const ul = el("div", "ct-typelist");
    for (const c of data.permission_categories) {
      const row = el("div", "ct-typerow");
      const label = field("input"); label.value = c.label;
      row.append(label, btn(T("ct.save"), "", () => act("api/contacts/categories", { action: "rename", id: c.id, label: label.value })),
        btn(T("ct.remove"), "ghost", () => { if (confirm(T("ct.category.remove.confirm", c.label))) act("api/contacts/categories", { action: "remove", id: c.id }); }));
      ul.append(row);
    }
    box.append(ul);
    const newLabel = field("input", T("ct.category.add.ph"));
    const addRow = el("div", "ct-addrow");
    addRow.append(newLabel, btn(T("ct.category.add"), "primary", () => { if (newLabel.value.trim()) act("api/contacts/categories", { action: "add", label: newLabel.value.trim() }); }));
    box.append(addRow);
    return box;
  }

  function draw() {
    if (!data) { root.replaceChildren(el("p", "help", T("ct.loading"))); return; }
    const frag = document.createDocumentFragment();
    const st = statusLine();
    if (st) frag.append(st);
    frag.append(contactsPanel(), typesPanel(), categoriesPanel());
    root.replaceChildren(frag);
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "contacts") load(); });
  document.addEventListener("tadamoo:lang", () => { if (document.body.dataset.view === "contacts" && data) draw(); });
  if (document.body.dataset.view === "contacts") load();
})();
