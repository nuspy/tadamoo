"""Avvia-Basalt.ps1 and Ferma-Basalt.ps1: the unified options (-gui, -browser, -server, one or two dashes, any case)
checked with -prova, so nothing starts."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell scripts")
class Options(unittest.TestCase):
    def run_script(self, name, *args):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "BASALT_DATA": d, "BASALT_CACHE": d}
            p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / name),
                                *args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                               timeout=120)
        return p.returncode, p.stdout + p.stderr

    def test_the_app_is_the_default(self):
        code, out = self.run_script("Avvia-Basalt.ps1", "-prova", "--port", "7285", "--vram-libera", "1.5")
        if not (ROOT / "tools/windows/desktop/publish/BasaltPannello.exe").is_file():
            self.assertIn("dotnet publish", out)
            return
        self.assertEqual(code, 0, out)
        self.assertIn("BasaltPannello.exe --port 7285 --vram-libera 1.5", out)
        self.assertIn("http://127.0.0.1:", out)
        self.assertIn("il browser non si apre", out)

    def test_both_dash_forms_and_any_case(self):
        for args in (("-GUI", "-Prova"), ("--Gui", "--PROVA", "--config=Valori sicuri")):
            code, out = self.run_script("Avvia-Basalt.ps1", *args)
            self.assertIn("PROVA", out, args)

    def test_refusals(self):
        for args, text in ((("-gui", "-browser"), "un solo modo"), (("--server", "--browser"), "un solo modo"),
                           (("--vram-libera", "x"), "--vram-libera vuole"), (("--nope",), "Argomento sconosciuto"),
                           (("--vram-libera", "1", "--vram-reserve-mib", "100"), "usane uno solo")):
            code, out = self.run_script("Avvia-Basalt.ps1", *args)
            self.assertEqual(code, 1, (args, out))
            self.assertIn(text, out, args)

    def test_old_names_still_work(self):
        code, out = self.run_script("Avvia-Basalt.ps1", "-SoloServer", "-prova", "--file", "Z:/no/such/config.json")
        self.assertIn("-SoloServer è un nome vecchio", out)
        self.assertIn("Manca il file di configurazione", out)

    def test_stop_script_options(self):
        for args in (("--port", "7289"), ("-port=7289",), ("--PORT", "7289")):
            code, out = self.run_script("Ferma-Basalt.ps1", *args)
            self.assertEqual(code, 0, out)
            self.assertIn("7289", out)
        self.assertEqual(self.run_script("Ferma-Basalt.ps1", "--nope")[0], 1)


if __name__ == "__main__":
    unittest.main()
