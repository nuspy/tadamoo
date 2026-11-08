"""Blueprint /diag/* — verifiche puntuali per i test guidati della GUI.

Ogni endpoint esegue UNA verifica e risponde con dati oggettivi: il wizard
nella GUI compone questi check in sequenze passo-passo con esito pass/fail.

  GET  /diag/mic?device=<idx>&seconds=<s>   livello del microfono (RMS/picco)
  POST /diag/echo {"seconds": 3}            registra e riproduce (loop locale)
  GET  /diag/loopback                       cattura loopback (copilota) 1s
  GET  /diag/voice-engine                   health + modelli dei motori voce
  GET  /diag/hermes                         raggiungibilita' del cervello
  GET  /diag/acp                            WSL + agente ACP pronto
  POST /diag/tts-roundtrip {"text"}         TTS -> STT -> similarita'
"""
from __future__ import annotations

import base64
import difflib
import io
import logging
import shlex
import subprocess
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


@bp.get("/diag/hermes")
def diag_hermes():
    from .api_components import _hermes_status

    status = _hermes_status(_cfg())
    return jsonify({"ok": status.get("state") == "running", **status})


@bp.get("/diag/acp")
def diag_acp():
    cfg = _cfg()
    if not cfg.acp_enabled:
        return jsonify({"ok": False, "error": "acp_enabled=false"}), 501
    # 1) il runtime dell'agente e' raggiungibile?
    #    - WSL: la distro risponde   - nativo: l'eseguibile hermes esiste
    if cfg.acp_use_wsl:
        try:
            proc = subprocess.run(
                ["wsl.exe", "-d", cfg.acp_wsl_distro, "--", "true"],
                capture_output=True, timeout=20,
            )
            runtime_ok = proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            runtime_ok = False
        runtime_label = f"wsl:{cfg.acp_wsl_distro}"
    else:
        from pathlib import Path
        toks = shlex.split(cfg.acp_native_command, posix=False)
        exe = toks[0].strip('"') if toks else ""
        runtime_ok = bool(exe) and Path(exe).is_file()
        runtime_label = f"native:{exe}"
    # 2) l'agente del PTT e' avviato (warm)?
    ptt = current_app.config.get("CB_PTT")
    brain = getattr(ptt, "brain_full", None) if ptt else None
    agent_ready = bool(getattr(brain, "is_ready", False))
    return jsonify({
        "ok": runtime_ok and agent_ready,
        "mode": "wsl" if cfg.acp_use_wsl else "native",
        "runtime": runtime_label,
        "runtime_ok": runtime_ok,
        "wsl_ok": runtime_ok,  # compat con la GUI esistente
        "agent_ready": agent_ready,
        "note": None if agent_ready else
        "agente non avviato: parte col warm-up o alla prima sessione",
    })


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
