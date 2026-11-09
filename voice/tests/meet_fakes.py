"""Stand-ins for the meeting bot's tests: a clock, a platform adapter, a browser, speech recognition, an audio source and sink, a TTS and brains.
No browser, no audio device, no network, no thread of their own: a test moves the clock and calls `session.step()`."""
from __future__ import annotations

import queue
from dataclasses import dataclass

import numpy as np

from callbridge.meetbot.adapters.base import ChatMessage, JoinResult, MeetingAdapter, SpeakerInfo
from callbridge.meetbot.audit import MeetingAudit
from callbridge.meetbot.bot import Deps, MeetingSession, Plan

MEET = "https://meet.google.com/abc-defg-hij?pwd=secret"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s

    def sleep(self, s: float) -> None:
        self.now += s


class FakeAdapter(MeetingAdapter):
    """A platform that does what the test tells it to."""
    platform = "meet"

    def __init__(self) -> None:
        super().__init__()
        self.in_call_now = True
        self.participants: list[str] = ["Ada", "Bruno"]
        self.active: str | None = None
        self.chat_out: list[str] = []
        self.chat_in: list[ChatMessage] = []
        self.chat_ok = True
        self.exit = ""
        self.left = False
        self.joined_with = None
        self.mic_ok = True
        self.mic = False
        self.mic_log: list[bool] = []
        self.cam = False

    @staticmethod
    def matches(url: str) -> bool:
        return True

    def join(self, page, display_name):
        self.joined_with = display_name
        return JoinResult(True, "in_call")

    def set_mic(self, page, on) -> bool:
        if not self.mic_ok:
            return False
        self.mic = on
        self.mic_log.append(on)
        return True

    def set_cam(self, page, on) -> bool:
        self.cam = on
        return True

    def in_call(self, page) -> bool:
        return self.in_call_now

    def speakers(self, page) -> SpeakerInfo:
        return SpeakerInfo(self.active, list(self.participants))

    def send_chat(self, page, text) -> bool:
        if self.chat_ok:
            self.chat_out.append(text)
        return self.chat_ok

    def read_chat(self, page):
        out, self.chat_in = self.chat_in, []
        return out

    def exit_reason(self, page) -> str:
        return self.exit

    def leave(self, page) -> None:
        self.left = True


class FakeBrowser:
    def __init__(self, media=False) -> None:
        self.media, self.closed, self.url = media, False, None

    def open(self, url):
        self.url = url
        return object()

    def close(self) -> None:
        self.closed = True


class FakeStt:
    """Speech recognition: a test puts what was said in `utterances`."""

    def __init__(self) -> None:
        self.utterances: "queue.Queue[str]" = queue.Queue()
        self.speech_events: "queue.Queue[str]" = queue.Queue()
        self.audio: list = []
        self.closed = False

    def send_audio(self, pcm) -> None:
        self.audio.append(pcm)

    def close(self) -> None:
        self.closed = True

    def say(self, *texts: str) -> None:
        for t in texts:
            self.utterances.put(t)


class FakeSource:
    def __init__(self) -> None:
        self.started = self.stopped = False

    def start(self, on_pcm) -> None:
        self.started, self.on_pcm = True, on_pcm

    def stop(self) -> None:
        self.stopped = True


class FakeSink:
    """The virtual microphone: keeps what was played; `others_speak` is called while it plays (to simulate someone starting to talk)."""
    plays: list = []

    def __init__(self, name: str) -> None:
        self.name = name
        self.on_block = None

    def play(self, samples, rate, keep_going=lambda: True) -> bool:
        if self.on_block:
            self.on_block()
        if not keep_going():
            return False
        FakeSink.plays.append((self.name, len(samples), rate))
        return True


@dataclass
class _Seg:
    samples: np.ndarray
    sample_rate: int = 24000


