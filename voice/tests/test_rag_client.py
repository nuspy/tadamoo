"""The knowledge archives for the voice functions, from the voice service's side: the request to the server with its time budget, the markers that are never silent,
and the consent of other people's ears (a question once, remembered for the session, answered in the dialog or by voice on a private channel)."""
import pytest

from callbridge import rag_client
from callbridge.config import Config
from callbridge.rag_client import Knowledge, VoiceAsker, parse_choice, question_text
from callbridge.tadamoo_api import ApiError

BLOCK = '<untrusted_data source="knowledge-archive" id="0123456789abcdef">\n[Boats / sailing.txt]\nthe keel keeps the boat upright\n</untrusted_data id="0123456789abcdef">'


class RagApi:
    """The server's voice routes in memory."""

    def __init__(self):
        self.calls, self.fail, self.replies, self.pending, self.sources = [], None, [], [], []
        self.answers = {}
        self.n = 0

    def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        if self.fail:
            raise self.fail
        if path == rag_client.PASSAGES:
            reply = self.replies.pop(0) if self.replies else {"block": BLOCK, "sources": self.sources, "pending": self.pending, "marker": "", "profile": "hybrid-small", "elapsed_ms": 40}
            return reply
        if path == rag_client.ASK:
            self.n += 1
            return {"question": {"id": f"q{self.n}", "state": "pending"}}
        if path.startswith("/api/voice/rag/ask/") and path.endswith("/answer"):
            self.answers[path.split("/")[-2]] = body["answer"]
            return {"question": {}}
        return {}

    def get(self, path):
        self.calls.append(("GET", path, None))
        qid = path.rsplit("/", 1)[-1]
        if qid in self.answers:
            return {"question": {"id": qid, "state": "answered", "answer": self.answers[qid], "by": "dialog"}}
        return {"question": {"id": qid, "state": "pending"}}

    def sent(self, path):
        return [c[2] for c in self.calls if c[1] == path]


def know(api=None, function="meeting", archives=("boats-1",), **kw):
    api = api or RagApi()
    k = Knowledge(Config(), function, archives, session="s1", api=api, spawn=lambda fn, kind: fn() if kind == "aloud" else k.jobs.append(fn), sleep=lambda s: None, **kw)
    k.jobs = []                                  # the watchers wait for the tests to say when the person answered
    return k, api


PENDING = [{"archive_id": "boats-1", "archive": "Boats", "ask": "dialog", "preview": "the keel keeps the boat upright"}]


def test_the_passages_are_asked_for_with_the_function_the_archives_and_the_functions_time_budget():
    k, api = know(function="copilot")
    got = k.fetch("how does the keel of a sloop work", how="screen")
    body = api.sent(rag_client.PASSAGES)[0]
    assert got.block == BLOCK and got.profile == "hybrid-small" and got.elapsed_ms == 40 and got.marker == ""
    assert body == {"function": "copilot", "archives": ["boats-1"], "query": "how does the keel of a sloop work", "how": "screen", "granted": [], "denied": [], "budget_ms": 1200}
    assert know(function="call")[0].function == "call" and rag_client.BUDGET_MS["call"] < rag_client.BUDGET_MS["meeting"] < rag_client.BUDGET_MS["minutes"]


def test_nothing_is_asked_for_a_short_utterance_or_without_archives():
    k, api = know()
    assert k.fetch("yes ok").block == "" and k.fetch("   ").block == "" and api.calls == []
    off, api2 = know(archives=())
    assert not off.enabled and off.fetch("how does the keel of a sloop work").block == "" and api2.calls == []
    k.warm()
    assert api.sent(rag_client.WARM) == [{"archives": ["boats-1"]}] and off.warm() is None and api2.calls == []
    minutes, api3 = know(function="minutes")
    assert minutes.fetch("keel").block == BLOCK and api3.sent(rag_client.PASSAGES)[0]["budget_ms"] == 20000        # a minutes segment is searched whatever its length


