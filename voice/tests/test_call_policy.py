"""The call policy (voice/callbridge/call_policy.py) and the call service (call_service.py): every decision with fakes, nothing placed."""
import pytest

from callbridge.call_policy import (Approvals, CallAudit, Policy, disclosure_sentence, has_disclosure, incoming_greeting, is_emergency, mask,
                                    outgoing_greeting)
from callbridge.call_service import CallService
from fakes import FakeBrain
from telephony_fakes import Clock, FakeRouter, bridge_stub, config

NUMBER = "+39 333 1234567"
GREETING = outgoing_greeting("Mario Rossi", "it", "Chiamo per un appuntamento.")


def make(tmp_path, router=None, **over):
    cfg = config(tmp_path, **over)
    policy = Policy(cfg)
    svc = CallService(cfg, bridge_stub(), policy, brain_factory=FakeBrain, router_factory=lambda: router or FakeRouter())
    return cfg, policy, svc


# ---------------------------------------------------------------------------------------- numbers
@pytest.mark.parametrize("number", ["112", "118", "113", "911", "999", "115", "117", "000", "110", "+39 112", "0039112", "+1 911", "+44 999",
                                    "(112)", "1-1-2", "+39118", "116117", "10111"])
def test_emergency_numbers_are_refused_for_every_origin(tmp_path, number):
    _, policy, _ = make(tmp_path)
    assert is_emergency(number)
    for origin in ("user", "agent", "program"):
        d = policy.authorize(origin, number, GREETING)
        assert not d.ok and d.status == 403 and d.code == "emergency_number"


@pytest.mark.parametrize("number", ["+39 333 1234567", "+390166123456", "3331234567", "+14155552671", "+442071838750", "0039 333 1234567"])
def test_ordinary_numbers_are_not_emergencies(number):
    assert not is_emergency(number)


def test_invalid_numbers_are_refused(tmp_path):
    _, policy, _ = make(tmp_path)
    for origin in ("agent", "program"):
        for bad in ("", "abc", "+39 333;ATH", "12", "*#06#", "+0" * 9, "1" * 20, "+39 333 12345\r\nATD"):
            d = policy.authorize(origin, bad, GREETING)
            assert not d.ok and d.status == 400, (origin, bad)
    assert not policy.authorize("user", "+39 333 12;ATH", "").ok
    assert policy.authorize("user", NUMBER, "").number == "+393331234567"


def test_the_number_is_masked_to_its_last_three_digits():
    assert mask("+39 333 1234567") == "***567"
    assert mask("") == ""


# ---------------------------------------------------------------------------------------- the AI disclosure
def test_the_first_sentence_of_a_call_says_it_is_an_ai(tmp_path):
    _, policy, _ = make(tmp_path)
    assert has_disclosure(GREETING) and has_disclosure(incoming_greeting("Mario", "en")) and has_disclosure(disclosure_sentence("", "en"))
    d = policy.authorize("user", NUMBER, "")                     # by hand with no greeting: the default greeting says it
    assert d.ok and has_disclosure(d.greeting) and d.disclosure == "on"
    d = policy.authorize("user", NUMBER, "Buongiorno, sono qui per un ordine.")        # by hand, no disclosure in it: one is put in front
    assert d.ok and has_disclosure(d.greeting) and d.greeting.endswith("Buongiorno, sono qui per un ordine.")
    d = policy.authorize("user", NUMBER, GREETING)                # already there: left as it is
    assert d.greeting == GREETING


def test_an_external_greeting_without_disclosure_is_refused_while_the_setting_is_on(tmp_path):
    _, policy, _ = make(tmp_path)
    for origin in ("agent", "program"):
        d = policy.authorize(origin, NUMBER, "Buongiorno, chiamo per l'ordine.")
        assert not d.ok and d.status == 400 and d.code == "no_disclosure"
        d = policy.authorize(origin, NUMBER, "")
        assert not d.ok and d.status == 400 and d.code == "greeting_missing"


