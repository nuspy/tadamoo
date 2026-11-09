"""The voice gateway: what one phone connection does. The server that accepts the connections is voice/gateway.py.

Protocol (JSON text frames and binary PCM16 little-endian mono frames):
  phone -> gw  {"type":"auth","token":"...","render_modes":["wav2lip","visemes","video"],"device":"android","engine":"wav2lip_gan96","resolution":96}
               first message; answered by auth_ok (the token is not needed when the handshake proved it; render_modes are the ways the device can
               draw the avatar, best first; a device that declares none gets the voice only; engine and resolution are optional: the device's own
               choice, else the person's choice for its kind of device, and the engine of that choice is served first)
  gw -> phone  {"type":"auth_ok"} then {"type":"ready","voice":bool,"missing":[...],"avatar":bool,"brain":"direct|harness","persona":"...",
                "render_mode":"wav2lip|visemes|video|none","render_modes":[...],"avatar_name":"...","engine":"...","resolution":px,
                "language":"it","language_mode":"follow|locked"}
  gw -> phone  {"type":"state","voice":bool,"missing":[...],"avatar":bool,"language":"it","language_mode":"follow|locked"}   whenever the loaded functions change
  gw -> phone  {"type":"language","language":"fr","mode":"follow|locked","reason":"detected|command|auto|refused","warning":"","engine_fallback":bool}
               whenever the spoken language or its mode changes, or a request was refused (docs/voice/language.md)
  phone -> gw  {"type":"language","language":"fr|auto"}   the person's own request: a language they enabled, or back to automatic
  phone -> gw  {"type":"start","mode":"hold|continuous","lang":"it","voice":"...","sample_rate":16000,"duplex":false}
  phone -> gw  <PCM16 frames>, then {"type":"end"} (hold mode only; continuous closes the turn on a pause)
  phone -> gw  {"type":"text","text":"..."}           a typed or corrected sentence: the same turn without the STT
  gw -> phone  {"type":"transcript","text":"..."}, {"type":"reply","chunk":"..."}, {"type":"audio_start","sample_rate":24000,"segment":n},
               <PCM16 frames>, {"type":"audio_end"}, {"type":"done"}
  gw -> phone  in visemes mode, before the PCM of every segment: {"type":"visemes","segment":n,"text":"...","lang":"it","duration_ms":int,
               "pcm_bytes":int,"visemes":[{"t":ms,"v":"aa","dur":ms}...]}
  phone -> gw  {"type":"interrupt"} or a new start while the reply plays: the reply stops (gw -> phone {"type":"interrupt"})
  phone -> gw  {"type":"user_speaking","state":"start|end","level":0..1,"ms":n} and a start with "barge_in": true: the person speaks over the voice;
               gw -> phone {"type":"duck","level":0..1}, then nothing, an interrupt, or the new answer (interruption.py, docs/voice/interruptions.md)
  gw -> phone  {"type":"resume_offer","text":"...","question":"...","lang":"it"}; phone -> gw {"type":"resume","accept":bool}
  phone -> gw  {"type":"brain","brain":"direct|harness"} / {"type":"persona","persona":"slug"}; both are confirmed with the same message
  phone -> gw  {"type":"render_mode","mode":"wav2lip|visemes"}   the device chooses among the modes the gateway offers it
  phone -> gw  {"type":"render_modes","modes":["wav2lip","visemes"],"engine":"...","resolution":px}   the device's capabilities changed (a self-test passed or
               failed, another engine, a thermal downgrade): the same declaration as in auth, validated the same way, at most 6 per minute; the modes
               replace the declared ones (a mode reported as failing is tried again), engine and resolution only when present (null clears);
               gw -> phone {"type":"render_modes","modes":[effective set],"render_mode":"...","avatar_name":"...","engine":"...","resolution":px}
  phone -> gw  {"type":"render_failed","mode":"...","why":"..."}   the mode does not work on the device; gw -> phone {"type":"render_mode","mode":next|"none",
               "previous":"...","why":"...","render_modes":[...]}: the next one of the device's list, remembered for the connection
  phone -> gw  {"type":"video_start","reason":"emergency","sdp":"...","persona":"...","brain":"..."}; gw -> phone {"type":"video_answer","sdp":"..."},
               {"type":"video_state","state":"preparing|live","detail":"..."} or {"type":"video_unavailable","reason":"emergency_only|no_avatar|no_webrtc"}
  phone -> gw  {"type":"video_stop"}; gw -> phone {"type":"video_stopped"}
  gw -> phone  {"type":"error","code":"...","message":"..."}

The brain is the one of callbridge/brains.py; the phone never chooses its permission level and cannot answer an approval by voice (the text
goes to it with the source "remote", which the brains leave on the screen). The STT and the TTS are the loaded functions of the service."""
from __future__ import annotations

import hmac
import json
import logging
import queue
import re
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from voice.avatar_engines import selection as engine_selection

from . import chunk_policy
from . import spoken
from .language_follow import LanguageSettings, iso
from .language_names import parse_language_command
from .language_session import LanguageSession, Outcome
from .interruption import IGNORE, STOP_AND_INTEGRATE, Decision, Heard, InterruptionManager, Interruptions, integration_note, offer_line, voice_verdict
from .turn_engine import ReplyProcessor, configure_tts
from .util import coalesce_utterances, drain_queue

log = logging.getLogger("callbridge.gateway")

STT_RATE = 16000
#: The ways to draw the avatar: on the device from the photo (wav2lip), on the device from a mouth-shape timeline (visemes), or a video made on
#: this PC (video, an emergency only: it is the one that uses the GPU).
RENDER_MODES = ("wav2lip", "visemes", "video")
#: Modes a device may also declare: its own model trained for one person (voice/avatar_engines/phone_export.py). The PC serves them as it serves wav2lip, the voice and nothing more;
#: the avatar's manifest names them in `render_modes` when the PC exported that engine for a phone.
PERSONAL_MODES = ("ultralight", "feathertalk")
DEVICE_MODES = RENDER_MODES + PERSONAL_MODES
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
#: What a declaration of render modes may carry: entries read from the list, the longest engine id, the tallest picture, and how many
#: declarations after the first one a connection may make in a window.
MODES_READ_MAX = 16
ENGINE_ID_MAX = 64
HEIGHT_MAX = 8192
REDECLARE_MAX = 6
REDECLARE_WINDOW_S = 60.0

#: A transcript shorter than this is noise (a breath, the avatar's own echo), not an interruption.
_MIN_BARGEIN_CHARS = 7

#: A turn with no speech ends after this long; after the button is released with nothing transcribed, after the second.
NO_SPEECH_TIMEOUT_S = 15.0
POST_END_TIMEOUT_S = 8.0

#: The longest wait for the transcript of speech heard over the avatar, once the phone says it ended.
BARGE_STT_WAIT_S = 4.0

#: Characters gathered before a sentence goes to the synthesis when the duplex engine is on.
DUPLEX_MIN_CARATTERI = 40


