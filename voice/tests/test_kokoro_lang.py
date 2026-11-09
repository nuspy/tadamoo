"""The language code Kokoro takes: one mapping for every Kokoro entry point (the engine in the process, the GPU helper), from the ISO codes of the
language settings. The helper runs as a real subprocess over a stand-in `kokoro_onnx` that records what it is asked."""
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from callbridge.engines import kokoro_lang as kl
from callbridge.engines import kokoro_tts
from callbridge.language_names import CODES
from callbridge.voice_commands import LANG_TABLE

ENGINES = Path(kl.__file__).parent


@pytest.mark.parametrize("given,wanted", [("en", "en-us"), ("EN", "en-us"), ("en_US", "en-us"), ("en-gb", "en-gb"), ("pt", "pt-br"), ("pt-PT", "pt-br"),
                                          ("zh", "cmn"), ("zh-CN", "cmn"), ("cmn", "cmn"), ("fr", "fr-fr"), ("it", "it"), ("es", "es"), ("ja", "ja"),
                                          ("hi", "hi"), ("", "it"), (None, "it"), ("xx", "xx"), ("hu", "hu")])
def test_the_iso_codes_map_to_kokoros(given, wanted):
    assert kl.kokoro_lang(given) == wanted


def test_every_language_kokoro_speaks_maps_to_a_code_it_accepts_and_its_own_codes_are_unchanged():
    from callbridge.engines import catalog
    accepted = {"it", "en-us", "en-gb", "fr-fr", "es", "pt-br", "ja", "cmn", "hi"}
    for code in catalog.spec("kokoro").langs:
        assert code in CODES and kl.kokoro_lang(code) in accepted, code
    for info in LANG_TABLE.values():
        if "f" in info:                                                  # a Kokoro language of the old table
            assert kl.kokoro_lang(info["lang"]) == info["lang"]


class FakeKokoro:
    def __init__(self):
        self.asked = []

    def create(self, text, voice="", speed=1.0, lang=""):
        self.asked.append(lang)
        return np.zeros(240, dtype=np.float32), 24000


def test_the_engine_maps_the_code_before_the_in_process_synthesis_and_before_the_gpu_helper(monkeypatch):
    engine = kokoro_tts.KokoroTtsEngine(model_dir="unused", device="cpu")
    engine._k = FakeKokoro()
    engine.synth("hello", "af_heart", "en", 1.0)
    engine.synth("ola", "pf_dora", "pt", 1.0)
    engine.synth("ni hao", "zf_xiaoxiao", "zh", 1.0)
    assert engine._k.asked == ["en-us", "pt-br", "cmn"]
    from callbridge.engines import device
    monkeypatch.setattr(device, "resolve_device", lambda d, m: "cuda")
    sent = []
    monkeypatch.setattr(engine, "_synth_helper", lambda text, voice, lang, speed: sent.append(lang) or (np.zeros(1, dtype=np.float32), 24000))
    engine.synth("hello", "af_heart", "en", 1.0)
    assert sent == ["en-us"]


def test_the_helper_process_maps_too(tmp_path):
    (tmp_path / "kokoro_onnx.py").write_text(
        "import sys\nimport numpy as np\n\nclass Kokoro:\n    def __init__(self, onnx, voices):\n        pass\n\n"
        "    def create(self, text, voice='', speed=1.0, lang=''):\n        print('LANG=' + lang, file=sys.stderr, flush=True)\n"
        "        return np.zeros(8, dtype=np.float32), 24000\n", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    (model / "kokoro-v1.0.onnx").write_bytes(b"x")
    (model / "voices-v1.0.bin").write_bytes(b"x")
    env = dict(os.environ, KOKORO_MODEL_DIR=str(model), KOKORO_DEVICE="cpu", PYTHONPATH=str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, "-u", str(ENGINES / "kokoro_tts_helper.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    lines = "".join(json.dumps({"text": "hello", "voice": "af_heart", "lang": lang}) + "\n" for lang in ("en", "pt", "zh", "fr-fr")) + json.dumps({"cmd": "quit"}) + "\n"
    out, err = proc.communicate(lines.encode("utf-8"), timeout=60)
    assert proc.returncode == 0, err.decode("utf-8", "replace")[-500:]
    assert [ln.split("LANG=")[1] for ln in err.decode("utf-8", "replace").splitlines() if "LANG=" in ln] == ["en-us", "pt-br", "cmn", "fr-fr"]
    counts = []
    view = memoryview(out)
    while len(view) >= 8:
        n, _ = struct.unpack("<II", view[:8])
        counts.append(n)
        view = view[8 + 4 * n:]
    assert counts == [8, 8, 8, 8]                                              # four syntheses, none an error (count 0)
