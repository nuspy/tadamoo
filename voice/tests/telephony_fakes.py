"""Stand-ins for the telephony tests: a transport router that places nothing, and a clock. No serial port, no phone, no network, no audio."""
from __future__ import annotations

import types

from callbridge.config import Config


class FakeRouter:
    """What `CallRouter` does for the policy: `place_call` is recorded and answered, `hangup` ends the call the way a real transport would."""

    def __init__(self, transport: str = "gsm", result: str = "calling", raises: Exception | None = None):
        self.transport, self.result, self.raises = transport, result, raises
        self.placed: list[dict] = []
        self.brain = None
        self.hangups = 0
        self._gsm_modem = None

    def place_call(self, number, voice=None, lang=None, greeting=None, force=None, brain=None):
        self.placed.append({"number": number, "voice": voice, "lang": lang, "greeting": greeting, "force": force})
        if self.raises:
            raise self.raises
        self.brain = brain
        if self.result != "calling":
            return {"status": self.result, "message": "no transport"}
        return {"status": "calling", "transport": force or self.transport, "number": number}

    def hangup(self):
        self.hangups += 1
        self.finish()
        return {"status": "ok"}

    def finish(self):
        """The transport says the call is over."""
        ended = getattr(self.brain, "call_ended", None)
        if callable(ended):
            ended()

    def refresh(self):
        return self.transport

    def status(self):
        return {"selected": self.transport, "mode": "auto", "transports": {}}


class Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


def config(tmp_path, **over) -> Config:
    cfg = Config()
    cfg.call_transport = "auto"
    cfg.call_audit_file = str(tmp_path / "call-audit.jsonl")
    cfg.gsm_port = "COM-NOT-OPENED"
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def bridge_stub():
    """The part of `CallBridge` the call service touches; it never reaches a phone."""
    return types.SimpleNamespace(gate=None, hangups=0, watchers=0, start_incoming_watcher=lambda: None, hangup=lambda: {"status": "ok"})
