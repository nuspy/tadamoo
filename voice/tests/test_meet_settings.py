"""The meetings section of voice.json as the service reads it: careful defaults, bad values replaced, the fields set on the Config."""
import json

from callbridge.config import Config
from voice import settings_bridge as sb

CAREFUL = sb.Meetings()


def load(tmp_path, meetings):
    path = tmp_path / "voice.json"
    path.write_text(json.dumps({"meetings": meetings} if meetings is not None else {}), encoding="utf-8")
    return sb.load(path)


def test_the_defaults_are_the_careful_ones(tmp_path):
    m = load(tmp_path, None).meetings
    assert m == CAREFUL
    assert (m.speak, m.camera, m.proactive, m.calendar_autojoin, m.copilot_audio_reply) == (False, False, False, False, False)
    assert (m.consent_mode, m.consent_window_s, m.copilot_hide_from_capture, m.copilot_replies) == ("window", 45, True, 3)


def test_bad_values_read_as_defaults(tmp_path):
    bad = {"speak": "yes", "camera": "on", "proactive": 1, "consent_mode": "never", "consent_window_s": 5, "on_behalf_of": 7, "display_name": "", "virtual_mic": ["x"],
           "calendar_autojoin": "true", "copilot_replies": 99, "copilot_audio_reply": None, "copilot_hide_from_capture": 0}
    assert load(tmp_path, bad).meetings == sb.Meetings(copilot_hide_from_capture=True)
    assert load(tmp_path, 5).meetings == CAREFUL


def test_good_values_are_kept(tmp_path):
    good = {"speak": True, "camera": True, "proactive": True, "consent_mode": "explicit", "consent_window_s": 90, "on_behalf_of": " Mario Rossi ", "display_name": "Helper",
            "virtual_mic": "CABLE", "wake_phrase": "hey helper", "calendar_autojoin": True, "copilot_replies": 5, "copilot_audio_reply": True,
            "copilot_hide_from_capture": False}
    m = load(tmp_path, good).meetings
    assert (m.speak, m.camera, m.proactive, m.consent_mode, m.consent_window_s, m.on_behalf_of, m.display_name) == (True, True, True, "explicit", 90, "Mario Rossi", "Helper")
    assert (m.virtual_mic, m.wake_phrase, m.calendar_autojoin, m.copilot_replies, m.copilot_audio_reply, m.copilot_hide_from_capture) == (
        "CABLE", "hey helper", True, 5, True, False)


def test_the_config_gets_the_fields_and_the_audit_sits_next_to_voice_json(tmp_path):
    settings = load(tmp_path, {"speak": True, "consent_window_s": 60, "on_behalf_of": "Mario"})
    config = Config()
    applied = sb.apply(config, settings)
    assert config.meetings_speak is True and config.meetings_consent_window_s == 60 and config.meetings_on_behalf_of == "Mario"
    assert config.meetings_audit_file == str(tmp_path / "meetings-audit.jsonl") and "meetings_audit_file" in applied
    assert config.copilot_num_responses == 3 and config.copilot_hide_from_capture is True


def test_nothing_in_the_config_ships_a_secret_or_a_default_oauth_app():
    c = Config()
    assert not [f for f in vars(Config)["__dataclass_fields__"] if f.startswith("calendar_") and f != "calendar_poll_s"]
    assert c.meetings_calendar_autojoin is False and c.meetings_speak is False
