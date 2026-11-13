"""The voice's time: the XTTS process that stays loaded between batches, the rounds that read again only the sentences with no right take, the numbers the CTC read-back cannot judge, and
the speech-to-text worker that is reloaded once when its CUDA context is dead."""
import json
import sys
import textwrap

from tadamoo.speech import clausevoice, voicecheck

FAKE = textwrap.dedent('''
    import json, os, sys, wave
    def run(path):
        job = json.load(open(path, encoding="utf-8"))
        os.makedirs(job["out_dir"], exist_ok=True)
        for it in job["items"]:
            w = wave.open(os.path.join(job["out_dir"], it["id"] + ".wav"), "wb")
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\\0\\0" * 2400); w.close()
            print(json.dumps({"id": it["id"], "pid": os.getpid()}), flush=True)
        print(json.dumps({"batch": "done"}), flush=True)
    for line in sys.stdin:
        if line.strip():
            run(line.strip())
''')


def test_the_xtts_process_loads_once_for_all_the_batches(tmp_path, monkeypatch):
    fake = tmp_path / "fake_xtts.py"
    fake.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(clausevoice, "RUNNER", fake)
    p = clausevoice.Params(xtts_python=sys.executable, xtts_model="m")
    runner = clausevoice.LocalXtts(p, tmp_path / "work")
    item = lambda n: {"id": f"c0_s{n}", "text": "Jó napot", "lang": "hu", "seed": n}
    try:
        first = runner.run([item(1), item(2)])
        pid = runner.proc.pid
        second = runner.run([item(1), item(3)])                 # one take is there already and is not made again
        assert runner.proc.pid == pid and runner.batches == 2
        assert set(first) == {"c0_s1", "c0_s2"} and set(second) == {"c0_s1", "c0_s3"}
    finally:
        runner.close()
    assert runner.proc is None


def test_a_process_that_dies_is_an_error_with_the_last_line_of_its_log(tmp_path, monkeypatch):
    fake = tmp_path / "dies.py"
    fake.write_text("import sys\nsys.stderr.write('CUDA out of memory\\n')\nsys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(clausevoice, "RUNNER", fake)
    runner = clausevoice.LocalXtts(clausevoice.Params(xtts_python=sys.executable), tmp_path / "work")
    try:
        runner.run([{"id": "c0_s1", "text": "x", "lang": "hu", "seed": 1}])
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    finally:
        runner.close()
    assert raised and "CUDA out of memory" in raised


def test_a_sentence_with_a_right_take_is_not_read_again():
    p = clausevoice.Params()
    assert p.seeds * p.rounds >= 36 and p.seeds <= 4


def test_a_number_the_normaliser_spelled_is_found_and_ordinary_words_are_not():
    has = clausevoice.has_number_words
    assert has("Berlin nem hagyja jóvá a kétezer-huszonnyolc és kétezer-harmincnégy közötti", "hu")
    assert has("nagyjából egy milliárd euró", "hu")
    assert not has("hat hét múlva egy ember százalék hatás hétfő", "hu")
    assert not has("two thousand twenty eight", "en")


def test_the_letters_of_a_clause_with_a_number_are_not_held_against_the_voice():
    class Ctc:
        def transcribe(self, wav, lang):
            return "berlin nem hagyja jóvá a two thousandand huszonnyolc"

    class Judge:
        profile = type("P", (), {"identity_floor": 0.0, "f0_std_semitones": 0.0})()
        targets = type("T", (), {"f0_std_over": 9.0})()

        def score(self, wav, text, similarity, lang):
            return type("V", (), {"numbers": {"identity": 0.99, "pace": 5.2, "f0_hz": 200.0, "f0_std_semitones": 1.0}})()

    rater = clausevoice.Rater("http://x", Ctc(), Judge(), clausevoice.Params())
    import io
    import wave
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(b"\x00\x00" * 24000)
    w.close()
    orig = voicecheck.transcribe
    voicecheck.transcribe = lambda url, wav, lang, key="": [{"text": t, "start_s": i * 0.3, "end_s": i * 0.3 + 0.3} for i, t in enumerate("Berlin nem hagyja jóvá a 2028".split())]
    try:
        m = rater.measure(buf.getvalue(), "berlin nem hagyja jóvá a kétezer-huszonnyolc", ",")
    finally:
        voicecheck.transcribe = orig
    assert m["numbers"] is True and m["letters"] == m["similarity"]


def test_a_dead_cuda_context_reloads_the_worker_once_and_a_second_failure_is_raised(monkeypatch):
    calls = []
    answers = iter([OSError("the speech-to-text refused (HTTP 500): trascrizione fallita: CUDA error: unspecified launch failure"), [{"text": "ok"}]])

    def fake(url, wav, lang, key=""):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(voicecheck, "_service_words", fake)
    monkeypatch.setattr(voicecheck, "RELOAD_STT", lambda: calls.append("reload") or "HTTP 200")
    monkeypatch.setattr(voicecheck, "WAIT_FOR_SERVICE_S", 3600.0)
    assert voicecheck.transcribe("u", b"", "hu") == [{"text": "ok"}]
    assert calls == ["reload"]
    bad = OSError("the speech-to-text refused (HTTP 500): CUDA error: unspecified launch failure")
    monkeypatch.setattr(voicecheck, "_service_words", lambda *a, **k: (_ for _ in ()).throw(bad))
    try:
        voicecheck.transcribe("u", b"", "hu")
        assert False, "the failure after a reload is raised"
    except OSError as exc:
        assert "CUDA" in str(exc)
    assert calls == ["reload", "reload"]


def test_the_refusal_carries_the_services_own_words(monkeypatch):
    class R:
        status = 500

        def read(self, n):
            return json.dumps({"message": "trascrizione fallita: CUDA error"}).encode()

    class C:
        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return R()

        def close(self):
            pass

    monkeypatch.setattr(voicecheck.http.client, "HTTPConnection", C)
    try:
        voicecheck._service_words("http://127.0.0.1:1", b"", "hu")
    except OSError as exc:
        assert "HTTP 500" in str(exc) and "CUDA error" in str(exc)
