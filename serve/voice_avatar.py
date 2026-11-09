"""The Voice tab's avatar requests that the JSON proxy (voice_proxy.py) cannot carry, and the avatar settings.

    GET  /api/voice/avatar                                  {virtual_camera, enhance, engines, resolution, fps_hint}: the camera output (off until the
                                                            person turns it on), the acceleration of the video (everything off) and the engine and
                                                            output height chosen for each target (pc, web, android, esp32)
    POST /api/voice/avatar {virtual_camera?, enhance?, engines?, resolution?, fps_hint?}   change them (voice.json `avatar`; the service follows by
                                                            itself); `enhance` takes any of upscale, scale, interpolate, factor, worker_fps_cap,
                                                            worker_height_cap, rtx_video_path; `engines` and `resolution` are {target: value}
    GET  /api/voice/avatar/engines                          {avatar, engines: [descriptor + status for the active avatar], selection, resolutions}
    POST /api/voice/avatar/engines/select {target, engine, resolution?}   choose one target's engine (and height); answers the selection
    POST /api/voice/avatar/wizard/<draft>/<foto|pose|video|voce>   a photo, a video or a voice sample for the creation wizard: the multipart body
                                                            passes through to the service in pieces, without being read here, and only up to
                                                            the size of its step
    POST /api/voice/avatar/<name>/training-video            the long video a personalised engine is trained from: the same relay, up to 2000 MB
                                                            (its list and removal are JSON routes of voice_proxy.py)
    GET  /api/voice/avatar/preview.mjpg                     the rendered frames, a long-lived MJPEG stream relayed from the service
    GET  /api/voice/avatar/<name>/poster|sprites.png        the portrait of an avatar; its mouth sprites over it in one picture
    GET  /api/voice/avatar/<name>/assets.zip|manifest.json  the files a phone or the ESP32 renders the avatar from (this PC or a phone with its key)
    GET  /api/voice/avatar/models/wav2lip.onnx              the Wav2Lip model the phones run, with Range and ETag (same callers)

The rest is this PC only. The service's token is added here and never reaches the browser; a response is not searched for it because nothing of the
service's that carries one is relayed (the stream, an image and a JSON answer of the service)."""
from __future__ import annotations

import http.client
import json
import re

from serve import settings, voice_service, voice_settings

NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}"
UPLOAD = re.compile(r"/api/voice/avatar/wizard/([0-9a-f]{32})/(foto|pose|video|voce)")
TRAINING_UPLOAD = re.compile(rf"/api/voice/avatar/({NAME})/training-video")
TRAINING_LIMIT = 2000 << 20                                                              # the service's own limit for the training video
POSTER = re.compile(rf"/api/voice/avatar/({NAME})/poster")
SPRITES = re.compile(rf"/api/voice/avatar/({NAME})/sprites\.png")
PREVIEW = "/api/voice/avatar/preview.mjpg"
DEVICE_FILES = re.compile(rf"/api/voice/avatar/(?:{NAME}/(?:assets\.zip|manifest\.json)|models/wav2lip\.onnx)")
FORWARDED_IN = ("Range", "If-Range", "If-None-Match")
FORWARDED_OUT = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Cache-Control", "Content-Disposition", "X-Content-SHA256")
FILE_WAIT_S = 120.0
BASE = "/api/voice/avatar"
LIMITS = {"foto": 20 << 20, "pose": 20 << 20, "video": 500 << 20, "voce": 25 << 20}     # the service's own limits, per step
OVERHEAD = 1 << 16                                                                       # the multipart framing around the file
CHUNK = 1 << 20
IMAGE_MAX = 20 << 20
UPLOAD_WAIT_S = 600.0
STREAM_WAIT_S = 60.0
KEYS = ("virtual_camera", "enhance") + voice_settings.AVATAR_ENGINE_KEYS
ENGINES = "/api/voice/avatar/engines"
SELECT = ENGINES + "/select"


def _refuse(handler, status: int, code: str, message: str) -> None:
    handler._json(status, {"error": {"type": "voice_avatar", "code": code, "message": message}})


# ---------------------------------------------------------------------------------------- settings
def view() -> dict:
    stored = voice_settings._read().get("avatar")
    stored = stored if isinstance(stored, dict) else {}
    return {"virtual_camera": stored.get("virtual_camera") is True, "enhance": voice_settings.enhance(stored.get("enhance")),
            **voice_settings.avatar_selection(stored)}


