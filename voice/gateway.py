"""The voice gateway of the service: the WebSocket server the phone's Avatar tab talks to, through TaDaMoo's server.

It listens on loopback only, on a port the service chooses (reported by `GET /gateway` and in `/status`). A connection is accepted only with the
service token, in the handshake (`Authorization: Bearer <token>`, which TaDaMoo's server adds after it has checked the phone's own key) or in the
first message; anything else is refused or closed. What one connection does is `callbridge/voice_gateway.py`."""
from __future__ import annotations

import collections
import hmac
import logging
import threading
from http import HTTPStatus

import requests

from callbridge.interruption import LOG_KEEP, TechnicalJudge
from callbridge.tadamoo_api import TadamooApi
from callbridge.voice_gateway import RENDER_MODES, Connection

log = logging.getLogger("voice.gateway")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
PATH = "/ws"


class Host:
    """What a connection needs from the service: the config, the brains, which functions are loaded, STT and TTS clients over them, and the control routes."""

    def __init__(self, state, token: str, stt_factory=None, tts_factory=None) -> None:
        self.state = state
        self.token = token
        self._stt_factory, self._tts_factory = stt_factory, tts_factory
        self.interruption_log: collections.deque = collections.deque(maxlen=LOG_KEEP)      # the last decisions of every connection

    @property
    def interruption_judge(self):
        """The judge of the interruption manager: a call to TaDaMoo's technical session; None when TaDaMoo's address is not usable."""
        cfg = self.cfg
        try:
            return TechnicalJudge(TadamooApi(cfg.tadamoo_base_url, cfg.tadamoo_token))
        except ValueError:
            return None

    @property
    def cfg(self):
        return self.state.config

    @property
    def brains(self):
        return self.state.brains

    def functions(self) -> dict:
        """{function: loaded?} as the service sees them now."""
        manager = self.state.functions
        if manager is None:
            return {"stt": False, "tts": False, "avatar": False}
        return {name: manager._live(name) == "loaded" for name in ("stt", "tts", "avatar")}

    def phone_avatar(self, persona: str = "") -> dict:
        """{avatar, render_modes} of a personality's avatar (the service's own route; the personality of Settings > Voice when `persona` is empty); {} when unknown."""
        status, body = self.control("GET", "/avatar/phone" + (f"?persona={persona}" if persona else ""))
        return body if status == 200 and isinstance(body, dict) else {}

    def stt_client(self, lang: str, follow: bool = False):
        """`follow`: the client reports the language it hears (and `lang` is only a hint, "" = detect)."""
        if self._stt_factory is not None:
            return self._stt_factory(lang, follow) if follow else self._stt_factory(lang)
        from callbridge import engines
        cfg = self.cfg
        client = engines.NativeSttClient(engines._get_stt_engine(cfg), lang=lang or (None if follow else cfg.stt_language or None), vad_threshold=cfg.stt_vad_threshold,
                                         report_language=follow,
                                         allowed_languages=tuple(getattr(cfg, "language_enabled", ()) or ()),
                                         endpoint_silence_s=cfg.stt_endpoint_silence_s, min_utterance_s=cfg.stt_min_utterance_s,
                                         vad_model=cfg.stt_vad_model, speech_prob=cfg.stt_vad_speech_prob,
                                         chunk_silence_s=getattr(cfg, "stt_chunk_silence_s", 0.0), chunk_min_s=getattr(cfg, "stt_chunk_min_s", 2.0))
        client.start()
        return client

    def tts_client(self):
        if self._tts_factory is not None:
            return self._tts_factory()
        from callbridge import engines
        client = engines.NativeTtsClient(engines._get_tts_engine(self.cfg), self.cfg)
        client.start()
        return client

    def control(self, method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict | list]:
        """One request to the service's own control app, with its token: (status, JSON). (0, {}) when it cannot be made."""
        cfg = self.cfg
        try:
            r = requests.request(method, f"http://127.0.0.1:{cfg.control_port}{path}", json=body if method != "GET" else None, timeout=timeout,
                                 headers={"Authorization": f"Bearer {cfg.control_auth_token}"})
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.debug("control %s %s: %s", method, path, e)
            return 0, {}
        return r.status_code, data if isinstance(data, (dict, list)) else {}


class Gateway:
    """The listening side. `start()` returns the port; `stop()` closes the server and every connection."""

    def __init__(self, host: Host, bind: str = "127.0.0.1", port: int = 0) -> None:
        if bind not in LOOPBACK:
            raise ValueError(f"the gateway listens on loopback only, not on {bind}")
        self.host, self.bind, self._port = host, bind, port
        self._server = None
        self._thread: threading.Thread | None = None
        self._clients = 0
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self._port

    def _token_ok(self, given: str) -> bool:
        return bool(given) and hmac.compare_digest(given.encode("utf-8", "replace"), self.host.token.encode("utf-8"))

    def _process_request(self, connection, request):
        """The handshake: the path must be /ws, and a bearer token, when sent, must be right. No token here means the first message must carry it."""
        if request.path.split("?", 1)[0] != PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        auth = request.headers.get("Authorization", "")
        if auth:
            if not (auth.lower().startswith("bearer ") and self._token_ok(auth[7:].strip())):
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
            connection.handshake_authed = True
        return None

    def _handle(self, ws) -> None:
        with self._lock:
            self._clients += 1
        try:
            Connection(ws, self.host, authed=getattr(ws, "handshake_authed", False)).run()
        except Exception as e:  # noqa: BLE001 (a connection must not take the server down)
            log.error("gateway: connection error: %s", e)
        finally:
            with self._lock:
                self._clients -= 1

    def start(self) -> int:
        from websockets.sync.server import serve
        self._server = serve(self._handle, self.bind, self._port, process_request=self._process_request, max_size=1 << 20)
        self._port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="voice-gateway", daemon=True)
        self._thread.start()
        log.info("gateway listening on %s:%d", self.bind, self._port)
        return self._port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(5)

    def info(self) -> dict:
        fn = self.host.functions()
        return {"host": self.bind, "port": self._port, "path": PATH, "clients": self._clients, "voice": bool(fn["stt"] and fn["tts"]),
                "stt": fn["stt"], "tts": fn["tts"], "avatar": fn["avatar"], "render_modes": list(RENDER_MODES),
                "brain": self.host.brains.mode, "persona": self.host.cfg.brain_persona}


def register(app, gateway: Gateway) -> None:
    """GET /gateway on the control app (its before_request already demands the bearer token)."""
    from flask import jsonify

    @app.get("/gateway")
    def gateway_info():
        return jsonify(gateway.info())

    @app.get("/interruptions")
    def interruptions():
        """The last decisions about speech over the avatar: what was decided and why, with at most 80 characters of what was said."""
        return jsonify({"interruptions": list(gateway.host.interruption_log)})
