"""The master switch of the technical session: on until the person turns it off in the chat, then every use is off; reset keeps it."""
from tadamoo import side_uses


def test_it_is_on_by_default_and_turning_it_off_turns_every_use_off(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path))
    side_uses.reset()
    assert side_uses.session_on() and side_uses.enabled("triage") and side_uses.enabled("inject_screen")
    side_uses.set_session(False)
    assert not side_uses.session_on()
    assert not any(side_uses.enabled(use.id) for use in side_uses.USES)
    side_uses.set_session(True)
    assert side_uses.enabled("triage")


def test_reset_all_keeps_the_master_switch_and_a_bad_value_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path))
    side_uses.set_session(False)
    side_uses.reset()
    assert not side_uses.session_on()
    try:
        side_uses.set_session("no")
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a bad value")
    side_uses.set_session(True)
