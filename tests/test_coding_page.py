"""The Agent tab's role selector and project-checks card: the script is run in node against a small stand-in for the page, and the markup,
styles and strings are checked as text. No browser, no server, no model."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.roles import CODING_ROLE, ROLE_IDS

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "serve" / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "coding.js").read_text(encoding="utf-8")
CSS = (WEB / "coding.css").read_text(encoding="utf-8")

STUB = r"""
const fs = require("fs");
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.className = ""; this.hidden = false; this.value = ""; this.checked = false; this.disabled = false; this.title = ""; this._text = ""; this.id = ""; }
  get nodeType() { return 1; }
  append(...k) { k.forEach(x => this.children.push(x)); }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  setAttribute(k, v) { this.attrs[k] = v; }
  get textContent() { return this._text + this.children.map(c => (c.textContent !== undefined ? c.textContent : "")).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  all(out = []) { this.children.forEach(c => { if (c instanceof El) { out.push(c); c.all(out); } }); return out; }
  querySelectorAll(sel) { const m = /^input\[data-command\]$/.exec(sel); return this.all().filter(e => m && e.tag === "input" && e.dataset.command !== undefined); }
  querySelector(sel) { const id = sel.replace(/^#/, ""); return this.all().find(e => e.id === id) || null; }
}
class Text { constructor(t) { this.textContent = t; this.nodeType = 3; } }
const byId = {};
for (const id of ["coderole", "coderole-label", "coding-card", "coding-sum", "coding-toggle", "coding-body", "coding-go", "coding-bug", "coding-next"]) { byId[id] = new El(id === "coderole" ? "select" : "div"); byId[id].id = id; }
const handlers = {};
global.window = global;
global.document = { body: { dataset: { view: "agent" } }, getElementById: id => byId[id] || null, createElement: t => new El(t), createTextNode: t => new Text(t),
  addEventListener: (n, f) => { (handlers[n] = handlers[n] || []).push(f); } };
global.Node = El;
const store = {};
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
global.T = (k, ...a) => k + (a.length ? "(" + a.join("|") + ")" : "");
global.authHeaders = () => ({});
global.applyT = () => {};
const rec = { id: "s1", workspace: "C:/proj", role: null };
let sent = [];
global.agentChat = { active: () => true, record: () => rec, folder: () => "C:/proj", current: () => "s1", busy: () => false, refresh: () => {}, send: t => sent.push(t) };
const calls = [];
const view = { folder: "C:/proj", empty: false, kinds: ["test"], read_only: ["git status", "ls"], own: ["ruff check ."], limits: { own: 24, command_chars: 500 }, verify_gate: "off",
  checks: [{ kind: "test", command: "npm test", source: "package.json", runs: "jest", hint: "", digest: "d", state: "none", gate: true },
           { kind: "lint", command: "npm run lint", source: "package.json", runs: "eslint .", hint: "", digest: "e", state: "changed", gate: true },
           { kind: "build", command: "", source: "CMakeLists.txt", runs: "", hint: "cmake", digest: "f", state: "none", gate: true }] };
global.fetch = async (url, opts) => {
  calls.push({ url, method: opts.method, body: opts.body ? JSON.parse(opts.body) : null });
  const ok = data => ({ ok: true, status: 200, json: async () => data });
  if (url === "api/coding" && opts.method !== "POST") return ok({ roles: JSON.parse(process.env.ROLES), coding_role: process.env.CODING });
  if (url.startsWith("api/coding?")) return ok(view);
  if (url === "api/coding") return ok({ ...view, told: 1 });
  if (url.endsWith("/role")) { rec.role = JSON.parse(opts.body).role; return ok({ session: rec }); }
  return { ok: false, status: 404, json: async () => ({}) };
};
const wait = () => new Promise(r => setTimeout(r, 20));
(async () => {
  vm = require("vm");
  vm.runInThisContext(fs.readFileSync(process.env.SCRIPT, "utf8"));
  await wait();
  const out = {};
  const sel = byId["coderole"];
  out.options = sel.children.map(o => o.value);
  out.labelHidden = byId["coderole-label"].hidden;
  out.cardHidden = byId["coding-card"].hidden;
  out.selected = sel.value;
  out.sum = byId["coding-sum"].textContent;
  out.bodyHidden = byId["coding-body"].hidden;
  byId["coding-toggle"].onclick();
  await wait();
  out.bodyOpen = !byId["coding-body"].hidden;
  const body = byId["coding-body"];
  out.rows = body.all().filter(e => e.className === "cc-row").length;
  out.stateTexts = body.all().filter(e => e.className === "cc-state").map(e => e.textContent);
  out.cmake = body.textContent.includes("cd.cmake");
  const boxes = body.querySelectorAll("input[data-command]");
  out.commands = boxes.map(b => b.dataset.command);
  out.gateDisabledAtStart = body.querySelector("#cc-gate-0").disabled;
  boxes[0].checked = true; boxes[0].onchange();
  out.gateEnabledAfterTick = !body.querySelector("#cc-gate-0").disabled;
  const before = body.querySelectorAll("input[data-command]")[0];
  const optionsBefore = sel.children[0];
  global.codingUi.refresh(); global.codingUi.refresh();
  out.keptAcrossRefresh = body.querySelectorAll("input[data-command]")[0] === before && before.checked === true && sel.children[0] === optionsBefore;
  const save = body.all().find(e => e.className === "btn primary");
  await save.onclick();
  await wait();
  const post = calls.find(c => c.method === "POST" && c.url === "api/coding");
  out.post = post && post.body;
  sel.value = "plan"; sel.onchange();
  await wait();
  out.rolePost = calls.find(c => c.url.endsWith("/role")).body;
  out.goVisibleInPlan = !byId["coding-go"].hidden;
  sent = [];
  await byId["coding-go"].onclick();
  await wait();
  out.afterGo = { role: rec.role, sent };
  (handlers["basalt:sent"] || []).forEach(f => f({ detail: { text: "the build fails with an error" } }));
  out.bugVisible = !byId["coding-bug"].hidden;
  out.roleStillBuild = rec.role;
  global.agentChat.active = () => false;
  byId["coding-toggle"].onclick; (handlers["basalt:view"] || []).forEach(f => f({ detail: "chat" }));
  out.hiddenOutsideAgent = byId["coderole-label"].hidden && byId["coding-card"].hidden;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not NODE, reason="node is needed to run the script")
def test_the_script_fills_the_selector_shows_the_card_and_saves_what_is_ticked(tmp_path):
    stub = tmp_path / "stub.js"
    stub.write_text(STUB, encoding="utf-8")
    env = {**os.environ, "SCRIPT": str(WEB / "coding.js"), "ROLES": json.dumps(list(ROLE_IDS)), "CODING": CODING_ROLE}
    done = subprocess.run([NODE, str(stub)], capture_output=True, text=True, timeout=30, env=env)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["options"] == list(ROLE_IDS) and out["selected"] == CODING_ROLE and out["labelHidden"] is False and out["cardHidden"] is False
    assert out["sum"] == "cd.card.sum(0|2)" and out["bodyHidden"] is True and out["bodyOpen"] is True
    assert out["rows"] == 3 and out["stateTexts"] == ["cd.state.none", "cd.state.changed"] and out["cmake"] is True
    assert out["commands"] == ["npm test", "npm run lint"] and out["gateDisabledAtStart"] is True and out["gateEnabledAfterTick"] is True
    assert out["keptAcrossRefresh"] is True                                    # a poll does not redraw a list the person is working in
    assert out["post"] == {"workspace": "C:/proj", "accepted": [{"command": "npm test", "gate": True}], "own": "ruff check ."}
    assert out["rolePost"] == {"role": "plan"} and out["goVisibleInPlan"] is True
    assert out["afterGo"] == {"role": CODING_ROLE, "sent": ["cd.go.text"]}
    assert out["bugVisible"] is True                                           # a message about a fault, sent in Build, only offers Debug: the role does not change by itself
    assert out["roleStillBuild"] == CODING_ROLE and out["hiddenOutsideAgent"] is True


@pytest.mark.skipif(not NODE, reason="node is needed to check the script")
def test_the_script_parses():
    done = subprocess.run([NODE, "--check", str(WEB / "coding.js")], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_the_page_wires_the_script_the_styles_the_selector_and_the_card():
    assert 'href="web/coding.css"' in HTML and 'src="web/coding.js"' in HTML
    assert HTML.index("web/agentchat.js") < HTML.index("web/coding.js")           # it uses window.agentChat
    for ident in ("coderole", "coderole-label", "coding-card", "coding-sum", "coding-toggle", "coding-body", "coding-go", "coding-bug", "coding-next"):
        assert f'id="{ident}"' in HTML, ident
    assert re.search(r'<label class="effort coderole" for="coderole"[^>]*hidden', HTML)                  # only the Agent tab shows it
    assert 'aria-controls="coding-body"' in HTML and 'role="status"' in HTML


def test_what_the_server_and_the_project_say_enters_the_page_as_text():
    assert "innerHTML" not in JS and "insertAdjacentHTML" not in JS and "outerHTML" not in JS and "document.write" not in JS
    chat = (WEB / "agentchat.js").read_text(encoding="utf-8")
    assert 'case "role"' in chat and 'case "checks"' in chat and "window.codingUi.role()" in chat and "basalt:sent" in chat


def test_every_string_exists_in_both_languages_with_the_same_placeholders():
    out = subprocess.run([NODE, "-e", "const fs=require('fs');global.document={documentElement:{dataset:{},lang:''},querySelector:()=>null};global.location={search:''};"
                          "global.localStorage={getItem:()=>null,setItem(){}};const src=fs.readFileSync(process.argv[1],'utf8')+';process.stdout.write(JSON.stringify(STRINGS))';"
                          "eval(src)", str(WEB / "i18n.js")], capture_output=True, text=True, timeout=30) if NODE else None
    if out is None:
        pytest.skip("node is needed to read the translations")
    assert out.returncode == 0, out.stderr
    strings = json.loads(out.stdout)
    used = set(re.findall(r'\bT\(\s*"(cd\.[\w.]+)"', JS)) - {k for k in re.findall(r'\bT\(\s*"(cd\.[\w.]+)"', JS) if k.endswith(".")}
    used |= {"cd.role." + r for r in ROLE_IDS} | {"cd.role." + r + ".d" for r in ROLE_IDS} | {"cd.kind." + k for k in ("test", "lint", "typecheck", "build")}
    used |= {"cd.gate.mode.always", "cd.gate.mode.unattended"} | set(re.findall(r'data-t(?:-title)?="(cd\.[\w.]+)"', HTML))
    used |= set(re.findall(r'T\("(cd\.[\w.]+)"', (WEB / "agentchat.js").read_text(encoding="utf-8"))) | {"sl.r.role", "sl.r.tools.out"}      # also /status and /tools
    used = {k for k in used if not k.endswith(".")}                                           # "cd.role." + id is read as one key per role, above
    assert len(used) > 30
    for lang in ("en", "it"):
        assert sorted(k for k in used if k not in strings[lang]) == [], lang
    for key in used:
        assert sorted(re.findall(r"\{\d+\}", strings["en"][key])) == sorted(re.findall(r"\{\d+\}", strings["it"][key])), key
    for key in ("cd.role.build", "cd.card.help", "cd.card.title", "cd.go", "cd.bug", "cd.empty", "cd.role.tip"):
        assert strings["en"][key] != strings["it"][key], key                                     # translated, not copied


def test_the_buttons_are_30_px_the_text_wraps_and_the_card_never_scrolls_sideways():
    assert "var(--btn-h)" in CSS and "overflow-wrap: anywhere" in CSS and "flex-wrap: wrap" in CSS
    assert "overflow-x" not in CSS and "white-space: nowrap" not in CSS and "min-width: 0" in CSS
    app = (WEB / "app.css").read_text(encoding="utf-8")
    assert "--btn-h: 30px" in app and re.search(r"\.btn \{[^}]*min-height: var\(--btn-h\)", app)         # every .btn, so the card's buttons too
    assert "max-height: calc(60vh" in CSS and "overflow: auto" in CSS                                    # a long list scrolls inside the card