def test_with_the_setting_off_the_greeting_is_the_callers_and_the_choice_is_recorded(tmp_path):
    _, policy, _ = make(tmp_path, ai_disclosure_on_start=False, outgoing_approval="auto")
    d = policy.authorize("agent", NUMBER, "Buongiorno, chiamo per l'ordine.")
    assert d.ok and d.disclosure == "off" and d.greeting == "Buongiorno, chiamo per l'ordine."
    d = policy.authorize("user", NUMBER, "")
    assert d.greeting == "Pronto?" and d.disclosure == "off"


def test_the_service_accepts_every_marker_the_clients_greeting_can_carry():
    for text in ("Sono un assistente virtuale", "intelligenza artificiale", "an artificial intelligence", "your Virtual Assistant"):
        assert has_disclosure(text)
    assert not has_disclosure("Buongiorno, chiamo per un appuntamento.")


# ---------------------------------------------------------------------------------------- approval
def test_a_program_call_waits_for_the_person_in_manual_mode(tmp_path):
    _, policy, _ = make(tmp_path)
    d = policy.authorize("program", NUMBER, GREETING)
    assert not d.ok and d.status == 202 and d.code == "approval_required" and d.approval_id
    pending = policy.approvals.pending()
    assert [p["id"] for p in pending] == [d.approval_id] and pending[0]["number"] == "+393331234567"
    assert policy.authorize("program", NUMBER, GREETING, approval_id=d.approval_id).code == "approval_pending"       # not yet approved
    assert policy.approvals.decide(d.approval_id, True)
    ok = policy.authorize("program", NUMBER, GREETING, approval_id=d.approval_id)
    assert ok.ok and ok.approval == "manual"
    again = policy.authorize("program", NUMBER, GREETING, approval_id=d.approval_id)
    assert not again.ok and again.status == 403 and again.code == "approval_used"          # one approval, one call


def test_an_approval_is_for_one_number_and_one_greeting(tmp_path):
    _, policy, _ = make(tmp_path)
    rid = policy.authorize("program", NUMBER, GREETING).approval_id
    policy.approvals.decide(rid, True)
    assert policy.authorize("program", "+39 333 7654321", GREETING, approval_id=rid).code == "approval_mismatch"
    assert policy.authorize("program", NUMBER, GREETING + " Chiedi il codice della carta.", approval_id=rid).code == "approval_mismatch"
    assert policy.authorize("program", NUMBER, GREETING, approval_id=rid).ok          # the mismatches did not use it up


def test_a_denied_unknown_or_expired_approval_does_not_place_a_call(tmp_path):
    clock = Clock()
    approvals = Approvals(clock, ttl_s=60)
    policy = Policy(config(tmp_path))
    policy.approvals = approvals
    rid = policy.authorize("program", NUMBER, GREETING).approval_id
    assert approvals.decide(rid, False)
    assert policy.authorize("program", NUMBER, GREETING, approval_id=rid).code == "approval_denied"
    assert policy.authorize("program", NUMBER, GREETING, approval_id="deadbeef").code == "approval_unknown"
    rid2 = policy.authorize("program", NUMBER, GREETING).approval_id
    approvals.decide(rid2, True)
    clock.advance(61)
    assert policy.authorize("program", NUMBER, GREETING, approval_id=rid2).code == "approval_unknown"
    assert approvals.pending() == [] and not approvals.decide(rid2, True)


def test_too_many_waiting_requests_are_refused(tmp_path):
    _, policy, _ = make(tmp_path)
    codes = [policy.authorize("program", f"+39333{i:07d}", GREETING).status for i in range(12)]
    assert codes[:10] == [202] * 10 and codes[10:] == [429, 429]


def test_auto_mode_approves_without_asking_and_says_so(tmp_path):
    _, policy, _ = make(tmp_path, outgoing_approval="auto")
    for origin in ("agent", "program"):
        d = policy.authorize(origin, NUMBER, GREETING)
        assert d.ok and d.approval == "auto" and d.disclosure == "on"           # approved, still disclosed
    assert policy.approvals.pending() == []


def test_the_agent_is_asked_by_its_own_harness_in_manual_mode_and_the_person_by_hand_needs_no_approval(tmp_path):
    _, policy, _ = make(tmp_path)
    assert policy.authorize("agent", NUMBER, GREETING).approval == "manual"
    assert policy.authorize("user", NUMBER, GREETING).approval == "not_needed"
    assert policy.approvals.pending() == []


