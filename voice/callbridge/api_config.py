"""Blueprint /config — lettura e modifica della configurazione dalla GUI.

  GET /config  -> {"config": {...}, "restart_required_fields": [...]}
  PUT /config  {"updates": {campo: valore, ...}}
               -> {"applied": [...], "requires_restart": [...], "rejected": {...}}

I campi "hot" vengono mutati in-place sulla Config condivisa (i moduli tengono
un riferimento all'oggetto, quindi vedono subito il nuovo valore); tutto viene
comunque salvato su config.json. I campi in RESTART_REQUIRED hanno effetto
solo al riavvio del processo (hotkey gia' registrate, server gia' bindati...):
la GUI mostra il banner "riavvio necessario".
"""
from __future__ import annotations

import logging
import threading
from dataclasses import fields

from flask import Blueprint, current_app, jsonify, request

from .config import Config

log = logging.getLogger("callbridge.api.config")

bp = Blueprint("config", __name__)

# Serializza le PUT /config: due richieste concorrenti che chiamano cfg.save()
# potrebbero intrecciare la scrittura di config.json e corromperlo.
_write_lock = threading.Lock()

# Campi letti UNA VOLTA all'avvio (registrazione hotkey, bind dei server,
# costruzione dei cervelli/thread): cambiarli richiede il restart del main.
RESTART_REQUIRED = {
    # Il worker avatar legge AVATAR_FPS all'avvio: cambiarlo a caldo non
    # sposta nulla finche' non riparte.
    "avatar_fps",
    "control_host", "control_port", "control_auth_token",
    "gateway_enabled", "gateway_host", "gateway_port", "gateway_auth_token",
    "gateway_media_port",
    "push_to_talk_enabled", "push_to_talk_hotkey",
    "push_to_talk_hotkey_fullduplex", "push_to_talk_hotkey_limited",
    "push_to_talk_hotkey_copilot",
    # Modalità LM Studio (Ctrl+Shift+F4/F5): brain costruito e hotkey registrate
    # una sola volta all'avvio. base_url/model/system_prompt sono invece "hot"
    # (riletti a ogni begin_session in LmStudioClient), quindi NON qui.
    "avatar_chat_enabled",
    "push_to_talk_hotkey_lmstudio", "push_to_talk_hotkey_lmstudio_full",
    "copilot_enabled", "copilot_use_acp",
    "hermes_mode", "hermes_base_url",
    "acp_enabled", "acp_wsl_distro", "acp_wsl_command", "acp_cwd",
    "call_transport",
}

_FIELDS = {f.name: f for f in fields(Config)}


def _coerce(name: str, value, current):
    """Porta `value` al tipo del campo; ValueError se incompatibile."""
    if value is None:
        # None e' ammesso SOLO per i campi opzionali (default None nel dataclass,
        # es. device/modello): usare `current` non basta perche' un opzionale con
        # valore corrente non-None deve poter tornare None, e un campo
        # obbligatorio (int/str/bool) non deve mai poter diventare None (config.json
        # corrotta -> backend che non riparte).
        f = _FIELDS.get(name)
        if f is not None and f.default is None:
            return None
        raise ValueError("campo non annullabile")
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        raise ValueError("atteso booleano")
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, bool):
            raise ValueError("atteso intero")
        # int(8770.9) troncherebbe in silenzio: accetta solo interi esatti.
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("atteso un intero, non un decimale")
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        if not isinstance(value, str):
            raise ValueError("attesa stringa")
        return value
    # campi attualmente None (es. device opzionali): accetta int o str
    if isinstance(value, (int, str, float)):
        return value
    raise ValueError("tipo non gestito")


def _campi_tts_toccati(applied: list[str]) -> set[str]:
    """Campi appena modificati che richiedono il reset degli engine TTS.

    Un motore del catalogo con id `x` usa `x_device`/`x_gpu_memory`; Kokoro fa
    eccezione (campi storici `tts_device`/`tts_gpu_memory`) e ha in piu' la
    precisione del modello.
    """
    campi = {"tts_device", "tts_gpu_memory", "kokoro_precision"}
    try:
        from .engines.catalog import SPECS
        for s in SPECS:
            campi.add(f"{s.id}_device")
            campi.add(f"{s.id}_gpu_memory")
            # Parametri passati all'helper via env (es. modello di Qwen3-TTS):
            # cambiarli deve riavviarlo, come il device.
            campi.update((s.cfg_env or {}).values())
    except Exception as e:  # noqa: BLE001 - senza catalogo restano i campi storici
        log.debug("catalogo motori non disponibile: %s", e)
    return campi.intersection(applied)


