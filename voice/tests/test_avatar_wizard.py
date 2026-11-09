"""The avatar creation wizard: every step is checked before it is kept, a name or draft id that is not plain is refused, the avatar is built
(poster, Wav2Lip face and mouth box, clip, poses), and the voice sample goes to the cloning folder with an engine only when one is installed."""
import io
import json
import os
from pathlib import Path

import pytest

from callbridge import models_store as ms
from callbridge.avatar_store import AvatarStore
from callbridge.engines import catalog
from voice import avatar_wizard
from voice.avatar_wizard import Wizard, WizardError

from avatar_fakes import make_config, photo_bytes, video_file, wav_bytes


class Storage:
    """What werkzeug hands over for an uploaded file."""

    def __init__(self, data: bytes, filename="../../evil.png"):
        self.stream, self.filename = io.BytesIO(data), filename


@pytest.fixture
def w(tmp_path, monkeypatch):
    models = tmp_path / "models"
    monkeypatch.setenv(ms.ROOT_ENV, str(models))
    cfg = make_config(tmp_path)
    store = AvatarStore(cfg.avatars_dir)
    wiz = Wizard(Path(cfg.avatars_dir).parent / "avatar-drafts", store, cfg)
    wiz.tmp, wiz.models = tmp_path, models
    return wiz


def draft(w):
    return w.start()["draft_id"]


def test_a_draft_has_a_random_id_and_the_steps(w):
    out = w.start()
    assert len(out["draft_id"]) == 32 and out["steps"] == ["foto", "voce", "video", "pose"] and out["limits"]["video"] == 500 << 20
    assert w.status(out["draft_id"])["foto"] is False


def test_a_draft_id_or_step_that_is_not_plain_is_refused(w):
    for bad in ("../x", "", "A" * 32, "0" * 31, "0" * 32, "0" * 32 + "\n", None, "..\\.."):
        with pytest.raises(WizardError) as e:
            w.upload(bad, "foto", Storage(photo_bytes()))
        assert e.value.status == 404
    d = draft(w)
    for step in ("../foto", "elabora", "photo", "", "foto.jpg"):
        with pytest.raises(WizardError, match="unknown step"):
            w.upload(d, step, Storage(photo_bytes()))
    assert list((w.drafts / d).iterdir()) == []                                     # nothing was written by the refused ones


def test_the_photo_is_decoded_and_kept_as_jpeg_whatever_the_client_called_it(w):
    d = draft(w)
    for ext in (".png", ".jpg", ".webp"):
        out = w.upload(d, "foto", Storage(photo_bytes(ext=ext), filename="../../x" + ext))
        assert out["file"] == "foto.jpg" and (out["width"], out["height"]) == (200, 200)
    assert (w.drafts / d / "foto.jpg").read_bytes()[:2] == b"\xff\xd8"
    assert sorted(p.name for p in (w.drafts / d).iterdir()) == ["foto.jpg"]


def test_a_file_that_is_not_a_photo_or_is_too_big_or_too_small_is_refused(w):
    d = draft(w)
    for data, text in ((b"not an image at all", "photo"), (b"", "empty"), (photo_bytes(side=32), "sides"), (b"GIF89a" + b"\0" * 20, "photo")):
        with pytest.raises(WizardError, match=text):
            w.upload(d, "foto", Storage(data))
    with pytest.raises(WizardError) as e:
        w.upload(d, "foto", Storage(b"x" * 100), declared_length=avatar_wizard.LIMITS["foto"] + (1 << 20))
    assert e.value.status == 413
    with pytest.raises(WizardError) as e:
        w.upload(d, "foto", Storage(b"x" * (avatar_wizard.LIMITS["foto"] + 1)))
    assert e.value.status == 413
    assert not (w.drafts / d / "foto.jpg").exists()


def test_at_most_six_poses_and_each_is_numbered_by_the_wizard(w):
    d = draft(w)
    for n in range(1, 7):
        assert w.upload(d, "pose", Storage(photo_bytes()))["file"] == f"pose-{n}.jpg"
    with pytest.raises(WizardError) as e:
        w.upload(d, "pose", Storage(photo_bytes()))
    assert e.value.status == 409 and w.status(d)["pose"] == 6


