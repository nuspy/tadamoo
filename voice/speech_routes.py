"""The speech routes of callAPIcall's old Handy server, served by the voice service itself: `/transcribe`, `/tts/test`, `/tts/models`,
`/tts/voices`, `/voices/cloned`, `/voices/clone`, `/models/status` and `/models/available`.

They answer in the shape callAPIcall's clients know and use the engines of the service (voice/callbridge/engines), which read models only
from the models folder: a model that is not installed is a 409 with `code: model_not_installed` and the plain reason. Nothing here loads
a library until a route is used."""
from __future__ import annotations

import base64
import os

from flask import Blueprint, current_app, jsonify, request

from callbridge import models_store

bp = Blueprint("speech", __name__)


def _hc():
    from callbridge import handy_compat
    return handy_compat


def _cfg():
    return current_app.config["CB_CONFIG"]


def _missing(e: Exception):
    body = _hc()._model_missing(e)
    return (jsonify(body), 409) if body else None


def _stt_not_installed(cfg) -> models_store.ModelNotInstalled | None:
    model = "whisper-" + str(getattr(cfg, "stt_model", "") or "")
    if model in models_store.MODELS and not models_store.is_installed(model):
        return models_store.ModelNotInstalled(model, "stt")
    return None


def _truthy(name: str) -> bool:
    return request.args.get(name, "") in ("1", "true", "yes")


@bp.post("/transcribe")
def transcribe():
    cfg = _cfg()
    sent = next(iter(request.files.values()), None)
    if sent is None:
        return jsonify({"status": "error", "message": "send the audio as a multipart file"}), 400
    absent = _stt_not_installed(cfg)
    if absent is not None:
        return _missing(absent)
    hc = _hc()
    try:
        audio, rate = hc._decode_audio(sent.read())
    except Exception as e:                                               # noqa: BLE001 (any decoder error: the file is not audio)
        return jsonify({"status": "error", "message": f"the audio cannot be read: {e}"}), 400
    if rate != hc.STT_RATE:
        audio = hc._resample(audio, rate, hc.STT_RATE)
    from callbridge import engines
    engine = engines._get_stt_engine(cfg)
    lang = request.args.get("lang") or cfg.stt_language or None
    try:
        if _truthy("segments") and hasattr(engine, "transcribe_detailed"):
            return jsonify(engine.transcribe_detailed(audio, lang, _truthy("words")))
        return jsonify({"text": engine.transcribe(audio, lang)})
    except Exception as e:                                               # noqa: BLE001
        return _missing(e) or (jsonify({"status": "error", "message": str(e)}), 500)


@bp.post("/tts/test")
def tts_test():
    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify({"status": "error", "message": "empty text"}), 400
    voice = body.get("voice") or cfg.default_voice
    lang = body.get("lang") or cfg.default_lang
    hc = _hc()
    asked = body.get("engine") or body.get("model") or ""
    engine = hc._engine_id_valido(asked) if asked else ""
    from callbridge import engines
    try:
        samples, rate = engines._get_tts_engine(cfg).synth(text, voice, lang, cfg.tts_speed, engine)
    except Exception as e:                                               # noqa: BLE001
        return _missing(e) or (jsonify({"status": "error", "message": str(e)}), 500)
    wav = hc._pcm_f32_to_wav_bytes(samples, rate)
    return jsonify({"status": "ok", "voice": voice, "engine": engine or "auto", "sample_rate": int(rate),
                    "duration_ms": int(1000 * len(samples) / rate), "audio_b64": base64.b64encode(wav).decode("ascii")})


@bp.get("/tts/models")
def tts_models():
    cfg = _cfg()
    from callbridge import engines
    from callbridge.engines.kokoro_tts import available_precisions
    router = engines._get_tts_engine(cfg)
    models = router.catalogo() if hasattr(router, "catalogo") else []
    return jsonify({"kokoro_precisions": sorted(available_precisions(router.model_dir).keys()),
                    "kokoro_precision": getattr(cfg, "kokoro_precision", "fp16"), "models": models,
                    "selected": _hc()._engine_id_valido(cfg.tts_engine), "selected_voice": cfg.default_voice})


def _voices_of(cfg, engine_id: str) -> list:
    from callbridge import engines
    router = engines._get_tts_engine(cfg)
    return router.voci_di(engine_id) if hasattr(router, "voci_di") else []


@bp.get("/tts/voices")
def tts_voices():
    cfg = _cfg()
    engine_id = _hc()._engine_id_valido(request.args.get("model") or cfg.tts_engine)
    return jsonify({"model": engine_id, "voices": _voices_of(cfg, engine_id)})


@bp.get("/voices/cloned")
def voices_cloned():
    return jsonify({"voices": _hc()._list_cloned()})


@bp.post("/voices/clone")
def voice_clone():
    hc = _hc()
    body = request.get_json(silent=True) or {}
    name, fmt, b64 = str(body.get("name") or "").strip(), str(body.get("format") or "wav").lstrip(".").lower(), body.get("audio_b64") or ""
    if not name or not b64:
        return jsonify({"status": "error", "message": "name and audio are needed"}), 400
    if not hc._CLONE_NAME.match(name) or fmt not in hc._CLONE_EXTS:
        return jsonify({"status": "error", "message": "invalid name or format"}), 400
    folder = hc._cloned_dir()
    if not folder:
        return jsonify({"status": "error", "message": "there is no models folder for the voices"}), 409
    try:
        raw = base64.b64decode(b64, validate=True)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{name}.{fmt}"), "wb") as f:
            f.write(raw)
    except (ValueError, OSError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify({"status": "ok", "voices": [v["name"] for v in hc._list_cloned()]})


@bp.delete("/voices/cloned/<name>")
def voice_clone_delete(name: str):
    hc = _hc()
    folder = hc._cloned_dir()
    removed = False
    for fn in (os.listdir(folder) if folder and os.path.isdir(folder) else ()):
        if os.path.splitext(fn)[0] == name and os.path.splitext(fn)[1].lstrip(".").lower() in hc._CLONE_EXTS:
            os.remove(os.path.join(folder, fn))
            removed = True
    if not removed:
        return jsonify({"status": "error", "message": "voice not found"}), 404
    return jsonify({"status": "ok", "voices": [v["name"] for v in hc._list_cloned()]})


@bp.get("/models/status")
def models_status():
    cfg = _cfg()
    from callbridge import engines
    stt, router = engines._get_stt_engine(cfg), engines._get_tts_engine(cfg)
    states = router.stato_motori() if hasattr(router, "stato_motori") else {}
    chosen = _hc()._engine_id_valido(cfg.tts_engine)
    mine = states.get(chosen) or {}
    return jsonify({"stt": _hc()._stt_stato(stt, bool(getattr(stt, "loaded", False))),
                    "tts": {"selected_model": chosen, "loaded": mine.get("state") == "ready", "state": mine.get("state", "unloaded"),
                            "message": mine.get("message", "")},
                    "engines": states})


@bp.get("/models/available")
def models_available():
    cfg = _cfg()
    from callbridge import engines
    stt = engines._get_stt_engine(cfg)
    chosen = _hc()._engine_id_valido(cfg.tts_engine)
    return jsonify({"stt": {"current_model": cfg.stt_model, "loaded": bool(getattr(stt, "loaded", False))},
                    "tts": {"engine": chosen, "selected_model": chosen, "voices": _voices_of(cfg, chosen)}})