def test_a_server_that_does_not_answer_gives_no_passages_and_says_so_never_silently():
    api = RagApi()
    api.fail = ApiError("TaDaMoo does not answer (OSError)")
    k, _ = know(api)
    got = k.fetch("how does the keel of a sloop work")
    assert got.block == "" and got.timed_out and "did not answer" in got.marker
    api2 = RagApi()
    api2.replies = [{"block": "", "marker": "words only: the semantic search took more than 1200 ms", "timed_out": True, "sources": [], "pending": []}]
    late, _ = know(api2)
    assert "took more than 1200 ms" in late.fetch("how does the keel of a sloop work").marker
    warm_fail = RagApi()
    warm_fail.fail = ApiError("down")
    know(warm_fail)[0].warm()                                                          # warming never fails a session


# -- the consent of other people's ears
def test_an_archive_that_asks_is_asked_about_once_and_the_answer_is_kept_for_the_session():
    k, api = know()
    api.pending = PENDING
    got = k.fetch("how does the keel of a sloop work")
    asks = api.sent(rag_client.ASK)
    assert got.pending == PENDING and asks == [{"session": "s1", "function": "meeting", "archive_id": "boats-1", "preview": "the keel keeps the boat upright", "how": "spoken", "voice_ok": False}]
    k.fetch("and the mast of the sloop and its sails")
    assert len(api.sent(rag_client.ASK)) == 1                                       # the same question is not put twice while it waits
    api.answers["q1"] = "meeting"
    k._watch("boats-1", "q1")
    api.pending = []
    k.fetch("what about the rigging of the boat today")
    assert api.sent(rag_client.PASSAGES)[-1]["granted"] == ["boats-1"]
    k.fetch("and one more question about the sloop")
    assert api.sent(rag_client.PASSAGES)[-1]["granted"] == ["boats-1"] and len(api.sent(rag_client.ASK)) == 1                # for the rest of the meeting


def test_this_time_is_used_up_by_the_passage_that_used_it_and_a_no_stays_a_no():
    k, api = know()
    api.pending = PENDING
    k.fetch("how does the keel of a sloop work")
    api.answers["q1"] = "once"
    k._watch("boats-1", "q1")
    api.pending, api.sources = [], [{"archive_id": "boats-1", "archive": "Boats", "others_ok": "ask_dialog"}]
    k.fetch("what about the rigging of the boat today")
    assert api.sent(rag_client.PASSAGES)[-1]["granted"] == ["boats-1"]
    k.fetch("and one more question about the sloop")
    assert api.sent(rag_client.PASSAGES)[-1]["granted"] == []
    api.pending = PENDING
    k.fetch("a third question about the keel of the boat")
    api.answers["q2"] = "no"
    k._watch("boats-1", "q2")
    k.fetch("a fourth question about the keel of the boat")
    assert api.sent(rag_client.PASSAGES)[-1]["denied"] == ["boats-1"] and len(api.sent(rag_client.ASK)) == 2


def test_a_question_nobody_answered_is_not_put_again_at_once_and_is_not_a_no():
    clock = [0.0]
    k, api = know()
    k._clock = lambda: clock[0]
    api.pending = PENDING
    k.fetch("how does the keel of a sloop work")
    k._apply("boats-1", {"answer": "no", "by": "timeout"})
    assert "boats-1" not in k.denied
    k.fetch("another question about the keel of a boat")
    assert len(api.sent(rag_client.ASK)) == 1
    clock[0] += rag_client.RETRY_AFTER_S + 1
    k.fetch("another question about the keel of a boat")
    assert len(api.sent(rag_client.ASK)) == 2


