"""Blueprint /diag/* — verifiche puntuali per i test guidati della GUI.

Ogni endpoint esegue UNA verifica e risponde con dati oggettivi: il wizard
nella GUI compone questi check in sequenze passo-passo con esito pass/fail.

  GET  /diag/mic?device=<idx>&seconds=<s>   livello del microfono (RMS/picco)
  POST /diag/echo {"seconds": 3}            registra e riproduce (loop locale)
  GET  /diag/loopback                       cattura loopback (copilota) 1s
  GET  /diag/voice-engine                   health + modelli dei motori voce
  GET  /diag/brain                          TaDaMoo raggiungibile dal servizio vocale
  POST /diag/tts-roundtrip {"text"}         TTS -> STT -> similarita'
"""
from __future__ import annotations

import base64
import difflib
import io
import logging
import threading
import wave

import numpy as np
import requests
from flask import Blueprint, current_app, jsonify, request

log = logging.getLogger("callbridge.api.diag")

bp = Blueprint("diag", __name__)

# sd.rec/sd.play usano lo stato GLOBALE di sounddevice: due richieste
# /diag/mic|echo concorrenti (o durante una sessione PTT) si pesterebbero lo
# stream. Le serializziamo con questo lock di modulo.
_audio_lock = threading.Lock()


def _cfg():
    return current_app.config["CB_CONFIG"]


def _record(device, seconds: float, samplerate: int = 16000) -> np.ndarray:
    import sounddevice as sd

    frames = int(seconds * samplerate)
    data = sd.rec(frames, samplerate=samplerate, channels=1,
                  dtype="float32", device=device)
    sd.wait()
    return data[:, 0]


@bp.get("/diag/mic")
def diag_mic():
    import sounddevice as sd

    cfg = _cfg()
    device = request.args.get("device", type=int)
    if device is None:
        device = cfg.ptt_input_device  # None = default OS
    seconds = min(5.0, max(0.2, request.args.get("seconds", default=1.0, type=float)))
    try:
        with _audio_lock:
            samples = _record(device, seconds)
    except Exception as e:  # noqa: BLE001 - device assente/occupato
        return jsonify({"ok": False, "error": str(e)}), 502
    rms = float(np.sqrt(np.mean(samples ** 2)))
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    try:
        name = sd.query_devices(device)["name"] if device is not None \
            else sd.query_devices(kind="input")["name"]
    except Exception:  # noqa: BLE001
        name = None
    return jsonify({"ok": True, "rms": round(rms, 4), "peak": round(peak, 4),
                    "seconds": seconds, "device_name": name})


@bp.post("/diag/echo")
def diag_echo():
    """Registra dal mic del PTT e riproduce sulle casse del PTT: l'utente
    sente la propria voce -> mic e uscita funzionano entrambi."""
    import sounddevice as sd

    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    seconds = min(10.0, max(0.5, float(body.get("seconds", 3.0))))
    try:
        with _audio_lock:
            samples = _record(cfg.ptt_input_device, seconds)
            sd.play(samples, samplerate=16000, device=cfg.ptt_output_device)
            sd.wait()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502
    rms = float(np.sqrt(np.mean(samples ** 2)))
    return jsonify({"ok": True, "rms": round(rms, 4), "seconds": seconds})


@bp.get("/diag/loopback")
def diag_loopback():
    """Verifica la cattura loopback usata dal copilota (audio in uscita)."""
    try:
        import soundcard as sc

        speaker = sc.default_speaker()
        mic = sc.get_microphone(speaker.name, include_loopback=True)
        rate = _cfg().copilot_loopback_samplerate
        with mic.recorder(samplerate=rate, channels=1) as rec:
            data = rec.record(numframes=rate)  # 1 secondo
        rms = float(np.sqrt(np.mean(np.asarray(data) ** 2)))
        return jsonify({"ok": True, "device_name": speaker.name,
                        "rms": round(rms, 5),
                        "note": "rms>0 solo se c'e' audio in riproduzione"})
    except Exception as e:  # noqa: BLE001 - soundcard/WASAPI non disponibili
        return jsonify({"ok": False, "error": str(e)}), 502


@bp.get("/diag/voice-engine")
def diag_voice_engine():
    cfg = _cfg()
    out: dict = {"base_url": cfg.handy_base_url()}
    try:
        health = requests.get(f"{cfg.handy_base_url()}/health", timeout=3)
        out["health"] = health.ok
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e), **out}), 502
    try:
        out["models"] = requests.get(f"{cfg.handy_base_url()}/models/status",
                                     timeout=5).json()
    except (requests.RequestException, ValueError) as e:
        out["models_error"] = str(e)
    return jsonify({"ok": bool(out.get("health")), **out})


@bp.get("/diag/brain")
def diag_brain():
    """TaDaMoo's server answers the service (the same address and token the two brains use)."""
    from .tadamoo_api import ApiError, TadamooApi
    cfg = _cfg()
    out: dict = {"mode": cfg.brain_mode, "base_url": cfg.tadamoo_base_url}
    try:
        status, _body = TadamooApi(cfg.tadamoo_base_url, cfg.tadamoo_token, timeout=5).request("GET", "/api/personas")
    except (ApiError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e), **out}), 502
    return jsonify({"ok": status == 200, "status": status, **out}), (200 if status == 200 else 502)


@bp.post("/diag/tts-roundtrip")
def diag_tts_roundtrip():
    """TTS (Handy /tts/test) -> STT (Handy /transcribe) e confronto: verifica
    l'intera catena audio di Handy senza toccare i device del PC."""
    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "Prova della catena voce di Handy.").strip()
    try:
        synth = requests.post(
            f"{cfg.handy_base_url()}/tts/test",
            json={"text": text, "lang": cfg.default_lang},
            timeout=300,
        ).json()
    except (requests.RequestException, ValueError) as e:
        return jsonify({"ok": False, "step": "tts", "error": str(e)}), 502
    if synth.get("status") != "ok":
        return jsonify({"ok": False, "step": "tts",
                        "error": synth.get("message", "sintesi fallita")}), 502
    audio_b64 = synth.get("audio_b64")
    if not audio_b64:
        return jsonify({"ok": False, "step": "tts",
                        "error": "risposta senza audio"}), 502

    wav_bytes = base64.b64decode(audio_b64)
    try:
        trans = requests.post(
            f"{cfg.handy_base_url()}/transcribe",
            files={"file": ("roundtrip.wav", io.BytesIO(wav_bytes), "audio/wav")},
            timeout=120,
        ).json()
    except (requests.RequestException, ValueError) as e:
        return jsonify({"ok": False, "step": "stt", "error": str(e)}), 502
    heard = (trans.get("text") or "").strip()
    match = difflib.SequenceMatcher(
        None, text.lower(), heard.lower()).ratio()
    # WAV valido? (controllo di forma sul payload sintetizzato)
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            duration_s = w.getnframes() / max(1, w.getframerate())
    except wave.Error:
        duration_s = None
    return jsonify({
        "ok": match >= 0.6,
        "spoken": text,
        "heard": heard,
        "match": round(match, 3),
        "tts_duration_s": round(duration_s, 2) if duration_s else None,
        "voice": synth.get("voice"),
    })
