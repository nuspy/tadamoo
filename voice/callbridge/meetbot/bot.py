"""MeetingSession: one meeting, one bot. It joins as a guest, announces itself, waits for consent, listens, writes minutes and may speak.

The order that matters:
  1. join (the microphone and the camera stay off unless speaking is allowed), and wait to be let in;
  2. announce in the meeting chat and, when speaking is possible, by voice; if no channel carries it the bot leaves;
  3. only then the audio is captured and the speech recognised; what is heard is held in memory, nothing is on disk;
  4. the consent gate (consent.py) decides; on an objection, a removal or a Leave before consent everything held is deleted and the bot
     leaves; on consent the meeting's folder is made and the transcript is kept there until the person deletes it.
The browser is used from the session's own thread only. `step()` is one turn of its loop and takes no time of its own, so a test drives a
session with a fake clock and fake pieces."""
from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import consent, speak
from .audio import find_virtual_mic
from .adapters import adapter_for
from .audit import AuditError, MeetingAudit, mask_name
from .browser import BrowserMissing
from .transcribe import SpeakerTimeline, TranscriptStore

log = logging.getLogger("callbridge.meetbot")

TICK_S = 0.4
ADMIT_WAIT_S = 600.0
CALL_LOST_S = 15.0
REANNOUNCE_GAP_S = 30.0
ACTIVE = ("joining", "waiting_admit", "in_call", "leaving")
_OWN_SENDERS = ("you", "tu")


@dataclass
class Plan:
    """What one join is: the address, who it is for and the rules it runs under."""
    url: str
    on_behalf_of: str
    title: str = ""
    display_name: str = "TaDaMoo Bot"
    rights: str = "ticked"                 # "ticked" (the person ticked the box) | "calendar" (the person asked for this event)
    speak: bool = False
    proactive: bool = False
    consent_mode: str = "window"
    consent_window_s: int = consent.WINDOW_DEFAULT_S
    lang: str = "en"
    wake_phrase: str = ""
    virtual_mic: str = ""
    host: str = ""                         # the organiser when known (calendar); masked in the audit


