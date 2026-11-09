"""The routes of Settings > Voice: the catalog of voice models with what is installed, the choice and placement per service, the models
folder, and downloads that start only on an explicit request (plan first, then confirm and the license acknowledgement).

The download is serve/downloads.py's, so the plan, the `.part` files, the SHA-256 check and the Hugging Face token rules are the same;
this module only decides where the files go (the voice models folder) and what the page may ask. Nothing here writes a token."""
from __future__ import annotations

import os

from serve import downloads, models, userdata, voice_catalog, voice_proxy, voice_settings


def _folder() -> str:
    """The voice models folder, created when it is missing so a plan can be made for it."""
    folder = voice_settings.models_dir()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass                                                    # the plan reports a folder that cannot be used
    return folder


def _entry(model_id) -> dict:
    entry = voice_catalog.find(model_id)
    if entry is None:
        raise models.Refused(404, {"error": {"type": "not_found", "message": "no such voice model"}})
    return entry


def overview() -> dict:
    root = voice_settings.models_dir()
    services = []
    for service in voice_catalog.SERVICES:
        services.append({"id": service, "selected": voice_settings.selected(service), "placement": voice_settings.placement(service),
                         "enabled": voice_settings.enabled(service),
                         "models": [voice_catalog.public(e, root) for e in voice_catalog.models_of(service)]})
    return {"models_dir": root, "default_models_dir": voice_settings.default_models_dir(), "services": services,
            "download": downloads.DOWNLOADS.status()}


def select(req: dict) -> dict:
    voice_settings.select(req.get("service"), req.get("model"))
    return overview()


def placement(req: dict) -> dict:
    voice_settings.set_placement(req.get("service"), req.get("device"), req.get("memory"))
    return overview()


def set_enabled(req: dict) -> dict:
    voice_settings.set_enabled(req.get("service"), req.get("enabled"))
    return overview()


def set_models_dir(req: dict) -> dict:
    voice_settings.set_models_dir(req.get("path"))
    return overview()


def plan(model_id: str) -> dict:
    _entry(model_id)
    return downloads.plan(model_id, _folder(), folders=[])


def download(req: dict) -> dict:
    entry = _entry(req.get("id"))
    return downloads.DOWNLOADS.start(entry["id"], _folder(), req.get("token"), req.get("confirm"), req.get("ack"), folders=[],
                                     noncommercial=req.get("noncommercial"))


def status() -> dict:
    return downloads.DOWNLOADS.status()


def cancel() -> dict:
    return {"cancelled": downloads.DOWNLOADS.cancel(), **downloads.DOWNLOADS.status()}


def brain_view() -> dict:
    return voice_settings.brain()


def set_brain(req: dict) -> dict:
    changes = voice_settings.check_brain(req)
    persona = changes.get("persona")
    if persona and not (userdata.path("personas") / persona / "persona.json").is_file():
        raise voice_settings.VoiceSettingsError("persona", "That personality does not exist.", "Quella personalità non esiste.")
    return voice_settings.set_brain(req)


def telephony_view() -> dict:
    return voice_settings.telephony()


def set_telephony(req: dict) -> dict:
    return voice_settings.set_telephony(req)


def service_call(req: dict) -> tuple[int, dict]:
    """The Voice tab's request to the voice service, if it is on the list (serve/voice_proxy.py)."""
    try:
        return voice_proxy.call(req)
    except voice_proxy.Refused as e:
        return e.status, {"error": {"type": "voice_proxy", "code": e.code, "message": e.message}}
