"""The voice-identification search: gender and pitch band only order which contact is tried first, they never gate one out - a
mispredicted branch climbs back up and tries the next one, cyclically, until every enrolled contact has been compared if it must."""
import math

from tadamoo.speech import voiceid


def _voiced(contact_id: str, f0_hz: float, embedding, channel: str = "mic"):
    return {"id": contact_id, "voice": {"embeddings": {channel: [embedding]}, "f0_hz": f0_hz}}


def test_a_voice_matched_in_its_own_predicted_branch_returns_at_once():
    contacts = [_voiced("mario", 110.0, [1.0, 0.0]), _voiced("anna", 220.0, [0.0, 1.0])]
    found = voiceid.identify([1.0, 0.0], 110.0, contacts)
    assert found is not None and found.contact_id == "mario" and found.confident


def test_a_voice_whose_pitch_lands_in_the_wrong_branch_is_still_found_by_climbing_back_up():
    # anna's own pitch (210 Hz) sits in the "f" branch; her embedding is only enrolled over there, but the search is asked to
    # match an embedding identical to hers while pretending its pitch reads as a low male voice (90 Hz) - the wrong branch first.
    contacts = [_voiced("mario", 90.0, [1.0, 0.0]), _voiced("anna", 210.0, [0.0, 1.0])]
    found = voiceid.identify([0.0, 1.0], 90.0, contacts)
    assert found is not None and found.contact_id == "anna"


def test_nobody_close_enough_anywhere_returns_none():
    contacts = [_voiced("mario", 110.0, [1.0, 0.0]), _voiced("anna", 220.0, [0.0, 1.0])]
    assert voiceid.identify([0.0, -1.0], 110.0, contacts) is None


def test_a_weak_but_real_match_is_returned_marked_unconfident_not_dropped():
    # a unit vector at cosine 0.85 from [1, 0]: between PASSABLE (0.80) and CONFIDENT (0.90) - close enough to report, not to trust.
    weak = [0.85, math.sqrt(1 - 0.85 ** 2)]
    contacts = [_voiced("mario", 110.0, [1.0, 0.0])]
    found = voiceid.identify(weak, 110.0, contacts)
    assert found is not None and found.contact_id == "mario" and not found.confident


def test_enrolled_on_one_channel_is_still_matched_from_another():
    contacts = [_voiced("mario", 110.0, [1.0, 0.0], channel="phone")]
    found = voiceid.identify([1.0, 0.0], 110.0, contacts)
    assert found is not None and found.contact_id == "mario" and found.channel == "phone"


def test_an_empty_roster_matches_nothing():
    assert voiceid.identify([1.0, 0.0], 110.0, []) is None
    assert voiceid.identify([1.0, 0.0], 110.0, [{"id": "x", "voice": None}]) is None
