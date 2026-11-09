"""Choosing the brain: the voice commands in Italian and English, the mode of a session's brain, the hotkeys of the two brains, and that what is
spoken never carries code whichever brain answers."""
import collections

import pytest

from callbridge import spoken
from callbridge.brains import DirectBrain, HarnessBrain, ModeBrain, make_brain
from callbridge.config import Config
from callbridge.streaming import StreamingSession
from callbridge.turn_engine import ReplyProcessor
from callbridge.voice_commands import parse_command
from fakes import FakeApi, ev_done, ev_tool


@pytest.mark.parametrize("text,mode", [
    ("switch to the agent", "harness"), ("Switch to agent", "harness"), ("use the harness", "harness"), ("go to the agent", "harness"),
    ("passa all'agente", "harness"), ("usa l'agente", "harness"), ("parla con l'agente", "harness"), ("passa all'harness", "harness"),
    ("switch to direct", "direct"), ("go direct", "direct"), ("use direct", "direct"),
    ("passa al diretto", "direct"), ("torna alla conversazione diretta", "direct"), ("passa alla modalità diretta", "direct"),
])
def test_a_command_to_switch_the_brain_is_recognised(text, mode):
    assert parse_command(text) == {"kind": "brain", "mode": mode}


@pytest.mark.parametrize("text", [
    "switch to english", "cambia lingua a inglese", "nuova sessione", "what does the agent do when it is late for work today",
    "tell me about the direct flight to Rome please now", "ciao come stai",
])
def test_other_words_are_not_a_brain_command(text):
    cmd = parse_command(text)
    assert cmd is None or cmd["kind"] != "brain"


def test_the_other_commands_still_work():
    assert parse_command("switch to english")["kind"] == "lang"
    assert parse_command("new session") == {"kind": "new_session"}


def session(brain, lang="en"):
    s = object.__new__(StreamingSession)
    s.brain = brain
    s._lang_code = lang
    return s


def test_a_voice_command_switches_the_session_brain_and_says_so_in_the_session_language():
    cfg = Config()
    root = ModeBrain(cfg, DirectBrain(cfg, FakeApi()), HarnessBrain(cfg, FakeApi()), "direct")
    mine = root.fork()
    s = session(mine, "en")
    assert s._run_voice_command({"kind": "brain", "mode": "harness"}, None) == spoken.saying("harness", "en")
    assert mine.mode == "harness" and root.mode == "direct"                        # the session's own mode, not the process's
    s2 = session(mine, "it")
    assert s2._run_voice_command({"kind": "brain", "mode": "direct"}, None) == "Va bene, ora conversazione diretta."
    assert mine.mode == "direct"


def test_a_brain_that_cannot_switch_says_nothing():
    s = session(object())
    assert s._run_voice_command({"kind": "brain", "mode": "harness"}, None) == ""


def test_set_mode_refuses_an_unknown_mode():
    b = ModeBrain(Config(), DirectBrain(Config(), FakeApi()), HarnessBrain(Config(), FakeApi()), "direct")
    assert b.set_mode("harness") is True and b.mode == "harness"
    assert b.set_mode("hermes") is False and b.mode == "harness"


def test_a_fork_starts_in_the_configured_mode_or_the_one_asked_and_shares_the_brains_and_the_log():
    cfg = Config()
    cfg.brain_mode = "harness"
    root = make_brain(cfg)
    a, b = root.fork(), root.fork("direct")
    assert (a.mode, b.mode) == ("harness", "direct")
    assert a.direct is b.direct is root.direct and a.harness is root.harness
    cfg.brain_mode = "bogus"
    assert make_brain(cfg).mode == "direct"


def test_the_current_brain_answers():
    d, h = FakeApi(), FakeApi()
    d.pieces = ["from direct. "]
    h.script = [[ev_done("from harness")]]
    cfg = Config()
    cfg.agent_max_wait_s = 2
    log = collections.deque(maxlen=20)
    b = ModeBrain(cfg, DirectBrain(cfg, d, sink=log.append), HarnessBrain(cfg, h, sink=log.append, sleep=lambda s: None), "direct", log)
    assert "".join(b.respond_stream("hi")) == "from direct. "
    b.set_mode("harness")
    assert "".join(b.respond_stream("hi")) == "from harness"
    assert b.state()["last"]["mode"] == "harness" and b.state()["pending"] is None


