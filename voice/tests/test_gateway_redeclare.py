"""A device declares its render modes again on an open connection: same validation as in auth, only from an authenticated socket, a limit per
connection, the effective set in the answer, and the new set served from then on. Fake WebSocket and host."""
from callbridge import voice_gateway as vg
from voice.tests.test_gateway import CLOSED, TOKEN, FakeWs
from voice.tests.test_gateway_render_modes import BOTH, auth, connect, host_with


def declare(modes, **extra):
    return {"type": "render_modes", "modes": modes, **extra}


def answers(ws):
    return ws.json("render_modes")


def test_a_device_that_lost_wav2lip_is_served_visemes_from_then_on():
    host = host_with(BOTH)
    ws = connect(host, auth("wav2lip", "visemes"), [declare(["visemes"]), CLOSED])
    assert ws.json("ready")[0]["render_mode"] == "wav2lip"
    (answer,) = answers(ws)
    assert answer["modes"] == ["visemes"] and answer["render_mode"] == "visemes" and answer["avatar_name"] == "ada"
    assert answer["engine"] == "visemes"


def test_a_passed_self_test_adds_wav2lip_and_it_goes_first():
    ws = connect(host_with(BOTH), auth("visemes"), [declare(["wav2lip", "visemes"]), CLOSED])
    (answer,) = answers(ws)
    assert answer["modes"] == ["wav2lip", "visemes"] and answer["render_mode"] == "wav2lip" and answer["engine"] == "wav2lip_gan96"


def test_the_new_modes_decide_what_the_next_turn_sends():
    host = host_with(BOTH)
    host.tts = type(host.tts)(segments=1, samples=4800)
    script = [declare(["visemes"]), {"type": "start", "mode": "hold", "lang": "en", "sample_rate": 16000}, b"\x00\x00" * 800, {"type": "end"}, CLOSED]
    ws = connect(host, auth("wav2lip", "visemes"), script)
    assert "visemes" in ws.kinds


def test_unknown_repeated_and_non_string_modes_are_dropped_and_the_video_is_last():
    host = host_with(BOTH, avatar=True)
    ws = connect(host, auth("visemes"), [declare(["video", "bogus", 7, None, "visemes", "visemes", "wav2lip"]), CLOSED])
    assert answers(ws)[0]["modes"] == ["visemes", "wav2lip", "video"]


def test_only_what_the_avatar_files_allow_is_offered_and_an_empty_list_is_voice_only():
    ws = connect(host_with({"avatar": "ada", "render_modes": ["visemes"]}), auth("wav2lip", "visemes"), [declare(["wav2lip", "visemes"]), declare([]), CLOSED])
    first, second = answers(ws)
    assert first["modes"] == ["visemes"]
    assert (second["modes"], second["render_mode"], second["engine"], second["resolution"]) == ([], "none", "", 0)


def test_a_list_longer_than_the_limit_is_read_only_up_to_the_limit():
    padding = ["bogus"] * vg.MODES_READ_MAX
    ws = connect(host_with(BOTH), auth("visemes"), [declare([*padding, "wav2lip"]), CLOSED])
    assert answers(ws)[0]["modes"] == []


def test_engine_and_resolution_are_validated_like_in_auth_and_kept_when_absent():
    host = host_with(BOTH)
    ws = connect(host, auth("wav2lip", "visemes"), [
        declare(["wav2lip", "visemes"], engine="visemes", resolution=192),
        declare(["wav2lip", "visemes"]),                                          # absent: the last ones stand
        declare(["wav2lip", "visemes"], engine=None, resolution=None),            # null clears them
        declare(["wav2lip", "visemes"], engine="x" * 500, resolution=10 ** 9),    # too big: none
        declare(["wav2lip", "visemes"], engine=7, resolution=True),
        CLOSED])
    one, two, three, four, five = answers(ws)
    assert (one["render_mode"], one["engine"], one["resolution"]) == ("visemes", "visemes", 192)
    assert (two["engine"], two["resolution"]) == ("visemes", 192)
    assert (three["render_mode"], three["engine"]) == ("wav2lip", "wav2lip_gan96")
    assert (four["engine"], five["engine"]) == ("wav2lip_gan96", "wav2lip_gan96")


