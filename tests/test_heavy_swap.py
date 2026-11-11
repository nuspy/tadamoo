"""The heavy-job swap: the person's level and answer, the engine put to sleep and woken, the conversations moved to a provider and back, and the client
that asks for VRAM first and for the swap only when lending is not enough."""
import threading
import time

import pytest

from serve.heavy_swap import HeavyPrefs, HeavySwap
from tadamoo.heavy import HeavyLease
from tadamoo.llm.credentials import FileCredentials
from tadamoo.llm.store import SecureRegistry


class Engine:
    def __init__(self, sleeps=("asleep",)):
        self.sleeps, self.calls, self.woken, self.heavy_hold = list(sleeps), 0, 0, None

    def sleep(self):
        self.calls += 1
        return self.sleeps.pop(0) if len(self.sleeps) > 1 else self.sleeps[0]

    def wake_now(self):
        self.woken += 1


@pytest.fixture
def world(tmp_path):
    reg = SecureRegistry(tmp_path / "llm_providers.json", credentials=FileCredentials(tmp_path / "creds.json"))
    cloud = reg.add({"type": "zai_coding", "api_key": "zk-123456789012"})
    prefs = HeavyPrefs(tmp_path / "heavy.json")
    prefs.write({"provider": cloud["id"]})
    engine = Engine()
    return reg, prefs, engine, cloud


def make(world, **kw):
    reg, prefs, engine, _ = world
    return HeavySwap(engine, reg, prefs, pause=lambda s: None, **kw)


def ask_in_thread(swap, *args, **kw):
    out = {}
    t = threading.Thread(target=lambda: out.update(swap.begin(*args, **kw)))
    t.start()
    return t, out


def wait_card(swap):
    for _ in range(200):
        cards = swap.pending()
        if cards:
            return cards[0]
        time.sleep(0.02)
    raise AssertionError("no question was asked")


def test_the_default_level_asks_and_a_yes_puts_the_model_to_sleep_and_the_conversations_on_the_provider(world):
    reg, prefs, engine, cloud = world
    swap = make(world)
    t, out = ask_in_thread(swap, "video", "a clip for the newscast", 4)
    card = wait_card(swap)
    assert card["provider"] and card["kind"] == "video" and card["minutes"] == 4 and "executor" in card["functions"] and "conversations" in card["leaves"]
    assert swap.answer(card["id"], True)["ok"]
    t.join(5)
    assert out["ok"] and engine.calls == 1 and reg.swap()["pid"] == cloud["id"] and "video" in engine.heavy_hold
    assert reg.load()["assignments"].get("executor") == cloud["id"] and swap.state()["active"]["kind"] == "video"
    assert swap.touch(out["swap"]["id"])["ok"]
    assert swap.end(out["swap"]["id"])["ok"] and engine.woken == 1 and engine.heavy_hold is None and reg.swap() is None and swap.state()["active"] is None


def test_a_no_changes_nothing_and_so_does_a_question_nobody_answers(world):
    reg, prefs, engine, _ = world
    swap = make(world)
    t, out = ask_in_thread(swap, "video", "x", 4)
    swap.answer(wait_card(swap)["id"], False)
    t.join(5)
    assert out["reason"] == "not_allowed" and engine.calls == 0 and reg.swap() is None and swap.state()["active"] is None
    got = swap.begin("video", "x", 4, wait_s=0.2)
    assert got["reason"] == "timeout" and swap.pending() == [] and engine.calls == 0


def test_never_and_a_missing_provider_refuse_at_once(world):
    reg, prefs, engine, _ = world
    swap = make(world)
    prefs.write({"mode": "never"})
    assert swap.begin("video", "x")["reason"] == "not_allowed"
    prefs.write({"mode": "ask", "provider": ""})
    got = swap.begin("video", "x")
    assert got["reason"] == "no_provider" and "Heavy jobs" in got["message"] and "Lavori pesanti" in got["message_it"]
    prefs.write({"provider": "ghost", "mode": "allow"})
    assert swap.begin("video", "x")["reason"] == "bad_provider" and engine.calls == 0 and swap.state()["active"] is None


def test_allow_covers_image_and_video_without_asking_but_any_other_kind_still_asks(world):
    reg, prefs, engine, _ = world
    prefs.write({"mode": "allow"})
    swap = make(world)
    out = swap.begin("image", "a picture")
    assert out["ok"] and swap.pending() == []
    swap.end(out["swap"]["id"])
    assert swap.begin("training", "x", wait_s=0.1)["reason"] == "timeout"


