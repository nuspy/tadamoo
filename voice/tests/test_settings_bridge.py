"""voice/settings_bridge.py: voice.json -> what the voice service uses. A missing or damaged file is never an error."""
import json
import os
from dataclasses import fields

from callbridge.config import Config
from callbridge import models_store as ms
from voice import settings_bridge as sb


def _write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_missing_file_gives_defaults(tmp_path):
    s = sb.load(tmp_path / "voice.json")
    assert s.models_dir == str(tmp_path.resolve() / "voice-models")
    assert {k: v.model for k, v in s.services.items()} == ms.DEFAULTS
    assert s.services["stt"].device == "auto" and s.services["stt"].memory == "vram"
    assert s.services["vad"].device == "cpu" and s.services["vad"].memory == "ram"


def test_default_models_dir_is_used_when_the_file_names_none(tmp_path):
    other = str(tmp_path / "elsewhere")
    assert sb.load(tmp_path / "voice.json", other).models_dir == other


def test_damaged_files_give_defaults(tmp_path):
    for text in ("", "{", "[1, 2]", "null", '{"services": 5}', '{"services": {"stt": "x"}}', "\xff\xfe"):
        s = sb.load(_write(tmp_path / "voice.json", text))
        assert s.services["stt"].model == "whisper-small", text
    assert sb.load(tmp_path / "no-such-folder" / "voice.json").services["tts"].model == "kokoro"


def test_a_relative_models_dir_is_ignored(tmp_path):
    s = sb.load(_write(tmp_path / "voice.json", {"models_dir": "relative/dir"}))
    assert s.models_dir.endswith("voice-models")


def test_the_choice_and_the_placement_are_read(tmp_path):
    root = str(tmp_path / "models")
    s = sb.load(_write(tmp_path / "voice.json", {"models_dir": root, "services": {
        "stt": {"selected": "whisper-medium", "device": "cuda:1", "memory": "vram"},
        "tts": {"selected": "piper", "device": "cpu", "memory": "ram"}}}))
    assert s.models_dir == root
    stt = s.services["stt"]
    assert (stt.model, stt.device, stt.memory, stt.cuda_index, stt.engine) == ("whisper-medium", "cuda:1", "vram", 1, "whisper")
    assert s.services["tts"].engine == "piper" and s.services["tts"].on_cpu


def test_bad_values_fall_back_one_by_one(tmp_path):
    s = sb.load(_write(tmp_path / "voice.json", {"services": {
        "stt": {"selected": "kokoro", "device": "gpu0", "memory": "disk"},          # a TTS model for stt, junk placement
        "tts": {"selected": "nope", "device": "cpu", "memory": "vram"}}}))          # cpu with vram is not possible
    assert s.services["stt"].model == "whisper-small" and s.services["stt"].device == "auto" and s.services["stt"].memory == "vram"
    assert s.services["tts"].model == "kokoro" and s.services["tts"].device == "cpu" and s.services["tts"].memory == "ram"


def test_overrides_map_to_config_fields(tmp_path):
    root = str(tmp_path / "models")
    s = sb.load(_write(tmp_path / "voice.json", {"models_dir": root, "services": {
        "stt": {"selected": "whisper-large-v3", "device": "cuda:0", "memory": "vram"},
        "tts": {"selected": "xtts-v2", "device": "cuda:1", "memory": "vram"}}}))
    o = sb.overrides(s)
    names = {f.name for f in fields(Config)}
    assert set(o) <= names, set(o) - names
    assert (o["stt_model"], o["stt_device"], o["stt_gpu_memory"]) == ("large-v3", "cuda", "dedicated")
    assert (o["tts_engine"], o["xtts_device"], o["xtts_gpu_memory"]) == ("xtts", "cuda", "dedicated")
    assert o["kokoro_model_dir"] == os.path.join(root, "tts", "kokoro")
    assert o["piper_model_dir"] == os.path.join(root, "tts", "piper")
    assert o["diarization_model_dir"] == os.path.join(root, "diarization", "sherpa-diarization")
    assert o["stt_vad_model"] == os.path.join(root, "vad", "silero-vad-v4", "silero_vad_v4.onnx")
    assert o["diarization_device"] == "cpu"