class FakeTts:
    def __init__(self) -> None:
        self.audio: "queue.Queue[_Seg]" = queue.Queue()
        self.said: list[str] = []
        self.closed = False

    def configure(self, **kw) -> None:
        pass

    def send_text(self, text: str) -> None:
        self.said.append(text)

    def flush(self) -> None:
        self.audio.put(_Seg(np.ones(2400, dtype=np.float32) * 0.1))

    def busy(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class DirectBrain:
    mode = "direct"

    def __init__(self, reply: str = "Sure, I can help.") -> None:
        self.reply, self.prompts = reply, []

    def ask_stream(self, text, source="remote"):
        self.prompts.append(text)
        yield self.reply


class HarnessThatMustNotRun:
    """An agent with tools: any use of it fails the test."""
    mode = "harness"

    def respond_stream(self, *a, **kw):
        raise AssertionError("the meeting text reached the harness brain")

    ask_stream = respond_stream


class Brains:
    """The two brains as the service holds them, in harness mode: the meeting must still use only the direct one."""

    def __init__(self, direct=None) -> None:
        self.direct = direct or DirectBrain()
        self.harness = HarnessThatMustNotRun()
        self.mode = "harness"


def make_plan(**kw) -> Plan:
    base = dict(url=MEET, on_behalf_of="Mario Rossi", title="Weekly", display_name="TaDaMoo Bot", lang="en")
    base.update(kw)
    return Plan(**base)


class Rig:
    """A session wired to fakes, with everything a test looks at."""

    def __init__(self, tmp_path, plan: Plan | None = None, mics=("CABLE Input (VB-Audio Virtual Cable)", "Speakers"), tts=True, brains=None) -> None:
        self.clock, self.adapter, self.stt, self.source = Clock(), FakeAdapter(), FakeStt(), FakeSource()
        self.browser, self.tts, self.brains = FakeBrowser(), FakeTts(), brains or Brains()
        self.finished: list = []
        self.audit_path = tmp_path / "meetings-audit.jsonl"
        FakeSink.plays = []
        self.stt_started_at: float | None = None
        self.events: list[str] = []

        def stt_factory(lang):
            self.stt_started_at = self.clock()
            self.events.append("stt")
            return self.stt

        def source_factory():
            self.events.append("source")
            return self.source

        self.deps = Deps(audit=MeetingAudit(self.audit_path), root=tmp_path / "meetings", brains=self.brains, browser_factory=lambda media: self.browser,
                         stt_factory=stt_factory, source_factory=source_factory, mic_names=lambda: list(mics), sink_factory=FakeSink,
                         tts_factory=(lambda: self.tts) if tts else None, configure_tts=None, lips=None,
                         on_finished=lambda meta, folder: self.finished.append((meta, folder)), clock=self.clock, sleep=self.clock.sleep)
        self.session = MeetingSession(plan or make_plan(), self.deps)
        self.session.adapter, self.session.page, self.session.browser = self.adapter, object(), self.browser
        self.session.state = "in_call"
        self.session.audit_join()

    def announce(self) -> bool:
        return self.session._announce()

    def run(self, seconds: float, step: float = 0.4) -> None:
        end = self.clock() + seconds
        while self.clock() < end and not self.session._ending:
            self.session.step()
            self.clock.advance(step)

    def audit(self) -> list[dict]:
        import json
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()] if self.audit_path.exists() else []

    def events_named(self, name: str) -> list[dict]:
        return [r for r in self.audit() if r["event"] == name]

    def finish(self) -> None:
        self.session._close_all()

    def settle(self, seconds: float = 3.0) -> None:
        """Keep the session's loop turning (the microphone is switched by it) until the voice threads and the reply are done."""
        import time
        end = time.monotonic() + seconds
        s = self.session
        while time.monotonic() < end:
            if not any(t.is_alive() for t in s._voice_threads) and not s._busy_reply and not s._mic_want:
                break
            s.step()
            self.clock.advance(0.01)
            time.sleep(0.005)
        s.step()

    @property
    def folder(self):
        return self.session.dir

    def meeting_files(self) -> list[str]:
        root = self.deps.root
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []
