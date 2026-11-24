"""The voice's time: the XTTS process that stays loaded between batches, the rounds that read again only the sentences with no right take, the numbers the CTC read-back cannot judge, and
the speech-to-text worker that is reloaded once when its CUDA context is dead."""
import json
import sys
import textwrap

import pytest

pytest.importorskip("librosa")                            # the voiceprint's library, which the clause measure runs

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

        def identity(self, wav):
            return 0.99

        def score(self, wav, text, similarity, lang, known=None):
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


def test_the_seeds_of_a_sentence_are_one_group_and_a_group_is_cut_at_the_batch_size():
    from tadamoo.speech import xtts_run
    items = [{"id": f"a{k}", "text": "egy", "seed": k} for k in range(5)] + [{"id": f"b{k}", "text": "kettő", "seed": k} for k in range(2)] + [{"id": "c0", "text": "egy", "seed": 9, "speed": 1.2}]
    groups = xtts_run.group_items(items, 3)
    assert [[i["id"] for i in g] for g in groups] == [["a0", "a1", "a2"], ["a3", "a4"], ["b0", "b1"], ["c0"]]


def test_a_take_of_a_batch_ends_at_its_first_stop_token():
    from tadamoo.speech import xtts_run
    assert xtts_run.take_end([5, 6, 8193, 8193, 8193], 8193) == 3
    assert xtts_run.take_end([5, 6, 7], 8193) == 3


def test_a_sentence_with_one_take_missing_is_made_again_whole_and_the_job_carries_the_batch_and_the_precision(tmp_path, monkeypatch):
    fake = tmp_path / "fake_xtts.py"
    fake.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(clausevoice, "RUNNER", fake)
    p = clausevoice.Params(xtts_python=sys.executable, xtts_model="m", xtts_batch=4, xtts_precision="bf16")
    runner = clausevoice.LocalXtts(p, tmp_path / "work")
    item = lambda text, n: {"id": f"{text}_s{n}", "text": text, "lang": "hu", "seed": n}
    try:
        runner.run([item("egy", 1), item("egy", 2), item("ketto", 3)])
        job = json.loads((tmp_path / "work" / "job.json").read_text(encoding="utf-8"))
        assert job["batch"] == 4 and job["precision"] == "bf16" and len(job["items"]) == 3
        (tmp_path / "work" / "out" / "egy_s2.wav").unlink()                        # one take of "egy" is gone: both are made again, "ketto" is kept
        runner.run([item("egy", 1), item("egy", 2), item("ketto", 3)])
        assert [i["id"] for i in json.loads((tmp_path / "work" / "job.json").read_text(encoding="utf-8"))["items"]] == ["egy_s1", "egy_s2"]
    finally:
        runner.close()


def test_a_group_of_takes_made_by_one_production_is_found_again_by_another_in_the_shared_folder(tmp_path, monkeypatch):
    fake = tmp_path / "fake_xtts.py"
    fake.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(clausevoice, "RUNNER", fake)
    p = clausevoice.Params(xtts_python=sys.executable, xtts_model="m", xtts_cache=str(tmp_path / "shared"))
    item = lambda text, n: {"id": f"{text}_s{n}", "text": text, "lang": "hu", "seed": n}
    first = clausevoice.LocalXtts(p, tmp_path / "work1")
    try:
        made = first.run([item("egy", 1), item("egy", 2)])
    finally:
        first.close()
    assert first.batches == 1
    second = clausevoice.LocalXtts(p, tmp_path / "work2")
    try:
        found = second.run([{**item("egy", 1), "id": "c9_s1"}, {**item("egy", 2), "id": "c9_s2"}])        # another production: other ids, same text and seeds
        other = second.run([item("egy", 7)])                                                              # other seeds are made
    finally:
        second.close()
    assert second.batches == 1 and found["c9_s1"] == made["egy_s1"] and found["c9_s2"] == made["egy_s2"] and set(other) == {"egy_s7"}


