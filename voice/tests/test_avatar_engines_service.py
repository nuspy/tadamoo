"""The service's engine route: every engine with its availability for the avatar of the personality, and the selection the service runs with.
The rig is the avatar service test's (a temp user folder, a stand-in worker, no network)."""
from voice.avatar_engines import registry
from voice.tests.test_avatar_service import _isolated, rig  # noqa: F401 (fixtures)


def by_id(body):
    return {e["id"]: e for e in body["engines"]}


def test_the_route_needs_the_token(rig):
    assert rig.client.get("/avatar/engines").status_code == 401


def test_every_engine_is_listed_with_its_descriptor_and_what_exists_for_the_avatar(rig):
    body = rig.get("/avatar/engines").get_json()
    assert body["avatar"] == "ada" and [e["id"] for e in body["engines"]] == list(registry.ENGINES)
    got = by_id(body)
    assert got["visemes"]["status"]["available"] is False and "not made yet" in got["visemes"]["status"]["reason"]
    assert got["musetalk_15"]["status"]["available"] is False and got["musetalk_15"]["status"]["reason"].startswith("no video")
    assert "model file" in got["wav2lip_gan96"]["status"]["reason"]
    assert "not part of this build" in got["ultralight"]["status"]["reason"]
    assert got["visemes"]["runs_on"] == ["pc", "web", "android", "esp32"] and got["wav2lip_gan96"]["license"]["noncommercial"] is True
    rig.get("/avatar/ada/manifest.json")                                               # a device asks for the files: the sprites are made
    rig.store.record_prepared("ada", "walk.mp4", 5)
    got = by_id(rig.get("/avatar/engines").get_json())
    assert got["visemes"]["status"]["available"] is True and got["musetalk_15"]["status"]["available"] is True


def test_the_selection_is_the_one_in_voice_json_and_the_status_route_says_the_same(rig):
    assert rig.get("/avatar/engines").get_json()["selection"] == {"engines": registry.DEFAULT_ENGINES, "resolution": registry.DEFAULT_RESOLUTION, "fps_hint": 25}
    rig.write_settings(avatar={"engines": {"android": "visemes"}, "resolution": {"android": 144}, "fps_hint": 30})
    sel = rig.get("/avatar/engines").get_json()["selection"]
    assert sel["engines"]["android"] == "visemes" and sel["resolution"]["android"] == 144 and sel["fps_hint"] == 30
    assert rig.get("/status").get_json()["avatar_engines"] == sel


def test_with_no_avatar_chosen_nothing_is_available_and_it_says_why(rig):
    rig.write_settings(brain={"persona": "no-avatar"})
    body = rig.get("/avatar/engines").get_json()
    assert body["avatar"] == "" and all(e["status"]["available"] is False and "no avatar" in e["status"]["reason"] for e in body["engines"])
