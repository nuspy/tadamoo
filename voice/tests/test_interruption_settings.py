"""voice.json `interruptions` to the config the gateway reads."""
import json


def test_voice_json_reaches_the_config_the_gateway_reads(tmp_path):
    from callbridge.config import Config
    from voice import settings_bridge as sb
    path = tmp_path / "voice.json"
    path.write_text(json.dumps({"interruptions": {"enabled": False, "judge_timeout_ms": 1500, "duck_level": 0.45, "resume_offer": False}}), encoding="utf-8")
    config = Config()
    sb.apply(config, sb.load(path))
    assert (config.interruptions_enabled, config.interruption_judge_timeout_ms, config.interruption_duck_level, config.interruption_resume_offer) == (False, 1500, 0.45, False)
    config = Config()
    sb.apply(config, sb.load(tmp_path / "missing.json"))
    assert (config.interruptions_enabled, config.interruption_judge_timeout_ms, config.interruption_duck_level, config.interruption_resume_offer) == (True, 800, 0.3, True)
    path.write_text("{broken", encoding="utf-8")
    sb.apply(config, sb.load(path))
    assert config.interruption_judge_timeout_ms == 800                       # a damaged file is the defaults
