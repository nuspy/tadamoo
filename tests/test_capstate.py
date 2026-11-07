"""The note about settings that changed during a conversation: only what changed, once, and everything again after a fold."""
from types import SimpleNamespace

from tadamoo import capstate
from tadamoo.permissions import PermissionLevel, PermissionManager


class Tools:
    def __init__(self, names=()):
        self.names = set(names)

    def get(self, name):
        return object() if name in self.names else None


def agent(effort="", level=PermissionLevel.AUTO, plan_mode=False, steps=40, tools=()):
    return SimpleNamespace(sampling={"reasoning_effort": effort} if effort else {}, permissions=PermissionManager(level=level, plan_mode=plan_mode),
                           max_turns=steps, tools=Tools(tools))


def test_read_gives_plain_values():
    state = capstate.read(agent("high", PermissionLevel.AUTO, True, 0, ("session_send",)), window=32768, unattended=True)
    assert state == {"window": 32768, "effort": "high", "level": "auto", "plan_mode": True, "unattended": True, "steps": "unlimited", "orchestration": True}
    assert capstate.read(agent(), window=1000, unattended=False)["effort"] == "default"


def test_the_first_note_names_what_the_prompt_does_not_and_leaves_out_the_window():
    state = capstate.read(agent("low"), window=8192, unattended=False)
    teller = capstate.Teller({"window": 8192})
    note = teller.note(state)
    assert note.startswith("[state] ") and "effort=low" in note and "level=auto" in note and "window" not in note
    assert teller.note(state) == ""                                       # nothing changed: nothing said


def test_only_the_changed_field_comes_the_next_time():
    teller = capstate.Teller({"window": 8192})
    teller.note(capstate.read(agent("low"), window=8192, unattended=False))
    assert teller.note(capstate.read(agent("high"), window=8192, unattended=False)) == "[state] effort=high"
    assert teller.note(capstate.read(agent("high"), window=65536, unattended=False)) == "[state] window=65536"
    assert teller.note(capstate.read(agent("high", plan_mode=True), window=65536, unattended=False)) == "[state] plan_mode=on"


def test_after_a_fold_everything_is_told_again_once():
    state = capstate.read(agent("low"), window=8192, unattended=False)
    teller = capstate.Teller({"window": 8192})
    teller.note(state, folds=0)
    again = teller.note(state, folds=1)
    assert "effort=low" in again and "window=8192" in again
    assert teller.note(state, folds=1) == ""
