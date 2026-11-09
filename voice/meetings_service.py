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
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from callbridge import turn_engine
from callbridge.avatar_store import AvatarStore
from callbridge.call_policy import AuditError
from callbridge.calendar_oauth import GoogleCalendar, MicrosoftCalendar, Vault, default_vault
from callbridge.copilot import CopilotController
from callbridge.meetbot import consent
from callbridge.meetbot.adapters import adapter_for, platform_names
from callbridge.meetbot.audio import DeviceSink, find_virtual_mic, make_source, output_device_names
from callbridge.meetbot.audit import MeetingAudit
from callbridge.meetbot.bot import ACTIVE, Deps, MeetingSession, Plan
from callbridge.meetbot.browser import PlaywrightBrowser
from callbridge.meetbot.errors import Refused
from callbridge.meetbot.calendar_watch import CalendarWatcher
from callbridge.meetbot.plan_store import FILE as PLAN_FILE, PlanStore
from callbridge.meetbot.planner import Planner
from callbridge.meetbot.speak import Floor, MeetingVoice
from callbridge.meetbot.summary import analyze_meeting
from callbridge.tadamoo_api import ApiError, TadamooApi
from callbridge.util import is_safe_artifact_id
from voice import persona_link

log = logging.getLogger("voice.meetings")
URL_MAX = 2000