def test_the_video_must_be_an_mp4_that_opens_and_is_not_too_long_or_big(w, tmp_path):
    d = draft(w)
    good = video_file(tmp_path / "v.mp4").read_bytes()
    out = w.upload(d, "video", Storage(good))
    assert out["file"] == "video.mp4" and out["frames"] == 12 and out["width"] == 160 and w.status(d)["video_info"]["fps"] == 10.0
    d2 = draft(w)
    for data, text in ((b"RIFF....AVI LIST" + b"\0" * 100, "MP4"), (b"\0\0\0\x18ftypmp42" + b"\0" * 200, "frames"), (b"", "MP4")):
        with pytest.raises(WizardError, match=text):
            w.upload(d2, "video", Storage(data))
    assert list((w.drafts / d2).iterdir()) == []                                    # no half-written file is left behind
    small = video_file(tmp_path / "small.mp4", side=64).read_bytes()
    with pytest.raises(WizardError, match="sides"):
        w.upload(d2, "video", Storage(small))
    with pytest.raises(WizardError) as e:
        w.upload(d2, "video", Storage(good), declared_length=avatar_wizard.LIMITS["video"] + (1 << 20))
    assert e.value.status == 413
    avatar_wizard.LIMITS["video"], saved = 1000, avatar_wizard.LIMITS["video"]
    try:
        with pytest.raises(WizardError) as e:
            w.upload(d2, "video", Storage(good))
        assert e.value.status == 413 and list((w.drafts / d2).iterdir()) == []
    finally:
        avatar_wizard.LIMITS["video"] = saved


def test_the_voice_sample_is_an_audio_type_of_the_cloning_folder_and_a_sensible_length(w):
    d = draft(w)
    out = w.upload(d, "voce", Storage(wav_bytes(4.0)))
    assert out["format"] == "wav" and out["seconds"] == 4.0
    for data, text in ((b"MZ" + b"\0" * 100, "WAV, MP3"), (wav_bytes(1.0), "between"), (wav_bytes(200.0), "between"), (b"RIFF\0\0\0\0WAVEjunk", "cannot be read")):
        with pytest.raises(WizardError, match=text):
            w.upload(d, "voce", Storage(data))
    assert w.upload(d, "voce", Storage(b"fLaC" + b"\0" * 100))["format"] == "flac"
    assert len(list((w.drafts / d).glob("voce.*"))) == 1                            # one sample per draft


def test_too_many_open_drafts_and_old_ones_are_cleaned(w):
    ids = [draft(w) for _ in range(avatar_wizard.MAX_DRAFTS)]
    with pytest.raises(WizardError) as e:
        w.start()
    assert e.value.status == 409
    old = w.drafts / ids[0]
    os.utime(old, (1, 1))
    w.start()
    assert not old.exists()
    w.cancel(ids[1])
    assert not (w.drafts / ids[1]).exists()


def test_finishing_needs_a_photo_and_a_plain_name(w):
    d = draft(w)
    with pytest.raises(WizardError, match="name"):
        w.finish(d, "")
    with pytest.raises(WizardError, match="name"):
        w.finish(d, "x" * 41)
    with pytest.raises(WizardError, match="letter"):
        w.finish(d, "???")
    with pytest.raises(WizardError, match="route"):
        w.finish(d, "Wizard")
    with pytest.raises(WizardError, match="photo"):
        w.finish(d, "Ada")
    assert w.store.names() == []


def test_finishing_builds_the_avatar_from_the_photo_alone(w):
    d = draft(w)
    w.upload(d, "foto", Storage(photo_bytes(side=300)))
    out = w.finish(d, "Ada Lovelace")
    assert out["avatar"] == "Ada-Lovelace" and out["display_name"] == "Ada Lovelace" and out["clips"] == [] and out["voice"] == {}
    folder = w.store.path("Ada-Lovelace")
    assert (folder / "poster.jpg").is_file() and (folder / "wav2lip" / "face.png").is_file()
    box = json.loads((folder / "wav2lip" / "box.json").read_text(encoding="utf-8"))
    assert box["poster_w"] == 300 and "bocca" in box and len(box["inv"]) == 6          # the mouth corners, a default to correct
    assert w.store.info("Ada-Lovelace")["wav2lip"] and w.store.info("Ada-Lovelace")["visemes"] and not (w.drafts / d).exists()
    assert out["render_modes"] == ["wav2lip", "visemes"] and out["mouth"] == "default"            # no video: no PC video, and no GPU was needed
    assert (folder / "visemes" / "aa.png").is_file() and (folder / "visemes" / "frame.json").is_file()
    d2 = draft(w)
    w.upload(d2, "foto", Storage(photo_bytes()))
    with pytest.raises(WizardError) as e:
        w.finish(d2, "ada lovelace")
    assert e.value.status == 409 and e.value.code == "exists" and (w.drafts / d2).exists()      # the draft stays: only the name changes


def test_finishing_with_all_the_materials(w, tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "disponibile", lambda cfg, spec, router=None: spec.id == "xtts")
    d = draft(w)
    w.upload(d, "foto", Storage(photo_bytes()))
    w.upload(d, "pose", Storage(photo_bytes()))
    w.upload(d, "video", Storage(video_file(tmp_path / "v.mp4").read_bytes()))
    w.upload(d, "voce", Storage(wav_bytes(5.0)))
    out = w.finish(d, "Bea", "it")
    assert out["clips"] == ["Bea.mp4"] and out["poses"] == 1 and out["render_modes"] == ["wav2lip", "visemes"]          # the clip is not prepared by finishing
    assert out["voice"] == {"engine": "xtts", "voice": "Bea"} and out["voice_sample"] == "Bea"
    assert (w.models / "tts" / "voices" / "Bea.wav").read_bytes() == wav_bytes(5.0)         # in the TTS cloning folder
    assert w.store.voice_of("Bea") == {"engine": "xtts", "voice": "Bea"}
    assert w.store.info("Bea")["clips"] == ["Bea.mp4"] and (w.store.path("Bea") / "poses" / "pose-1.jpg").is_file()