def update(req) -> dict:
    if not isinstance(req, dict):
        raise voice_settings.VoiceSettingsError("body", "Send a JSON object.", "Manda un oggetto JSON.")
    for key in req:
        if key not in KEYS:
            raise voice_settings.VoiceSettingsError("field", f"Unknown field: {key}.", f"Campo sconosciuto: {key}.")
    if "virtual_camera" in req and not isinstance(req["virtual_camera"], bool):
        raise voice_settings.VoiceSettingsError("virtual_camera", "virtual_camera is true or false.", "virtual_camera è true o false.")
    with voice_settings._lock:
        data = voice_settings._read()
        stored = data.get("avatar") if isinstance(data.get("avatar"), dict) else {}
        if "enhance" in req:
            req = {**req, "enhance": voice_settings.merge_enhance(stored.get("enhance"), req["enhance"])}      # checked as a whole before anything is written
        if set(req) & set(voice_settings.AVATAR_ENGINE_KEYS):
            req = {**{k: v for k, v in req.items() if k not in voice_settings.AVATAR_ENGINE_KEYS},
                   **voice_settings.merge_avatar_selection(stored, {k: v for k, v in req.items() if k in voice_settings.AVATAR_ENGINE_KEYS})}
        stored.update(req)
        data["avatar"] = stored
        voice_settings._write(data)
    return view()


# ---------------------------------------------------------------------------------------- the engines
def resolutions_of(selection: dict) -> dict:
    """{target: the heights the engine chosen for it offers}."""
    return {t: list(voice_settings.AVATAR_ENGINES[e][1]) for t, e in selection["engines"].items()}


def engines_view() -> tuple[int, dict]:
    """The service describes the engines for the active avatar; the choice is read here from voice.json, which this server owns."""
    try:
        status, data = voice_service.call("GET", "/avatar/engines", None, timeout=voice_service.TIMEOUT)
    except voice_service.ServiceDown as e:
        return 503, {"error": {"type": "voice_avatar", "code": "voice_service_down", "message": str(e)}}
    if status != 200 or not isinstance(data, dict):
        return status, data if isinstance(data, dict) else {}
    selection = voice_settings.avatar_selection(voice_settings._read().get("avatar"))
    return 200, {"avatar": data.get("avatar", ""), "engines": data.get("engines", []), "selection": selection, "resolutions": resolutions_of(selection)}


# ---------------------------------------------------------------------------------------- relays
def _connect(timeout: float):
    host, port, token = voice_service.endpoint()
    return http.client.HTTPConnection(host, port, timeout=timeout), token


def _local(handler) -> bool:
    if handler.remote_listener or not settings.is_loopback(handler.client_address[0]):
        _refuse(handler, 403, "forbidden", "The avatar is shown and made only on the PC the server runs on.")
        return False
    return True


def _down(handler, e: Exception) -> None:
    _refuse(handler, 503, "voice_service_down", str(e) if isinstance(e, voice_service.ServiceDown) else "the voice service does not answer")


def upload(handler, step: str, draft: str) -> None:
    """Copy the request body to the service's wizard route; the answer of the service is the answer."""
    relay_upload(handler, LIMITS[step], f"/avatar/wizard/{draft}/{step}")


def relay_upload(handler, cap: int, upstream: str) -> None:
    """Copy a multipart request body of at most `cap` bytes of file to `upstream` on the service, in pieces."""
    limit = cap + OVERHEAD
    ctype = handler.headers.get("Content-Type", "")
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked" or "Content-Length" not in handler.headers:
        handler.close_connection = True
        return _refuse(handler, 411, "length_required", "Send the file with a Content-Length.")
    try:
        length = int(handler.headers["Content-Length"])
    except ValueError:
        length = -1
    if length <= 0 or not ctype.lower().startswith("multipart/form-data") or "boundary=" not in ctype.lower():
        handler._discard_body()
        return _refuse(handler, 400, "bad_request", "Send the file as multipart/form-data.")
    if length > limit:
        handler.close_connection = True
        return _refuse(handler, 413, "too_big", f"The file is too big: at most {cap >> 20} MB.")
    try:
        conn, token = _connect(UPLOAD_WAIT_S)
    except voice_service.ServiceDown as e:
        handler._discard_body()
        return _down(handler, e)
    try:
        conn.putrequest("POST", upstream)
        conn.putheader("Authorization", "Bearer " + token)
        conn.putheader("Content-Type", ctype)
        conn.putheader("Content-Length", str(length))
        conn.endheaders()
        left = length
        try:
            while left > 0:
                piece = handler.rfile.read(min(CHUNK, left))
                if not piece:
                    break
                left -= len(piece)
                conn.send(piece)
        except OSError:
            pass                                       # the service answered early and closed; its answer is read below
        if left > 0:
            handler.close_connection = True
        resp = conn.getresponse()
        raw = resp.read(1 << 20)
    except (OSError, http.client.HTTPException) as e:
        handler.close_connection = True
        return _down(handler, e)
    finally:
        conn.close()
    try:
        body = json.loads(raw)
    except ValueError:
        body = {"error": "the voice service gave an answer that is not JSON"}
    handler._json(resp.status, body if isinstance(body, dict) else {})


