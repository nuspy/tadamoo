"""The meeting bot and the copilot as a feature of the voice service: the pieces a session needs, built from the service's own STT, TTS, brains and
avatar, and the operations the routes of `callbridge/api_meetings.py` call.

A session is built here and run by `callbridge/meetbot/bot.py`. The rules that cannot be weakened live there and in `consent.py`: the person
must confirm the right to bring an assistant, the announcement comes first, nothing is listened to before it, and no consent means the bot
leaves and deletes what it held. This module adds the checks of a request and the folder of past meetings."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from callbridge import turn_engine
from callbridge.call_policy import AuditError
from callbridge.calendar_oauth import GoogleCalendar, MicrosoftCalendar, Vault, default_vault
from callbridge.copilot import CopilotController
from callbridge.meetbot import consent
from callbridge.meetbot.adapters import adapter_for
from callbridge.meetbot.audio import DeviceSink, find_virtual_mic, make_source, output_device_names
from callbridge.meetbot.audit import MeetingAudit
from callbridge.meetbot.bot import ACTIVE, Deps, MeetingSession, Plan
from callbridge.meetbot.browser import PlaywrightBrowser
from callbridge.meetbot.errors import Refused
from callbridge.meetbot.calendar_watch import AutojoinStore, CalendarWatcher
from callbridge.meetbot.speak import Floor, MeetingVoice
from callbridge.meetbot.summary import analyze_meeting
from callbridge.util import is_safe_artifact_id

log = logging.getLogger("voice.meetings")
URL_MAX = 2000


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class MeetingsService:
    def __init__(self, state, vault: Vault | None = None, deps_hook=None) -> None:
        self.state = state
        self.vault = vault if vault is not None else default_vault()
        self._deps_hook = deps_hook                 # tests replace pieces of the dependencies
        self._lock = threading.Lock()
        self.session: MeetingSession | None = None
        self._analyzing: set[str] = set()
        self._watcher: CalendarWatcher | None = None
        self._providers = [GoogleCalendar(self.vault), MicrosoftCalendar(self.vault)]
        self.copilot = CopilotController(state.config, state.brains, self._desktop, self._stt, self._source, self._copilot_voice)

    # ---- where things are
    @property
    def cfg(self):
        return self.state.config

    def root(self) -> Path:
        p = Path(self.cfg.meetings_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _loaded(self, name: str) -> bool:
        functions = self.state.functions
        return functions is not None and functions._live(name) == "loaded"

    def _host(self):
        from voice.gateway import Host
        return Host(self.state, self.cfg.control_auth_token)

    def _desktop(self):
        from callbridge.desktop import platform
        return platform()

    def _stt(self, lang: str):
        return self._host().stt_client(lang)

    def _source(self):
        return make_source(rate=self.cfg.copilot_loopback_samplerate)

    def _tts(self):
        return self._host().tts_client()

    def _configure_tts(self, tts, lang: str) -> None:
        turn_engine.configure_tts(tts, self.cfg, self.cfg.default_voice, lang)

    def _lips(self, samples, rate: int) -> None:
        """Move the avatar's lips with the sentence the bot is about to speak (no sound from the avatar: the voice goes to the meeting)."""
        avatar = self.state.avatar
        if avatar is None or not avatar.loaded() or not avatar.client().is_ready():
            return
        fd, path = tempfile.mkstemp(prefix="meet-lips-", suffix=".wav")
        with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
        avatar.client().speak_wav(path, mute=True)

    def _copilot_voice(self) -> MeetingVoice:
        c = self.cfg
        mic = find_virtual_mic(output_device_names(), c.meetings_virtual_mic)
        return MeetingVoice(enabled=True, mic_name=mic, tts_factory=self._tts if self._loaded("tts") else None, sink_factory=DeviceSink, floor=Floor(),
                            lang=c.copilot_lang, configure=self._configure_tts)

    def deps(self) -> Deps:
        c = self.cfg
        deps = Deps(audit=MeetingAudit(c.meetings_audit_file or None), root=self.root(), brains=self.state.brains, browser_factory=PlaywrightBrowser,
                    stt_factory=self._stt, source_factory=self._source, mic_names=output_device_names, sink_factory=DeviceSink,
                    tts_factory=self._tts if self._loaded("tts") else None, configure_tts=self._configure_tts, lips=self._lips,
                    on_finished=self._finished)
        return self._deps_hook(deps) if self._deps_hook else deps

    # ---- the bot
    def settings_defaults(self) -> dict:
        c = self.cfg
        return {"speak": c.meetings_speak, "proactive": c.meetings_proactive, "consent_mode": c.meetings_consent_mode, "consent_window_s": c.meetings_consent_window_s,
                "display_name": c.meetings_display_name, "on_behalf_of": c.meetings_on_behalf_of}

    def plan_from(self, body: dict, calendar: bool = False) -> Plan:
        """The plan of a join from a request, or Refused. Nothing is started here. `calendar`: the person asked for this event in the calendar
        (set by the watcher only, never by a request)."""
        if not isinstance(body, dict):
            raise Refused(400, "bad_request", "Send a JSON object.")
        c = self.cfg
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or len(url) > URL_MAX or adapter_for(url) is None:
            raise Refused(400, "unsupported_platform", "Give the address of a Google Meet, Microsoft Teams or Zoom meeting.")
        if body.get("rights_confirmed") is not True and not calendar:
            raise Refused(400, "rights_required", "Confirm that you have the right to bring an assistant to this meeting (rights_confirmed: true).")
        who = body.get("on_behalf_of", c.meetings_on_behalf_of)
        if not isinstance(who, str) or not who.strip() or len(who.strip()) > 80 or any(ord(ch) < 32 for ch in who):
            raise Refused(400, "on_behalf_missing", "Say on whose behalf the assistant attends (Settings > Voice > Meetings): the announcement names that person.")
        mode = body.get("consent_mode", c.meetings_consent_mode)
        if mode not in consent.MODES:
            raise Refused(400, "bad_request", "consent_mode is window or explicit.")
        window = body.get("consent_window_s", c.meetings_consent_window_s)
        if _int(window) is None or not consent.WINDOW_MIN_S <= window <= consent.WINDOW_MAX_S:
            raise Refused(400, "bad_request", f"consent_window_s is a whole number from {consent.WINDOW_MIN_S} to {consent.WINDOW_MAX_S}.")
        flags = {k: body.get(k, getattr(c, f"meetings_{k}")) for k in ("speak", "proactive")}
        flags["camera"] = c.meetings_camera
        if not all(isinstance(v, bool) for v in flags.values()):
            raise Refused(400, "bad_request", "speak and proactive are true or false.")
        title = body.get("title") if isinstance(body.get("title"), str) else ""
        return Plan(url=url, on_behalf_of=who.strip(), title=title.strip()[:120], display_name=c.meetings_display_name,
                    rights="calendar" if calendar else "ticked", speak=flags["speak"], camera=flags["camera"], proactive=flags["proactive"],
                    consent_mode=mode, consent_window_s=window, lang=c.default_lang or "en", wake_phrase=c.meetings_wake_phrase,
                    virtual_mic=c.meetings_virtual_mic, host=body.get("host", "") if calendar and isinstance(body.get("host"), str) else "")

    def join(self, body: dict, calendar: bool = False) -> dict:
        plan = self.plan_from(body, calendar)
        if not self._loaded("stt"):
            raise Refused(409, "not_loaded", "The speech to text must be loaded first (Models > Voice services): the bot listens with the service's own STT.", missing=["stt"])
        with self._lock:
            if self.session is not None and self.session.state in ACTIVE:
                raise Refused(409, "busy", "A meeting is in progress: one at a time.")
            session = MeetingSession(plan, self.deps())
            try:
                session.start()
            except AuditError as e:
                raise Refused(500, "audit_failed", f"The audit file cannot be written, so the bot does not join: {e}") from None
            self.session = session
        return {"session_id": session.session_id, "state": session.state, "speak_blocked": session.speak_blocked}

    def leave(self) -> dict:
        session = self.session
        if session is None or session.state not in ACTIVE:
            return {"state": "idle"}
        session.leave()
        return {"state": "leaving", "session_id": session.session_id}

    def status(self) -> dict:
        session = self.session
        out = session.status() if session is not None else {"state": "idle"}
        out["copilot"] = self.copilot.state()
        return out

    # ---- minutes
    def _finished(self, meta: dict, folder: Path) -> None:
        """The meeting ended with consent: the minutes are written in a thread of their own (the model may take minutes)."""
        def run() -> None:
            self._analyzing.add(folder.name)
            try:
                analyze_meeting(self.cfg, folder, meta)
            finally:
                self._analyzing.discard(folder.name)

        threading.Thread(target=run, name="meeting-minutes", daemon=True).start()

    # ---- past meetings
    def _folder(self, mid: str) -> Path:
        if not is_safe_artifact_id(mid):
            raise Refused(400, "bad_request", "invalid id")
        folder = self.root() / mid
        if folder.resolve().parent != self.root().resolve() or not folder.is_dir():
            raise Refused(404, "not_found", "meeting not found")
        return folder

    @staticmethod
    def _meta(folder: Path) -> dict:
        try:
            data = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def list(self) -> list[dict]:
        out = []
        for folder in sorted(self.root().iterdir(), reverse=True):
            meta = self._meta(folder) if folder.is_dir() else {}
            if not meta:
                continue
            out.append({"id": folder.name, "title": meta.get("title"), "platform": meta.get("platform"), "created": meta.get("created"), "ended": meta.get("ended"),
                        "participants": meta.get("participants") or [], "segments": meta.get("segments"), "end_reason": meta.get("end_reason"),
                        "deleted": bool(meta.get("deleted")), "analysis_state": "analyzing" if folder.name in self._analyzing else meta.get("analysis_state", "none")})
        return out

    def get(self, mid: str) -> dict:
        folder = self._folder(mid)
        out = {"id": folder.name, **self._meta(folder)}
        if folder.name in self._analyzing:
            out["analysis_state"] = "analyzing"
        try:
            out["analysis"] = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return out

    def transcript(self, mid: str) -> list[dict]:
        path = self._folder(mid) / "transcript.jsonl"
        if not path.is_file():
            raise Refused(404, "not_found", "no transcript for this meeting")
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def delete(self, mid: str) -> None:
        """Delete the whole folder: the transcript and the minutes with it."""
        folder = self._folder(mid)
        if self.session is not None and self.session.session_id == folder.name and self.session.state in ACTIVE:
            raise Refused(409, "in_progress", "This meeting is in progress.")
        if folder.name in self._analyzing:
            raise Refused(409, "analyzing", "The minutes are being written: try again in a moment.")
        shutil.rmtree(folder)

    # ---- calendars
    def calendar_status(self) -> dict:
        c = self.cfg
        out = {"autojoin": bool(c.meetings_calendar_autojoin)}
        for p in self._providers:
            out[p.name] = {"configured": p.configured(), "connected": p.connected() if p.configured() else False, "flow_error": p.flow_error,
                           "hint": "" if p.configured() else p.NOT_CONFIGURED}
        return out

    def _provider(self, name: str):
        found = next((p for p in self._providers if p.name == name), None)
        if found is None:
            raise Refused(400, "bad_request", "unknown provider")
        return found

    def calendar_connect(self, name: str) -> dict:
        return self._provider(name).start_connect()

    def calendar_disconnect(self, name: str) -> None:
        self._provider(name).disconnect()

    def watcher(self) -> CalendarWatcher:
        if self._watcher is None:
            self._watcher = CalendarWatcher(self._providers, AutojoinStore(self.root() / "autojoin.json"), self._calendar_join, float(self.cfg.calendar_poll_s))
            if self.cfg.meetings_calendar_autojoin:
                self._watcher.start()
        return self._watcher

    def _calendar_join(self, url: str, title: str, organizer: str) -> bool:
        try:
            self.join({"url": url, "title": title, "host": organizer}, calendar=True)
        except Refused as e:
            log.warning("auto-join refused: %s", e.message)
            return False
        return True
