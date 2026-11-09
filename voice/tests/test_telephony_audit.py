"""The audit of every call: one row at the start and one at the end, the number masked, no word of the conversation, and no call without it."""
import json

import pytest

from callbridge import call_policy
from callbridge.call_policy import AuditError, CallAudit, Policy, outgoing_greeting
from callbridge.call_service import CallService
from fakes import FakeBrain
from telephony_fakes import Clock, FakeRouter, bridge_stub, config

NUMBER = "+39 333 1234567"
GREETING = outgoing_greeting("Mario Rossi", "it", "Chiamo per un appuntamento.")


def rows(tmp_path):
    return [json.loads(line) for line in (tmp_path / "call-audit.jsonl").read_text(encoding="utf-8").splitlines()]


def setup(tmp_path, **over):
    cfg = config(tmp_path, **over)
    mono = Clock(100.0)
    wall = Clock(1_800_000_000.0)
    policy = Policy(cfg, CallAudit(cfg.call_audit_file, wall), monotonic=mono)
    router = FakeRouter()
    svc = CallService(cfg, bridge_stub(), policy, brain_factory=FakeBrain, router_factory=lambda: router)
    return policy, svc, router, mono


def test_a_call_leaves_a_start_row_and_an_end_row(tmp_path):
    policy, svc, router, mono = setup(tmp_path)
    svc.place("user", NUMBER)
    mono.advance(42.4)
    list(router.brain.respond_stream("Pronto?"))                 # one turn of conversation
    router.finish()
    start, end = rows(tmp_path)
    assert start["event"] == "start" and end["event"] == "end" and start["id"] == end["id"]
    for row in (start, end):
        assert row["direction"] == "outgoing" and row["number"] == "***567" and row["origin"] == "user"
        assert row["approval"] == "not_needed" and row["disclosure"] == "on" and row["ts"].startswith("2027-01-15T")
    assert start["transport"] == "" and end["transport"] == "gsm"
    assert end["duration_s"] == 42.4 and end["outcome"] == "completed"


def test_the_rows_say_who_started_the_call_and_how_it_was_approved(tmp_path):
    policy, svc, router, _ = setup(tmp_path, outgoing_approval="auto")
    svc.place("agent", NUMBER, greeting=GREETING)
    router.finish()
    svc.place("program", NUMBER, greeting=GREETING)
    router.finish()
    got = [(r["origin"], r["approval"]) for r in rows(tmp_path) if r["event"] == "start"]
    assert got == [("agent", "auto"), ("program", "auto")]


def test_no_digit_but_the_last_three_and_no_word_of_the_conversation_is_written(tmp_path):
    policy, svc, router, _ = setup(tmp_path)
    svc.place("user", NUMBER, greeting="Parola segreta del cliente: arancione")
    list(router.brain.respond_stream("la mia carta e' 4111 1111 1111 1111"))
    router.finish()
    text = (tmp_path / "call-audit.jsonl").read_text(encoding="utf-8")
    for leaked in ("333", "1234567", "4111", "arancione", "carta", "segreta", "Mario"):
        assert leaked not in text
    assert "***567" in text


def test_a_refused_call_is_recorded_with_its_reason(tmp_path):
    policy, svc, _, _ = setup(tmp_path)
    svc.place("agent", "112", greeting=GREETING)
    svc.place("program", NUMBER, greeting="Buongiorno.")
    svc.place("user", "abc")
    got = [(r["event"], r["reason"], r["origin"]) for r in rows(tmp_path)]
    assert got == [("refused", "emergency_number", "agent"), ("refused", "no_disclosure", "program"), ("refused", "invalid_number", "user")]
    assert rows(tmp_path)[0]["number"] == "***112"


def test_a_call_waiting_for_approval_is_not_a_call_yet(tmp_path):
    _, svc, router, _ = setup(tmp_path)
    assert svc.place("program", NUMBER, greeting=GREETING)[0] == 202
    assert router.placed == [] and not (tmp_path / "call-audit.jsonl").exists()


def test_disclosure_off_is_written_in_the_row(tmp_path):
    _, svc, router, _ = setup(tmp_path, ai_disclosure_on_start=False)
    svc.place("user", NUMBER)
    assert rows(tmp_path)[0]["disclosure"] == "off"


def test_the_outcomes(tmp_path):
    policy, svc, router, _ = setup(tmp_path)
    svc.place("user", NUMBER)
    router.finish()                                              # the line dropped before anybody spoke
    svc.place("user", NUMBER)
    list(router.brain.respond_stream("Pronto"))
    router.finish()
    svc.place("user", NUMBER)
    svc.hangup()
    svc2 = FakeRouter(result="error")
    svc._router = svc2
    svc.place("user", NUMBER)
    ends = [r["outcome"] for r in rows(tmp_path) if r["event"] == "end"]
    assert ends == ["no_conversation", "completed", "hung_up", "failed"]


def test_ending_twice_writes_one_end_row(tmp_path):
    policy, svc, router, _ = setup(tmp_path)
    svc.place("user", NUMBER)
    router.finish()
    router.finish()
    assert sum(1 for r in rows(tmp_path) if r["event"] == "end") == 1


def test_a_call_that_cannot_be_recorded_is_not_placed(tmp_path):
    cfg = config(tmp_path)
    (tmp_path / "call-audit.jsonl").mkdir()                      # a folder where the file should be
    router = FakeRouter()
    svc = CallService(cfg, bridge_stub(), Policy(cfg), brain_factory=FakeBrain, router_factory=lambda: router)
    code, body = svc.place("user", NUMBER)
    assert code == 500 and body["code"] == "audit_unavailable" and router.placed == []
    assert svc.incoming("+393331234567", "gsm", explicit=True) is None


def test_no_audit_file_set_means_no_call(tmp_path):
    cfg = config(tmp_path, call_audit_file="")
    router = FakeRouter()
    svc = CallService(cfg, bridge_stub(), Policy(cfg), brain_factory=FakeBrain, router_factory=lambda: router)
    assert svc.place("user", NUMBER)[0] == 500 and router.placed == []


def test_the_file_is_rotated_when_it_grows_and_the_tail_skips_damaged_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(call_policy, "AUDIT_MAX_BYTES", 200)
    audit = CallAudit(tmp_path / "a.jsonl")
    for i in range(10):
        audit.write(event="x", n=i)
    assert (tmp_path / "a.jsonl.1").exists()
    with open(tmp_path / "a.jsonl", "a", encoding="utf-8") as f:
        f.write("not json\n[1]\n")
    tail = audit.tail(20)
    assert tail and all(isinstance(r, dict) and r["event"] == "x" for r in tail)
    assert CallAudit(tmp_path / "missing.jsonl").tail() == []
    with pytest.raises(AuditError):
        CallAudit("").write(event="x")


def test_the_server_can_read_the_rows_back_masked(tmp_path):
    policy, svc, router, _ = setup(tmp_path)
    svc.place("user", NUMBER)
    router.finish()
    got = policy.audit.tail(10)
    assert [r["event"] for r in got] == ["start", "end"] and all(r["number"] == "***567" for r in got)
