"""The phone's way to the voice gateway: one address, TaDaMoo's, with the key the phone already has.

`GET /api/voice/gateway` tells the app whether the gateway is there and what the PC can do (the state of the voice and avatar functions, the
render modes). `GET /api/voice/gateway/ws` is the WebSocket: after the handler has checked the phone's key (or that the client is this PC),
`relay` opens the WebSocket handshake to the gateway on loopback with the service token, which the phone never sees, and then copies bytes both
ways until one side closes. The server does not read the frames; the gateway speaks the protocol."""
from __future__ import annotations

import select
import socket
import threading

from serve import voice_proxy, voice_service

WS_PATH = "/api/voice/gateway/ws"
MAX_CLIENTS = 4
CHUNK = 1 << 16
INFO_KEYS = ("voice", "stt", "tts", "avatar", "render_modes", "brain", "persona")

_active = 0
_lock = threading.Lock()


def info(personas: list | None = None) -> dict:
    """What the Avatar tab needs before it connects: {available, reason?, path, voice, stt, tts, avatar, render_modes, brain, persona, personas}.
    `personas` is the list of {slug, name} the phone may choose from."""
    try:
        status, body = voice_proxy.call({"method": "GET", "path": "/gateway"})
    except voice_proxy.Refused as e:
        return {"available": False, "reason": e.message, "personas": []}
    if status != 200 or not body.get("port"):
        return {"available": False, "reason": "the voice gateway is not listening", "personas": []}
    return {"available": True, "path": WS_PATH, **{k: body.get(k) for k in INFO_KEYS}, "personas": personas or []}


def _gateway_port() -> int:
    try:
        status, body = voice_proxy.call({"method": "GET", "path": "/gateway"})
    except voice_proxy.Refused:
        return 0
    port = body.get("port") if status == 200 else 0
    return port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else 0


def _refuse(handler, code: int, kind: str, message: str) -> None:
    handler._json(code, {"error": {"type": kind, "message": message}})


def _token() -> str:
    return voice_service.ensure_token().read_text(encoding="utf-8").strip()


def _upgrade_headers(handler) -> str | None:
    """The Sec-WebSocket-Key of a valid upgrade request, or None."""
    h = handler.headers
    wants = "websocket" in h.get("Upgrade", "").lower() and "upgrade" in h.get("Connection", "").lower()
    key = h.get("Sec-WebSocket-Key", "").strip()
    return key if wants and key and h.get("Sec-WebSocket-Version", "").strip() == "13" else None


def relay(handler) -> None:
    """Passes the phone's WebSocket to the gateway. The caller has already decided that this client may."""
    global _active
    key = _upgrade_headers(handler)
    if key is None:
        return _refuse(handler, 400, "invalid_request_error", "this route is a WebSocket: send a normal upgrade request")
    port = _gateway_port()
    if not port:
        return _refuse(handler, 503, "voice_gateway_down", "the voice gateway is not listening: the voice service has not started")
    with _lock:
        if _active >= MAX_CLIENTS:
            return _refuse(handler, 503, "voice_gateway_busy", "too many phones are connected to the voice gateway")
        _active += 1
    upstream = None
    try:
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
            upstream.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                              f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {_token()}\r\n\r\n").encode("ascii"))
        except OSError:
            return _refuse(handler, 503, "voice_gateway_down", "the voice gateway does not answer")
        upstream.settimeout(None)
        handler.close_connection = True
        _pipe(handler.connection, upstream)
    finally:
        if upstream is not None:
            upstream.close()
        with _lock:
            _active -= 1


def _pipe(client: socket.socket, upstream: socket.socket) -> None:
    """Copies bytes both ways until one side closes or fails."""
    sockets = [client, upstream]
    while True:
        try:
            ready, _, _ = select.select(sockets, [], [], 60)
        except (OSError, ValueError):
            return
        for s in ready:
            try:
                data = s.recv(CHUNK)
            except OSError:
                return
            if not data:
                return
            try:
                (upstream if s is client else client).sendall(data)
            except OSError:
                return