@dataclass
class Deps:
    """The pieces a session uses; the service builds them from real devices, tests from fakes."""
    audit: MeetingAudit
    root: Path
    brains: object
    browser_factory: Callable[[bool], object]
    stt_factory: Callable[[str], object]
    source_factory: Callable[[], object]
    mic_names: Callable[[], list[str]]
    sink_factory: Callable[[str], object]
    tts_factory: Callable | None = None
    configure_tts: Callable | None = None
    lips: Callable | None = None
    on_finished: Callable[[dict, Path], None] | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def safe_url(url: str) -> str:
    """The address without its query and fragment (a passcode lives there)."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


class MeetingSession:
    def __init__(self, plan: Plan, deps: Deps) -> None:
        if not plan.rights:
            raise ValueError("the right to bring an assistant to this meeting was not confirmed")
        self.plan, self.deps = plan, deps
        self.adapter = adapter_for(plan.url)
        self.platform = self.adapter.platform if self.adapter else ""
        self.session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{self.platform or 'x'}-{uuid.uuid4().hex[:4]}"
        self.dir = deps.root / self.session_id
        self.state, self.error, self.end_reason = "joining", "", ""
        self.gate = consent.ConsentGate(plan.consent_mode, plan.consent_window_s)
        self.transcript = TranscriptStore(deps.clock)
        self.timeline = SpeakerTimeline(clock=deps.clock)
        self.floor = speak.Floor(deps.clock)
        self.announced = {"chat": False, "voice": False}
        self._names = speak.names_of(plan.display_name, plan.wake_phrase)
        self.voice = self._make_voice()
        self._cmds: "queue.Queue[str]" = queue.Queue()
        self._said: list[str] = []
        self._posted: list[str] = []
        self._present: set[str] = set()
        self._pending_new: set[str] = set()
        self._last_reannounce = float("-inf")
        self._last_proactive = float("-inf")
        self._lost_since: float | None = None
        self._ending = self._finalized = self._kept = False
        self._busy_reply = False
        self._threads: list[threading.Thread] = []
        self._voice_threads: list[threading.Thread] = []
        self.page = self.browser = self.stt = self.source = None
        self.participants_seen = 0
        self.started = deps.clock()

    # ---- set-up
    def _make_voice(self) -> speak.MeetingVoice:
        d, p = self.deps, self.plan
        self.mic_name = find_virtual_mic(d.mic_names(), p.virtual_mic) if p.speak else None
        return speak.MeetingVoice(enabled=p.speak, mic_name=self.mic_name, tts_factory=d.tts_factory, sink_factory=d.sink_factory, floor=self.floor,
                                  lang=p.lang, lips=d.lips, sleep=d.sleep, clock=d.clock,
                                  configure=(lambda tts, lang: d.configure_tts(tts, lang)) if d.configure_tts else None)

    @property
    def speak_blocked(self) -> str:
        return self.voice.block_reason() if self.plan.speak else ""

    def _audit(self, event: str, **fields) -> None:
        try:
            self.deps.audit.event(self.session_id, event, **fields)
        except AuditError as e:
            log.warning("audit row '%s' not written: %s", event, e)

    # ---- lifecycle
    def start(self) -> None:
        """Write the first audit row (the bot does not join if it cannot) and run the session in its own thread."""
        self.audit_join()
        t = threading.Thread(target=self.run, name="meeting-session", daemon=True)
        t.start()
        self._threads.append(t)

    def audit_join(self) -> None:
        p = self.plan
        self.deps.audit.join(self.session_id, self.platform, rights=p.rights, on_behalf_of=mask_name(p.on_behalf_of), host=mask_name(p.host),
                             consent_mode=self.gate.mode, consent_window_s=self.gate.window_s, speak=bool(p.speak and not self.speak_blocked),
                             speak_blocked=self.speak_blocked, proactive=p.proactive)

    def leave(self) -> None:
        self._cmds.put("leave")

    def run(self) -> None:
        try:
            if self.adapter is None:
                self._fail("unsupported_platform", "this address is not a Meet, Teams or Zoom meeting")
                return
            if not self._open():
                return
            if not self._announce():
                return
            while not self._finalized and not self._ending:
                self.step()
                self.deps.sleep(TICK_S)
        except BrowserMissing as e:
            self._fail("browser_missing", str(e))
        except Exception as e:  # noqa: BLE001 (any failure must still delete what was held)
            log.error("meeting session failed: %s", e)
            self._fail("error", str(e))
        finally:
            self._close_all()

    def _fail(self, reason: str, message: str) -> None:
        self.state, self.error = "error", message
        self.end_reason = self.end_reason or reason

    def _open(self) -> bool:
        p = self.plan
        media = bool(p.speak and not self.speak_blocked)
        self.browser = self.deps.browser_factory(media)
        self.page = self.browser.open(p.url)
        res = self.adapter.join(self.page, p.display_name, mic=media, cam=media)
        if not res.ok:
            self._fail("join_failed", res.message or "the bot could not join")
            return False
        self.state = res.state
        waited = 0.0
        while self.state == "waiting_admit" and waited < ADMIT_WAIT_S:
            self.deps.sleep(2.0)
            waited += 2.0
            if self._take("leave"):
                self.end_reason, self.state = "left_in_lobby", "leaving"
                return False
            if self.adapter.in_call(self.page):
                self.state = "in_call"
        if self.state != "in_call":
            self._fail("not_admitted", "the bot was not let in within 10 minutes")
            return False
        return True

    # ---- the announcement
    def _announce(self) -> bool:
        p = self.plan
        info = self.adapter.speakers(self.page)
        self._present = {n for n in info.participants if not self._is_me(n)}
        chat = consent.chat_announcement(p.display_name, p.on_behalf_of, self.gate.mode)
        self._posted.append(chat)
        self.announced["chat"] = bool(self.adapter.send_chat(self.page, chat))
        spoken = consent.announcement(p.display_name, p.on_behalf_of, p.lang, self.gate.mode)
        can_speak = not self.speak_blocked and p.speak
        if can_speak and not self.announced["chat"]:
            self._said.append(spoken)
            self.announced["voice"] = self.voice.say(spoken, wait_for_pause=False)
        elif can_speak:
            self._said.append(spoken)
            self._async(lambda: self._speak_announcement(spoken))
            self.announced["voice"] = True                      # being said now; the audit row of the result follows
        if not (self.announced["chat"] or self.announced["voice"]):
            self._audit("announce_failed", chat=False, voice=False)
            self._fail("announcement_failed", "the announcement could not be made in the meeting chat or by voice")
            return False
        self._audit("announced", **self.announced)
        self.gate.announced(self.deps.clock())
        self._start_pipeline()
        return self.stt is not None

    def _speak_announcement(self, text: str) -> None:
        ok = self.voice.say(text, wait_for_pause=False)
        self._audit("spoken", kind="announcement", ok=ok)

    def _start_pipeline(self) -> None:
        """Capture and recognition start here and nowhere earlier."""
        try:
            self.stt = self.deps.stt_factory("")                    # no language forced: the STT setting, else detected
            self.source = self.deps.source_factory()
            self.source.start(self.stt.send_audio)
        except Exception as e:  # noqa: BLE001 (no STT, no loopback)
            log.error("the bot cannot listen: %s", e)
            self.stt = None
            self._fail("cannot_listen", f"the bot cannot listen: {e}")

    # ---- one turn
    def step(self) -> None:
        now = self.deps.clock()
        if self._take("leave"):
            return self._on_person_left()
        self._poll_call(now)
        if self._ending:
            return
        self._drain_chat()
        self._drain_speech(now)
        if not self._ending:
            self.gate.poll(now)
            self._sync_gate()

    def _take(self, cmd: str) -> bool:
        try:
            return self._cmds.get_nowait() == cmd
        except queue.Empty:
            return False

    def _poll_call(self, now: float) -> None:
        info = self.adapter.speakers(self.page)
        mine = [n for n in info.participants if not self._is_me(n)]
        if info.active and not self._is_me(info.active):
            self.timeline.record(info.active)
            self.floor.heard()
        self.transcript.note_participants(mine)
        self.participants_seen = max(self.participants_seen, len(mine))
        fresh = [n for n in mine if n not in self._present]
        self._present = set(mine)
        self._pending_new.update(fresh)
        if self._pending_new and now - self._last_reannounce >= REANNOUNCE_GAP_S and self.gate.state in (consent.WAITING, consent.GRANTED):
            self._reannounce(now)
        if self.adapter.in_call(self.page):
            self._lost_since = None
            return
        self._lost_since = now if self._lost_since is None else self._lost_since
        if now - self._lost_since < CALL_LOST_S:
            return
        why = self.adapter.exit_reason(self.page)
        if why == "removed" or (why != "ended" and self.gate.state == consent.WAITING):
            self.gate.removed()                                 # a host removal, or a call that dropped while consent was still asked for
            self._sync_gate()
        else:
            self._end("meeting_ended")

    def _reannounce(self, now: float) -> None:
        p = self.plan
        self._pending_new.clear()
        self._last_reannounce = now
        text = consent.reannouncement(p.display_name, p.on_behalf_of, p.lang)
        self._posted.append(text)
        chat = bool(self.adapter.send_chat(self.page, text))
        voice = bool(p.speak and not self.speak_blocked)
        if voice:
            self._said.append(text)
            self._async(lambda: self.voice.say(text))
        self._audit("reannounced", chat=chat, voice=voice)

    def _drain_chat(self) -> None:
        for msg in self.adapter.read_chat(self.page):
            if self._is_me(msg.sender) or msg.sender.lower() in _OWN_SENDERS or consent.is_echo(msg.text, self._posted, minimum=3):
                continue
            if self.gate.hear(msg.text, "chat") == "objection":
                self._sync_gate()
                return

    def _drain_speech(self, now: float) -> None:
        if self.stt is None:
            return
        try:
            while True:
                self.stt.speech_events.get_nowait()
                self.floor.heard()
        except (queue.Empty, AttributeError):
            pass
        while True:
            try:
                text = self.stt.utterances.get_nowait().strip()
            except queue.Empty:
                return
            if not text or consent.is_echo(text, self._said):
                continue
            self.floor.heard()
            if self.gate.hear(text, "voice") == "objection":
                self._sync_gate()
                return
            self.transcript.add(text, speaker=self.timeline.dominant(now - 6.0, now), at=now)
            self._sync_gate()
            if self.gate.state == consent.GRANTED:
                self._maybe_reply(text, now)

    # ---- the gate's decisions
    def _sync_gate(self) -> None:
        if self.gate.state == consent.ENDED and not self._ending:
            self._end(self.gate.outcome)
        elif self.gate.state == consent.GRANTED and not self._kept:
            self._keep()

    def _keep(self) -> None:
        """Consent is given: the meeting's folder is made and what is held is written to it."""
        self._kept = True
        self.dir.mkdir(parents=True, exist_ok=True)
        self._write_meta(analysis_state="pending")
        self.transcript.commit(self.dir / "transcript.jsonl")
        self._audit("consent", outcome=self.gate.outcome, mode=self.gate.mode)

    def _write_meta(self, **more) -> None:
        meta = {"title": self.plan.title or self.platform, "url": safe_url(self.plan.url), "platform": self.platform,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "consent": {"mode": self.gate.mode, "outcome": self.gate.outcome}}
        path = self.dir / "meta.json"
        try:
            meta = {**json.loads(path.read_text(encoding="utf-8")), **more}
        except (OSError, ValueError):
            meta.update(more)
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def _on_person_left(self) -> None:
        self.gate.left()
        self._end("left" if self.gate.ever_granted else "left_before_consent")

    def _end(self, reason: str) -> None:
        """End the session now: goodbye when the bot leaves of its own accord and may speak, then leave the call."""
        if self._ending:
            return
        self._ending, self.end_reason, self.state = True, reason, "leaving"
        self.voice.interrupt()
        for t in self._voice_threads:
            t.join(timeout=2.0)
        if reason in ("objection", "no_consent") and self.adapter.in_call(self.page):
            bye = consent.goodbye(self.plan.lang)
            self._posted.append(bye)
            self.adapter.send_chat(self.page, bye)
            if self.plan.speak and not self.speak_blocked:
                self.voice.rearm()
                self.voice.say(bye, wait_for_pause=False)

    # ---- replies
    def _maybe_reply(self, text: str, now: float) -> None:
        if not self.plan.speak or self.speak_blocked or self._busy_reply or self._ending:
            return
        if not speak.addressed(text, self._names):
            if not self.plan.proactive or now - self._last_proactive < speak.PROACTIVE_GAP_S:
                return
            self._last_proactive = now
        self._busy_reply = True
        self._async(lambda: self._reply(text), track=False)

    def _reply(self, text: str) -> None:
        try:
            p = self.plan
            context = [f"{s['speaker']}: {s['text']}" for s in self.transcript.tail(speak.CONTEXT_LINES)]
            answer = speak.ask(self.deps.brains, speak.reply_prompt(p.display_name, p.on_behalf_of, context, text, p.lang))
            if answer and not self._ending and self.gate.state == consent.GRANTED:
                self._said.append(answer)
                ok = self.voice.say(answer)
                self._audit("spoken", kind="reply", ok=ok)
        except Exception as e:  # noqa: BLE001 (a brain that is down must not stop the minutes)
            log.warning("no reply: %s", e)
        finally:
            self._busy_reply = False

    def _async(self, fn: Callable[[], None], track: bool = True) -> None:
        t = threading.Thread(target=fn, name="meeting-voice", daemon=True)
        t.start()
        if track:
            self._voice_threads.append(t)

    def _is_me(self, name: str) -> bool:
        return bool(name) and name.strip().lower() == self.plan.display_name.strip().lower()

    # ---- the end
    def _close_all(self) -> None:
        if self._finalized:
            return
        try:
            if self.page is not None and self.adapter is not None:
                self.adapter.leave(self.page)
        except Exception:  # noqa: BLE001
            pass
        for closer in (getattr(self.source, "stop", None), getattr(self.stt, "close", None), self.voice.close,
                       getattr(self.browser, "close", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        self._finalize()

    def _finalize(self) -> None:
        """The audit row of the end, then either the minutes are started (consent given, no objection) or everything held is deleted."""
        self._finalized = True
        reason = self.end_reason or "ended"
        if self.state != "error":
            self.state = "ended"
        keep = self.gate.ever_granted and not self.gate.refused and self._kept
        self._audit("end", reason=reason, outcome=self.gate.outcome or "none", objections=list(self.gate.objections), participants=self.participants_seen,
                    speak=bool(self.plan.speak and not self.speak_blocked), duration_s=round(self.deps.clock() - self.started, 1), kept=keep)
        count = len(self.transcript.segments)
        names = list(self.transcript.participants)
        if keep:
            self._write_meta(ended=time.strftime("%Y-%m-%dT%H:%M:%S"), participants=names, segments=count, end_reason=reason)
            if self.deps.on_finished is not None:
                self.deps.on_finished(json.loads((self.dir / "meta.json").read_text(encoding="utf-8")), self.dir)
            return
        self.transcript.discard()
        if self.dir.exists():                                   # consent was given and then withdrawn: only a stub is left
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir.mkdir(parents=True, exist_ok=True)
            self._write_meta(ended=time.strftime("%Y-%m-%dT%H:%M:%S"), end_reason=reason, deleted=True, analysis_state="none")

    # ---- what the person sees
    def status(self) -> dict:
        g = self.gate
        left = max(0.0, g.deadline_s - (self.deps.clock() - g.t0)) if g.state == consent.WAITING else 0.0
        return {"session_id": self.session_id, "state": self.state, "platform": self.platform, "title": self.plan.title,
                "consent": {"mode": g.mode, "window_s": g.window_s, "state": g.state, "outcome": g.outcome, "remaining_s": round(left)},
                "announced": dict(self.announced), "participants": self.participants_seen,
                "speak": {"on": bool(self.plan.speak), "blocked": self.speak_blocked, "proactive": self.plan.proactive},
                "transcript_tail": self.transcript.tail(8) if g.state == consent.GRANTED else [], "end_reason": self.end_reason, "error": self.error}
