"""Every path of the consent rule through a whole session, with fakes: objection by voice, by chat, the host's removal, the Leave button, silence in
both modes, a participant who joins later, and the rule that nothing is stored or listened to before the announcement."""
import json

import pytest

from callbridge.meetbot import consent
from callbridge.meetbot.adapters.base import ChatMessage
from meet_fakes import Rig, make_plan


def test_nothing_is_listened_to_or_stored_before_the_announcement(tmp_path):
    r = Rig(tmp_path)
    assert r.events == [] and not r.source.started and r.stt_started_at is None
    assert r.meeting_files() == []
    assert r.adapter.chat_out == []
    assert r.announce()
    assert r.adapter.chat_out and r.events == ["stt", "source"] and r.source.started
    announced = r.events_named("announced")
    assert announced and announced[0]["chat"] is True
    assert r.meeting_files() == []                         # announced and listening, but no consent yet: still nothing on disk


def test_the_audit_join_row_has_no_address_and_no_clear_names(tmp_path):
    r = Rig(tmp_path, make_plan(host="Anna Verdi"))
    row = r.events_named("join")[0]
    assert row["platform"] == "meet" and row["rights"] == "ticked" and row["consent_mode"] == "window" and row["consent_window_s"] == 45
    assert row["on_behalf_of"] == "M*** R***" and row["host"] == "A*** V***"
    text = r.audit_path.read_text(encoding="utf-8")
    assert "meet.google.com" not in text and "secret" not in text and "Mario" not in text and "Anna" not in text


def test_a_failed_announcement_means_the_bot_leaves_without_listening(tmp_path):
    r = Rig(tmp_path)
    r.adapter.chat_ok = False
    assert not r.announce()
    assert r.session.end_reason == "announcement_failed" and r.events == [] and not r.source.started
    r.finish()
    assert r.events_named("end")[0]["reason"] == "announcement_failed" and r.meeting_files() == []


def test_objection_by_voice_ends_at_once_and_deletes_the_buffer(tmp_path):
    r = Rig(tmp_path)
    r.announce()
    r.stt.say("hello everyone, let us start", "sorry, stop")
    r.run(5)
    s = r.session
    assert s._ending and s.end_reason == "objection" and s.gate.outcome == "objection"
    r.finish()
    assert s.transcript.segments == [] and s.transcript.path is None
    assert r.meeting_files() == [] and r.finished == []
    end = r.events_named("end")[0]
    assert end["reason"] == "objection" and end["outcome"] == "objection" and end["objections"] == [{"source": "voice", "reason": "stop"}]
    assert end["kept"] is False
    text = r.audit_path.read_text(encoding="utf-8")
    assert "sorry" not in text and "hello everyone" not in text
    assert r.adapter.left and r.browser.closed and r.source.stopped and r.stt.closed


def test_objection_by_chat_ends_at_once(tmp_path):
    r = Rig(tmp_path)
    r.announce()
    r.adapter.chat_in = [ChatMessage("Bruno", "I do not consent to this")]
    r.run(2)
    assert r.session.end_reason == "objection"
    r.finish()
    assert r.events_named("end")[0]["objections"] == [{"source": "chat", "reason": "do not consent"}]
    assert r.meeting_files() == []


def test_the_bots_own_announcement_in_the_chat_is_not_an_objection(tmp_path):
    r = Rig(tmp_path)
    r.announce()
    r.adapter.chat_in = [ChatMessage("TaDaMoo Bot", r.adapter.chat_out[0]), ChatMessage("You", "say or type stop"), ChatMessage("", r.adapter.chat_out[0])]
    r.run(3)
    assert not r.session._ending and r.session.gate.state == consent.WAITING


def test_the_bots_own_voice_coming_back_is_not_an_objection(tmp_path):
    r = Rig(tmp_path, make_plan(speak=True))
    r.announce()
    r.stt.say("If anyone objects, say or type stop and I will leave and delete what I heard")
    r.run(3)
    assert not r.session._ending


def test_host_removal_during_the_window_deletes_and_ends(tmp_path):
    r = Rig(tmp_path)
    r.announce()
    r.stt.say("welcome all")
    r.run(2)
    r.adapter.in_call_now, r.adapter.exit = False, "removed"
    r.run(40)
    assert r.session.end_reason == "removed"
    r.finish()
    assert r.events_named("end")[0]["outcome"] == "removed" and r.meeting_files() == [] and r.session.transcript.segments == []


def test_a_call_that_drops_while_consent_is_asked_counts_as_removal(tmp_path):
    r = Rig(tmp_path)
    r.announce()
    r.adapter.in_call_now = False
    r.run(40)
    assert r.session.end_reason == "removed"


def test_the_leave_button_before_consent_deletes_everything(tmp_path):
    r = Rig(tmp_path)
    r.announce()
    r.stt.say("some words that are held in memory")
    r.run(2)
    assert r.session.transcript.segments
    r.session.leave()
    r.run(2)
    assert r.session.end_reason == "left_before_consent"
    r.finish()
    assert r.session.transcript.segments == [] and r.meeting_files() == [] and r.finished == []
    assert r.events_named("end")[0]["outcome"] == "left"