def preview(handler) -> None:
    """Relay the service's MJPEG stream until either side closes."""
    try:
        conn, token = _connect(STREAM_WAIT_S)               # also how long a stream may be silent before it is dropped
    except voice_service.ServiceDown as e:
        return _down(handler, e)
    try:
        conn.request("GET", "/avatar/preview.mjpg", headers={"Authorization": "Bearer " + token})
        resp = conn.getresponse()
        if resp.status != 200:
            try:
                body = json.loads(resp.read(1 << 16))
            except ValueError:
                body = {}
            return handler._json(resp.status, body if isinstance(body, dict) else {})
        handler.send_response(200)
        handler.send_header("Content-Type", resp.getheader("Content-Type", "multipart/x-mixed-replace"))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        while True:
            piece = resp.read1(1 << 16)
            if not piece:
                break
            handler.wfile.write(piece)
            handler.wfile.flush()
    except (OSError, http.client.HTTPException):
        handler.close_connection = True              # the viewer left, or the service stopped
    finally:
        conn.close()


def image(handler, upstream: str, mime: str, missing: str) -> None:
    """Relay one picture of the service of type `mime`; anything else it answers is "not found"."""
    try:
        conn, token = _connect(10.0)
    except voice_service.ServiceDown as e:
        return _down(handler, e)
    try:
        conn.request("GET", upstream, headers={"Authorization": "Bearer " + token})
        resp = conn.getresponse()
        data = resp.read(IMAGE_MAX + 1)
    except (OSError, http.client.HTTPException) as e:
        return _down(handler, e)
    finally:
        conn.close()
    if resp.status != 200 or len(data) > IMAGE_MAX or resp.getheader("Content-Type") != mime:
        return _refuse(handler, 404 if resp.status in (200, 404) else resp.status, "not_found", missing)
    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)


# ---------------------------------------------------------------------------------------- the handler's entry points
def device_file(handler, path: str) -> None:
    """Relay a file of the service to this PC or to a phone with its key; ranges and conditional requests pass through, the body in pieces."""
    if not handler._local_or_device():
        return _refuse(handler, 403, "forbidden", "The avatar files are given to this PC or to a phone with its key, with phones turned on.")
    try:
        conn, token = _connect(FILE_WAIT_S)
    except voice_service.ServiceDown as e:
        return _down(handler, e)
    started = False
    try:
        headers = {"Authorization": "Bearer " + token, **{k: handler.headers[k] for k in FORWARDED_IN if handler.headers.get(k)}}
        conn.request("GET", path[len("/api/voice"):], headers=headers)
        resp = conn.getresponse()
        started = True
        handler.send_response(resp.status)
        for name in FORWARDED_OUT:
            if resp.getheader(name):
                handler.send_header(name, resp.getheader(name))
        handler.end_headers()
        while True:
            piece = resp.read(CHUNK)
            if not piece:
                break
            handler.wfile.write(piece)
    except (OSError, http.client.HTTPException) as e:
        handler.close_connection = True
        if not started:
            _down(handler, e)                        # after the headers the phone left or the service stopped: nothing more to say
    finally:
        conn.close()


def get(handler, path: str) -> None:
    if DEVICE_FILES.fullmatch(path):
        return device_file(handler, path)
    if not _local(handler):
        return
    if path == BASE:
        return handler._json(200, view())
    if path == ENGINES:
        return handler._json(*engines_view())
    if path == PREVIEW:
        return preview(handler)
    m = POSTER.fullmatch(path)
    if m:
        return image(handler, f"/personas/{m.group(1)}/poster", "image/jpeg", "That avatar has no portrait.")
    m = SPRITES.fullmatch(path)
    if m:
        return image(handler, f"/avatar/{m.group(1)}/sprites.png", "image/png", "That avatar has no mouth sprites.")
    _refuse(handler, 404, "not_found", "not found")


def _selected(req) -> dict:
    selection = voice_settings.select_avatar_engine(req)
    return {"selection": selection, "resolutions": resolutions_of(selection)}


def post(handler, path: str) -> None:
    if not _local(handler):
        handler._discard_body()
        return
    m = UPLOAD.fullmatch(path)
    if m:
        return upload(handler, m.group(2), m.group(1))
    m = TRAINING_UPLOAD.fullmatch(path)
    if m:
        return relay_upload(handler, TRAINING_LIMIT, f"/avatar/{m.group(1)}/training-video")
    if path not in (BASE, SELECT):
        handler._discard_body()
        return _refuse(handler, 404, "not_found", "not found")
    try:
        try:
            raw = handler._body(1 << 12)
        except Exception as e:                           # noqa: BLE001 (the handler's BadBody: it carries the status to send)
            if not hasattr(e, "status"):
                raise
            return handler._json(e.status, {"error": {"message": str(e)}})
        req = json.loads(raw or b"{}")
        handler._json(200, update(req) if path == BASE else _selected(req))
    except voice_settings.VoiceSettingsError as e:
        handler._json(400, {"error": e.body()})
    except ValueError:
        handler._json(400, {"error": {"type": "invalid_request_error", "message": "the body is not valid JSON"}})