@dataclass
class PersonaInfo:
    """What a meeting takes from a personality: its avatar, its voice and its own text."""
    slug: str
    found: bool = False
    avatar: str = ""
    engine: str = ""
    voice: str = ""
    language: str = ""
    block: str = ""
    problem: str = ""


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class MeetingsService:
    def __init__(self, state, vault: Vault | None = None, deps_hook=None, persona_hook=None, clock=None) -> None:
        self.state = state
        self.vault = vault if vault is not None else default_vault()
        self._deps_hook = deps_hook                 # tests replace pieces of the dependencies
        self._persona_hook = persona_hook           # tests: (slug) -> PersonaInfo
        self._clock = clock                         # tests: the planner's clock
        self._lock = threading.Lock()
        self.session: MeetingSession | None = None
        self._analyzing: set[str] = set()
        self._watcher: CalendarWatcher | None = None
        self._planner: Planner | None = None
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

    def _configure_tts(self, tts, lang: str, voice: str = "", engine: str = "") -> None:
        turn_engine.configure_tts(tts, self.cfg, voice or self.cfg.default_voice, lang, engine=engine)

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

    def deps(self, plan: Plan | None = None) -> Deps:
        c = self.cfg
        voice, engine = (plan.voice_name, plan.voice_engine) if plan is not None else ("", "")
        deps = Deps(audit=MeetingAudit(c.meetings_audit_file or None), root=self.root(), brains=self.state.brains, browser_factory=PlaywrightBrowser,
                    stt_factory=self._stt, source_factory=self._source, mic_names=output_device_names, sink_factory=DeviceSink,
                    tts_factory=self._tts if self._loaded("tts") else None, lips=self._lips, on_finished=self._finished,
                    configure_tts=lambda tts, lang: self._configure_tts(tts, lang, voice, engine))
        return self._deps_hook(deps) if self._deps_hook else deps

    # ---- the bot
    def settings_defaults(self) -> dict:
        c = self.cfg
        return {"speak": c.meetings_speak, "proactive": c.meetings_proactive, "consent_mode": c.meetings_consent_mode, "consent_window_s": c.meetings_consent_window_s,
                "display_name": c.meetings_display_name, "on_behalf_of": c.meetings_on_behalf_of}

    def plan_from(self, body: dict, rights: str = "ticked", item: dict | None = None) -> Plan:
        """The plan of a join from a request, or Refused. Nothing is started here. `rights` is set by the code that calls, never by a request:
        "ticked" needs `rights_confirmed` in the body; "planned" is the person's own plan (a meeting they planned and ticked Participate for), and
        `item` is that plan's item, which brings the personality, the camera and the role."""
        if not isinstance(body, dict):
            raise Refused(400, "bad_request", "Send a JSON object.")
        c = self.cfg
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or len(url) > URL_MAX or adapter_for(url) is None:
            raise Refused(400, "unsupported_platform", "Give the address of a meeting on a platform with an adapter (" + ", ".join(platform_names()) + ").")
        if body.get("rights_confirmed") is not True and rights == "ticked":
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
        flags["camera"] = bool(item.get("camera")) if item is not None else c.meetings_camera
        if not all(isinstance(v, bool) for v in flags.values()):
            raise Refused(400, "bad_request", "speak and proactive are true or false.")
        title = body.get("title") if isinstance(body.get("title"), str) else ""
        plan = Plan(url=url, on_behalf_of=who.strip(), title=title.strip()[:120], display_name=c.meetings_display_name,
                    rights=rights, speak=flags["speak"], camera=flags["camera"], proactive=flags["proactive"],
                    consent_mode=mode, consent_window_s=window, lang=c.default_lang or "en", wake_phrase=c.meetings_wake_phrase,
                    virtual_mic=c.meetings_virtual_mic, host=body.get("host", "") if item is not None and isinstance(body.get("host"), str) else "")
        if item is not None:
            plan.minutes = item.get("role") != "listen"
            self._apply_persona(plan, item)
        return plan

    # ---- the personality of a planned meeting
    def _persona_info(self, slug: str) -> PersonaInfo:
        if self._persona_hook is not None:
            return self._persona_hook(slug)
        c = self.cfg
        api = TadamooApi(c.tadamoo_base_url, c.tadamoo_token, timeout=5)
        store = AvatarStore(c.avatars_dir or "")
        link = persona_link.resolve(slug, api, store)
        info = PersonaInfo(slug, bool(link.name), link.avatar, link.voice.get("engine", ""), link.voice.get("voice", ""), link.language, "", link.problem)
        if info.found:
            if not info.engine and info.avatar:
                made = store.voice_of(info.avatar)
                info.engine, info.voice = made.get("engine", ""), made.get("voice", "")
            try:
                info.block = str(api.post(f"/api/personas/{slug}/compose", {"goal": ""}).get("system") or "")      # the personality's text only, not its notes about the person
            except ApiError as e:
                info.problem = info.problem or f"the personality's text could not be read: {e}"
        return info

    def _apply_persona(self, plan: Plan, item: dict) -> None:
        """Put the personality of the item on the plan: its text, its voice, its language, and its avatar as the camera. A camera that cannot show
        the personality's avatar stays off and the plan says why."""
        slug = item.get("persona") or ""
        info = self._persona_info(slug) if slug else None
        if info is not None and info.found:
            plan.persona, plan.persona_block, plan.voice_engine, plan.voice_name = slug, info.block, info.engine, info.voice
            plan.lang = info.language or plan.lang
        elif info is not None:
            log.warning("the personality of a planned meeting is not available: %s", info.problem)
        if not plan.camera:
            return
        avatar = self.state.avatar
        if info is None:
            plan.camera_blocked = "no_persona"
        elif not info.found:
            plan.camera_blocked = "persona_missing"
        elif not info.avatar:
            plan.camera_blocked = "no_avatar"
        elif not self.cfg.avatar_virtual_camera:
            plan.camera_blocked = "virtual_camera_off"
        elif avatar is None or not avatar.loaded():
            plan.camera_blocked = "avatar_not_loaded"
        else:
            client = avatar.client()
            if client._active_persona != info.avatar:
                client.start_async(info.avatar)                 # the personality's avatar, not the one that happened to be loaded
        if plan.camera_blocked:
            plan.camera = False

    def join(self, body: dict, rights: str = "ticked", item: dict | None = None) -> dict:
        plan = self.plan_from(body, rights, item)
        if not self._loaded("stt"):
            raise Refused(409, "not_loaded", "The speech to text must be loaded first (Models > Voice services): the bot listens with the service's own STT.", missing=["stt"])
        with self._lock:
            if self.session is not None and self.session.state in ACTIVE:
                raise Refused(409, "busy", "A meeting is in progress: one at a time.")
            session = MeetingSession(plan, self.deps(plan))
            try:
                session.start()
            except AuditError as e:
                raise Refused(500, "audit_failed", f"The audit file cannot be written, so the bot does not join: {e}") from None
            self.session = session
        return {"session_id": session.session_id, "state": session.state, "speak_blocked": session.speak_blocked,
                **({"camera_blocked": plan.camera_blocked} if plan.camera_blocked else {})}

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
        out = {"autojoin": bool(c.meetings_autojoin)}
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

    def calendar_events(self, days: int) -> list[dict]:
        events = self.watcher().fetch_events(days=days)
        planner = self.planner()
        return [{**e, "participate": planner.participating(e["id"])} for e in events]

    def calendar_participate(self, event_id: str, enabled: bool) -> dict:
        self.planner().set_participate(event_id, enabled)
        return {"id": event_id, "participate": enabled}

    # ---- the plan: the meetings the person planned and the calendar's, and the loop that joins them
    def watcher(self) -> CalendarWatcher:
        if self._watcher is None:
            self._watcher = CalendarWatcher(self._providers, lambda now: self.planner().tick(now), lambda: bool(self.cfg.meetings_autojoin),
                                            float(self.cfg.calendar_poll_s), clock=self._clock or time.time)
        return self._watcher

    def planner(self) -> Planner:
        if self._planner is None:
            kw = {"clock": self._clock} if self._clock else {}
            self._planner = Planner(PlanStore(self.root() / PLAN_FILE, **kw), lambda: self.watcher().cached_events(), self._planned_join,
                                    lambda: bool(self.cfg.meetings_autojoin), self._active_id, **kw)
        return self._planner

    def start(self) -> None:
        """Start the loop that joins planned meetings. It does nothing while the person's master switch (meetings.autojoin) is off."""
        self.watcher().start()

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()

    def _active_id(self) -> str:
        session = self.session
        return session.session_id if session is not None and session.state in ACTIVE else ""

    @staticmethod
    def _item_body(item: dict) -> dict:
        body = {"url": item["url"], "title": item.get("title") or "", "speak": bool(item.get("speak")), "host": item.get("organizer") or ""}
        if item.get("consent_mode"):
            body["consent_mode"] = item["consent_mode"]
        return body

    def _planned_join(self, item: dict) -> dict:
        """The loop's join of a meeting the person planned and ticked. The consent rule is the bot's and is not skipped."""
        return self.join(self._item_body(item), rights="planned", item=item)

    def plan_list(self) -> dict:
        planner = self.planner()
        return {"items": planner.view(), "platforms": platform_names(), "autojoin": bool(self.cfg.meetings_autojoin), "lead_s": planner.lead_s}

    def plan_add(self, body: dict) -> dict:
        return {"item": self.planner().add(body)}

    def plan_update(self, item_id: str, body: dict) -> dict:
        return {"item": self.planner().update(item_id, body)}

    def plan_delete(self, item_id: str) -> dict:
        self.planner().delete(item_id)
        return {"status": "ok"}

    def plan_join_now(self, item_id: str, body: dict) -> dict:
        """A join of a planned meeting that the person asks for now: the same checks as a join from the page, with the box confirmed."""
        item = self.planner().get(item_id)
        if item is None:
            raise Refused(404, "not_found", "No such meeting in the plan.")
        body = body if isinstance(body, dict) else {}
        if set(body) - {"rights_confirmed"}:
            raise Refused(400, "bad_request", "Send rights_confirmed: true.")
        out = self.join({**self._item_body(item), "rights_confirmed": body.get("rights_confirmed")}, rights="ticked", item=item)
        self.planner().mark_joined(item_id, out["session_id"], out.get("camera_blocked", ""))
        return out