def _motori_tts_toccati(applied: list[str]) -> set[str]:
    """Id dei motori TTS ai quali appartengono i campi appena modificati.

    Serve a ricaricare SOLO quelli: cambiare il device di XTTS non deve toccare
    Kokoro. Kokoro usa i campi storici `tts_device`/`tts_gpu_memory` e la
    precisione; gli altri `<id>_device`, `<id>_gpu_memory` e i campi di
    `cfg_env` (es. il modello di Qwen3-TTS)."""
    motori: set[str] = set()
    cambiati = set(applied)
    if cambiati & {"tts_device", "tts_gpu_memory", "kokoro_precision"}:
        motori.add("kokoro")
    try:
        from .engines.catalog import SPECS
        for s in SPECS:
            propri = {f"{s.id}_device", f"{s.id}_gpu_memory",
                      *((s.cfg_env or {}).values())}
            if cambiati & propri:
                motori.add(s.id)
    except Exception as e:  # noqa: BLE001
        log.debug("catalogo motori non disponibile: %s", e)
    return motori


@bp.get("/config")
def config_get():
    cfg = current_app.config["CB_CONFIG"]
    return jsonify({
        "config": cfg.as_dict(),
        "restart_required_fields": sorted(RESTART_REQUIRED),
    })


@bp.put("/config")
def config_put():
    cfg = current_app.config["CB_CONFIG"]
    body = request.get_json(silent=True) or {}
    updates = body.get("updates")
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": "campo 'updates' mancante o vuoto"}), 400

    applied: list[str] = []
    requires_restart: list[str] = []
    rejected: dict[str, str] = {}
    with _write_lock:
        for name, value in updates.items():
            f = _FIELDS.get(name)
            if f is None:
                rejected[name] = "campo sconosciuto"
                continue
            current = getattr(cfg, name)
            try:
                coerced = _coerce(name, value, current)
            except (ValueError, TypeError) as e:
                rejected[name] = str(e)
                continue
            if coerced == current:
                continue  # nessun cambiamento
            setattr(cfg, name, coerced)
            applied.append(name)
            if name in RESTART_REQUIRED:
                requires_restart.append(name)

        if applied:
            cfg.save()
            log.info("Config aggiornata dalla GUI: %s%s", ", ".join(applied),
                     f" (riavvio per: {', '.join(requires_restart)})"
                     if requires_restart else "")
            # Device/memoria/precisione degli engine: reset a caldo, niente
            # riavvio. STT (unico, sempre attivo): ricarica SUBITO sul nuovo
            # modello/device/precisione. TTS: il motore attivo che era in memoria
            # viene ricaricato subito, gli altri al prossimo uso.
            # I campi si derivano dal CATALOGO dei motori: la lista era cablata e
            # i motori aggiunti dopo (XTTS, CosyVoice, Orpheus, Dia) cambiavano
            # device senza che il loro helper venisse mai riavviato — restava
            # acceso col device precedente.
            # Cambio di avatar attivo: il motore TTS segue la voce della
            # nuova identita', subito. Senza, in VRAM restava caldo quello
            # dell'identita' precedente finche' non arrivava una sintesi.
            if "avatar_persona" in applied:
                try:
                    from .api_personas import allinea_motore_tts_persona
                    allinea_motore_tts_persona(cfg.avatar_persona)
                except Exception as e:  # noqa: BLE001
                    log.warning("TTS: allineamento al nuovo avatar fallito: %s", e)
            _STT_ENGINE_FIELDS = {"stt_device", "stt_gpu_memory", "stt_model",
                                  "stt_compute_type"}
            touched_tts = _campi_tts_toccati(applied)
            touched_stt = _STT_ENGINE_FIELDS.intersection(applied)
            if touched_tts or touched_stt:
                try:
                    from .engines import reset_engines
                    reset_engines(cfg, tts=bool(touched_tts), stt=bool(touched_stt),
                                  motori_tts=_motori_tts_toccati(applied) or None)
                except Exception as e:  # noqa: BLE001
                    log.warning("Reset engine dopo config fallito: %s", e)
            # Modello di Qwen3-TTS cambiato mentre e' il motore attivo: lo si
            # riscalda SUBITO in background. Una taglia nuova va scaricata (GB):
            # senza, il download lo pagava la prima frase, a voce ferma; cosi'
            # la GUI mostra "carico…" e la conversazione trova il motore pronto.
            if any(c.startswith("qwen3tts_") for c in touched_tts):
                try:
                    from .engines import _get_tts_engine
                    router = _get_tts_engine(cfg)
                    stato = router.stato_motori().get("qwen3tts", {}).get("state")
                    if router.attivo == "qwen3tts" and stato != "loading":
                        router.attiva("qwen3tts")
                except Exception as e:  # noqa: BLE001
                    log.warning("Qwen3-TTS: riscaldamento dopo il cambio fallito: %s", e)
    status = 200 if not rejected or applied else 400
    return jsonify({
        "applied": applied,
        "requires_restart": requires_restart,
        "rejected": rejected,
    }), status