def test_a_mode_reported_as_failing_is_tried_again_when_the_device_declares_it_again():
    ws = connect(host_with(BOTH), auth("wav2lip", "visemes"), [
        {"type": "render_failed", "mode": "wav2lip", "why": "onnx"}, declare(["wav2lip", "visemes"]), CLOSED])
    assert ws.json("render_mode")[0]["mode"] == "visemes"
    assert answers(ws)[0]["render_mode"] == "wav2lip"


def test_a_message_without_a_list_is_refused_and_changes_nothing():
    ws = connect(host_with(BOTH), auth("wav2lip", "visemes"), [declare("wav2lip"), {"type": "render_modes"}, declare(None), CLOSED])
    assert [e["code"] for e in ws.json("error")] == ["bad_render_modes"] * 3 and not answers(ws)


def test_at_most_six_declarations_per_window_then_the_clock_lets_one_through():
    host = host_with(BOTH)
    now = [100.0]
    host.render_clock = lambda: now[0]

    def later():
        now[0] += vg.REDECLARE_WINDOW_S + 1
        return declare(["visemes"])

    script = [declare(["visemes"]) for _ in range(vg.REDECLARE_MAX + 2)] + [later, CLOSED]
    ws = connect(host, auth("wav2lip", "visemes"), script)
    assert len(answers(ws)) == vg.REDECLARE_MAX + 1
    assert [e["code"] for e in ws.json("error")] == ["render_modes_rate_limited"] * 2
    assert ws.json("render_modes")[-1]["modes"] == ["visemes"] and ws.kinds[-1] == "render_modes"


def test_a_malformed_message_counts_against_the_limit_too():
    host = host_with(BOTH)
    host.render_clock = lambda: 5.0
    ws = connect(host, auth("wav2lip", "visemes"), [declare("x") for _ in range(vg.REDECLARE_MAX)] + [declare(["visemes"]), CLOSED])
    assert not answers(ws) and ws.json("error")[-1]["code"] == "render_modes_rate_limited"


def test_the_limit_is_per_connection():
    host = host_with(BOTH)
    host.render_clock = lambda: 5.0
    for _ in range(2):
        ws = connect(host, auth("wav2lip", "visemes"), [declare(["visemes"]) for _ in range(vg.REDECLARE_MAX)] + [CLOSED])
        assert len(answers(ws)) == vg.REDECLARE_MAX


def test_nothing_is_read_before_the_auth_message():
    ws = FakeWs([declare(["visemes"]), CLOSED])
    vg.Connection(ws, host_with(BOTH), authed=True).run()
    assert not answers(ws) and ws.json("error")[0]["code"] == "auth_failed" and "ready" not in ws.kinds


def test_a_connection_that_is_not_authenticated_refuses_the_message_if_it_ever_gets_there():
    ws = FakeWs()
    conn = vg.Connection(ws, host_with(BOTH), authed=False)
    conn._side_message(declare(["visemes"]))
    assert ws.json("error")[0]["code"] == "auth_required" and not answers(ws)
    assert conn._declared == []


def test_a_wrong_token_never_gets_to_declare():
    ws = FakeWs([{"type": "auth", "token": "wrong"}, declare(["visemes"]), CLOSED])
    vg.Connection(ws, host_with(BOTH), authed=False).run()
    assert ws.json("error")[0]["code"] == "auth_failed" and not answers(ws)


def test_the_message_is_handled_in_the_middle_of_a_turn_too():
    host = host_with(BOTH)
    host.tts = type(host.tts)(segments=2, samples=4800)
    sent = []

    def mid():
        sent.append(1)
        return declare(["visemes"])

    script = [{"type": "start", "mode": "hold", "lang": "en", "sample_rate": 16000}, b"\x00\x00" * 800, mid, {"type": "end"}, CLOSED]
    ws = connect(host, auth("wav2lip", "visemes"), script)
    assert sent and answers(ws)[0]["render_mode"] == "visemes"