def test_ram_and_cpu_run_on_the_cpu(tmp_path):
    s = sb.load(_write(tmp_path / "voice.json", {"services": {
        "stt": {"selected": "whisper-base", "device": "cuda:0", "memory": "ram"},
        "tts": {"selected": "qwen3tts-1.7b", "device": "cpu", "memory": "ram"}}}))
    o = sb.overrides(s)
    assert (o["stt_device"], o["stt_gpu_memory"]) == ("cpu", "shared")
    assert (o["qwen3tts_device"], o["qwen3tts_size"]) == ("cpu", "1.7B")
    assert sb.helper_env(s) == {}                       # nothing on a GPU: no GPU number to pass


def test_the_gpu_number_goes_to_the_helpers_of_that_service(tmp_path):
    s = sb.load(_write(tmp_path / "voice.json", {"services": {"stt": {"device": "cuda:1", "memory": "vram"}, "tts": {"device": "auto"}}}))
    assert sb.helper_env(s) == {"stt": {"CUDA_VISIBLE_DEVICES": "1"}}


def test_apply_sets_the_config_and_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(ms.ROOT_ENV, "")
    monkeypatch.setattr(ms, "_service_env", {})
    root = str(tmp_path / "models")
    s = sb.load(_write(tmp_path / "voice.json", {"models_dir": root, "services": {"stt": {"selected": "whisper-tiny", "device": "cuda:1"}}}))
    cfg = Config()
    applied = sb.apply(cfg, s)
    assert cfg.stt_model == "tiny" and cfg.kokoro_model_dir == os.path.join(root, "tts", "kokoro") and "stt_model" in applied
    assert os.environ[ms.ROOT_ENV] == root
    env = ms.helper_env("stt", {"PATH": "x"})
    assert env["CUDA_VISIBLE_DEVICES"] == "1" and env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert env[ms.ROOT_ENV] == root and env["PATH"] == "x"
    assert "CUDA_VISIBLE_DEVICES" not in ms.helper_env("tts", {})
    sb.apply(cfg, sb.load(tmp_path / "gone.json"))      # settings changed back: the GPU number is dropped again
    assert "CUDA_VISIBLE_DEVICES" not in ms.helper_env("stt", {})


# ---------------------------------------------------------------------------------------- telephony
def test_telephony_defaults_are_the_careful_ones(tmp_path):
    t = sb.load(tmp_path / "voice.json").telephony
    assert (t.transport, t.outgoing_approval, t.ai_disclosure_on_start, t.incoming_auto_answer, t.on_behalf_of) == ("off", "manual", True, False, "")


def test_telephony_values_are_read_and_reach_the_config(tmp_path):
    p = _write(tmp_path / "voice.json", {"telephony": {"transport": "gsm", "outgoing_approval": "auto", "ai_disclosure_on_start": False,
                                                       "incoming_auto_answer": True, "on_behalf_of": " Mario Rossi "}})
    cfg = Config()
    sb.apply(cfg, sb.load(p))
    assert (cfg.call_transport, cfg.outgoing_approval, cfg.ai_disclosure_on_start, cfg.incoming_auto_answer, cfg.call_on_behalf_of) == \
        ("gsm", "auto", False, True, "Mario Rossi")
    assert cfg.call_audit_file == str(tmp_path.resolve() / "call-audit.jsonl")


def test_a_bad_telephony_value_takes_its_default_and_only_a_real_false_turns_the_disclosure_off(tmp_path):
    for stored in ({"transport": "bluetooth", "outgoing_approval": "yes", "ai_disclosure_on_start": "no", "incoming_auto_answer": "true",
                    "on_behalf_of": 5}, "x", None, [], {"on_behalf_of": "a\nb"}):
        t = sb.load(_write(tmp_path / "voice.json", {"telephony": stored})).telephony
        assert (t.transport, t.outgoing_approval, t.ai_disclosure_on_start, t.incoming_auto_answer, t.on_behalf_of) == ("off", "manual", True, False, "")
