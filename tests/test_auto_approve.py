"""The technical session may approve a call the person would be asked about; it never decides alone anything the person must decide."""
from tadamoo import auto_approve, side_uses
from tadamoo.permissions import PermissionLevel, PermissionManager
from tadamoo.tools.registry import EffectClass


class Client:
    def __init__(self, word, local=True):
        self.word, self.calls = word, 0
        self._local = local

    def stays_local(self):
        return self._local

    def chat_completion(self, messages, **kw):
        self.calls += 1
        return {"choices": [{"message": {"content": self.word}}]}


def manager(client, level=PermissionLevel.DEFAULT, on=True, monkeypatch=None):
    asked, told = [], []
    perms = PermissionManager(level=level, confirm_callback=lambda t, a: asked.append(t) or False)
    perms.approver = auto_approve.make_approver(client, lambda: "tidy the folder")
    perms.on_auto_approved = lambda t, a: told.append(t)
    return perms, asked, told


def test_a_yes_lets_the_call_run_and_is_reported(monkeypatch):
    monkeypatch.setattr(auto_approve, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    side_uses.save("auto_approve", on=True)
    perms, asked, told = manager(Client("yes"))
    assert perms.evaluate("shell_command", EffectClass.IRREVERSIBLE, {"command": "move a b"})[0]
    assert told == ["shell_command"] and not asked


def test_a_no_or_a_doubt_leaves_the_question_to_the_person(monkeypatch):
    monkeypatch.setattr(auto_approve, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    side_uses.save("auto_approve", on=True)
    perms, asked, told = manager(Client("no"))
    assert not perms.evaluate("shell_command", EffectClass.IRREVERSIBLE, {"command": "del x"})[0] and asked == ["shell_command"] and not told


def test_it_is_off_until_the_person_switches_it_on(monkeypatch):
    monkeypatch.setattr(auto_approve, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    side_uses.reset()
    client = Client("yes")
    perms, asked, _ = manager(client)
    assert not perms.evaluate("shell_command", EffectClass.IRREVERSIBLE, {"command": "x"})[0] and client.calls == 0 and asked


def test_what_the_person_must_decide_is_never_put_to_the_model(monkeypatch):
    monkeypatch.setattr(auto_approve, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    side_uses.save("auto_approve", on=True)
    client = Client("yes")
    perms, asked, told = manager(client)
    perms.note_untrusted_input("browse_url")
    perms.note_private_data("a file with keys")
    assert not perms.evaluate("browse_url", EffectClass.PURE, {"url": "https://x.example"}, outward=True)[0] and asked          # the Rule of Two
    perms.note_instructions("read_attachment")
    assert not perms.evaluate("write_file", EffectClass.REVERSIBLE, {"path": "a"})[0]                                           # orders from outside
    assert not perms.evaluate("phone_call", EffectClass.IRREVERSIBLE, {})[0] and client.calls == 0 and not told                 # a tool that always asks
