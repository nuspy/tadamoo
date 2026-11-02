"""The Chat continues a reply the output limit cut by itself, up to the setting "Automatic continuations", and joins the pieces cleanly.
node runs the page's own chat.js against stubs: a scripted server answers /v1/chat/completions with the replies of each test."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")

DRIVER = r"""
const vm = require('vm'), fs = require('fs');
const web = process.argv[1], script = JSON.parse(process.argv[2]), limit = Number(process.argv[3]);
const sent = [], store = {};
function stub() {
  const own = {}, fn = function () {};
  return new Proxy(fn, {
    get(_, p) {
      if (p in own) return own[p];
      if (p === 'firstChild' || p === 'nextSibling') return null;
      if (p === 'dataset' || p === 'style') return (own[p] = {});
      if (p === 'classList') return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (p === 'querySelectorAll') return () => [];
      if (p === Symbol.toPrimitive) return () => 0;
      if (p === 'lastElementChild') return stub();
      return (...a) => stub();
    },
    set(_, p, v) { own[p] = v; return true; },
    apply() { return stub(); },
  });
}
const els = {}, dollar = s => (els[s] = els[s] || stub());
function sse(chunks) {
  const enc = new TextEncoder(), lines = chunks.map(c => 'data: ' + JSON.stringify(c) + '\n').concat(['data: [DONE]\n']);
  let i = 0;
  return { read: async () => (i < lines.length ? { value: enc.encode(lines[i++]), done: false } : { value: undefined, done: true }) };
}
const ctx = {
  console, setTimeout, clearTimeout, TextDecoder, TextEncoder, AbortController, performance,
  document: { createElement: () => stub(), addEventListener() {}, scrollingElement: stub(), body: stub() },
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } },
  navigator: {}, innerHeight: 900, getComputedStyle: () => ({ overflowY: 'visible' }),
  requestAnimationFrame: f => setTimeout(f, 0), cancelAnimationFrame: clearTimeout,
  $: dollar, T: (k, ...a) => k + (a.length ? '[' + a.join(',') + ']' : ''), LANG: 'en', authHeaders: () => ({}), nfmt: n => String(n), askKey() {},
  healthReady: Promise.resolve(null), URL, Blob,
  chatSettings: { policy: () => 'stop', system: () => '', body: () => ({}), threshold: () => 0.8, showReasoning: () => true, autoContinue: () => limit },
  fetch: async (url, opt) => {
    if (String(url) !== 'v1/chat/completions') return { ok: false, status: 404, json: async () => ({}) };
    const body = JSON.parse(opt.body);
    sent.push(body.messages);
    const step = script[sent.length - 1];
    const chunks = [];
    for (const piece of step.pieces) chunks.push({ choices: [{ delta: { content: piece }, finish_reason: null }] });
    chunks.push({ choices: [{ delta: {}, finish_reason: step.finish }], usage: { completion_tokens: 7 }, ...(step.extra || {}) });
    return { ok: true, status: 200, body: { getReader: () => sse(chunks) }, json: async () => ({}) };
  },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(web + '/cuttext.js', 'utf8') + '\n;this.cutText = cutText;', ctx);
vm.runInContext(fs.readFileSync(web + '/chat.js', 'utf8'), ctx);
(async () => {
  dollar('#prompt').value = 'write';
  dollar('#composer').onsubmit({ preventDefault() {} });
  const settled = () => new Promise(r => setTimeout(r, 20));
  let still = 0, last = -1;
  for (let i = 0; i < 400 && still < 4; i++) {            // done when the stop button has been hidden and no request left for a while
    await settled();
    still = dollar('#stop').hidden === true && sent.length === last ? still + 1 : 0;
    last = sent.length;
  }
  console.log(JSON.stringify({ sent, messages: JSON.parse(store['basalt.chat'] || '[]') }));
  process.exit(0);
})();
"""


def run(script, limit):
    out = subprocess.run([NODE, "-e", DRIVER, str(WEB), json.dumps(script), str(limit)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def step(*pieces, finish="length", **extra):
    return {"pieces": list(pieces), "finish": finish, "extra": extra}


@unittest.skipUnless(NODE, "node is needed to run the page's script")
class ChatContinuesByItself(unittest.TestCase):
    def test_a_cut_reply_goes_on_by_itself_and_the_pieces_join_in_one_message(self):
        got = run([step("The international rel"), step("relations of states ", "are old, and "), step("so are the rules.", finish="stop")], 4)
        assert len(got["sent"]) == 3
        assert got["sent"][1][-2] == {"role": "assistant", "content": "The international "}                  # the unfinished word is written again
        assert got["sent"][1][-1]["role"] == "user" and "Continue exactly where you stopped" in got["sent"][1][-1]["content"]
        assert got["sent"][2][-2]["content"] == "The international relations of states are old, and "
        reply = got["messages"][-1]
        assert reply["text"] == "The international relations of states are old, and so are the rules."
        assert reply["finish"] == "stop" and reply["chain"] == 2 and len(got["messages"]) == 2              # one message, two automatic continuations

    def test_the_setting_is_the_most_continuations_and_the_button_stays_at_the_limit(self):
        script = [step("one "), step("two "), step("three "), step("four ")]
        got = run(script, 2)
        assert len(got["sent"]) == 3 and got["messages"][-1]["text"] == "one two three " and got["messages"][-1]["finish"] == "length"
        assert got["messages"][-1]["chain"] == 2
        assert len(run(script[:1], 0)["sent"]) == 1                                                          # 0: off

    def test_a_loop_or_the_answer_cap_is_never_continued(self):
        for extra in ({"repeated": True}, {"answer_capped": True}):
            got = run([step("some text ", **extra), step("never")], 4)
            assert len(got["sent"]) == 1, extra

    def test_a_continuation_that_brings_nothing_ends_the_chain(self):
        got = run([step("some text "), step(""), step("never")], 4)
        assert len(got["sent"]) == 2

    def test_the_words_the_model_wrote_again_and_a_doubled_space_do_not_show(self):
        got = run([step("Rome was not built in a day, and the people knew that walls need patience, so they began with the foundations and went up"),
                   step("went up one course ", "at a time.", finish="stop")], 4)
        assert got["messages"][-1]["text"].endswith("foundations and went up one course at a time.")
        assert "went up went up" not in got["messages"][-1]["text"]


if __name__ == "__main__":
    unittest.main()