def test_always_allow_on_the_card_changes_the_level(world):
    reg, prefs, engine, _ = world
    swap = make(world)
    t, out = ask_in_thread(swap, "video", "x")
    swap.answer(wait_card(swap)["id"], True, always=True)
    t.join(5)
    assert out["ok"] and prefs.read()["mode"] == "allow"
    swap.end(out["swap"]["id"])


def test_only_one_swap_at_a_time_and_a_model_that_cannot_sleep_rolls_everything_back(world):
    reg, prefs, engine, _ = world
    prefs.write({"mode": "allow"})
    swap = make(world)
    first = swap.begin("video", "one")
    assert swap.begin("video", "two")["reason"] == "busy"
    swap.end(first["swap"]["id"])
    engine.sleeps = ["no ram"]
    got = swap.begin("video", "three")
    assert got["reason"] == "cannot_sleep" and "no ram" in got["message"] and reg.swap() is None and engine.heavy_hold is None and swap.state()["active"] is None
    engine.sleeps, before = ["busy", "busy", "asleep"], engine.calls                # a request the engine still runs finishes first
    assert swap.begin("video", "four")["ok"] and engine.calls == before + 3


def test_a_swap_nobody_touches_ends_by_itself_and_the_model_wakes(world, monkeypatch):
    reg, prefs, engine, _ = world
    prefs.write({"mode": "allow"})
    now = [0.0]
    swap = make(world, clock=lambda: now[0], hold_s=30)
    monkeypatch.setattr("serve.heavy_swap.TICK_S", 0.01)
    out = swap.begin("image", "x")
    assert out["ok"]
    now[0] = 100.0
    for _ in range(200):
        if engine.woken:
            break
        time.sleep(0.02)
    assert engine.woken == 1 and reg.swap() is None and swap.state()["active"] is None and swap.end(out["swap"]["id"])["reason"] == "not_found"


class Server:
    """A stand-in for the two sets of routes of the server, as `http_transport` would reach them."""

    def __init__(self, lease_answers, swap_answer):
        self.lease_answers, self.swap_answer, self.log = list(lease_answers), swap_answer, []

    def __call__(self, method, path, body=None):
        self.log.append(path)
        if path == "/api/vram/lease":
            return self.lease_answers.pop(0)
        if path == "/api/heavy/begin":
            return self.swap_answer
        return 200, {"ok": True}


GRANTED = (200, {"ok": True, "lease": {"id": "a" * 16, "granted_mib": 24000, "hold_s": 60}})
NOT_ENOUGH = (409, {"ok": False, "reason": "not_enough", "message": "no room", "message_it": "niente spazio"})


def test_the_client_borrows_vram_first_and_asks_to_sleep_the_model_only_when_that_is_not_enough():
    s = Server([GRANTED], None)
    with HeavyLease("video", 24000, "x", transport=s) as lease:
        assert lease.gpu_ok and not lease.swapped
    assert "/api/heavy/begin" not in s.log
    s = Server([NOT_ENOUGH, GRANTED], (200, {"ok": True, "swap": {"id": "b" * 16, "hold_s": 90}}))
    with HeavyLease("video", 24000, "x", transport=s) as lease:
        assert lease.gpu_ok and lease.swapped
    assert s.log == ["/api/vram/lease", "/api/heavy/begin", "/api/vram/lease", "/api/vram/lease/" + "a" * 16 + "/release", "/api/heavy/" + "b" * 16 + "/end"]


def test_the_client_says_why_when_the_person_said_no_or_the_vram_is_still_missing():
    s = Server([NOT_ENOUGH], (409, {"ok": False, "reason": "not_allowed", "message": "no", "message_it": "no"}))
    with HeavyLease("video", 24000, "x", transport=s) as lease:
        assert not lease.gpu_ok and lease.reason == "not_allowed" and not lease.swapped
    s = Server([NOT_ENOUGH, NOT_ENOUGH], (200, {"ok": True, "swap": {"id": "c" * 16, "hold_s": 90}}))
    with HeavyLease("video", 99000, "x", transport=s) as lease:
        assert not lease.gpu_ok and lease.reason == "not_enough" and not lease.swapped
    assert s.log[-1] == "/api/heavy/" + "c" * 16 + "/end"                          # the model is woken again
