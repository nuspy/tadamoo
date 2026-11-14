"""The components of the media skills: what is checked first, what is left to install, and that an install runs only the missing steps and checks again. Stand-in LTX Studio, no download."""
import pytest

from media_fake import make_ltx
from tadamoo.media import client, components
from tadamoo.media.settings import DEFAULTS


@pytest.fixture
def settings(tmp_path):
    client.forget_checks()
    return {**DEFAULTS, "ltx_dir": str(make_ltx(tmp_path / "ltx")), "image_python": ""}


def by_id(rows):
    return {r["id"]: r for r in rows}


def test_every_skill_of_the_catalogue_names_only_known_components():
    for skill, ids in components.SKILLS.items():
        assert ids and all(i in components.COMPONENTS for i in ids), skill


def test_what_is_there_is_present_and_nothing_is_asked_to_install(settings, monkeypatch):
    monkeypatch.setattr(components.shutil, "which", lambda name: "/bin/ffmpeg")
    rows = components.check("video-generation", settings)
    assert all(r["present"] for r in rows)
    assert components.steps("video-generation", settings, rows) == []


def test_what_is_missing_has_its_steps_and_what_is_present_has_none(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(components.shutil, "which", lambda name: "")
    settings["ltx_dir"] = str(make_ltx(tmp_path / "ltx2", missing=["qwen21-te-nvfp4"], rife=False))
    rows = by_id(components.check("video-generation", settings))
    assert rows["video-model"]["present"] and not rows["frame-maker"]["present"] and rows["frame-maker"]["missing"][0].startswith("RIFE") and not rows["ffmpeg"]["present"]
    todo = components.steps("video-generation", settings)
    assert [t["component"] for t in todo] == ["ffmpeg", "frame-maker"]
    assert todo[1]["step"][1:] == [str(tmp_path / "ltx2") + "/scripts/download_weights.py", "--ids", "rife-flownet-v4"]
    pictures = components.steps("image-generation", settings)
    assert [t["component"] for t in pictures] == ["image-model"] and pictures[0]["step"][-1] == "qwen21-dit-nvfp4"


def test_without_ltx_studio_the_components_that_need_it_wait_and_the_way_to_get_it_is_said(settings, tmp_path):
    settings["ltx_dir"] = str(tmp_path / "nowhere")
    rows = by_id(components.check("image-generation", settings))
    assert not rows["ltx-studio"]["present"] and not rows["image-model"]["present"]
    assert components.steps("image-generation", settings) == []
    out = components.install("image-generation", settings, run=lambda argv: pytest.fail("nothing to run"))
    assert not out["ready"] and out["by_hand"] and "ltx_dir" in out["by_hand"][0]


def test_an_install_runs_the_missing_steps_in_order_and_checks_again(settings, tmp_path):
    ltx = tmp_path / "ltx3"
    settings["ltx_dir"] = str(make_ltx(ltx, missing=["qwen21-te-nvfp4"]))
    ran = []

    def run(argv):
        ran.append(list(argv))
        (ltx / "core" / "weights_catalog.py").write_text("MISSING = []\n\n\ndef is_present(entry, root):\n    return True\n", encoding="utf-8")
        return 0

    out = components.install("image-generation", settings, run=run)
    assert len(ran) == 1 and ran[0][-2:] == ["--ids", "qwen21-dit-nvfp4"] and out["ready"] and out["failed"] == ""
    again = components.install("image-generation", settings, run=lambda argv: pytest.fail("everything is there"))
    assert again["ran"] == 0 and again["ready"]


def test_an_install_stops_at_the_first_failing_step(settings, tmp_path):
    settings["ltx_dir"] = str(make_ltx(tmp_path / "ltx4", missing=["qwen21-te-nvfp4"]))
    ran = []
    out = components.install("image-generation", settings, run=lambda argv: ran.append(argv) or 1)
    assert len(ran) == 1 and out["failed"] == "image-model" and not out["ready"]


def test_the_models_of_the_seam_detector_are_downloaded_to_their_folder_when_absent(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(components, "LANDMARKERS", tmp_path / "qa")
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / "face_landmarker.task").write_bytes(b"x")
    rows = by_id(components.check("video-editing", settings))
    assert rows["landmarkers"]["missing"] == ["pose_landmarker.task"]
    todo = [t for t in components.steps("video-editing", settings, list(rows.values())) if t["component"] == "landmarkers"]
    assert len(todo) == 1 and todo[0]["step"]["to"] == str(tmp_path / "qa" / "pose_landmarker.task") and todo[0]["step"]["download"].endswith(".task")
    got = []
    components.install("video-editing", settings, run=lambda argv: 0, download=lambda url, to: got.append((url, to)) or 0, say=lambda line: None)
    assert any(to.endswith("pose_landmarker.task") for _, to in got)


def test_triton_for_the_xtts_environment_is_checked_in_that_python_and_installed_with_the_package_ltx_studio_uses(settings):
    import sys
    settings["xtts_python"] = sys.executable
    rows = by_id(components.check("speech-synthesis", settings))
    assert rows["xtts-triton"]["where"] == sys.executable
    if not rows["xtts-triton"]["present"]:
        assert components.steps("speech-synthesis", settings) == [{"component": "xtts-triton", "step": [sys.executable, "-m", "pip", "install", "-U", "triton-windows"]}]
    ran = []
    components.install("speech-synthesis", settings, run=lambda argv: ran.append(list(argv)) or 0, say=lambda line: None)
    assert all(argv[-1] == "triton-windows" for argv in ran)