def test_an_archive_that_asks_by_voice_is_asked_aloud_only_when_there_is_a_private_channel():
    said, heard = [], ["questo e' per questa riunione"]
    asker = VoiceAsker(lambda t: said.append(t) or True, lambda s: heard.pop(0) if heard else None, lang="it")
    k, api = know(asker=asker)
    api.pending = [{**PENDING[0], "ask": "voice"}]
    k.fetch("how does the keel of a sloop work")
    assert api.sent(rag_client.ASK)[0]["voice_ok"] is True and "L'archivio Boats" in said[0] and api.answers == {"q1": "meeting"}          # the answer by voice goes to the server
    plain, api2 = know()
    api2.pending = [{**PENDING[0], "ask": "voice"}]
    plain.fetch("how does the keel of a sloop work")
    assert api2.sent(rag_client.ASK)[0]["voice_ok"] is False and api2.answers == {}                                    # no private channel: the dialog alone, which says why


def test_a_voice_that_cannot_be_played_or_understood_leaves_the_dialog_to_answer():
    assert VoiceAsker(lambda t: False, lambda s: "yes").ask("Boats", "x", "spoken") is None
    heard = ["mumble", "what"]
    assert VoiceAsker(lambda t: True, lambda s: heard.pop(0) if heard else None).ask("Boats", "x", "spoken") is None and heard == []                    # two tries
    assert VoiceAsker(lambda t: True, lambda s: (_ for _ in ()).throw(OSError("no microphone"))).ask("Boats", "x", "spoken") is None
    assert VoiceAsker(lambda t: True, lambda s: "sì").ask("Boats", "x", "spoken") == "once"


@pytest.mark.parametrize("words,expected", [("yes", "once"), ("sì", "once"), ("ok, this time", "once"), ("questa volta", "once"), ("for this meeting", "meeting"), ("per questa riunione", "meeting"),
                                            ("va bene per tutta la chiamata", "meeting"), ("always for this archive", "always"), ("sempre", "always"), ("no", "no"), ("non adesso", "no"),
                                            ("never", "no"), ("not always", "no"), ("blue", None), ("", None)])
def test_the_answer_in_the_persons_words_in_english_or_italian(words, expected):
    assert parse_choice(words) == expected


def test_the_question_names_the_archive_and_what_would_be_output_in_the_persons_language():
    en, it = question_text("Boats", "the keel keeps the boat upright", "written", "en"), question_text("Boats", "la chiglia tiene la barca", "spoken", "it")
    assert "Boats" in en and "written" in en and "keel keeps" in en and "for this meeting" in en and "always for this archive" in en
    assert "Boats" in it and "la chiglia" in it and "per questa riunione" in it and "sempre per questo archivio" in it


# -- the copilot's own consent: only when a suggestion is really output
def test_a_suggestion_of_the_private_window_asks_only_when_it_is_said_to_the_others():
    k, api = know(function="copilot")
    src = lambda setting: [{"archive_id": "boats-1", "archive": "Boats", "others_ok": setting}]
    assert k.confirm_output(src("allow")) == (True, "") and api.calls == []
    ok, why = k.confirm_output(src("deny"))
    assert not ok and "not to be said" in why and api.calls == []
    api.answers["q1"] = "meeting"
    assert k.confirm_output(src("ask_dialog")) == (True, "") and api.sent(rag_client.ASK)[0]["how"] == "spoken"
    assert k.confirm_output(src("ask_dialog")) == (True, "") and len(api.sent(rag_client.ASK)) == 1                # for this meeting: not asked again
    k2, api2 = know(function="copilot")
    api2.answers["q1"] = "no"
    ok, why = k2.confirm_output(src("ask_dialog"))
    assert not ok and "Boats" in why
    ok, why = k2.confirm_output(src("ask_voice"))
    assert not ok and "you answered no" in why
    k3, api3 = know(function="copilot")
    ok, why = k3.confirm_output(src("ask_dialog"), wait_s=0.0)                         # nobody answered
    assert not ok and "no answer yes" in why
    k4, api4 = know(function="copilot")
    api4.answers["q1"] = "once"
    assert k4.confirm_output(src("ask_dialog")) == (True, "") and "boats-1" not in k4.once
    api4.answers["q2"] = "always"
    assert k4.confirm_output(src("ask_dialog"), "written") == (True, "") and "boats-1" in k4.granted
