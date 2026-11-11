"""The route of the picture and video tools (tadamoo/media), so the page and the model can tell whether they can be used.

    GET  /api/media   {settings, ltx_dir, image: {ready, missing, ...}, video: {...}, tools: ["generate_image", ...]}
    POST /api/media   {enabled?, ltx_dir?, image_python?, video_python?, video_model?, image_mib?, video_mib?, wait_s?, idle_s?}   the same answer after the change

A kind is `ready` when LTX Studio's code, its environment and its weights are all there; `tools` are the ones a new conversation offers. The settings
are `media.json` in the user data folder; a change is made from the PC the server runs on."""
from __future__ import annotations

import json

from serve import settings, userdata
from tadamoo.media import client
from tadamoo.media.errors import MediaError
from tadamoo.media.settings import MediaSettings

PATH = "/api/media"
BODY_MAX = 1 << 12


def prefs() -> MediaSettings:
    return MediaSettings(userdata.path("media.json"))


def status() -> dict:
    config = prefs().read()
    found = client.check(config)
    on = config["enabled"]
    return {"settings": config, "ltx_dir": found.get("ltx_dir", ""), "image": found["image"], "video": found["video"],
            "tools": [f"generate_{k}" for k in ("image", "video") if on and found[k].get("ready")]}


def get(handler) -> None:
    handler._json(200, status())


def post(handler) -> None:
    if handler.remote_listener or not settings.is_loopback(handler.client_address[0]):
        handler._discard_body()
        return handler._json(403, {"error": {"type": "forbidden", "message": "the picture and video settings are changed on the PC the server runs on"}})
    try:
        req = json.loads(handler._body(BODY_MAX) or b"{}")
        if not isinstance(req, dict):
            raise ValueError("send a JSON object")
        prefs().write(req)
        client.forget_checks()
    except (MediaError, ValueError) as e:
        return handler._json(400, {"error": {"type": "media", "message": str(e) or "the body is not valid JSON"}})
    except Exception as e:  # noqa: BLE001 - a body that cannot be read (BadBody)
        return handler._json(getattr(e, "status", 400), {"error": {"type": "invalid_request_error", "message": str(e)}})
    handler._json(200, status())
