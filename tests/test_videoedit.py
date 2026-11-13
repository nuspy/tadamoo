"""The virtual camera and the editing with the real ffmpeg: the S-curve and the move library, shots with their layers, joins by copy, clips edited in untouched, frames drawn and encoded."""
import base64
import http.server
import io
import json
import shutil
import subprocess
import threading
import wave
from pathlib import Path

import pytest

from media_fake import Broker
from tadamoo.media import client
from tadamoo.media.errors import Cancelled, MediaError
from tadamoo.media.settings import DEFAULTS
from tadamoo.speech import service as speech
from tadamoo.videoedit import camera, compose, render
from video_fakes import make_clip, wav_bytes
from tadamoo.newscast import layouts, plan as planner, produce, settings as settings_mod
from tadamoo.newscast import script as script_mod
from tadamoo.newscast.script import ScriptError

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


# ---- the virtual camera
def test_the_s_curve_starts_and_ends_at_rest_and_is_monotonic():
    values = [camera.smooth(i / 100) for i in range(101)]
    assert values[0] == 0 and values[-1] == 1 and all(b >= a for a, b in zip(values, values[1:])) and values[1] < 0.001 and values[99] > 0.999 and camera.smooth(0.5) == 0.5


def test_every_move_of_the_library_stays_inside_the_limits_whatever_its_length():
    for name in camera.MOVES:
        for seconds in (2.0, 4.0, 6.0, 9.0, 15.0):
            for start in (camera.START["warm"], camera.START["firm"]):
                move = camera.make_move(name, start, seconds)
                assert camera.check(move) == [], (name, seconds)
                assert move.peak_zoom_per_s() <= camera.LIMITS["zoom_per_s"] and abs(move.end.roll) <= camera.LIMITS["roll_max"]
    assert camera.check(camera.Move("fast", camera.State(1.0), camera.State(2.0), 2.0))


def test_a_move_in_between_is_between_and_keeps_the_zoom_ratio_even():
    move = camera.Move("m", camera.State(1.0, 0.4, 0.3), camera.State(1.5, 0.6, 0.4, 2.0), 10.0)
    mid = move.at(5.0)
    assert mid.zoom == pytest.approx(1.5 ** 0.5) and mid.fx == pytest.approx(0.5) and mid.roll == pytest.approx(1.0) and move.at(0).zoom == 1.0 and move.at(99).zoom == 1.5


def test_a_cut_falls_on_motion_with_the_face_in_the_same_place_and_a_small_step_in_scale():
    first = camera.sequence([("warm", 6.0)])
    after = camera.sequence([("firm", 5.0)], previous=first[-1].end)
    assert first[0].start == camera.START["warm"] and first[0].end.zoom > first[0].start.zoom
    step = after[0].start.zoom / first[-1].end.zoom
    assert after[0].start.fx == first[-1].end.fx and abs(step - 1) < 0.2
    assert after[0].start.zoom >= camera.START["firm"].zoom                      # a firm block is tighter
    run = camera.sequence([("warm", 6.0), ("warm", 5.0), ("warm", 7.0)])
    assert all(run[i + 1].start == run[i].end for i in range(2)) and all(m.end.zoom <= 1.46 * 1.4 for m in run)
    assert [m.name for m in run][0] != [m.name for m in run][1]


def test_the_window_puts_the_face_where_the_state_says_even_with_a_roll():
    import numpy as np
    img = np.zeros((512, 768, 3), dtype=np.uint8)
    img[140:170, 340:380] = 255                                                  # the "face"
    canvas = layouts.Frame(794, 530)
    out = np.asarray(render.warp(img, camera.State(1.6, 0.5, 0.4, 0.0), (360 / 768, 155 / 512), canvas, sharpen=False))
    ys, xs = np.where(out[:, :, 0] > 200)
    assert abs(xs.mean() / 794 - 0.5) < 0.03 and abs(ys.mean() / 530 - 0.4) < 0.03
    rolled = np.asarray(render.warp(img, camera.State(1.3, 0.5, 0.4, 3.0), (360 / 768, 155 / 512), canvas, sharpen=False))
    assert rolled.shape == (530, 794, 3) and rolled.max() > 200



