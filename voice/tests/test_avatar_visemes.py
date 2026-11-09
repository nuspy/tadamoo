"""The mouth sprites made from a photo (callbridge/avatar_visemes.py), the files a phone gets (avatar_assets.py) and the photo-only preparation
(avatar_prep.py). A synthetic face is drawn with OpenCV: no model, no GPU, no network."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from callbridge import avatar_assets, avatar_prep, avatar_visemes as av
from callbridge.viseme_gen import VISEMI

LEFT, RIGHT = (120.0, 280.0), (180.0, 280.0)


def face(w=300, h=400, left=LEFT, right=RIGHT):
    img = np.full((h, w, 3), (150, 170, 215), np.uint8)                           # skin
    cv2.line(img, (int(left[0]), int(left[1])), (int(right[0]), int(right[1])), (70, 60, 170), 3)
    cv2.circle(img, (110, 180), 8, (40, 40, 40), -1)
    cv2.circle(img, (190, 180), 8, (40, 40, 40), -1)
    return img


def sprites(tmp_path, img=None, p1=LEFT, p2=RIGHT):
    img = face() if img is None else img
    frame = av.write_sprites(img, p1, p2, tmp_path)
    out = {v: cv2.imread(str(tmp_path / "visemes" / f"{v}.png"), cv2.IMREAD_UNCHANGED) for v in VISEMI}
    return img, frame, out


def dark(sprite):
    lum = sprite[..., :3].mean(axis=2)
    return int(((lum < 75) & (sprite[..., 3] > 0)).sum())


def extent(sprite):
    cols = np.where((sprite[..., :3].mean(axis=2) < 75).any(axis=0))[0]
    return int(cols.max() - cols.min()) if cols.size else 0


def test_one_sprite_per_viseme_of_the_generator_all_in_the_same_frame(tmp_path):
    assert set(av.SHAPES) == set(VISEMI) and len(VISEMI) == 15
    _img, frame, out = sprites(tmp_path)
    assert sorted(p.name for p in (tmp_path / "visemes").iterdir()) == sorted([f"{v}.png" for v in VISEMI] + ["frame.json"])
    assert {s.shape for s in out.values()} == {(frame["size"][1], frame["size"][0], 4)}
    assert frame["visemes"] == list(VISEMI) and frame["alpha"] is True and json.loads((tmp_path / "visemes" / "frame.json").read_text()) == frame


def test_a_sprite_changes_only_the_mouth_and_the_rest_is_the_photo(tmp_path):
    img, frame, out = sprites(tmp_path)
    crop, _ = av.frame_of(img, LEFT, RIGHT)
    for name, sprite in out.items():
        transparent = sprite[..., 3] == 0
        assert np.array_equal(sprite[..., :3][transparent], crop[transparent]), name           # outside the mouth the pixels are the photo's own
        assert transparent.mean() > 0.5, name                                                 # the mouth is a small part of the frame
    assert np.array_equal(out["sil"][..., :3], crop)                                          # the rest position is the photo unchanged
    assert dark(out["sil"]) == 0


def test_the_openings_follow_the_shapes_the_mouth_makes(tmp_path):
    _img, _frame, out = sprites(tmp_path)
    area = {v: dark(s) for v, s in out.items()}
    assert area["aa"] > area["O"] > area["E"] > area["I"] > area["FF"] > 0
    assert area["aa"] > area["U"] > area["PP"]
    assert 0 < area["PP"] < area["aa"] / 3                                                    # the closed mouth shows only its line
    assert extent(out["I"]) > extent(out["O"]) and extent(out["E"]) > extent(out["U"])      # a smile is wide, a pucker is narrow
    assert extent(out["aa"]) > extent(out["U"])


def test_the_frame_is_upright_centred_on_the_mouth_and_maps_back_to_the_photo(tmp_path):
    left, right = (120.0, 270.0), (180.0, 296.0)                                            # a head turned by about 23 degrees
    _img, frame, _out = sprites(tmp_path, face(left=left, right=right), left, right)
    inv = np.array(frame["inv"]).reshape(2, 3)
    fwd = cv2.invertAffineTransform(inv)
    corners = [fwd @ np.array([x, y, 1.0]) for x, y in (left, right)]
    centre = (frame["size"][0] / 2, frame["size"][1] / 2)
    assert abs(corners[0][1] - corners[1][1]) < 0.01 and abs(corners[0][1] - centre[1]) < 0.01      # the lip line is horizontal, at the middle
    assert abs((corners[0][0] + corners[1][0]) / 2 - centre[0]) < 0.01
    assert abs(corners[1][0] - corners[0][0] - frame["mouth"]["w"]) < 0.01 and abs(frame["angle_deg"] - 23.4) < 0.1
    assert np.allclose(inv @ np.array([*centre, 1.0]), [150.0, 283.0], atol=0.01)            # the middle of the frame is the middle of the mouth


def test_a_large_photo_gives_a_frame_of_at_most_the_cap_and_a_tiny_mouth_is_refused(tmp_path):
    img = np.full((3000, 4000, 3), 180, np.uint8)
    _i, frame, _o = sprites(tmp_path, img, (1500.0, 2000.0), (2300.0, 2000.0))
    assert frame["size"][0] <= av.MAX_FRAME_W and frame["mouth"]["w"] < 800
    with pytest.raises(ValueError, match="too small"):
        av.write_sprites(face(), (100, 100), (103, 100), tmp_path / "x")


# ---------------------------------------------------------------------------------------- the preparation from the photo
def poster(folder: Path, img=None):
    folder.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(folder / "poster.jpg"), face() if img is None else img)
    return folder / "poster.jpg"


def test_preparing_from_the_photo_alone_makes_the_wav2lip_face_and_the_sprites(tmp_path):
    folder = tmp_path / "ada"
    done = avatar_prep.prepara_wav2lip(poster(folder), folder)
    assert (folder / "wav2lip" / "face.png").is_file() and (folder / "wav2lip" / "box.json").is_file()
    assert done["visemes"]["visemes"] == list(VISEMI) and (folder / "visemes" / "aa.png").is_file()
    assert avatar_assets.render_modes(folder) == ["wav2lip", "visemes"]


def test_marking_the_mouth_by_hand_redoes_the_sprites_on_the_marked_corners(tmp_path):
    folder = tmp_path / "ada"
    avatar_prep.prepara_wav2lip(poster(folder), folder)
    avatar_prep.prepara_da_bocca(folder / "poster.jpg", folder, [120, 280], [180, 280])
    frame = json.loads((folder / "visemes" / "frame.json").read_text())
    inv = np.array(frame["inv"]).reshape(2, 3)
    assert np.allclose(inv @ np.array([frame["size"][0] / 2, frame["size"][1] / 2, 1.0]), [150.0, 280.0], atol=0.01)


def test_completing_a_folder_keeps_the_mouth_the_person_marked(tmp_path):
    folder = tmp_path / "ada"
    avatar_prep.prepara_da_bocca(poster(folder), folder, [120, 280], [180, 280])
    for f in (folder / "visemes").iterdir():
        f.unlink()
    (folder / "visemes").rmdir()
    avatar_prep.completa(folder)
    frame = json.loads((folder / "visemes" / "frame.json").read_text())
    assert abs(frame["mouth"]["w"] - 60.0) < 0.1                                             # the 60 pixels between the marked corners
    (folder / "wav2lip" / "face.png").unlink()
    avatar_prep.completa(folder)                                                             # a missing face is remade, with the default mouth
    assert (folder / "wav2lip" / "face.png").is_file()
    with pytest.raises(ValueError):
        avatar_prep.completa(tmp_path / "empty")


# ---------------------------------------------------------------------------------------- what a device gets
def test_the_manifest_lists_every_file_with_its_sha256_and_the_modes_the_files_allow(tmp_path):
    folder = tmp_path / "ada"
    avatar_prep.prepara_wav2lip(poster(folder), folder)
    man = avatar_assets.manifest(folder, "ada", "2026-11-09T10:00:00+00:00")
    assert man["version"] == 1 and man["avatar"] == "ada" and man["created"].startswith("2026-11-09") and man["render_modes"] == ["wav2lip", "visemes"]
    assert sorted(man["files"]) == sorted(["poster.jpg", "wav2lip/face.png", "wav2lip/box.json", "visemes/frame.json"] + [f"visemes/{v}.png" for v in VISEMI])
    for rel, digest in man["files"].items():
        assert hashlib.sha256((folder / rel).read_bytes()).hexdigest() == digest
    assert avatar_assets.manifest(folder, "ada", video=True)["render_modes"] == ["wav2lip", "visemes", "video"]
    data = avatar_assets.zip_bytes(folder, man)
    z = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(z.namelist()) == sorted(list(man["files"]) + ["manifest.json"]) and json.loads(z.read("manifest.json")) == man


def test_a_folder_missing_a_sprite_does_not_offer_the_visemes_and_one_missing_the_face_does_not_offer_wav2lip(tmp_path):
    folder = tmp_path / "ada"
    avatar_prep.prepara_wav2lip(poster(folder), folder)
    (folder / "visemes" / "U.png").unlink()
    assert avatar_assets.render_modes(folder) == ["wav2lip"]
    (folder / "wav2lip" / "box.json").unlink()
    assert avatar_assets.render_modes(folder) == []


def test_the_checksum_of_a_file_follows_the_file_when_it_changes(tmp_path):
    f = tmp_path / "m.onnx"
    f.write_bytes(b"one")
    first = avatar_assets.sha256_of(f)
    assert first == hashlib.sha256(b"one").hexdigest() and avatar_assets.sha256_of(f) == first
    f.write_bytes(b"other")
    assert avatar_assets.sha256_of(f) == hashlib.sha256(b"other").hexdigest()


def test_the_wav2lip_model_is_where_the_catalog_puts_it(tmp_path):
    assert avatar_assets.model_path(str(tmp_path)) is None
    target = tmp_path / "avatar" / "wav2lip-int8"
    target.mkdir(parents=True)
    (target / "wav2lip_int8.onnx").write_bytes(b"x")
    assert avatar_assets.model_path(str(tmp_path)) == target / "wav2lip_int8.onnx"