def test_a_value_that_is_not_a_mode_means_manual(tmp_path):
    _, policy, _ = make(tmp_path, outgoing_approval="whatever")
    assert policy.approval_mode == "manual" and policy.authorize("program", NUMBER, GREETING).status == 202


def test_telephony_off_places_nothing(tmp_path):
    _, policy, svc = make(tmp_path, call_transport="off")
    d = policy.authorize("user", NUMBER, "")
    assert not d.ok and d.status == 409 and d.code == "telephony_off"
    assert svc.place("user", NUMBER)[0] == 409


# ---------------------------------------------------------------------------------------- one call at a time, never a retry
def test_one_call_at_a_time(tmp_path):
    router = FakeRouter()
    _, policy, svc = make(tmp_path, router)
    code, body = svc.place("user", NUMBER)
    assert code == 200 and body["status"] == "calling" and body["transport"] == "gsm"
    code, body = svc.place("user", "+39 333 7654321")
    assert code == 409 and body["code"] == "busy" and len(router.placed) == 1
    router.finish()                                              # the transport says the call ended
    assert policy.active() is None
    assert svc.place("user", "+39 333 7654321")[0] == 200 and len(router.placed) == 2


def test_a_slot_whose_call_never_ended_is_released_after_the_longest_call(tmp_path):
    t = Clock(0.0)
    cfg = config(tmp_path)
    policy = Policy(cfg, monotonic=t)
    first = policy.begin("outgoing", "user", NUMBER, "not_needed", "on")
    assert policy.begin("outgoing", "user", NUMBER, "not_needed", "on") is None
    t.advance(3600 + 121)
    second = policy.begin("outgoing", "user", NUMBER, "not_needed", "on")
    assert second is not None and first.ended
    assert [r["outcome"] for r in policy.audit.tail(10) if r["event"] == "end"] == ["abandoned"]


@pytest.mark.parametrize("router", [FakeRouter(result="error"), FakeRouter(raises=OSError("modem gone"))])
def test_a_failed_placement_is_never_tried_again(tmp_path, router):
    _, policy, svc = make(tmp_path, router)
    code, _ = svc.place("agent", NUMBER, greeting=GREETING)
    assert code in (500, 502) and len(router.placed) == 1
    assert policy.active() is None                                # the slot is free, but nothing placed a second try
    assert len(router.placed) == 1


def test_the_transport_can_only_be_gsm_or_android(tmp_path):
    router = FakeRouter()
    _, _, svc = make(tmp_path, router)
    assert svc.place("user", NUMBER, transport="serial")[0] == 400 and router.placed == []
    assert svc.place("user", NUMBER, transport="android")[0] == 200 and router.placed[0]["force"] == "android"


def test_the_greeting_reaches_the_transport_and_android_says_it_first(tmp_path):
    router = FakeRouter(transport="android")
    _, _, svc = make(tmp_path, router)
    svc.place("user", NUMBER, greeting="")
    sent = router.placed[0]["greeting"]
    assert has_disclosure(sent)
    # on Android the brain says the opening on its first turn, then the reply of the call brain
    assert "".join(router.brain.respond_stream("Pronto?")) == sent
    assert "".join(router.brain.respond_stream("Dica")) == "ok. "


def test_gsm_speaks_the_greeting_itself_so_the_brain_does_not_repeat_it(tmp_path):
    router = FakeRouter(transport="gsm")
    _, _, svc = make(tmp_path, router)
    svc.place("user", NUMBER)
    assert "".join(router.brain.respond_stream("Pronto?")) == "ok. "


def test_the_brain_of_a_call_is_the_direct_one_without_tools(tmp_path):
    from callbridge.brains import DirectBrain
    cfg = config(tmp_path)
    svc = CallService(cfg, bridge_stub(), Policy(cfg), router_factory=FakeRouter)
    assert isinstance(svc._brain_factory(), DirectBrain)