# ---- the editing, with the real ffmpeg
@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_shot_has_its_layers_graphics_and_voice_and_shots_join_by_copy(tmp_path):
    canvas = layouts.Frame(794, 530)
    clip = make_clip(tmp_path / "c.mp4", 3.0, "768x512")
    layouts.lower_third(canvas, "Cím", "Telex", "Forrás:").save(tmp_path / "l.png")
    (tmp_path / "v.wav").write_bytes(wav_bytes(2.0))
    spec = compose.ShotSpec(2.0, [compose.Layer(clip, 0.2, 1.0, None, (0.5, 0.5, 1.2), 0.06), compose.Layer(clip, 0.0, 0.85, layouts.Box(500, 14, 240, 160), (0.4, 0.4, 1.5))], [tmp_path / "l.png"], tmp_path / "v.wav")
    a = compose.render_shot(spec, canvas, 24, tmp_path / "a.mp4")
    info = compose.probe(a)
    assert (info["width"], info["height"], info["fps"], info["audio_rate"]) == (794, 530, 24.0, 32000) and info["duration"] == pytest.approx(2.0, abs=0.1)
    assert compose.probe(compose.join_copy([a, a], tmp_path / "body.mp4"))["duration"] == pytest.approx(4.0, abs=0.2)
    with pytest.raises(compose.ComposeError, match="could not be read"):
        compose.probe(tmp_path / "missing.mp4")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_sigla_are_edited_in_untouched_and_a_body_of_another_format_is_refused(tmp_path):
    sigla = make_clip(tmp_path / "open.mp4", 1.0, "794x530", audio=True)
    closing = make_clip(tmp_path / "close.mp4", 1.0, "794x530", audio=True)
    body = make_clip(tmp_path / "body.mp4", 2.0, "794x530", audio=True)
    final = compose.join_with_sigla(sigla, body, closing, tmp_path / "final.mp4")
    assert compose.probe(final)["duration"] == pytest.approx(4.0, abs=0.3)
    for path, where in ((sigla, "start"), (closing, "end")):
        proof = compose.verify_untouched(final, path, where)
        assert proof["untouched"] and proof["video"]["identical"] == proof["video"]["packets"] and proof["audio"]["compared"] == proof["audio"]["packets"] - 1
    assert compose.verify_untouched(final, body, "start")["untouched"] is False
    odd = make_clip(tmp_path / "odd.mp4", 1.0, "640x360", audio=True)
    with pytest.raises(compose.ComposeError, match="without re-encoding"):
        compose.join_with_sigla(sigla, odd, None, tmp_path / "bad.mp4")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_frames_are_drawn_and_encoded_with_the_voice(tmp_path):
    canvas = layouts.Frame(794, 530)
    clip = make_clip(tmp_path / "c.mp4", 3.0, "768x512")
    src = render.Source(render.decode(clip, 0.0, 3.0, 24), 24)
    shots = [render.Shot(0.0, 1.5, 1, lambda t, shift=0.0: render.warp(src.frame(t), camera.State(1.0 + 0.1 * t), camera.FACE_WIDE, canvas), None),
             render.Shot(1.5, 3.0, 1, lambda t, shift=0.0: render.warp(src.frame(t), camera.State(1.3, 0.5, 0.4, 2.0), camera.FACE_WIDE, canvas),
                         lambda t: render.warp(src.frame(t), camera.State(1.5, 0.5, 0.46), camera.FACE_WIDE, canvas))]
    g = {1: render.StoryGraphics(0.0, 3.0, layouts.side_card(canvas, "Cím", "Hír 1"), layouts.lower_third(canvas, "Cím", "Telex", "Forrás:"), {})}
    style = render.Style(card_hold_s=1.0)
    box, mask, _ = render.window(canvas, 0.3, 14, 9, 2)
    (tmp_path / "v.wav").write_bytes(wav_bytes(3.0))
    out = render.encode(lambda f: render.compose_frame(shots, f, 24, canvas, g, style, box, mask, layouts.ai_label(canvas, "AI")), 72, 24, canvas, tmp_path / "v.wav", tmp_path / "o.mp4")
    info = compose.probe(out)
    assert (info["width"], info["height"], info["fps"], info["audio_rate"], info["profile"], info["level"]) == (794, 530, 24.0, 32000, "High", 31) and info["duration"] == pytest.approx(3.0, abs=0.1)
    a, b = render.compose_frame(shots, 5, 24, canvas, g, style, box, mask, None), render.compose_frame(shots, 40, 24, canvas, g, style, box, mask, None)
    assert a.size == b.size == (794, 530) and a.tobytes() != b.tobytes()
    assert len(render.decode(clip, 1.0, 1.0, 24, 0.5)) == 24 and render.decode(clip, 9.0, 0.5, 24)[0].shape == (512, 768, 3)               # asked beyond its end, the last picture is held