def test_without_a_cloning_engine_the_voice_stays_and_the_reason_is_said(w, monkeypatch):
    monkeypatch.setattr(catalog, "disponibile", lambda cfg, spec, router=None: False)
    d = draft(w)
    w.upload(d, "foto", Storage(photo_bytes()))
    w.upload(d, "voce", Storage(wav_bytes(5.0)))
    out = w.finish(d, "Cy")
    assert out["voice"] == {} and out["voice_sample"] == "Cy"
    assert "saved as Cy but not used yet" in out["voice_note"] and "Kokoro" in out["voice_note"] and "no voice-cloning engine is installed" in out["voice_note"]
    assert (w.models / "tts" / "voices" / "Cy.wav").is_file()                          # the sample is kept for when an engine is installed
    assert w.store.voice_of("Cy") == {} and w.store.metadata("Cy")["voice_sample"] == "Cy"


def test_the_engine_is_the_best_installed_one_that_speaks_the_language(w, monkeypatch):
    asked = []

    def available(cfg, spec, router=None):
        asked.append(spec.id)
        return spec.id in ("chatterbox", "qwen3tts")
    monkeypatch.setattr(catalog, "disponibile", available)
    assert avatar_wizard.cloning_engine(w.cfg, "it") == ("qwen3tts", "")
    assert avatar_wizard.cloning_engine(w.cfg, "ru") == ("qwen3tts", "")
    assert avatar_wizard.cloning_engine(w.cfg, "hu")[0] == "" and "does not speak hu" in avatar_wizard.cloning_engine(w.cfg, "hu")[1]
    monkeypatch.setattr(catalog, "disponibile", lambda cfg, spec, router=None: spec.id == "dia")
    assert avatar_wizard.cloning_engine(w.cfg, "it")[0] == ""                          # Dia is English only
    assert avatar_wizard.cloning_engine(w.cfg, "en") == ("dia", "")


def test_a_sample_name_that_is_taken_gets_a_number(w, monkeypatch):
    monkeypatch.setattr(catalog, "disponibile", lambda cfg, spec, router=None: True)
    folder = w.models / "tts" / "voices"
    folder.mkdir(parents=True)
    (folder / "Dee.mp3").write_bytes(b"x")
    d = draft(w)
    w.upload(d, "foto", Storage(photo_bytes()))
    w.upload(d, "voce", Storage(wav_bytes(5.0)))
    out = w.finish(d, "Dee")
    assert out["voice_sample"] == "Dee-2" and (folder / "Dee.mp3").read_bytes() == b"x"


def test_no_models_folder_means_the_sample_is_not_kept_and_the_avatar_still_is(w, monkeypatch):
    monkeypatch.delenv(ms.ROOT_ENV)
    d = draft(w)
    w.upload(d, "foto", Storage(photo_bytes()))
    w.upload(d, "voce", Storage(wav_bytes(5.0)))
    out = w.finish(d, "Eve")
    assert out["voice"] == {} and "no models folder" in out["voice_note"] and w.store.names() == ["Eve"]


def test_a_failure_while_building_removes_the_half_made_avatar_and_keeps_the_draft(w, monkeypatch):
    from callbridge import avatar_prep
    d = draft(w)
    w.upload(d, "foto", Storage(photo_bytes()))
    monkeypatch.setattr(avatar_prep, "prepara_wav2lip", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        w.finish(d, "Fay")
    assert w.store.names() == [] and (w.drafts / d).exists()


def test_without_opencv_the_photo_is_refused_with_a_plain_reason_and_an_avatar_is_kept_without_its_mouth_files(w, monkeypatch):
    import builtins
    real = builtins.__import__

    def no_cv2(name, *a, **k):
        if name in ("cv2", "callbridge.avatar_prep") or (name == "callbridge" and a and a[2] and "avatar_prep" in a[2]):
            raise ImportError("No module named 'cv2'")
        return real(name, *a, **k)
    d = draft(w)
    photo = photo_bytes()
    w.upload(d, "foto", Storage(photo))
    monkeypatch.setattr(builtins, "__import__", no_cv2)
    with pytest.raises(WizardError, match="needs opencv") as e:
        w.upload(d, "pose", Storage(photo))
    assert e.value.code == "needs_opencv" and e.value.status == 501
    out = w.finish(d, "Cy")
    assert out["mouth"].startswith("not made: needs opencv") and out["render_modes"] == []
