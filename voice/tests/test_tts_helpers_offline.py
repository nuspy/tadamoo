"""The TTS helper scripts read their weights only from the models folder and never fetch.

The helpers run in their own Python environments, so the heavy libraries (torch, transformers, TTS, snac, ...) are replaced by fakes in
`sys.modules`. Two things are checked for every helper: a model that is missing raises `ModelNotInstalled` before any heavy library is
imported, and an installed model is loaded from its local folder (`local_files_only=True` where the loader takes it)."""
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

from callbridge import models_store as ms

ENGINES = Path(__file__).resolve().parents[1] / "callbridge" / "engines"
HEAVY = ("torch", "torchaudio", "soundfile", "transformers", "TTS", "snac", "faster_qwen3_tts", "whisper", "chatterbox", "cosyvoice",
         "huggingface_hub")


@pytest.fixture
def models(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setenv(ms.ROOT_ENV, str(root))
    monkeypatch.setenv("TTS_VENVS_DIR", str(tmp_path / "venvs"))
    monkeypatch.setattr(ms, "_service_env", {})
    return root


def install(root, service, model_id, files):
    """Put a model in its folder the way a finished download does: the files and the marker that lists them."""
    folder = root / service / model_id
    for name, size in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x" * size)
    (folder / ms.MARKER).write_text(json.dumps({"files": [{"name": n, "size": s} for n, s in files.items()]}), encoding="utf-8")
    return folder


def helper(monkeypatch, name):
    """Import a helper script as a module; the lines it runs at import (stdout, path, environment) are undone at the end of the test."""
    monkeypatch.syspath_prepend(str(ENGINES))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setenv("MIMALLOC_PURGE_DELAY", "0")
    spec = importlib.util.spec_from_file_location("test_h_" + name, ENGINES / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def block_heavy(monkeypatch):
    """Importing any heavy library now fails: a loader that gets past its model check would trip on this."""
    for name in HEAVY:
        monkeypatch.setitem(sys.modules, name, None)


def fake(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def fake_torch(monkeypatch):
    cuda = types.SimpleNamespace(is_available=lambda: False, is_initialized=lambda: False, empty_cache=lambda: None)
    return fake(monkeypatch, "torch", cuda=cuda, bfloat16="bf16", float16="f16", float32="f32")


class Recorder:
    """A stand-in for a library class: records what its loader is called with."""
    calls: list

    @classmethod
    def build(cls, name):
        return type(name, (cls,), {"calls": []})

    @classmethod
    def from_pretrained(cls, *a, **k):
        cls.calls.append((a, k))
        return cls()

    def eval(self):
        return self

    def to(self, *a, **k):
        return self


# --- a missing model stops every helper before a heavy import -------------------------------------

LOADERS = {
    "xtts_tts_helper": lambda m: m._carica(),
    "cosyvoice_tts_helper": lambda m: m._carica(),
    "dia_tts_helper": lambda m: m._carica(),
    "orpheus_tts_helper": lambda m: m._carica(),
    "qwen3tts_tts_helper": lambda m: m._carica("clone"),
}
NAMES = {"xtts_tts_helper": "XTTS v2", "cosyvoice_tts_helper": "CosyVoice 3 (0.5B)", "dia_tts_helper": "Dia 1.6B",
         "orpheus_tts_helper": "Orpheus 3B", "qwen3tts_tts_helper": "Qwen3-TTS 0.6B"}


@pytest.mark.parametrize("name", sorted(LOADERS))
def test_a_missing_model_raises_before_any_heavy_import(name, models, monkeypatch):
    block_heavy(monkeypatch)
    mod = helper(monkeypatch, name)
    with pytest.raises(ms.ModelNotInstalled) as e:
        LOADERS[name](mod)
    assert str(e.value) == f"{NAMES[name]} is not installed: install it from Settings > Voice."


def test_chatterbox_stops_at_the_model_check(models, monkeypatch, capsys):
    block_heavy(monkeypatch)
    mod = helper(monkeypatch, "chatterbox_tts_helper")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert mod.main() == 1
    assert "Chatterbox Multilingual is not installed" in capsys.readouterr().err


def test_without_a_models_folder_nothing_loads(monkeypatch):
    monkeypatch.delenv(ms.ROOT_ENV, raising=False)
    block_heavy(monkeypatch)
    mod = helper(monkeypatch, "dia_tts_helper")
    with pytest.raises(ms.ModelNotInstalled):
        mod._carica()


# --- an installed model is loaded from its local folder --------------------------------------------

def test_dia_loads_from_the_local_folder(models, monkeypatch):
    folder = install(models, "tts", "dia-1.6b", {"config.json": 2, "model.safetensors": 3})
    fake_torch(monkeypatch)
    proc, model = Recorder.build("Proc"), Recorder.build("Model")
    fake(monkeypatch, "transformers", AutoProcessor=proc, DiaForConditionalGeneration=model)
    mod = helper(monkeypatch, "dia_tts_helper")
    mod._carica()
    (pa, pk), = proc.calls
    assert pa == (str(folder),) and pk["local_files_only"] is True
    (ma, mk), = model.calls
    assert ma == (str(folder),) and mk["local_files_only"] is True


def test_orpheus_loads_the_model_and_the_codec_from_the_local_folder(models, monkeypatch):
    folder = install(models, "tts", "orpheus-3b", {"config.json": 2, "snac_24khz/config.json": 2, "snac_24khz/pytorch_model.bin": 3})
    fake_torch(monkeypatch)
    tok, lm, snac = Recorder.build("Tok"), Recorder.build("LM"), Recorder.build("Snac")
    fake(monkeypatch, "transformers", AutoTokenizer=tok, AutoModelForCausalLM=lm)
    fake(monkeypatch, "snac", SNAC=snac)
    mod = helper(monkeypatch, "orpheus_tts_helper")
    mod._carica()
    assert tok.calls[0][0] == (str(folder),) and tok.calls[0][1]["local_files_only"] is True
    assert lm.calls[0][0] == (str(folder),) and lm.calls[0][1]["local_files_only"] is True
    assert snac.calls[0][0] == (str(folder / "snac_24khz"),)


def test_xtts_loads_from_the_local_folder(models, monkeypatch):
    folder = install(models, "tts", "xtts-v2", {"config.json": 2, "model.pth": 3})
    seen = {}

    class Config:
        def load_json(self, path):
            seen["config"] = path

    class Xtts:
        @classmethod
        def init_from_config(cls, cfg):
            return cls()

        def load_checkpoint(self, cfg, checkpoint_dir=None, eval=True):
            seen["checkpoint_dir"] = checkpoint_dir

        def to(self, device):
            return self

    fake_torch(monkeypatch)
    fake(monkeypatch, "soundfile")
    fake(monkeypatch, "torchaudio")
    for pkg in ("TTS", "TTS.tts", "TTS.tts.configs", "TTS.tts.models"):
        fake(monkeypatch, pkg)
    fake(monkeypatch, "TTS.tts.configs.xtts_config", XttsConfig=Config)
    fake(monkeypatch, "TTS.tts.models.xtts", Xtts=Xtts)
    monkeypatch.setenv("TTS_DEVICE", "cpu")
    mod = helper(monkeypatch, "xtts_tts_helper")
    mod._carica()
    assert seen == {"config": str(folder / "config.json"), "checkpoint_dir": str(folder)}


def test_cosyvoice_loads_from_the_local_folder(models, monkeypatch):
    folder = install(models, "tts", "cosyvoice-3", {"cosyvoice3.yaml": 2})
    seen = {}

    class AutoModel:
        sample_rate = 24000

        def __init__(self, model_dir, **k):
            seen["model_dir"] = model_dir

    fake_torch(monkeypatch)
    fake(monkeypatch, "soundfile")
    fake(monkeypatch, "torchaudio")
    for pkg in ("cosyvoice", "cosyvoice.cli"):
        fake(monkeypatch, pkg)
    fake(monkeypatch, "cosyvoice.cli.cosyvoice", AutoModel=AutoModel)
    mod = helper(monkeypatch, "cosyvoice_tts_helper")
    mod._carica()
    assert seen["model_dir"] == str(folder)


@pytest.mark.parametrize("size,family,sub", [("0.6B", "clone", "base"), ("0.6B", "preset", "custom-voice"),
                                              ("1.7B", "clone", "base"), ("1.7B", "preset", "custom-voice")])
def test_qwen3tts_loads_the_folder_of_its_size_and_family(size, family, sub, models, monkeypatch):
    folder = install(models, "tts", "qwen3tts-" + size.lower(), {"base/config.json": 2, "custom-voice/config.json": 2})
    model = Recorder.build("Qwen")
    fake_torch(monkeypatch)
    fake(monkeypatch, "faster_qwen3_tts", FasterQwen3TTS=model)
    monkeypatch.setenv("QWEN3TTS_SIZE", size)
    monkeypatch.setenv("TTS_DEVICE", "cpu")
    mod = helper(monkeypatch, "qwen3tts_tts_helper")
    mod._carica(family)
    assert model.calls[0][0] == (str(folder / sub),)


def test_qwen3tts_has_no_download_entry_point(monkeypatch):
    mod = helper(monkeypatch, "qwen3tts_tts_helper")
    assert not hasattr(mod, "prefetch") and not hasattr(mod, "_backend")


def test_chatterbox_loads_from_the_local_folder(models, monkeypatch):
    folder = install(models, "tts", "chatterbox", {"ve.pt": 2})
    seen = {}

    class Multilingual:
        sr = 24000

        @classmethod
        def from_local(cls, path, device):
            seen["local"] = (path, device)
            return cls()

        @classmethod
        def from_pretrained(cls, *a, **k):
            pytest.fail("Chatterbox must not use from_pretrained")

    fake_torch(monkeypatch)
    fake(monkeypatch, "chatterbox")
    fake(monkeypatch, "chatterbox.mtl_tts", ChatterboxMultilingualTTS=Multilingual)
    mod = helper(monkeypatch, "chatterbox_tts_helper")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"cmd": "quit"}\n'))
    assert mod.main() == 0
    assert seen["local"] == (str(folder), "cpu")


# --- voice samples and the Whisper checkpoint -------------------------------------------------------

def test_voice_samples_come_from_the_tts_voices_folder(models, monkeypatch):
    voices = models / "tts" / "voices"
    voices.mkdir(parents=True)
    (voices / "anna.wav").write_bytes(b"RIFF")
    (voices / "luca.mp3").write_bytes(b"ID3")
    mod = helper(monkeypatch, "xtts_tts_helper")
    assert mod._riferimento("anna") == str(voices / "anna.wav")
    assert mod._riferimento("luca") == str(voices / "luca.mp3")
    assert mod._riferimento("nobody") is None
    assert mod._riferimento("../anna") is None


def test_a_voice_transcript_uses_the_local_whisper_checkpoint(models, monkeypatch):
    voices = models / "tts" / "voices"
    voices.mkdir(parents=True)
    sample = voices / "anna.wav"
    sample.write_bytes(b"RIFF")
    loaded = []

    class Whisper:
        def transcribe(self, path):
            return {"text": " hello "}

    fake(monkeypatch, "whisper", load_model=lambda arg: loaded.append(arg) or Whisper())
    mod = helper(monkeypatch, "cosyvoice_tts_helper")
    with pytest.raises(ms.ModelNotInstalled):
        mod._trascrizione(str(sample))
    assert loaded == []                                                    # the model is checked before the library is used
    folder = install(models, "stt", "whisper-small", {"small.pt": 4})
    assert mod._trascrizione(str(sample)) == "hello"
    assert loaded == [str(folder / "small.pt")]


def test_qwen3tts_without_whisper_clones_from_the_timbre_alone(models, monkeypatch):
    voices = models / "tts" / "voices"
    voices.mkdir(parents=True)
    sample = voices / "anna.wav"
    sample.write_bytes(b"RIFF")
    fake(monkeypatch, "whisper", load_model=lambda arg: pytest.fail("no checkpoint is installed"))
    mod = helper(monkeypatch, "qwen3tts_tts_helper")
    assert mod._trascrizione(str(sample)) == ""


# --- nothing in the engine code can reach the network ----------------------------------------------

def test_no_engine_module_can_download():
    for path in ENGINES.glob("*.py"):
        code = path.read_text(encoding="utf-8")
        for word in ("snapshot_download", "hf_hub_download", "HF_HUB_CACHE", "TTS_MODELS_DIR", "LOCALAPPDATA", "from_pretrained(device"):
            assert word not in code, f"{path.name} mentions {word}"