def test_a_run_that_takes_longer_than_its_time_is_stopped_with_a_message(tmp_path, monkeypatch):
    slow = tmp_path / "slow.py"
    slow.write_text("import sys, time\nfor line in sys.stdin:\n    time.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(clausevoice, "RUNNER", slow)
    runner = clausevoice.LocalXtts(clausevoice.Params(xtts_python=sys.executable, xtts_model="m"), tmp_path / "work")
    try:
        runner.run([{"id": "c0_s1", "text": "x", "lang": "hu", "seed": 1}], timeout=1.5)
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    finally:
        runner.close()
    assert raised and "took longer than 2 s and was stopped" in raised


def test_the_seeds_of_a_sentence_follow_its_text_and_not_its_place():
    p = clausevoice.Params()
    runs = []

    class Runner:
        def run(self, items, timeout=None):
            runs.append(items)
            raise clausevoice.VoiceFailed("stop here", [])

    for text in ("Egy két. Jó napot kívánok.", "Jó napot kívánok. Egy két."):
        try:
            clausevoice.speak(text, Runner(), None, p, lambda m: None)
        except clausevoice.VoiceFailed:
            pass
    seeds = lambda items, word: sorted(i["seed"] for i in items if i["text"].startswith(word))
    assert seeds(runs[0], "Jó napot") == seeds(runs[1], "Jó napot") and seeds(runs[0], "Egy") == seeds(runs[1], "Egy")


def test_with_a_timing_a_run_is_limited_by_the_audio_it_makes_and_a_good_run_is_recorded(tmp_path, monkeypatch):
    from tadamoo import jobtime
    timing = jobtime.Timing(tmp_path / "times.json", {"time_min_s": 60})
    said = textwrap.dedent('''
        import json, os, sys
        for line in sys.stdin:
            job = json.load(open(line.strip(), encoding="utf-8"))
            os.makedirs(job["out_dir"], exist_ok=True)
            for it in job["items"]:
                open(os.path.join(job["out_dir"], it["id"] + ".wav"), "wb").write(b"x")
                print(json.dumps({"id": it["id"], "seconds": 6.0}), flush=True)
            print(json.dumps({"batch": "done"}), flush=True)
    ''')
    fake = tmp_path / "fake.py"
    fake.write_text(said, encoding="utf-8")
    monkeypatch.setattr(clausevoice, "RUNNER", fake)
    runner = clausevoice.LocalXtts(clausevoice.Params(xtts_python=sys.executable, xtts_model="m"), tmp_path / "work", timing)
    try:
        runner.run([{"id": "c0_s1", "text": "Jó napot", "lang": "hu", "seed": 1}, {"id": "c0_s2", "text": "Jó napot", "lang": "hu", "seed": 2}])
    finally:
        runner.close()
    assert timing.ratio("voice")[1] == 1 and timing.ratio("voice")[0] > 0                    # twelve seconds of audio made: one ratio kept
    slow = tmp_path / "slow2.py"
    slow.write_text("import sys, time\nfor line in sys.stdin:\n    time.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(clausevoice, "RUNNER", slow)
    stuck = clausevoice.LocalXtts(clausevoice.Params(xtts_python=sys.executable, xtts_model="m"), tmp_path / "work2", jobtime.Timing(tmp_path / "t2.json", {"time_min_s": 1}))
    try:
        stuck.run([{"id": "c0_s1", "text": "x", "lang": "hu", "seed": 1}])
        raised = None
    except jobtime.Overrun as exc:
        raised = str(exc)
    finally:
        stuck.close()
    assert raised and "took longer than 1 seconds" in raised and "was ended" in raised


def test_the_wait_for_the_speech_to_text_is_the_limit_of_the_audio_and_its_end_is_an_overrun(monkeypatch, tmp_path):
    import io
    import wave
    from tadamoo import jobtime
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000)
    monkeypatch.setattr(voicecheck, "TIMING", jobtime.Timing(tmp_path / "t.json", {"time_min_s": 0.3}))
    monkeypatch.setattr(voicecheck, "RETRY_EVERY_S", 0.1)

    def refuse(*a, **k):
        raise OSError("the speech-to-text does not answer")

    monkeypatch.setattr(voicecheck, "_service_words", refuse)
    try:
        voicecheck.transcribe("http://x", buf.getvalue(), "hu")
        raised = None
    except jobtime.Overrun as exc:
        raised = str(exc)
    assert raised and "was ended" in raised


def test_the_takes_of_a_round_are_rated_at_the_same_time_and_come_back_in_their_order():
    import threading
    import time
    seen, peak, now = [], [0], [0]
    lock = threading.Lock()
    rater = clausevoice.Rater.__new__(clausevoice.Rater)

    def rate(wav, text, ending, seed, partial_ok=False):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.05)
        with lock:
            now[0] -= 1
        return seed

    rater.rate = rate
    rater.cache, rater.p = {}, clausevoice.Params()
    rater._cheap = lambda wav, text: {}
    rater.cheap_rejects = lambda m: []
    jobs = [(b"", "x", ".", n) for n in range(6)]
    assert rater.rate_many(jobs, 4) == list(range(6)) and peak[0] > 1
    peak[0] = 0
    assert rater.rate_many(jobs, 1) == list(range(6)) and peak[0] == 1