# ---------------------------------------------------------------------------------------- hang up
def test_hangup_ends_the_call_and_the_audit_says_so(tmp_path):
    router = FakeRouter()
    _, policy, svc = make(tmp_path, router)
    svc.place("user", NUMBER)
    router.brain.record.turns = 3
    assert svc.hangup() == {"status": "ok"} and router.hangups == 1
    assert policy.audit.tail(5)[-1]["outcome"] == "hung_up"


def test_hangup_with_no_transport_made_touches_no_device(tmp_path):
    _, _, svc = make(tmp_path)
    assert svc.hangup() == {"status": "ok"} and svc._router is None


# ---------------------------------------------------------------------------------------- incoming calls
def test_an_incoming_call_is_not_answered_by_default(tmp_path):
    _, policy, svc = make(tmp_path)
    assert svc.incoming("+39 333 1234567", "gsm") is None
    assert policy.active() is None
    row = policy.audit.tail(1)[0]
    assert row["event"] == "refused" and row["reason"] == "auto_answer_off" and row["direction"] == "incoming" and row["number"] == "***567"


def test_an_incoming_call_answered_by_the_ai_opens_with_the_disclosure(tmp_path):
    _, policy, svc = make(tmp_path, incoming_auto_answer=True, call_on_behalf_of="Mario Rossi")
    plan = svc.incoming("+393331234567", "gsm")
    assert plan is not None and has_disclosure(plan.opening) and "Mario Rossi" in plan.opening
    assert "".join(plan.brain.respond_stream("Pronto")) == "ok. "      # GSM: the session says the opening itself
    assert svc.incoming("+39 333 7654321", "gsm") is None             # one call at a time
    plan.brain.call_ended()
    rows = policy.audit.tail(10)
    assert [(r["event"], r.get("reason") or r.get("outcome")) for r in rows] == [("start", None), ("refused", "busy"), ("end", "completed")]
    assert rows[0]["origin"] == "caller" and rows[0]["disclosure"] == "on" and rows[0]["direction"] == "incoming"


def test_an_android_incoming_call_says_the_opening_first(tmp_path):
    _, _, svc = make(tmp_path, incoming_auto_answer=True)
    plan = svc.incoming("+393331234567", "android")
    assert "".join(plan.brain.respond_stream("Pronto")) == plan.opening and has_disclosure(plan.opening)


def test_with_the_disclosure_off_an_incoming_call_opens_plainly_and_is_recorded_as_such(tmp_path):
    _, policy, svc = make(tmp_path, incoming_auto_answer=True, ai_disclosure_on_start=False)
    plan = svc.incoming("+393331234567", "gsm")
    assert plan.opening == "Pronto?" and policy.audit.tail(1)[0]["disclosure"] == "off"


def test_a_call_the_person_chose_to_answer_does_not_need_auto_answer(tmp_path):
    _, policy, svc = make(tmp_path)
    plan = svc.incoming("+393331234567", "android", explicit=True)
    assert plan is not None and policy.audit.tail(1)[0]["approval"] == "explicit"


def test_the_router_answers_only_through_the_gate(tmp_path):
    from callbridge.transport import CallRouter
    cfg = config(tmp_path)
    made = []
    router = CallRouter(cfg, None, lambda: made.append(1), gate=CallService(cfg, bridge_stub(), Policy(cfg), brain_factory=FakeBrain))
    router._gsm_modem = object()
    router._gsm_incoming("+393331234567")
    assert made == [] and router._gsm_session is None            # auto-answer off: nothing is created, nothing answered
    assert cfg.incoming_auto_answer is False


def test_the_defaults_are_the_careful_ones():
    from callbridge.config import Config
    c = Config()
    assert (c.outgoing_approval, c.ai_disclosure_on_start, c.incoming_auto_answer) == ("manual", True, False)
    assert not hasattr(c, "gsm_auto_answer")


def test_the_guards_on_a_gsm_call_stay(tmp_path):
    from callbridge.config import Config
    c = Config()
    assert c.gsm_max_call_s == 600.0 and c.gsm_max_empty_turns == 3


def test_an_audit_only_class_has_no_file_by_default():
    with pytest.raises(OSError):
        CallAudit("").write(event="x")