def test_the_state_lists_the_last_exchanges():
    d = FakeApi()
    cfg = Config()
    b = ModeBrain(cfg, mode="direct")
    b.direct._api = d
    for i in range(7):
        "".join(b.respond_stream(f"q{i}"))
    st = b.state()
    assert [x["user"] for x in st["exchanges"]] == ["q2", "q3", "q4", "q5", "q6"] and st["last"]["user"] == "q6"


# ---------------------------------------------------------------------------------------------- what reaches the TTS
def sentences(chunks):
    proc = ReplyProcessor(extract_media=False)
    out = []
    for c in chunks:
        out += proc.feed(c).tts_sentences
    out += proc.flush().tts_sentences
    return " ".join(out)


def test_the_text_that_reaches_the_tts_has_no_code_from_either_brain():
    d = FakeApi()
    d.pieces = ["Sure. First run `pip install x`", " and then:\n```bash\nrm -rf /\n```\n", "That is all. <think>secret plan</think>"]
    h = FakeApi()
    h.script = [[*ev_tool("shell_command", arguments={"command": "rm -rf /"}), ev_done("```sh\nrm -rf /\n```\nI ran it.")]]
    cfg = Config()
    b = ModeBrain(cfg, DirectBrain(cfg, d), HarnessBrain(cfg, h, sleep=lambda s: None), "direct")
    for mode in ("direct", "harness"):
        b.set_mode(mode)
        heard = sentences(b.respond_stream("go"))
        assert heard, mode
        for forbidden in ("rm -rf", "pip install", "```", "`", "bash", "secret plan", "<think>", "SECRET OUTPUT"):
            assert forbidden not in heard, (mode, forbidden)


# ---------------------------------------------------------------------------------------------- hotkeys
def test_the_two_brains_have_separate_hotkeys_taken_from_the_config():
    from callbridge import ptt

    class Hook:
        def start(self):
            return True

        def stop(self):
            pass

    class FakeDesktop:
        def __init__(self):
            self.taken = []

        def push_to_talk_keys(self, name, hotkeys):
            self.taken.append(hotkeys)
            return Hook()

    cfg = Config()
    cfg.push_to_talk_enabled = True
    desktop = FakeDesktop()
    m = ptt.PushToTalkManager(cfg, make_brain(cfg), desktop=desktop)
    started = []
    m._toggle = lambda brain, half_duplex, mode, avatar=False: started.append((brain, half_duplex, mode, avatar))
    m.start()
    keys = desktop.taken[0]
    for combo in (cfg.push_to_talk_hotkey, cfg.push_to_talk_hotkey_fullduplex, cfg.push_to_talk_hotkey_direct, cfg.push_to_talk_hotkey_direct_full):
        assert combo in keys
    assert (cfg.push_to_talk_hotkey, cfg.push_to_talk_hotkey_fullduplex, cfg.push_to_talk_hotkey_direct, cfg.push_to_talk_hotkey_direct_full) ==         ("shift+f1", "shift+f2", "ctrl+shift+f4", "ctrl+shift+f5")
    # the harness hotkeys call the harness, the direct ones the direct brain
    for combo in (cfg.push_to_talk_hotkey, cfg.push_to_talk_hotkey_fullduplex, cfg.push_to_talk_hotkey_direct, cfg.push_to_talk_hotkey_direct_full):
        keys[combo]()
    assert started == [("harness", True, "half", False), ("harness", False, "full", False),
                       ("direct", True, "half", False), ("direct", False, "full", False)]


def test_a_session_started_for_a_brain_uses_a_fork_in_that_mode(monkeypatch):
    from callbridge import ptt

    cfg = Config()
    m = ptt.PushToTalkManager(cfg, make_brain(cfg))
    monkeypatch.setattr(ptt, "ensure_models_loaded", lambda c: True)
    monkeypatch.setattr(ptt, "negotiate_sample_rate", lambda *a: 16000)
    made = []

    class Fake:
        phase = "idle"
        language = "it"
        language_mode = "follow"

        def __init__(self, cfg, brain, *a, **k):
            made.append(brain)

        def run(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(ptt, "PushToTalkSession", Fake)
    import threading
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    assert m.start_session("half", brain="harness") == (True, "half")
    assert made[0].mode == "harness" and made[0] is not m.brains
    assert m.session_state()["brain"] == "harness"
    assert m.stop_session() is True
    assert m.start_session("half", brain="bogus")[0] is False
    assert m.start_session("limited")[0] is False                                    # the old phone-simulation mode is gone
