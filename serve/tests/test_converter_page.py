"""Models > Model converter: the page is wired in, every text it shows is in English and Italian, and the charts compute what they draw."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve import modelbench

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "converter.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
EN, IT = I18N.index("  en: {"), I18N.index("  it: {")


class Wiring(unittest.TestCase):
    def test_the_sub_tab_the_panel_the_scripts_and_the_style_are_in_the_page(self):
        self.assertIn('id="sub-converter"', HTML)
        self.assertIn('id="ld-converter"', HTML)
        self.assertLess(HTML.index("web/charts.js"), HTML.index("web/converter.js"))
        self.assertIn("web/converter.css", HTML)
        providers = (WEB / "providers.js").read_text(encoding="utf-8")
        self.assertIn('"converter"', providers)
        self.assertIn("basalt:models-sub", providers)
        self.assertIn("basalt:models-sub", JS)

    def test_a_chart_tab_does_not_use_an_attribute_the_page_already_has(self):
        self.assertNotIn("data-view", JS)                      # the body carries data-view: closest("[data-view]") would find it from anywhere

    def test_every_text_is_in_both_languages(self):
        keys = set(re.findall(r'\bT\("(cv\.[A-Za-z0-9_.]+)"', JS)) | set(re.findall(r'\bT\("(pv\.sub\.converter)"', HTML + JS))
        keys |= {"pv.sub.converter"}
        keys |= {f"cv.step.{s}" for s in ("fetch", "check", "tuned", "profile", "convert")}
        keys |= {f"cv.s.{s}" for s in modelbench.STEPS} | {f"cv.s.{s}.h" for s in modelbench.STEPS}
        keys |= {f"cv.group.{g}" for g in ("all", "weight", "speed", "quality")}
        keys |= {f"cv.profile.{m}" for m in ("auto", "plain", "file", "model")} | {f"cv.mtp.{m}" for m in ("original", "tuned_pack", "tuned_file", "none")}
        keys |= {f"cv.vision.{m}" for m in ("auto", "none", "file")} | {f"cv.compat.{m}" for m in ("auto", "true", "false")}
        keys |= {f"cv.m.{m['id']}" for m in modelbench.METRICS} | {f"cv.ma.{m['id']}" for m in modelbench.METRICS}
        keys = {k for k in keys if not k.endswith(".")}
        self.assertGreater(len(keys), 120)
        for key in sorted(keys):
            self.assertIn(f'"{key}":', I18N[EN:IT], key)
            self.assertIn(f'"{key}":', I18N[IT:], key)

    def test_every_server_string_is_escaped_before_it_becomes_markup(self):
        field = re.compile(r"(name|path|quant|message|text|error|out_path|title|label)")
        interp = re.compile(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}")
        self.assertNotIn("eval(", JS)
        for name in ("converter.js", "charts.js"):
            source = (WEB / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                for expr in interp.findall(line):
                    if not field.search(expr) or re.search(r"(esc|opt)\(", expr):
                        continue
                    # a name or a label put in a tooltip text: that text is escaped where it is written into the mark
                    self.assertTrue(name == "charts.js" and "const tip" in line, f"{name}: {line.strip()[:120]}")
        charts = (WEB / "charts.js").read_text(encoding="utf-8")
        self.assertIn('data-tip="${esc(tip)}"', charts)                  # the one place every tooltip text enters a mark
        self.assertIn("const opt = (v, text, cur) =>", JS)
        self.assertIn("${esc(text)}</option>", JS)                       # opt() escapes both the value and the text


@unittest.skipUnless(NODE, "node is needed to run the charts")
class Charts(unittest.TestCase):
    def run_js(self, body: str):
        script = f"require({json.dumps(str(WEB / 'charts.js'))});const C=globalThis.BasaltCharts;" + body
        out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_scores_run_from_the_worst_to_the_best_with_the_better_direction_outward(self):
        got = self.run_js('console.log(JSON.stringify({hi: C.scores([100, 150, 200], "higher"), lo: C.scores([10, 20, 30], "lower"), same: C.scores([5, 5], "higher"), gap: C.scores([1, null, 3], "higher")}))')
        self.assertEqual([round(x, 6) for x in got["hi"]], [0.2, 0.6, 1.0])
        self.assertEqual([round(x, 6) for x in got["lo"]], [1.0, 0.6, 0.2])
        self.assertEqual(got["same"], [1, 1])
        self.assertEqual(got["gap"], [0.2, None, 1])

    def test_the_star_keeps_only_the_axes_every_model_has_and_needs_three(self):
        got = self.run_js('''const ms = [{id: "a", name: "A", color: "#111", figures: {decode: 100, ram_gib: 40, load_s: 60, gsm8k_pct: 90}},
          {id: "b", name: "B", color: "#222", figures: {decode: 150, ram_gib: 50, load_s: 40}}];
          const metric = (id, better) => ({id, better, unit: ""});
          const d = C.radarData(ms, [metric("decode", "higher"), metric("ram_gib", "lower"), metric("gsm8k_pct", "higher"), metric("load_s", "lower")]);
          const few = C.radar({models: ms, metrics: [metric("decode", "higher"), metric("gsm8k_pct", "higher")], label: m => m.id});
          const ok = C.radar({models: ms, metrics: [metric("decode", "higher"), metric("ram_gib", "lower"), metric("load_s", "lower")], label: m => m.id});
          console.log(JSON.stringify({axes: d.axes.map(a => a.id), skipped: d.skipped.map(a => a.id), scores: d.scores, few: few.enough, ok: ok.enough, polygons: (ok.svg.match(/<polygon[^>]*fill-opacity/g) || []).length, marks: (ok.svg.match(/class="cv-pt"/g) || []).length}))''')
        self.assertEqual((got["axes"], got["skipped"]), (["decode", "ram_gib", "load_s"], ["gsm8k_pct"]))
        self.assertEqual([[round(x, 6) for x in row] for row in got["scores"]], [[0.2, 1.0, 0.2], [1.0, 0.2, 1.0]])
        self.assertEqual((got["few"], got["ok"], got["polygons"], got["marks"]), (False, True, 2, 6))

    def test_bars_draw_a_value_and_a_text_twin_for_each_model_and_a_dash_when_there_is_none(self):
        got = self.run_js('''const ms = [{id: "a", name: "A", color: "#111", figures: {decode: 120}}, {id: "b", name: "B", color: "#222", figures: {}},
          {id: "c", name: "C", color: "#333", kind: "published", figures: {decode: 240}}];
          const svg = C.bars({models: ms, metric: {id: "decode", unit: "tok/s", better: "higher"}, label: m => "Decode"});
          console.log(JSON.stringify({tips: [...svg.matchAll(/data-tip="([^"]*)"/g)].map(m => m[1]), paths: (svg.match(/<path /g) || []).length, light: svg.includes('fill-opacity="0.55"')}))''')
        self.assertEqual(got["tips"], ["A\nDecode: 120 tok/s", "B\nDecode: —", "C\nDecode: 240 tok/s"])
        self.assertEqual((got["paths"], got["light"]), (2, True))

    def test_points_leave_out_a_model_without_both_measures_and_say_how_many(self):
        got = self.run_js('''const ms = [{id: "a", name: "A", color: "#111", figures: {decode: 100, gsm8k_pct: 90, ram_gib: 40}},
          {id: "b", name: "B", color: "#222", figures: {decode: 150, gsm8k_pct: 95, ram_gib: 55}}, {id: "c", name: "C", color: "#333", figures: {decode: 150}}];
          const x = {id: "decode", unit: "tok/s", better: "higher"}, y = {id: "gsm8k_pct", unit: "%", better: "higher"}, size = {id: "ram_gib", unit: "GiB", better: "lower"};
          const out = C.scatter({models: ms, x, y, size, label: m => m.id});
          const none = C.scatter({models: [ms[2]], x, y, size: null, label: m => m.id});
          console.log(JSON.stringify({missing: out.missing, marks: (out.svg.match(/class="cv-pt"/g) || []).length, none: none.svg, tipHasSize: out.svg.includes("ram_gib: 55 GiB")}))''')
        self.assertEqual((got["missing"], got["marks"], got["none"], got["tipHasSize"]), (1, 2, "", True))

    def test_nice_ticks_and_the_figures_format(self):
        got = self.run_js('console.log(JSON.stringify({t: C.niceTicks(83, 101), f: [C.format(1234.5, ""), C.format(169.3, "tok/s"), C.format(4.5, ""), C.format(0, ""), C.format(null, "x")]}))')
        self.assertEqual(got["t"][0] <= 83 and got["t"][-1] >= 101, True)
        self.assertEqual(got["f"], ["1,235", "169 tok/s", "4.5", "0", "—"])

    def test_a_hostile_model_name_is_text_in_the_marks(self):
        got = self.run_js('''const ms = [{id: "a", name: '<img src=x onerror=alert(1)>"', color: "#111", figures: {decode: 5}}];
          const svg = C.bars({models: ms, metric: {id: "decode", unit: "", better: "higher"}, label: m => "D"});
          console.log(JSON.stringify({raw: svg.includes("<img"), esc: svg.includes("&lt;img")}))''')
        self.assertEqual((got["raw"], got["esc"]), (False, True))


if __name__ == "__main__":
    unittest.main()