def test_silence_in_window_mode_stays_and_keeps_the_transcript(tmp_path):
    r = Rig(tmp_path, make_plan(consent_window_s=20))
    r.announce()
    r.stt.say("first words")
    r.run(10)
    assert r.meeting_files() == [] and r.session.gate.state == consent.WAITING
    r.run(11)
    s = r.session
    assert s.gate.state == consent.GRANTED and s.gate.outcome == "granted_window"
    assert sorted(r.meeting_files()) == [f"{s.session_id}/meta.json", f"{s.session_id}/transcript.jsonl"]
    r.stt.say("second words")
    r.run(2)
    lines = [json.loads(x) for x in (r.folder / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["text"] for x in lines] == ["first words", "second words"]
    meta = json.loads((r.folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["url"] == "https://meet.google.com/abc-defg-hij" and "secret" not in json.dumps(meta)
    r.session.leave()
    r.run(2)
    r.finish()
    assert len(r.finished) == 1 and r.finished[0][0]["segments"] == 2 and r.events_named("end")[0]["kept"] is True
    assert r.events_named("consent")[0]["outcome"] == "granted_window"


def test_explicit_mode_without_a_yes_leaves_after_twice_the_window_and_deletes(tmp_path):
    r = Rig(tmp_path, make_plan(consent_mode="explicit", consent_window_s=20))
    r.announce()
    r.stt.say("we are talking about numbers")
    r.run(30)
    assert not r.session._ending and r.meeting_files() == []
    r.run(15)
    assert r.session.end_reason == "no_consent"
    r.finish()
    assert r.meeting_files() == [] and r.session.transcript.segments == [] and r.events_named("end")[0]["outcome"] == "no_consent"
    assert any("leav" in m for m in r.adapter.chat_out[1:])           # said goodbye in the chat


def test_explicit_mode_with_a_yes_stays(tmp_path):
    r = Rig(tmp_path, make_plan(consent_mode="explicit", consent_window_s=20))
    r.announce()
    assert '"ok"' in r.adapter.chat_out[0]
    r.stt.say("ok, fine by us")
    r.run(2)
    assert r.session.gate.outcome == "granted_explicit" and r.meeting_files()


def test_an_objection_after_consent_deletes_the_transcript_and_leaves_a_stub(tmp_path):
    r = Rig(tmp_path, make_plan(consent_window_s=20))
    r.announce()
    r.run(21)
    r.stt.say("an hour of meeting text")
    r.run(2)
    assert (r.folder / "transcript.jsonl").exists()
    r.stt.say("please remove the bot")
    r.run(2)
    r.finish()
    assert r.session.end_reason == "objection"
    assert r.meeting_files() == [f"{r.session.session_id}/meta.json"]
    meta = json.loads((r.folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["deleted"] is True and meta["end_reason"] == "objection" and "segments" not in meta
    assert r.finished == [] and r.session.transcript.segments == []


def test_a_participant_who_joins_later_gets_a_short_announcement(tmp_path):
    r = Rig(tmp_path)
    r.announce()
    r.run(10)
    assert len(r.adapter.chat_out) == 1
    r.adapter.participants = ["Ada", "Bruno", "Carla"]
    r.run(2)
    assert len(r.adapter.chat_out) == 2 and "stop" in r.adapter.chat_out[1] and "AI assistant" in r.adapter.chat_out[1]
    assert r.events_named("reannounced")[0]["chat"] is True
    r.adapter.participants = ["Ada", "Bruno", "Carla", "Dino"]
    r.run(5)
    assert len(r.adapter.chat_out) == 2                    # not again within the gap
    r.run(30)
    assert len(r.adapter.chat_out) == 3


def test_the_bots_own_tile_is_not_a_new_participant(tmp_path):
    r = Rig(tmp_path)
    r.adapter.participants = ["Ada", "TaDaMoo Bot"]
    r.announce()
    r.adapter.participants = ["Ada", "TaDaMoo Bot", "Bruno"]
    r.run(2)
    assert len(r.adapter.chat_out) == 2
    r.adapter.participants = ["Ada", "TaDaMoo Bot", "Bruno", "tadamoo bot"]
    r.run(40)
    assert len(r.adapter.chat_out) == 2


def test_rights_must_be_confirmed(tmp_path):
    with pytest.raises(ValueError):
        Rig(tmp_path, make_plan(rights=""))


def test_the_meeting_ending_normally_after_consent_keeps_the_minutes(tmp_path):
    r = Rig(tmp_path, make_plan(consent_window_s=20))
    r.announce()
    r.run(21)
    r.stt.say("decision: ship on Friday")
    r.run(2)
    r.adapter.in_call_now, r.adapter.exit = False, "ended"
    r.run(20)
    assert r.session.end_reason == "meeting_ended"
    r.finish()
    assert len(r.finished) == 1 and (r.folder / "transcript.jsonl").exists()