def _pcm16_to_f32(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _f32_to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _resample(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if from_rate == to_rate:
        return samples
    import soxr
    return soxr.resample(samples, from_rate, to_rate).astype(np.float32)


def _normalize_audio(samples: np.ndarray) -> np.ndarray:
    """Removes the DC offset and attenuates only when the signal clips; it never amplifies (that would turn background noise into speech)."""
    if samples.size == 0:
        return samples
    samples = samples - np.mean(samples)
    peak = float(np.max(np.abs(samples)))
    return samples / peak if peak > 1.0 else samples


def _words(s: str) -> set:
    return set(re.findall(r"\w+", (s or "").lower()))


def _is_echo(text: str, said: str) -> bool:
    """Whether `text` is most likely the avatar's own voice coming back through the microphone (compared by words, not characters)."""
    mine = _words(text)
    if not mine or not said:
        return False
    return len(mine & _words(said)) / len(mine) >= 0.6


def _real_phrase(q, said: str = "") -> str:
    """The first queued utterance that counts as an interruption, "" if none. The queue is looked at, not consumed."""
    try:
        from .engines.whisper_stt import _e_allucinazione
    except Exception:  # noqa: BLE001 (engine not importable)
        def _e_allucinazione(_t):
            return False
    try:
        for t in list(q.queue):
            if not isinstance(t, str):
                continue
            text = t.strip()
            if len(text) >= _MIN_BARGEIN_CHARS and not _e_allucinazione(text) and not _is_echo(text, said):
                return text
    except Exception:  # noqa: BLE001 (a queue of an unexpected kind)
        return "" if q.empty() else "?"
    return ""


def _loads(raw) -> dict:
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return msg if isinstance(msg, dict) else {}


class Connection:
    """One phone connection: authentication, then turns one after the other (hold or continuous), or a full-duplex session."""

    def __init__(self, ws, host, authed: bool = False) -> None:
        self.ws = ws
        self.host = host
        self.cfg = host.cfg
        self.authed = authed                      # the handshake already proved the token
        self._authenticated = False               # the auth message was accepted: nothing but it is read before
        self._redeclared: list[float] = []        # when the device declared its render modes again, inside the window
        self.brain = host.brains.fork()           # its own mode over the same two brains
        self.stt = None
        self.tts = None
        self._persona_set = False
        self._video = None                        # WebRTC session towards the phone
        self._frames = None                       # receives the avatar worker's frames
        self._declared: list[str] = []            # the modes the device can draw, best first
        self._device = ""
        self._want_engine = None                  # what the device declared in its auth message: an engine id and an output height
        self._want_height = None
        self._failed: set[str] = set()            # modes the device reported as not working
        self._render_mode = "none"                # the mode served now (video is the device's own emergency video_start)
        self._avatar_name = ""
        self._persona_slug = ""
        self._segment = 0                         # numbers the audio segments of the connection
        self._stt_lang = (self.cfg.default_lang or "it").split("-")[0]
        self._lang_turno = self._stt_lang
        # The language follows the person's speech (callbridge/language_session.py) when `language.follow` is on and the recognizer's own language is
        # not fixed; then `lang` in a start only gives the first language. Without it the phone's `lang` of each turn rules, as before.
        self._follow = bool(getattr(self.cfg, "language_follow", False)) and not getattr(self.cfg, "stt_language", "")
        self._lang: LanguageSession | None = None
        self._voce_persona: tuple[str, str] | None = None
        self._last_state: tuple | None = None
        self._next_start: dict | None = None      # a start that arrived while a reply was playing
        self._closing = False
        self._cut = False                         # the person interrupted the reply
        self._ultima_risposta = ""                # what the avatar is saying now, to tell its echo from a real interruption
        self._caratteri_tts = 0
        self._da_trattare: list[str] = []
        self._eco_viste = 0
        cfg = self.cfg
        self._im = InterruptionManager(
            Interruptions(getattr(cfg, "interruptions_enabled", True), getattr(cfg, "interruption_judge_timeout_ms", 800),
                          getattr(cfg, "interruption_duck_level", 0.3), getattr(cfg, "interruption_resume_offer", True)),
            judge=getattr(host, "interruption_judge", None), clock=getattr(host, "interruption_clock", time.monotonic), records=getattr(host, "interruption_log", None))
        self._barge: dict | None = None           # speech heard over the avatar that is being collected: {lang, rate, ms, level}
        self._decisions: queue.Queue = queue.Queue()
        self._voice = ""                          # the voice of the last turn, for what the PC says by itself
        self._unduck = False                      # the volume goes back up once the cut answer has been told to stop
        self._judging = 0                         # speech over the avatar whose decision has not been taken yet

    # ---------------------------------------------------------------- plumbing
    def _send_json(self, obj: dict) -> None:
        self.ws.send(json.dumps(obj))

    def _control(self, method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict | list]:
        """A request to the service's own control routes (the avatar worker); (0, {}) when it cannot be made."""
        return self.host.control(method, path, body, timeout)

    def _state_message(self) -> dict:
        fn = self.host.functions()
        missing = [n for n in ("stt", "tts") if not fn.get(n)]
        return {"type": "state", "voice": not missing, "missing": missing, "avatar": bool(fn.get("avatar")), **self._language_fields()}

    def _language_fields(self) -> dict:
        ls = self._lang
        if ls is not None:
            return {"language": ls.language, "language_mode": ls.mode}
        return {"language": self._lang_turno, "language_mode": "follow" if self._follow else "locked"}

    def _state_key(self, msg: dict) -> tuple:
        """What makes a state message worth sending again: the functions, and the language only when it follows the speech."""
        key = (msg["voice"], tuple(msg["missing"]), msg["avatar"])
        return key + (msg["language"], msg["language_mode"]) if self._lang is not None else key

    def _push_state(self) -> None:
        msg = self._state_message()
        key = self._state_key(msg)
        if key != self._last_state:
            self._last_state = key
            self._send_json(msg)

    # ---------------------------------------------------------------- the connection
    def run(self) -> None:
        if not self._authenticate():
            return
        state = self._state_message()
        self._last_state = self._state_key(state)
        offered = self._offered()
        self._render_mode = offered[0] if offered else "none"
        self._send_json({"type": "ready", "voice": state["voice"], "missing": state["missing"], "avatar": state["avatar"],
                         "brain": self.brain.mode, "persona": self.cfg.brain_persona, "render_mode": self._render_mode,
                         "render_modes": offered, "avatar_name": self._avatar_name, **self._engine_fields(self._render_mode),
                         **self._language_fields()})
        log.info("gateway: phone connected (voice %s, avatar %s, device %s, render %s)", state["voice"], state["avatar"], self._device or "?", self._render_mode)
        try:
            while True:
                start = self._wait_start()
                if start is None:
                    break
                if not self._open_voice():
                    self._send_json({"type": "done"})
                    continue
                if start.get("say") is not None:
                    self._do_say_turn(start)
                elif start.get("typed") is not None:
                    self._do_typed_turn(start)
                elif start.get("duplex"):
                    self._do_duplex_session(start)
                else:
                    self._do_turn(start)
        except Exception as e:  # noqa: BLE001 (the connection closed, or a turn failed)
            log.info("gateway: connection ended (%s: %s)", e.__class__.__name__, e)
        finally:
            self._cleanup()

    def _authenticate(self) -> bool:
        try:
            msg = _loads(self.ws.recv(timeout=10))
        except Exception:  # noqa: BLE001 (no message in time, or the peer left)
            return False
        given = msg.get("token")
        proved = self.authed or (isinstance(given, str) and bool(given) and hmac.compare_digest(given.encode("utf-8", "replace"), self.host.token.encode("utf-8")))
        if msg.get("type") == "auth" and proved:
            self._declare(msg)
            self._authenticated = True
            self._send_json({"type": "auth_ok"})
            return True
        self._send_json({"type": "error", "code": "auth_failed", "message": "authentication failed"})
        return False

    def _declare(self, msg: dict) -> None:
        """What the device says it can draw: the known modes of `render_modes` in its order (video always last), and its kind."""
        self._declare_modes(msg.get("render_modes"))
        device = msg.get("device")
        self._device = device if isinstance(device, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", device) else ""
        self._declare_engine(msg)

    def _declare_modes(self, raw) -> None:
        """The modes the device can draw: strings only, the first `MODES_READ_MAX` read, unknown and repeated ones dropped, video last."""
        asked = [m for m in raw[:MODES_READ_MAX] if isinstance(m, str)] if isinstance(raw, list) else []
        local = [m for m in dict.fromkeys(asked) if m in DEVICE_MODES and m != "video"]
        self._declared = local + (["video"] if "video" in asked else [])

    def _declare_engine(self, msg: dict, only_present: bool = False) -> None:
        """The engine id and the output height the device asks for; what is not a short string or a sane whole number is none."""
        if not only_present or "engine" in msg:
            engine = msg.get("engine")
            self._want_engine = engine if isinstance(engine, str) and 0 < len(engine) <= ENGINE_ID_MAX else None
        if not only_present or "resolution" in msg:
            height = msg.get("resolution")
            self._want_height = height if isinstance(height, int) and not isinstance(height, bool) and 0 < height <= HEIGHT_MAX else None

    def _redeclare(self, msg: dict) -> None:
        """The device declares its render modes again on an open connection; the answer is the effective set."""
        if not self._authenticated:
            self._send_json({"type": "error", "code": "auth_required", "message": "authenticate before declaring render modes"})
            return
        now = getattr(self.host, "render_clock", time.monotonic)()
        self._redeclared = [t for t in self._redeclared if now - t < REDECLARE_WINDOW_S]
        if len(self._redeclared) >= REDECLARE_MAX:
            self._send_json({"type": "error", "code": "render_modes_rate_limited",
                             "message": f"at most {REDECLARE_MAX} render_modes messages in {int(REDECLARE_WINDOW_S)} s"})
            return
        self._redeclared.append(now)
        if not isinstance(msg.get("modes"), list):
            self._send_json({"type": "error", "code": "bad_render_modes", "message": "render_modes needs a list of modes"})
            return
        self._declare_modes(msg["modes"])
        self._declare_engine(msg, only_present=True)
        self._failed.clear()
        offered = self._offered()
        self._render_mode = offered[0] if offered else "none"
        self._send_json({"type": "render_modes", "modes": offered, "render_mode": self._render_mode, "avatar_name": self._avatar_name,
                         **self._engine_fields(self._render_mode)})
        log.info("gateway: device declared render modes %s, serving %s", offered or "none", self._render_mode)

    def _wanted(self):
        """(engine descriptor or None, height or None) this device is configured for: its own declaration, else the person's choice for its target."""
        chosen = getattr(self.cfg, "avatar_engines", None)
        selection = engine_selection.parse({"engines": chosen, "resolution": getattr(self.cfg, "avatar_resolution", None)}) if chosen else None
        return engine_selection.wanted(selection, self._device, self._want_engine, self._want_height)

    def _engine_fields(self, mode: str) -> dict:
        """The engine and the output height that draw `mode` for this device, for the messages that name the mode; voice only has none."""
        if mode not in RENDER_MODES:
            return {"engine": "", "resolution": 0}
        info, height = self._wanted()
        engine, height = engine_selection.for_mode(mode, info, height, self._device)
        return {"engine": engine, "resolution": height}

    def _offered(self) -> list[str]:
        """The declared modes this PC can serve to this device now, best first: wav2lip and visemes when the personality's avatar has the
        files for them, video when the avatar function is loaded. A mode the device reported as failing is left out."""
        if not self._declared:
            return []
        info = self.host.phone_avatar(self._persona_slug)
        self._avatar_name = str(info.get("avatar") or "")
        have = set(info.get("render_modes") or [])
        if self.host.functions().get("avatar"):
            have.add("video")
        else:
            have.discard("video")
        offered = [m for m in self._declared if m in have and m not in self._failed]
        first = self._wanted()[0]
        if first is not None and first.mode in ("visemes", "wav2lip") and first.mode in offered:      # the video stays where the device put it: last
            offered.remove(first.mode)
            offered.insert(0, first.mode)
        return offered

    def _reselect(self, previous: str = "", why: str = "") -> None:
        """Looks again at what is offered after a change (the personality, a mode that failed) and tells the device when the mode or the avatar changed."""
        before = self._avatar_name
        offered = self._offered()
        mode = offered[0] if offered else "none"
        if mode == self._render_mode and self._avatar_name == before and not previous:
            return
        self._render_mode = mode
        msg = {"type": "render_mode", "mode": mode, "render_modes": offered, "avatar_name": self._avatar_name, **self._engine_fields(mode)}
        if previous:
            msg.update({"previous": previous, "why": why})
        self._send_json(msg)

    def _open_voice(self) -> bool:
        """Makes the STT and TTS clients over the loaded functions; says why when it cannot."""
        fn = self.host.functions()
        missing = [n for n in ("stt", "tts") if not fn.get(n)]
        if missing:
            self._send_json({"type": "error", "code": "not_loaded", "missing": missing,
                             "message": "the PC has not loaded: " + ", ".join(missing)})
            return False
        try:
            if self.stt is None:
                self.stt = self.host.stt_client("", follow=True) if self._follow else self.host.stt_client(self._stt_lang)
            if self.tts is None:
                self.tts = self.host.tts_client()
        except Exception as e:  # noqa: BLE001 (a model that went away)
            self._send_json({"type": "error", "code": "voice_failed", "message": f"{e.__class__.__name__}: {e}"})
            return False
        return True

    def _wait_start(self) -> dict | None:
        """The next start (or typed text) of the phone; None when the connection is over."""
        while True:
            self._apply_decision(in_turn=False)
            if self._next_start is not None:
                start, self._next_start = self._next_start, None
                return start
            if self._closing:
                return None
            self._tick_offer()
            try:
                raw = self.ws.recv(timeout=0.2 if self._barge is not None or self._judging or self._im.has_parked() else 1.0)
            except TimeoutError:
                self._push_state()
                continue
            except Exception:  # noqa: BLE001 (closed)
                return None
            if isinstance(raw, (bytes, bytearray)):
                self._barge_audio(bytes(raw))             # audio between turns is speech over a voice still playing, or nothing
                continue
            msg = _loads(raw)
            if self._side_message(msg):
                continue
            kind = msg.get("type")
            if kind == "start" and self._barge_start(msg):
                continue
            if kind == "start":
                return msg
            if kind == "text":
                return {"typed": (msg.get("text") or "").strip(), "lang": msg.get("lang"), "voice": msg.get("voice")}
            if kind == "close":
                return None

    def _cleanup(self) -> None:
        if self._video is not None:
            self._video_stop(notify=False)
        if self._frames is not None:
            self._frames.close()
        if self._persona_set:
            self.brain.begin_session(None)
        for c in (self.stt, self.tts):
            if c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- messages valid at any time
    def _side_message(self, msg: dict) -> bool:
        """The messages the phone may send in the middle of anything; True when `msg` was one of them."""
        kind = msg.get("type")
        if kind == "video_start":
            # The negotiation waits for ICE (seconds): in its own thread, so that the phone's audio is still read.
            threading.Thread(target=self._video_start, args=(msg,), name="gw-video-start", daemon=True).start()
            return True
        if kind == "video_stop":
            self._video_stop()
            return True
        if kind == "render_mode":
            mode = str(msg.get("mode") or "").strip()
            offered = self._offered()
            if mode in offered and mode != "video":
                self._render_mode = mode
                self._send_json({"type": "render_mode", "mode": mode, "render_modes": offered, "avatar_name": self._avatar_name, **self._engine_fields(mode)})
            else:
                self._send_json({"type": "error", "code": "render_mode_unavailable", "message": f"the PC does not offer {mode or '?'} to this device"})
            return True
        if kind == "render_modes":
            self._redeclare(msg)
            return True
        if kind == "render_failed":
            mode = str(msg.get("mode") or "").strip()
            if mode in DEVICE_MODES:
                self._failed.add(mode)
                if mode == "video" and self._video is not None:
                    self._video_stop(notify=False)
                why = str(msg.get("why") or "")[:200]
                log.info("gateway: %s failed on the device (%s)", mode, why)
                self._reselect(previous=mode, why=why)
            return True
        if kind == "user_speaking":
            self._user_speaking(msg)
            return True
        if kind == "end" and self._barge is not None:
            self._barge_end()
            return True
        if kind == "resume":
            self._resume_answer(msg.get("accept") is True)
            return True
        if kind == "brain":
            self.brain.set_mode((msg.get("brain") or "").strip().lower())
            self._send_json({"type": "brain", "brain": self.brain.mode})
            return True
        if kind == "language":
            self._language_request(str(msg.get("language") or ""))
            return True
        if kind == "persona":
            slug = msg.get("persona")
            slug = slug if isinstance(slug, str) and _SLUG.match(slug) else ""
            self.brain.begin_session(slug or None)
            self._persona_set = bool(slug)
            self._persona_slug = slug
            self._send_json({"type": "persona", "persona": slug})
            self._reselect()
            return True
        return False

    # ---------------------------------------------------------------- a reply
    def _stream_reply(self, utterance: str, bargein=None, motore=None, note: str = "") -> None:
        """Brain -> text to the phone and sentences to the synthesis (or to the duplex engine). `note` is extra context for this answer only."""
        self._ultima_risposta = ""
        self._caratteri_tts = 0
        self._im.begin_answer(utterance)
        proc = ReplyProcessor(extract_media=True, min_caratteri=DUPLEX_MIN_CARATTERI if motore is not None else None)

        def _emit(rc) -> None:
            for kind, src in rc.media:
                if src.startswith(("http://", "https://")):
                    self._send_json({"type": "media", "kind": kind, "url": src})
            if rc.app_text:
                self._ultima_risposta += " " + rc.app_text
                self._send_json({"type": "reply", "chunk": rc.app_text})
            for sentence in rc.tts_sentences:
                self._caratteri_tts += len(sentence)
                self._im.add_sentence(sentence)
                if motore is not None:
                    motore.dillo(sentence)
                else:
                    self.tts.send_text(sentence)
                    self.tts.flush()                    # synthesised while the brain still writes

        try:
            chunks = self.brain.respond_stream(utterance, "remote", context_note=note) if note else self.brain.respond_stream(utterance, "remote")
            for chunk in chunks:
                if bargein and bargein():
                    return
                if isinstance(chunk, str):
                    _emit(proc.feed(chunk))
            _emit(proc.flush())
            if motore is not None:
                motore.fine_risposta()
            else:
                self.tts.flush()
        except Exception as e:  # noqa: BLE001 (the brain failed)
            log.error("gateway: the brain failed: %s", e)
            self._send_json({"type": "error", "code": "brain_failed", "message": f"{e.__class__.__name__}: {e}"})

    def _tts_idle(self) -> bool:
        busy = getattr(self.tts, "busy", None)
        return callable(busy) and not busy() and self.tts.audio.empty()

    def _forward_tts_audio(self, barge_in=None, start_timeout: float = 20.0, stuck: float = 30.0) -> None:
        """Sends the synthesised segments to the phone until the synthesis is done. `barge_in()` true stops it."""
        t0 = time.monotonic()
        first = None
        idle_since = None
        while time.monotonic() - t0 < start_timeout:
            if barge_in and barge_in():
                self._send_json({"type": "audio_end"})
                return
            try:
                first = self.tts.audio.get(timeout=0.2)
                break
            except queue.Empty:
                # Idle must last: a job being taken from the queue is briefly neither queued nor running.
                idle_since = (idle_since or time.monotonic()) if self._tts_idle() else None
                if idle_since and time.monotonic() - idle_since >= 0.4:
                    break
        if first is None:
            self._send_json({"type": "audio_end"})        # the phone leaves its speaking state even with nothing to say
            return
        if self._cut or (barge_in and barge_in()):
            self._send_json({"type": "audio_end"})
            return
        # The lips start first, so that voice and mouth begin together; `audio_start` goes out only now.
        waited = time.monotonic()
        preamble = self._muovi_labbra(first, attendi=True)
        waited = time.monotonic() - waited
        chunk_policy.osserva_labiale(preamble)
        if waited > 0.5:
            log.info("gateway: lips took %.1fs to start (preamble %.2fs)", waited, preamble)
        self._segment += 1
        self._send_json({"type": "audio_start", "sample_rate": first.sample_rate, "segment": self._segment})
        self._emit_visemes(first)
        self._send_pcm(first.samples, first.sample_rate)
        self._note_segment(first)
        last = time.monotonic()
        idle_since = None
        while True:
            if barge_in and barge_in():
                break
            try:
                seg = self.tts.audio.get(timeout=0.2)
            except queue.Empty:
                if self._tts_idle():
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= 0.3:
                        break
                elif time.monotonic() - last >= stuck:
                    log.warning("gateway: the synthesis delivered nothing for %.0fs", stuck)
                    break
                continue
            idle_since = None
            if self._cut or (barge_in and barge_in()):
                break
            self._segment += 1
            self._muovi_labbra(seg, attendi=False)
            self._emit_visemes(seg)
            self._send_pcm(seg.samples, seg.sample_rate)
            self._note_segment(seg)
            last = time.monotonic()
        self._send_json({"type": "audio_end"})

    def _send_pcm(self, samples, rate: int = 24000, chunk_bytes: int = 8192) -> None:
        """The voice to the phone: on the WebRTC audio track when there is one, else PCM frames on the socket."""
        video = self._video
        if video is not None and video.scrivi_audio(samples, rate):
            return
        pcm = _f32_to_pcm16(samples)
        for i in range(0, len(pcm), chunk_bytes):
            self.ws.send(pcm[i:i + chunk_bytes])

    def _emit_visemes(self, seg) -> None:
        """In visemes mode, the mouth-shape timeline of this segment goes out just before its audio, with the number of the segment and the
        bytes of PCM that follow. A timeline that cannot be made goes out as a closed mouth, so that every segment has one."""
        if self._render_mode != "visemes" or self._video is not None:
            return
        text = (getattr(seg, "testo", "") or "").strip()
        seconds = len(seg.samples) / max(getattr(seg, "sample_rate", 24000), 1)
        try:
            from .viseme_gen import timeline
            msg = timeline(text, seconds, self._lang_turno)
        except Exception as e:  # noqa: BLE001 (the visemes must not stop the audio)
            log.warning("gateway: visemes not generated (%s)", e)
            msg = {"type": "visemes", "text": text, "lang": self._lang_turno, "duration_ms": round(seconds * 1000),
                   "visemes": [{"t": 0, "v": "sil", "dur": round(seconds * 1000)}]}
        msg.update({"segment": self._segment, "pcm_bytes": len(seg.samples) * 2})
        self._send_json(msg)

    # ---------------------------------------------------------------- interruption
    def _hold_interrupt(self) -> bool:
        """Looks at what the phone sent while a reply is generated or played: an interrupt, a new start, a typed text or a close."""
        while True:
            self._apply_decision(in_turn=True)
            try:
                raw = self.ws.recv(timeout=0)
            except TimeoutError:
                return self._cut
            except Exception:  # noqa: BLE001 (closed)
                self._closing = self._cut = True
                return True
            if isinstance(raw, (bytes, bytearray)):
                self._barge_audio(bytes(raw))
                continue
            msg = _loads(raw)
            kind = msg.get("type")
            if kind == "interrupt":
                self._cut = True
            elif kind == "start" and self._barge_start(msg):
                pass
            elif kind == "start":
                self._next_start, self._cut = msg, True
            elif kind == "text":
                self._next_start, self._cut = {"typed": (msg.get("text") or "").strip(), "lang": msg.get("lang"), "voice": msg.get("voice")}, True
            elif kind == "close":
                self._closing = self._cut = True
            else:
                self._side_message(msg)
            if self._cut:
                return True

    def _interrupt_output(self) -> None:
        """Stops the chain in the order it produces: the synthesis, what is already audio, the avatar's mouth, the video's audio buffer."""
        self.tts.abort()
        drain_queue(self.tts.audio)
        self._ferma_avatar()
        if self._video is not None:
            self._video.svuota_audio()

    def _finish_hold_turn(self) -> None:
        if self._cut:
            self._interrupt_output()
            self._send_json({"type": "interrupt"})
            if self._unduck:
                self._unduck = False
                self._send_json({"type": "duck", "level": 1.0})
        self._send_json({"type": "done"})
        self._im.answer_ended()

    # ---------------------------------------------------------------- speech over the avatar
    def _note_segment(self, seg) -> None:
        """The interruption manager learns what the phone is about to play, and for how long."""
        self._im.add_segment(getattr(seg, "testo", "") or "", len(seg.samples) / max(getattr(seg, "sample_rate", 24000), 1))

    def _user_speaking(self, msg: dict) -> None:
        """The phone heard the person begin or stop speaking while the avatar talks: the voice is lowered at once, not stopped."""
        if not self._im.settings.enabled:
            return
        if msg.get("state") == "start":
            level = self._im.user_started()
            if level is not None:
                self._send_json({"type": "duck", "level": level})
        elif msg.get("state") == "end" and self._barge is not None:
            level, ms = msg.get("level"), msg.get("ms")
            self._barge["level"] = float(level) if isinstance(level, (int, float)) and not isinstance(level, bool) else 0.0
            self._barge["ms"] = int(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else 0
            self._barge_end()

    def _barge_start(self, msg: dict) -> bool:
        """A `start` flagged `barge_in` while the avatar is still speaking is speech over it, not a turn: True when it was taken as that."""
        if msg.get("barge_in") is not True or not self._im.settings.enabled or self.stt is None or self._barge is not None or not self._im.speaking():
            return False
        rate = msg.get("sample_rate")
        self._barge = {"lang": msg.get("lang") or self.cfg.default_lang, "rate": rate if isinstance(rate, int) and rate > 0 else STT_RATE, "ms": 0, "level": 0.0}
        drain_queue(self.stt.utterances)
        return True

    def _barge_audio(self, raw: bytes) -> None:
        if self._barge is not None and self.stt is not None:
            self.stt.send_audio(_normalize_audio(_resample(_pcm16_to_f32(raw), self._barge["rate"], STT_RATE)))

    def _barge_end(self) -> None:
        """The speech over the avatar is over: the STT is flushed and a thread waits for the words and judges them, so that the audio goes on."""
        barge, self._barge = self._barge, None
        if barge is None:
            return
        try:
            self.stt.flush()
        except Exception:  # noqa: BLE001 (the judge then hears nothing)
            pass
        self._judging += 1
        threading.Thread(target=self._judge_barge, args=(barge,), name="gw-interruption", daemon=True).start()

    def _judge_barge(self, barge: dict) -> None:
        try:
            try:
                first = self.stt.utterances.get(timeout=BARGE_STT_WAIT_S)
                text = coalesce_utterances(self.stt.utterances, first, 0.25).strip()
            except queue.Empty:
                text = ""
            if not text:
                result = Decision(IGNORE, "nothing heard", "rules")
            elif self._im.offer_open() and voice_verdict(text):
                result = voice_verdict(text)
            else:
                result = self._im.decide(Heard(text, barge["ms"], barge["level"]), can_integrate=self.brain.mode == "direct")
        except Exception as e:  # noqa: BLE001 (a failed judgement must not leave the phone waiting)
            log.error("gateway: interruption not judged: %s", e)
            text, result = "", Decision(IGNORE, "error", "fallback", degraded=True)
        self._decisions.put((barge, text, result))

    def _apply_decision(self, in_turn: bool) -> None:
        """Carries out a decision that is ready. In a turn the reply is cut through `_cut`; between turns the phone is told directly."""
        try:
            barge, text, d = self._decisions.get_nowait()
        except queue.Empty:
            return
        self._judging -= 1
        if isinstance(d, str):                                    # the offer was answered by voice over the question
            self._send_json({"type": "transcript", "text": text})
            self._offer_verdict(d)
            self._send_json({"type": "duck", "level": 1.0})
            self._send_json({"type": "done"})
            return
        if d.action == IGNORE:
            self._send_json({"type": "transcript", "text": ""})
            self._send_json({"type": "duck", "level": 1.0})
            self._send_json({"type": "done"})
            return
        start = {"typed": text, "lang": barge["lang"], "voice": self._voice or None, "heard": True}
        if d.stops:
            spoken_part, left = self._im.split()
            if d.action == STOP_AND_INTEGRATE:
                start["note"] = integration_note(spoken_part, text)
                self._im.park("")
            else:
                self._im.park(left if d.offer_resume else "")
            self._send_json({"type": "reply_reset", "text": spoken_part + (" " if spoken_part else "")})
        else:
            self._send_json({"type": "duck", "level": 1.0})
        if self._next_start is not None:                          # the phone's own start or text came meanwhile and wins
            self._send_json({"type": "done"})
            return
        self._next_start = start
        if not d.stops:
            return
        if in_turn:
            self._cut = self._unduck = True
        else:
            self._interrupt_output()
            self._send_json({"type": "interrupt"})
            self._send_json({"type": "duck", "level": 1.0})

    def _offer_verdict(self, verdict: str) -> None:
        """The person took (`accept`) or turned down the offer to go on."""
        if verdict == "accept":
            rest = self._im.accept()
            if rest and self._next_start is None:
                self._next_start = {"say": rest}
        else:
            self._im.decline()

    def _answers_offer(self, utterance: str) -> bool:
        """A turn that is only the answer to the offer to go on: handled here, True."""
        verdict = voice_verdict(utterance) if self._im.offer_open() else None
        if verdict is None:
            return False
        self._offer_verdict(verdict)
        self._send_json({"type": "done"})
        return True

    def _resume_answer(self, accept: bool) -> None:
        """The phone's `resume` message: the tap on the offer."""
        if self._im.offer_open():
            self._offer_verdict("accept" if accept else "decline")

    def _tick_offer(self) -> None:
        """Between turns: makes the offer to go on once the new answer has been quiet long enough, and takes back one nobody answered."""
        event = self._im.poll_offer()
        if event is None:
            return
        kind, text = event
        if kind == "expired":
            self._send_json({"type": "resume_offer", "text": ""})
            return
        question = offer_line(self._lang_turno)
        self._send_json({"type": "resume_offer", "text": text, "question": question, "lang": spoken.language_of(self._lang_turno)})
        self._do_say_turn({"say": question})

    # ---------------------------------------------------------------- turns
    def _stt_per_lingua(self, lang: str) -> None:
        if self._follow:
            return                                    # the recognizer detects; the language state is the session's
        code = (lang or "").strip().lower().split("-")[0]
        if not code or code == self._stt_lang:
            return
        log.info("gateway: STT language %s (was %s)", code, self._stt_lang)
        try:
            self.stt.close()
        except Exception:  # noqa: BLE001
            pass
        self._stt_lang = self._lang_turno = code
        self.stt = self.host.stt_client(code)

    def _do_turn(self, start: dict) -> None:
        """Hold or continuous: capture the utterance, ask the brain, play the answer."""
        mode = start.get("mode", "hold")
        lang = start.get("lang") or self.cfg.default_lang
        self._stt_per_lingua(lang)
        in_rate = int(start.get("sample_rate") or STT_RATE)
        self._configura_voce(start.get("voice") or self.cfg.default_voice, lang)
        drain_queue(self.stt.utterances)
        self._take_heard()
        self._cut = False
        utterance = self._capture_utterance(mode, in_rate)
        heard = self._take_heard()
        if not utterance:
            self._send_json({"type": "transcript", "text": ""})
            self._send_json({"type": "done"})
            return
        self._send_json({"type": "transcript", "text": utterance})
        if not (start.get("transcribe_only") or mode == "transcribe") and self._handle_language(utterance, heard):
            self._send_json({"type": "done"})
            return
        if start.get("transcribe_only") or mode == "transcribe":      # dictate and correct: only the text goes back
            self._send_json({"type": "done"})
            return
        if self._answers_offer(utterance):
            return
        self._stream_reply(utterance, bargein=self._hold_interrupt)
        if not self._cut:
            self._forward_tts_audio(barge_in=self._hold_interrupt)
        self._finish_hold_turn()

    def _do_typed_turn(self, start: dict) -> None:
        """A typed sentence, or the speech heard over the avatar that was put off or took the place of the answer (`heard`, with its `note`)."""
        text = (start.get("typed") or "").strip()
        if not text:
            self._send_json({"type": "done"})
            return
        self._configura_voce(start.get("voice") or self.cfg.default_voice, start.get("lang") or self.cfg.default_lang)
        if self._lang is None:
            self._lang_turno = str(start.get("lang") or self._lang_turno).split("-")[0]
        self._cut = False
        if start.get("heard"):
            self._send_json({"type": "transcript", "text": text})
        if self._handle_language(text, None):
            self._send_json({"type": "done"})
            return
        self._stream_reply(text, bargein=self._hold_interrupt, note=start.get("note") or "")
        if not self._cut:
            self._forward_tts_audio(barge_in=self._hold_interrupt)
        self._finish_hold_turn()

    def _do_say_turn(self, start: dict) -> None:
        """Says a text the PC already has (the part of an answer that was left) without asking the brain."""
        text = (start.get("say") or "").strip()
        if not text:
            return
        if not start.get("keep"):
            self._configura_voce(self._voice or self.cfg.default_voice, self._lang_turno)
        self._cut = False
        self._im.begin_answer("")
        self._im.add_sentence(text)
        self._send_json({"type": "reply", "chunk": text})
        self.tts.send_text(text)
        self.tts.flush()
        self._forward_tts_audio(barge_in=self._hold_interrupt)
        if self._cut:
            self._interrupt_output()
            self._send_json({"type": "interrupt"})
        self._send_json({"type": "turn_done"})
        self._im.answer_ended()

    def _capture_utterance(self, mode: str, in_rate: int) -> str:
        """The audio of the phone to the STT until the turn ends; returns the whole transcribed utterance.

        hold: after {end} the STT is flushed and the turn closes once it has finished; continuous: the turn closes on a pause."""
        quiet = self.cfg.ptt_turn_pause_s
        t_start = time.monotonic()
        pieces: list[str] = []
        last_utt: float | None = None
        ended, end_time = False, None
        n_samples, peak, sq = 0, 0.0, 0.0
        deadline = t_start + 120.0
        while time.monotonic() < deadline and not self._closing:
            try:
                while True:
                    t = self.stt.utterances.get_nowait()
                    if t:
                        pieces.append(t)
                        last_utt = time.monotonic()
            except queue.Empty:
                pass
            if pieces and last_utt and time.monotonic() - last_utt >= quiet:
                break
            if not pieces:
                if mode != "continuous" and ended and end_time and time.monotonic() - end_time >= POST_END_TIMEOUT_S:
                    break
                if time.monotonic() - t_start >= NO_SPEECH_TIMEOUT_S:
                    break
            try:
                raw = self.ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 (closed)
                self._closing = True
                break
            if isinstance(raw, (bytes, bytearray)):
                samples = _normalize_audio(_resample(_pcm16_to_f32(bytes(raw)), in_rate, STT_RATE))
                if samples.size:
                    n_samples += samples.size
                    peak = max(peak, float(np.max(np.abs(samples))))
                    sq += float(np.sum(np.square(samples, dtype=np.float64)))
                self.stt.send_audio(samples)
                continue
            msg = _loads(raw)
            if msg.get("type") == "end":
                ended, end_time = True, time.monotonic()
                self.stt.flush()                        # the button is released: close the utterance now
            elif msg.get("type") == "interrupt":
                continue
            else:
                self._side_message(msg)
        if n_samples:
            log.info("gateway: turn of %.1fs from the phone (peak %.3f, rms %.4f)", n_samples / STT_RATE, peak, (sq / n_samples) ** 0.5)
            if peak < 0.02:
                log.warning("gateway: the audio is almost silent (peak %.3f): microphone covered, permission denied or gain at zero", peak)
        else:
            log.warning("gateway: no audio samples from the phone")
        return " ".join(pieces).strip()

    # ---------------------------------------------------------------- voice of the person on screen
    def _configura_voce(self, voice: str, lang: str) -> None:
        """The avatar's own voice wins; without an avatar the voice of the turn is used."""
        self._voice = voice
        if self._voce_persona is None and self.host.functions().get("avatar"):
            self._risolvi_voce_avatar()
        engine, name = self._voce_persona if self._voce_persona else ("", voice)
        ls = self._language_session(name, engine, lang)
        if ls is not None:
            name, engine, lang = ls.voice, ls.engine, ls.tts_lang
        if engine:
            configure_tts(self.tts, self.cfg, name, lang, engine)
        else:
            configure_tts(self.tts, self.cfg, name, lang)

    # ---------------------------------------------------------------- the spoken language
    def _language_session(self, voice: str, engine: str, lang: str) -> LanguageSession | None:
        """The connection's language state, made at its first turn from the voice in use and the language the phone asked for; None when the
        language does not follow the speech."""
        if not self._follow:
            return None
        if self._lang is None:
            code = iso(lang) if iso(lang) else iso(self.cfg.default_lang) or "it"

            def router():
                from . import engines
                return engines._get_tts_engine(self.cfg)

            self._lang = LanguageSession(LanguageSettings.from_config(self.cfg), language=code, voice=voice, engine=engine, router_fn=router,
                                         tts_lang=lang or code)
            self._lang_turno = self._lang.language
            self._tell_brain(self._lang.language)
            return self._lang
        ls = self._lang
        if (voice, (engine or "").lower()) != (ls.base_voice, ls.base_engine):
            ls.base_voice, ls.base_engine = voice, (engine or "").lower()
            ls.voice, ls.engine = ls.base_voice, ls.base_engine
        return ls

    def _tell_brain(self, code: str) -> None:
        from . import language_names
        fn = getattr(self.brain, "set_reply_language", None)
        if callable(fn) and language_names.is_known(code):
            fn(language_names.reply_name(code), code)

    def _take_heard(self) -> dict | None:
        fn = getattr(self.stt, "take_language_info", None)
        return fn() if callable(fn) else None

    def _handle_language(self, text: str, heard: dict | None) -> bool:
        """What the person's speech or typing says about the language. True when the text was a language command and is dealt with (confirmed
        aloud); a detection changes the language and the turn goes on in it. The phone is the person's own device: its voice and its text count as
        theirs."""
        ls = self._lang
        if ls is None:
            return False
        cmd = parse_language_command(text)
        out = ls.on_command(cmd, True) if cmd else None
        if out is not None:
            self._language_outcome(out)
            return True
        out = ls.on_detection(text, heard, True)
        if out is not None:
            self._language_outcome(out)
        return False

    def _language_request(self, code: str) -> None:
        """The phone's own `language` message: a language, or "auto"."""
        if not self._follow:
            self._send_json({"type": "error", "code": "language_unavailable", "message": "the language does not follow the speech on this PC"})
            return
        if self._lang is None:
            self._language_session(self._voice or self.cfg.default_voice, "", self._lang_turno)
        out = self._lang.request(code)
        self._language_outcome(out, speak=False)

    def _language_outcome(self, out: Outcome, speak: bool = True) -> None:
        """Puts a language change in effect: the notice with the voice that speaks now, then the synthesis, the brain, the recognizer's hint and the
        phone's `language` message, then the confirmation with the new voice."""
        if speak and out.pre_say:
            self._do_say_turn({"say": out.pre_say, "keep": True})
        ls = self._lang
        if out.changed:
            self._lang_turno = out.language
            if self.tts is not None:
                configure_tts(self.tts, self.cfg, out.voice, out.tts_lang, out.engine) if out.engine else configure_tts(self.tts, self.cfg, out.voice, out.tts_lang)
            self._tell_brain(out.language)
        hint = getattr(self.stt, "set_language", None)
        if callable(hint) and ls is not None:
            hint(ls.stt_hint() or None)
        self._send_json({"type": "language", "language": ls.language, "mode": ls.mode, "reason": out.reason, "warning": out.warning,
                         "engine_fallback": ls.routed_to_piper})
        if speak and out.say:
            self._do_say_turn({"say": out.say, "keep": True})

    def _risolvi_voce_avatar(self) -> None:
        persona = self._persona_corrente()
        if persona:
            self._leggi_voce_persona(persona)

    def _persona_corrente(self) -> str:
        status, st = self._control("GET", "/avatar/state")
        return str(st.get("persona") or "") if status == 200 else ""

    def _leggi_voce_persona(self, persona: str) -> None:
        if not persona:
            return
        status, data = self._control("GET", "/personas")
        if status != 200:
            return
        items = data if isinstance(data, list) else data.get("personas", []) if isinstance(data, dict) else []
        for p in items:
            if str(p.get("name", "")).lower() != persona.lower():
                continue
            v = p.get("voice") or {}
            if v.get("voice"):
                self._voce_persona = (v.get("engine") or "", v["voice"])
                log.info("gateway: voice of %s is %s/%s", persona, v.get("engine") or "auto", v["voice"])
            return

    # ---------------------------------------------------------------- video
    def _video_start(self, msg: dict) -> None:
        """Turns the avatar video on towards the phone and negotiates WebRTC. The avatar worker lives in the service: one, for the VRAM.
        The video is made on this PC with the GPU, so it is for an emergency only: the device says so with `reason: emergency`."""
        if msg.get("reason") != "emergency":
            self._send_json({"type": "video_unavailable", "reason": "emergency_only"})
            return
        if not self.host.functions().get("avatar"):
            self._send_json({"type": "video_unavailable", "reason": "no_avatar"})
            return
        try:
            from .avatar_webrtc import FrameServer, SessioneVideo
        except ImportError as e:
            log.warning("gateway: no WebRTC (%s)", e)
            self._send_json({"type": "video_unavailable", "reason": "no_webrtc"})
            return
        persona = (msg.get("persona") or "").strip()
        sdp = msg.get("sdp") or ""
        if not sdp:
            self._send_json({"type": "error", "code": "bad_request", "message": "the SDP offer is missing"})
            return
        try:
            if self._frames is None:
                self._frames = FrameServer()
            # Negotiate at once: preparing an avatar can take minutes, and the phone would give up waiting for the answer.
            self._video = SessioneVideo(self._frames, audio=getattr(self.cfg, "avatar_audio_webrtc", True))
            self._send_json({"type": "video_answer", "sdp": self._video.apri(sdp, msg.get("sdp_type") or "offer")})
            threading.Thread(target=self._video_accendi_avatar, args=(persona,), name="gw-avatar-up", daemon=True).start()
            log.info("gateway: avatar video negotiated (persona %s, brain %s)", persona or "current", msg.get("brain") or "current")
            if msg.get("brain"):
                self.brain.set_mode(str(msg["brain"]).strip().lower())
        except Exception as e:  # noqa: BLE001 (the phone must know it does not start)
            log.error("gateway: video start failed: %s", e)
            self._send_json({"type": "error", "code": "video_failed", "message": f"{e.__class__.__name__}: {e}"})

    @staticmethod
    def _progress(st: dict) -> str:
        prog = st.get("progress")
        if isinstance(prog, dict) and prog.get("percent") is not None:
            text = f"preparing the avatar - {float(prog['percent']):.0f}%"
            if prog.get("eta_s"):
                text += f" (~{int(float(prog['eta_s']))}s)"
            return text
        return f"preparing {st['preparing']}" if st.get("preparing") else "preparing the avatar"

    def _video_accendi_avatar(self, persona: str) -> None:
        """Prepares the persona and sends the frames to the phone, in the background, reporting each step."""
        try:
            current = persona or self._persona_corrente()
            self._leggi_voce_persona(current)
            if current:
                try:
                    self.brain.begin_session(current)
                except Exception as e:  # noqa: BLE001
                    log.warning("gateway: identity of %s not loaded: %s", current, e)
            if persona:
                status, body = self._control("POST", "/avatar/start", {"persona": persona}, timeout=30)
                if status == 404:
                    self._send_json({"type": "error", "code": "no_persona", "message": f"persona '{persona}' not found"})
                    return
            deadline = time.time() + 600
            last = ""
            while time.time() < deadline:
                _status, st = self._control("GET", "/avatar/state")
                if st.get("error"):
                    self._send_json({"type": "error", "code": "avatar_failed", "message": f"avatar: {st['error']}"})
                    return
                if st.get("running") and (not persona or st.get("persona") == persona):
                    break
                step = self._progress(st)
                if step != last:
                    last = step
                    self._send_json({"type": "video_state", "state": "preparing", "detail": step})
                time.sleep(1.0)
            else:
                self._send_json({"type": "error", "code": "avatar_failed", "message": "avatar: preparation took too long"})
                return
            if self._frames is None or self._video is None:
                return                                    # the phone already turned the video off
            status, body = self._control("POST", "/avatar/stream", {"on": True, "port": self._frames.port}, timeout=30)
            if status >= 400:
                self._send_json({"type": "error", "code": "avatar_failed", "message": f"avatar not ready: {body.get('error') or status}"})
                return
            self._send_json({"type": "video_state", "state": "live"})
        except Exception as e:  # noqa: BLE001
            log.error("gateway: avatar start failed: %s", e)
            self._send_json({"type": "error", "code": "avatar_failed", "message": f"{e.__class__.__name__}: {e}"})

    def _video_stop(self, notify: bool = True) -> None:
        """Turns the video off and gives the frames back to the PC's camera."""
        self._control("POST", "/avatar/stream", {"on": False}, timeout=15)
        if self._video is not None:
            self._video.chiudi()
            self._video = None
        self._voce_persona = None
        if notify:
            self._send_json({"type": "video_stopped"})

    def _ferma_avatar(self) -> None:
        """Cuts the lip-sync, including the lines already queued in the worker."""
        if self._video is not None:
            self._control("POST", "/avatar/interrupt", {"svuota": True}, timeout=4)

    def _muovi_labbra(self, seg, attendi: bool = True) -> float:
        """Has the avatar animate a line already sent to the phone (muted: the phone plays the voice). Returns the preparation time."""
        if self._video is None:
            return 0.0
        try:
            folder = Path(tempfile.gettempdir()) / "tadamoo-lipsync"
            folder.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder, prefix="seg-", suffix=".wav", delete=False) as f:
                path = f.name
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(seg.sample_rate)
                w.writeframes(_f32_to_pcm16(seg.samples))
            # The timeout exceeds the wait asked of the worker, so that the voice is not sent while the video is still about to start.
            _status, body = self._control("POST", "/avatar/lipsync", {"wav": path, "mute": True, "attendi_avvio": bool(attendi), "attesa_max_s": 8.0},
                                          timeout=12 if attendi else 4)
            return float(body.get("preparazione_s") or 0.0)
        except Exception as e:  # noqa: BLE001 (the turn must not die here)
            log.warning("gateway: lip-sync not sent: %s", e)
        return 0.0

    # ---------------------------------------------------------------- full duplex
    def _do_duplex_session(self, start: dict) -> None:
        """Headset or Bluetooth: speak and listen at once. A thread feeds the STT; the loop answers each finished utterance.

        If the person speaks again while the answer plays, it is interrupted. The session ends on {stop} or {close}."""
        lang = start.get("lang") or self.cfg.default_lang
        in_rate = int(start.get("sample_rate") or STT_RATE)
        self._configura_voce(start.get("voice") or self.cfg.default_voice, lang)
        drain_queue(self.stt.utterances)
        self._take_heard()
        stop = threading.Event()

        def _receiver() -> None:
            last_log = time.monotonic()
            n_blocks = n_samples = 0
            sq = peak = 0.0
            while not stop.is_set():
                try:
                    raw = self.ws.recv(timeout=0.4)
                except TimeoutError:
                    continue
                except Exception:  # noqa: BLE001 (closed)
                    self._closing = True
                    stop.set()
                    break
                if isinstance(raw, (bytes, bytearray)):
                    samples = _normalize_audio(_resample(_pcm16_to_f32(bytes(raw)), in_rate, STT_RATE))
                    self.stt.send_audio(samples)
                    n_blocks += 1
                    n_samples += len(samples)
                    if len(samples):
                        sq += float(np.square(samples).sum())
                        peak = max(peak, float(np.abs(samples).max()))
                else:
                    msg = _loads(raw)
                    if msg.get("type") in ("stop", "close"):
                        self._closing = self._closing or msg.get("type") == "close"
                        stop.set()
                        break
                    self._side_message(msg)
                if time.monotonic() - last_log >= 5.0:
                    if n_samples:
                        log.info("gateway duplex: %d blocks, %.1fs of audio (peak %.3f, rms %.4f)", n_blocks, n_samples / STT_RATE, peak, (sq / n_samples) ** 0.5)
                        if peak < 0.02:
                            log.warning("gateway duplex: the audio is almost silent (peak %.3f)", peak)
                    else:
                        log.warning("gateway duplex: no audio from the phone in the last 5s")
                    last_log = time.monotonic()
                    n_blocks = n_samples = 0
                    sq = peak = 0.0

        motore = self._crea_motore()
        threading.Thread(target=_receiver, name="gw-duplex-rx", daemon=True).start()
        if motore is not None:
            threading.Thread(target=self._ciclo_orecchie, args=(motore, stop), name="gw-orecchie", daemon=True).start()
        self._send_json({"type": "session_started"})
        self._eco_viste = 0
        self._da_trattare = []
        try:
            while not stop.is_set():
                if self._da_trattare:
                    utterance = self._da_trattare.pop(0)
                else:
                    try:
                        first = self.stt.utterances.get(timeout=0.4)
                    except queue.Empty:
                        continue
                    utterance = coalesce_utterances(self.stt.utterances, first, self.cfg.ptt_turn_pause_s, stop=stop)
                if not utterance:
                    continue
                heard = self._take_heard()
                self._send_json({"type": "transcript", "text": utterance})
                if self._handle_language(utterance, heard):
                    continue

                def bargein(only_stop: bool = False) -> bool:
                    if stop.is_set():
                        return True
                    if only_stop:
                        return False                      # with the engine the classifier judges the phrases
                    return bool(_real_phrase(self.stt.utterances, self._ultima_risposta))

                if motore is not None:
                    motore.rialza()
                    self._send_json({"type": "audio_start", "sample_rate": 24000})
                    self._stream_reply(utterance, motore=motore)
                    stopped = self._attendi_motore(motore, bargein)
                    if self._caratteri_tts and not motore.battute_ricevute:
                        log.warning("gateway: %d characters sent to the synthesis and no audio came back", self._caratteri_tts)
                    self._send_json({"type": "audio_end"})
                    self._send_json({"type": "interrupt" if stopped else "turn_done"})
                    continue
                # No interruption while the answer is being generated: nothing was heard yet, a queued transcript is noise or echo.
                self._stream_reply(utterance)
                self._forward_tts_audio(barge_in=bargein)
                if _real_phrase(self.stt.utterances, self._ultima_risposta):
                    self._interrupt_output()
                    self._send_json({"type": "interrupt"})
                else:
                    self._send_json({"type": "turn_done"})
        finally:
            stop.set()
            if motore is not None:
                log.info("gateway: duplex engine - %s", motore.stato())
                motore.ferma()
            self._send_json({"type": "done"})

    def _crea_motore(self):
        """The duplex delivery engine when the configuration turns it on, else None."""
        if not getattr(self.cfg, "duplex_motore_nuovo", False):
            return None
        from .duplex_engine import ANTICIPO_WEBRTC_S, ANTICIPO_WS_S, MotoreDuplex
        on_track = self._video is not None and getattr(self._video, "audio_attivo", False)
        motore = MotoreDuplex(self.tts, consegna=lambda pcm, rate: self._send_pcm(pcm, rate), muovi_labbra=self._labiale_da_pcm,
                              anticipo_s=ANTICIPO_WEBRTC_S if on_track else ANTICIPO_WS_S)
        motore.avvia()
        return motore

    def _ciclo_orecchie(self, motore, stop) -> None:
        """Lowers the voice as soon as the person starts speaking and raises it again after 2.5 s without an utterance."""
        last = 0.0
        while not stop.is_set():
            try:
                self.stt.speech_events.get(timeout=0.2)
                last = time.monotonic()
                if not motore.abbassato:
                    motore.abbassa()
            except queue.Empty:
                if motore.abbassato and time.monotonic() - last > 2.5:
                    motore.rialza()

    def _attendi_motore(self, motore, bargein) -> bool:
        """Follows the answer to its end, deciding what to do with what the person says meanwhile (the interruption manager: the rules, then
        the technical session). Returns True when the answer was stopped."""
        fermo_da = time.monotonic()
        consegnati = motore.consegnati_s
        while True:
            if bargein and bargein(only_stop=True):
                motore.taglia(anche_in_corso=True)
                self._ferma_avatar()
                if self._video is not None:
                    self._video.svuota_audio()
                return True
            text = self._prossima_frase()
            if text:
                detto, previsto = motore.copione.detto(), motore.copione.previsto()
                # Rewriting the rest is for the direct brain only: the agent may already have acted.
                d = self._im.decide(Heard(text), can_integrate=self.brain.mode == "direct", said=(detto, previsto))
                if d.action == IGNORE:
                    motore.rialza()
                    if d.reason.startswith("eco") and self._conta_eco():
                        motore.taglia(anche_in_corso=True)
                        self._ferma_avatar()
                        return True
                elif d.action == STOP_AND_INTEGRATE and self._integra(motore, text, detto, previsto):
                    pass
                elif d.stops:
                    motore.taglia(anche_in_corso=True)
                    self._ferma_avatar()
                    if self._video is not None:
                        self._video.svuota_audio()
                    self._da_trattare.append(text)
                    return True
                else:
                    self._da_trattare.append(text)        # queue: the avatar finishes, then answers
            if motore.consegnati_s > consegnati + 1e-6:
                consegnati = motore.consegnati_s
                fermo_da = time.monotonic()
            # The brain being done is not enough: the synthesis may still be working on the text it already has.
            quiet = 5.0 if motore.battute_ricevute else 45.0
            if motore._risposta_finita.is_set() and motore.copione.anticipo_s() <= 1e-6 and motore.silenzio_dal_tts() > quiet:
                return False
            if time.monotonic() - fermo_da > (25.0 if motore.battute_ricevute else 60.0):
                log.warning("gateway: no audio for 25s - %s", motore.stato())
                return False
            time.sleep(0.05)

    def _integra(self, motore, nuova: str, detto: str, previsto: str) -> bool:
        """Rewrites the rest of the answer in view of what the person just said: the sentence being spoken is finished, the ones after it are
        cut, and the brain writes the rest with a note of what was said and what was added. False when there is not enough left to rewrite
        (the caller stops the answer and answers the new request instead)."""
        if not previsto.strip() or motore.copione.anticipo_s() < getattr(self.cfg, "duplex_integra_da_s", 2.5):
            return False
        motore.riscrivi()
        self._send_json({"type": "reply_reset", "text": detto + (" " if detto else "")})
        proc = ReplyProcessor(extract_media=True, continuazione=True, min_caratteri=DUPLEX_MIN_CARATTERI)
        self._ultima_risposta = detto
        note = integration_note(detto, nuova)

        def _emit(rc) -> None:
            if rc.app_text:
                self._ultima_risposta += " " + rc.app_text
                self._send_json({"type": "reply", "chunk": rc.app_text})
            for sentence in rc.tts_sentences:
                self._caratteri_tts += len(sentence)
                motore.dillo(sentence)

        def _write() -> None:
            try:
                for chunk in self.brain.respond_stream(nuova, "remote", context_note=note):
                    if isinstance(chunk, str):
                        _emit(proc.feed(chunk))
                _emit(proc.flush())
            except Exception as e:  # noqa: BLE001
                log.error("gateway: the rewritten rest failed: %s", e)
            finally:
                motore.fine_risposta()

        threading.Thread(target=_write, name="gw-continuazione", daemon=True).start()
        return True

    def _conta_eco(self) -> bool:
        """Counts a recognised echo; true after the third one in a session, when turns are better than continuous listening."""
        self._eco_viste += 1
        if self._eco_viste < 3:
            return False
        log.warning("gateway: back to turns - the avatar hears itself (%d echoes)", self._eco_viste)
        self._send_json({"type": "conversazione", "modo": "turni", "motivo": "the avatar hears itself through the microphone: back to turns"})
        return True

    def _prossima_frase(self) -> str:
        try:
            text = self.stt.utterances.get_nowait()
        except queue.Empty:
            return ""
        return text.strip() if isinstance(text, str) else ""

    def _labiale_da_pcm(self, pcm, rate: int, attendi: bool) -> float:
        from types import SimpleNamespace
        return self._muovi_labbra(SimpleNamespace(samples=pcm, sample_rate=rate), attendi=attendi)
