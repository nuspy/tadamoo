"""The consent gate alone, with a clock that is just a number."""
from callbridge.meetbot import consent
from callbridge.meetbot.consent import ConsentGate


def gate(mode="window", window=45):
    g = ConsentGate(mode, window)
    g.announced(0.0)
    return g


def test_nothing_counts_before_the_announcement():
    g = ConsentGate("window", 45)
    assert g.hear("stop", "voice") == "" and g.state == "idle" and not g.objections
    assert g.poll(1000.0) == "idle"


def test_window_mode_stays_after_the_window_with_no_objection():
    g = gate("window", 45)
    assert g.poll(44.9) == consent.WAITING
    assert g.poll(45.0) == consent.GRANTED and g.outcome == "granted_window" and g.ever_granted and not g.refused


def test_explicit_mode_needs_a_yes_and_leaves_after_twice_the_window():
    g = gate("explicit", 30)
    assert g.poll(59.9) == consent.WAITING
    assert g.poll(60.0) == consent.ENDED and g.outcome == "no_consent" and g.refused
    g = gate("explicit", 30)
    assert g.hear("ok", "chat") == "" and g.state == consent.GRANTED and g.outcome == "granted_explicit"
    g = gate("explicit", 30)
    assert g.hear("va bene", "voice") == "" and g.state == consent.GRANTED


def test_window_mode_does_not_need_a_yes_and_a_yes_does_not_cut_the_wait():
    g = gate("window", 45)
    g.hear("ok", "voice")
    assert g.state == consent.WAITING


def test_an_objection_from_voice_or_chat_ends_at_once():
    for source in ("voice", "chat"):
        g = gate()
        assert g.hear("stop", source) == "objection"
        assert g.state == consent.ENDED and g.outcome == "objection" and g.refused
        assert g.objections == [{"source": source, "reason": "stop"}]


def test_an_objection_wins_over_a_yes_in_the_same_sentence():
    g = gate("explicit")
    assert g.hear("ok but stop", "voice") == "objection" and g.outcome == "objection"


def test_an_objection_after_consent_still_ends_it_with_a_stop_word_only():
    g = gate("window", 20)
    g.poll(20.0)
    assert g.hear("no, the other one", "voice") == "" and g.state == consent.GRANTED
    assert g.hear("please leave", "voice") == "objection" and g.refused


def test_removal_and_leave_before_consent_are_refusals():
    g = gate()
    g.removed()
    assert g.outcome == "removed" and g.refused
    g = gate()
    g.left()
    assert g.outcome == "left" and g.refused


def test_leave_after_consent_is_a_normal_end_but_removal_is_not():
    g = gate("window", 20)
    g.poll(20.0)
    g.left()
    assert g.outcome == "left" and not g.refused and g.ever_granted
    g = gate("window", 20)
    g.poll(20.0)
    g.removed()
    assert g.refused


def test_an_ended_gate_does_not_change():
    g = gate()
    g.hear("stop", "voice")
    g.poll(500.0)
    g.hear("ok", "voice")
    assert g.outcome == "objection"


def test_bad_settings_fall_back():
    g = ConsentGate("whatever", 5)
    assert g.mode == "window" and g.window_s == consent.WINDOW_DEFAULT_S