def test_a_local_speech_to_text_stands_in_for_the_server_and_the_words_are_kept_with_the_rating(monkeypatch):
    class Local:
        def words(self, wav, lang):
            return [{"text": "Jó", "start": 0.0, "end": 0.2}, {"text": "napot", "start": 0.25, "end": 0.6}]

    monkeypatch.setattr(voicecheck, "LOCAL_STT", Local())
    monkeypatch.setattr(voicecheck, "_service_words", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the server is not asked")))
    assert [w["text"] for w in voicecheck.transcribe("http://x", b"", "hu")] == ["Jó", "napot"]


def test_the_server_is_up_when_it_answers_and_not_when_nothing_listens():
    import http.server
    import threading
    from tadamoo.speech import service

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert service.server_up(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        port = server.server_address[1]
        server.server_close()
    assert not service.server_up(f"http://127.0.0.1:{port}", timeout=0.5)


def test_the_profile_of_a_voice_can_be_read_from_the_local_xtts_two_readings_to_a_text(tmp_path):
    made = []

    class Runner:
        def run(self, items):
            made.append([it["seed"] for it in items])
            return {it["id"]: f"wav{it['seed']}".encode() for it in items}

    synth = clausevoice.local_synth(Runner(), clausevoice.Params())
    assert synth("Egy,") != synth("Egy,") and len(made) == 1                    # the second reading of a text comes from the same pass
    synth("Kettő,")
    assert len(made) == 2 and len(set(made[0])) == 2


def test_the_rating_goes_to_the_gpu_only_when_it_has_room():
    from tadamoo.speech import localstt
    assert localstt.pick_device("cpu") == "cpu" and localstt.pick_device("cuda") == "cuda"
    assert localstt.pick_device("auto", need_gib=10000.0) == "cpu"


def test_a_take_the_cheap_measures_reject_is_not_read_back_and_a_later_call_completes_it(monkeypatch):
    import io
    import wave
    asked = []

    class Ctc:
        def __init__(self, heard):
            self.heard = heard

        def transcribe(self, wav, lang):
            return self.heard

    class Judge:
        profile = type("P", (), {"identity_floor": 0.5, "f0_std_semitones": 0.0})()
        targets = type("T", (), {"f0_std_over": 9.0})()

        def identity(self, wav):
            return 0.9

        def score(self, wav, text, similarity, lang, known=None):
            return type("V", (), {"numbers": {"identity": 0.9, "pace": 5.2, "f0_hz": 200.0, "f0_std_semitones": 1.0}})()

    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(b"\x00\x00" * 24000)
    w.close()
    wav = buf.getvalue()
    monkeypatch.setattr(voicecheck, "transcribe", lambda url, wav, lang, key="": asked.append(1) or [{"text": "jó", "start": 0.0, "end": 0.3}, {"text": "napot", "start": 0.3, "end": 0.6}])
    monkeypatch.setattr(clausevoice.voiceprint, "pace", lambda wav, text, lang: 5.2)
    monkeypatch.setattr(clausevoice.voiceprint, "f0", lambda wav: {"median_hz": 200.0, "std_semitones": 1.0, "voiced": 1.0})
    wrong = clausevoice.Rater("http://x", Ctc("zzzzzz"), Judge(), clausevoice.Params())
    m = wrong.measure(wav, "jó napot", ",", partial_ok=True)
    assert m["partial"] and not asked and not wrong.judge_take(m, 1, wav, "jó napot").ok and "letters" in wrong.judge_take(m, 1, wav, "jó napot").why[0]
    full = wrong.measure(wav, "jó napot", ",")                                       # asked for in full: the read-back is made now
    assert not full.get("partial") and asked == [1] and "similarity" in full
    right = clausevoice.Rater("http://x", Ctc("jonapot"), Judge(), clausevoice.Params())
    assert not right.measure(wav, "jó napot", ",", partial_ok=True).get("partial") and asked == [1, 1]
