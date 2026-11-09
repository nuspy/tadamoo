"""Blueprint /components/* — stato e controllo dei componenti dalla GUI.

  GET  /components/status                stato di main/gateway/voice_engine/brain/avatar
  POST /components/main/restart          exit(42) -> il supervisore run-ptt rilancia
  POST /components/main/stop             sentinella ptt.stop + exit -> resta giu'
                                         (fino al riavvio di Windows: run-ptt
                                         scarta una sentinella pre-boot)
  POST /components/gateway/restart       -> 409 restart_main_instead (v1)
  (dettatura Alt+Spazio: nessun endpoint di controllo. E' SEMPRE attiva e
   calda, parte con il main, e lo STT che usa - unico, condiviso con le sessioni
   voce - non si scarica mai. Lo stato e' in /components/status.)
  POST /components/suspend               libera la memoria SENZA toccare il main
                                         (il supervisore run-ptt non entra in gioco)
  POST /components/resume                fine sospensione: riscalda i motori

I motori voce (STT/TTS) girano IN-PROCESS: non hanno start/stop propri, si
riavviano riavviando il main. L'endpoint che pilotava l'eseguibile Handy
esterno e' stato rimosso con la migrazione ai motori nativi.

Stati: running | loading | stopping | blocked | error | stopped.

"main" e' il processo che ospita QUESTO server: stop/restart rispondono prima
di uscire (threading.Timer), e la ripartenza e' compito del supervisore
run-ptt.ps1. Quando il main e' giu' la GUI usa i comandi Tauri locali
(supervisor.rs) come fallback.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import requests
from flask import Blueprint, current_app, jsonify

log = logging.getLogger("callbridge.api.components")

bp = Blueprint("components", __name__)

# Exit code convenzionale del restart richiesto dalla GUI (visibile nel
# ptt-supervisor.log per distinguerlo da un crash).
RESTART_EXIT_CODE = 42

# Sentinella letta da run-ptt.ps1: finche' esiste, il supervisore NON rilancia.
# Vale solo per la sessione corrente: un supervisore che parte dopo un reboot
# scarta la sentinella se e' anteriore all'ultimo avvio di Windows, cosi' uno
# Stop dalla GUI non lascia callAPIcall spento per sempre.
STOP_SENTINEL = Path(__file__).resolve().parents[1] / "ptt.stop"


def _cfg():
    return current_app.config["CB_CONFIG"]


def _tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _engines_native(cfg) -> bool:
    """True quando STT e TTS girano IN-PROCESS nel bridge: la :8720 la serve il
    thread handy-compat di main.py, non un processo esterno. In quel caso
    start/stop/restart non hanno senso e la GUI mostra il solo riavvio del
    bridge."""
    return cfg.stt_backend == "native" and cfg.tts_backend == "native"


def _voice_engine_status(cfg) -> dict:
    mode = "native" if _engines_native(cfg) else "external"
    try:
        health = requests.get(f"{cfg.handy_base_url()}/health", timeout=2)
        if not health.ok:
            return {"state": "error", "mode": mode,
                    "detail": f"health HTTP {health.status_code}"}
    except requests.RequestException as e:
        return {"state": "stopped", "mode": mode, "detail": str(e)}
    try:
        models = requests.get(f"{cfg.handy_base_url()}/models/status", timeout=5).json()
    except (requests.RequestException, ValueError):
        models = None
    return {"state": "running", "mode": mode, "models": models}


def _brain_status(cfg) -> dict:
    """The brains need no process of their own: they speak to TaDaMoo's server. The state says which one answers by default."""
    return {"state": "running", "mode": cfg.brain_mode, "persona": cfg.brain_persona or None}


def _avatar_status(avatar, proc_alive: bool) -> dict:
    """Stato del worker MuseTalk per la card "Avatar" della GUI.

    Stessa semantica di GET /avatar/state, per non avere due verita' diverse
    nella stessa interfaccia: "running" vuol dire PRONTO (processo vivo, una
    persona attiva e nessuna preparazione in corso), non solo "processo vivo" —
    il worker resta su per decine di secondi mentre carica i modelli.
    Riporta anche `preparing` ed `error`: senza questi la card diceva solo
    "stopped" e un avvio fallito (tipicamente OOM CUDA, perche' un LLM tiene
    occupata la VRAM) era invisibile fuori dai log.
    """
    preparing = getattr(avatar, "preparing", None) if avatar is not None else None
    error = getattr(avatar, "last_error", None) if avatar is not None else None
    active = getattr(avatar, "_active_persona", None) if proc_alive else None
    ready = bool(proc_alive and active and not preparing)
    if ready:
        state = "running"
    elif preparing or proc_alive:
        state = "loading"
    elif error:
        state = "error"
    else:
        state = "stopped"
    return {
        "state": state,
        "persona": active if ready else None,
        "preparing": preparing,
        "progress": getattr(avatar, "progress", None) if avatar is not None else None,
        "detail": error if state in ("error", "stopped") else None,
    }


def _sospeso_se_fermo(stato: dict, sospeso: bool) -> dict:
    """Un componente FERMO mentre i componenti sono sospesi e' "suspended", non
    "stopped": la GUI deve dire che e' fermo per scelta e si riavvia con Riprendi.
    Errori, caricamenti e stati attivi restano quelli reali."""
    if sospeso and stato.get("state") == "stopped":
        return {**stato, "state": "suspended"}
    return stato


def _ptt_status(cfg, ptt, sospeso: bool) -> dict:
    """Hotkey push-to-talk (sessioni, avatar, chat avatar, copilota)."""
    if ptt is None or not cfg.push_to_talk_enabled:
        return {"state": "disabled"}
    if ptt.hotkeys_attive:
        return {"state": "running"}
    return {"state": "suspended" if sospeso else "stopped"}


def _chiamate_status(sospeso: bool) -> dict:
    """Watcher delle chiamate in arrivo: fermo (non risponde) da sospeso."""
    bridge = current_app.config.get("CB_BRIDGE")
    if bridge is None:
        return {"state": "disabled"}
    if sospeso or getattr(bridge, "watcher_sospeso", False):
        return {"state": "suspended"}
    return {"state": "running"}


@bp.get("/components/status")
def components_status():
    cfg = _cfg()
    sospeso = _in_sospensione()
    ptt = current_app.config.get("CB_PTT")
    avatar = current_app.config.get("CB_AVATAR")
    avatar_running = bool(avatar is not None and avatar._proc is not None
                          and avatar._proc.poll() is None)
    session = ptt.session_state() if ptt is not None else None
    return jsonify({
        "main": {"state": "running", "pid": os.getpid(),
                 "stopping": STOP_SENTINEL.exists(), "session": session,
                 "suspended": sospeso, "memory": _memoria_main()},
        "gateway": {
            "state": "running" if (cfg.gateway_enabled and
                                   _tcp_open("127.0.0.1", cfg.gateway_port))
            else ("stopped" if cfg.gateway_enabled else "disabled"),
            "port": cfg.gateway_port,
        },
        "voice_engine": _sospeso_se_fermo(_voice_engine_status(cfg), sospeso),
        "brain": _sospeso_se_fermo(_brain_status(cfg), sospeso),
        "avatar_worker": _sospeso_se_fermo(_avatar_status(avatar, avatar_running),
                                           sospeso),
        "push_to_talk": _ptt_status(cfg, ptt, sospeso),
        "incoming_calls": _chiamate_status(sospeso),
        "dictation": _dictation_status(),
    })


def _dictation_status() -> dict:
    from .dictation import current
    mgr = current()
    if mgr is None:
        return {"state": "disabled", "detail": "dettatura non avviata in questo processo"}
    return mgr.status()


def _cleanup_children(app) -> None:
    """Chiude best-effort i processi figli (worker avatar) prima di
    os._exit: os._exit salta finally/atexit, quindi senza questo i figli
    restano orfani (RAM / VRAM occupata) e ad ogni restart dalla GUI se ne
    accumula uno nuovo (il warm-up ne rispawna un altro)."""
    avatar = app.config.get("CB_AVATAR")
    if avatar is not None and hasattr(avatar, "stop"):
        try:
            avatar.stop()
        except Exception:  # noqa: BLE001
            log.warning("cleanup: stop worker avatar fallito", exc_info=True)
    from .dictation import current
    dettatura = current()
    if dettatura is not None:
        try:
            dettatura.stop()      # toglie la hotkey
        except Exception:  # noqa: BLE001
            log.warning("cleanup: stop dettatura fallito", exc_info=True)
    # Il bridge sta uscendo (os._exit salta i finally): senza questo il processo
    # STT unico resterebbe orfano con il modello in VRAM.
    try:
        from .engines import chiudi_stt
        chiudi_stt()
    except Exception:  # noqa: BLE001
        log.warning("cleanup: chiusura del processo STT fallita", exc_info=True)


def _exit_soon(code: int, cleanup=None) -> None:
    # Rispondi al client, POI esci. os._exit salta i finally/atexit, quindi il
    # cleanup dei figli (se richiesto) va fatto esplicitamente qui.
    def _do() -> None:
        if cleanup is not None:
            try:
                cleanup()
            except Exception:  # noqa: BLE001
                pass
        os._exit(code)

    threading.Timer(0.5, _do).start()


def _in_sospensione() -> bool:
    try:
        from .engines import in_sospensione
        return in_sospensione()
    except Exception:  # noqa: BLE001
        return False


def _memoria_main() -> dict:
    from .memoria import memoria_processo
    return memoria_processo()


def _vram_usata_mb() -> int | None:
    """VRAM usata sulla/e GPU (somma), letta da nvidia-smi. Solo lettura."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return None
        return sum(int(x) for x in r.stdout.split() if x.strip().isdigit())
    except Exception:  # noqa: BLE001 - nessuna GPU NVIDIA / nvidia-smi assente
        return None


def _commit_processo_mb(proc) -> int | None:
    """Commit (Private Bytes) di un Popen figlio, in MiB, prima che muoia."""
    try:
        import psutil
        info = psutil.Process(proc.pid).memory_info()
        return int(getattr(info, "private", info.vms) / 2**20)
    except Exception:  # noqa: BLE001
        return None


@bp.post("/components/suspend")
def suspend_all():
    """Libera la memoria SENZA spegnere il sistema.

    Serve per fare spazio ad altro (VRAM e RAM): si scarica tutto ciò che pesa e
    si lascia in piedi solo quanto basta a riprendere da soli.

    Va giù: worker avatar (MuseTalk), motori TTS (i loro processi escono: è
    l'unico modo di restituire anche il contesto CUDA), sessione
    voce, modelli di diarizzazione.
    Resta su: questo bridge (quindi la GUI, le hotkey e il push-to-talk), lo
    **STT**, che deve continuare a trascrivere, la **dettatura**, che resta
    sempre attiva (usa lo stesso STT) e il modello LLM in LM Studio, che non
    è roba nostra da scaricare.

    Il main NON esce: il supervisore `run-ptt.ps1` (che rilancia `main.py all`
    a ogni uscita) non c'entra e non può annullare la sospensione. La
    sospensione è uno stato SU DISCO (`call-bridge/sospeso.on`, vedi
    `engines.SOSPENSIONE_FILE`): sopravvive al riavvio del bridge e di Windows, e
    finché c'è né il keep-alive, né il warm-up all'avvio, né il preload
    dell'avatar caricano TTS o avatar (lo STT resta caldo). Finisce con
    /components/resume (o con un uso voluto, vedi
    `handy_client.RIPRENDE_CON_USO`).

    Risponde con ciò che è stato liberato e con le misure (VRAM e commit del
    main prima/dopo, commit dei processi chiusi).
    """
    app = current_app._get_current_object()
    cfg = _cfg()
    fatto: list[str] = []
    errori: dict[str, str] = {}
    chiusi_mb: dict[str, int | None] = {}

    from .engines import sospendi
    from .memoria import rilascia
    try:
        sospendi()      # stato SU DISCO (call-bridge/sospeso.on): vale anche dopo i riavvii
    except OSError as e:
        errori["stato"] = f"sospensione non salvata su disco: {e}"
    vram_prima = _vram_usata_mb()
    main_prima = _memoria_main()

    ptt = app.config.get("CB_PTT")
    if ptt is not None:
        try:
            if ptt.stop_session():
                fatto.append("sessione voce")
        except Exception as e:  # noqa: BLE001 - si prosegue col resto
            errori["sessione"] = str(e)
        try:
            # Hotkey push-to-talk (anche avatar, chat avatar, copilota) e copilota.
            if ptt.sospendi():
                fatto.append("hotkey push-to-talk")
        except Exception as e:  # noqa: BLE001
            errori["push-to-talk"] = str(e)

    avatar = app.config.get("CB_AVATAR")
    proc_avatar = getattr(avatar, "_proc", None) if avatar is not None else None
    if proc_avatar is not None and proc_avatar.poll() is None:   # era DAVVERO acceso
        try:
            chiusi_mb["worker avatar"] = _commit_processo_mb(proc_avatar)
            avatar.stop()
            fatto.append("worker avatar")
        except Exception as e:  # noqa: BLE001
            errori["avatar"] = str(e)

    # Watcher delle chiamate in arrivo: si ferma da solo (guarda la sentinella);
    # si elenca solo se stava vigilando.
    bridge = app.config.get("CB_BRIDGE")
    if bridge is not None and not getattr(bridge, "watcher_sospeso", False):
        fatto.append("watcher chiamate in arrivo")

    # REGOLA: Sospendi tocca solo cio' che ESISTE gia'. Niente istanze create e
    # niente import pesanti qui (un Sospendi da gia' sospeso faceva salire il commit
    # del bridge di 0,85 GiB: creava il router TTS e importava torch).
    #
    # Server dei motori voce (:8720): serve a sessioni, gateway, telefonate e skill
    # esterne, NON alla dettatura (che usa direttamente lo STT unico). Se il modulo
    # non e' mai stato importato il server non e' mai partito.
    try:
        compat = sys.modules.get(__package__ + ".handy_compat")
        if compat is not None and compat.ferma_compat():
            fatto.append("server motori voce (:8720)")
    except Exception as e:  # noqa: BLE001
        errori["server-voce"] = str(e)

    try:
        from . import engines
        tts = engines._tts_engine            # solo se esiste gia'
        if tts is not None:
            for eid, eng in list(getattr(tts, "_venv_engines", {}).items()):
                proc = getattr(eng, "_proc", None)
                if proc is not None and proc.poll() is None:
                    chiusi_mb[f"TTS {eid}"] = _commit_processo_mb(proc)
            scaricati = tts.scarica_tutti()
            if scaricati:
                fatto.append("motori TTS: " + ", ".join(scaricati))
                engines._release_gpu_memory()
    except Exception as e:  # noqa: BLE001
        errori["tts"] = str(e)

    try:
        diarize = sys.modules.get(__package__ + ".engines.diarize")
        if (diarize is not None and diarize._engine is not None
                and diarize._engine.scarica()):
            fatto.append("diarizzazione")
    except Exception as e:  # noqa: BLE001
        errori["diarizzazione"] = str(e)

    # I residui nel main (CPU e cache CUDA, se mai inizializzata) tornano al
    # sistema ora, non al prossimo giro dell'allocatore.
    rilascia()
    main_dopo = _memoria_main()
    vram_dopo = _vram_usata_mb()

    log.warning("Sospensione richiesta dalla GUI — liberati: %s; main %s -> %s GiB, "
                "VRAM %s -> %s MB",
                "; ".join(fatto) or "niente era in memoria",
                main_prima.get("commit_gib"), main_dopo.get("commit_gib"),
                vram_prima, vram_dopo)
    return jsonify({
        "status": "ok", "released": fatto, "errors": errori,
        "kept": ["STT", "dettatura", "LLM (LM Studio)",
                 "bridge (hotkey dettatura, processo STT, API di controllo)"],
        "measures": {
            "vram_mb": {"before": vram_prima, "after": vram_dopo},
            "main_commit_gib": {"before": main_prima.get("commit_gib"),
                                "after": main_dopo.get("commit_gib")},
            "closed_commit_mb": chiusi_mb,
        },
    })


@bp.post("/components/stt/reload")
def stt_reload():
    """Ricarica SOLO lo STT: rifa il processo e rimette il modello in VRAM.
    Funziona anche da sospeso (e' quello che fa «Load models» in quel caso): gli
    altri componenti restano come sono. Bloccante fino a modello pronto."""
    from .engines import _get_stt_engine
    try:
        info = _get_stt_engine(_cfg()).ricarica_forzata("richiesta dalla GUI")
    except Exception as e:  # noqa: BLE001
        log.error("Ricarica STT fallita: %s", e)
        return jsonify({"error": f"ricarica dello STT fallita: {e}"}), 500
    return jsonify({"status": "ok", "stt": info, "suspended": _in_sospensione()})


@bp.post("/components/resume")
def resume_all():
    """Fine della sospensione (toglie il file di stato): rimette tutto come
    all'avvio, subito e in background — motori voce (voce dell'avatar attivo /
    STT) e, se `avatar_preload` è acceso, l'avatar."""
    from .engines import riprendi
    riprendi()
    cfg = _cfg()
    avatar = current_app.config.get("CB_AVATAR")
    ptt = current_app.config.get("CB_PTT")
    if ptt is not None:
        try:
            ptt.riprendi()       # ri-registra le hotkey push-to-talk
        except Exception:  # noqa: BLE001
            log.warning("Riprendi: hotkey push-to-talk non riattivate", exc_info=True)

    def _scalda() -> None:
        if cfg.handy_compat_enabled:
            try:
                from .handy_compat import avvia_compat
                avvia_compat(cfg)         # server motori voce :8720
            except Exception:  # noqa: BLE001
                log.warning("Riprendi: server motori voce non riavviato", exc_info=True)
        try:
            from .handy_client import ensure_models_loaded
            ensure_models_loaded(cfg, stt_model=cfg.default_stt_model, tts=True)
        except Exception:  # noqa: BLE001 - best-effort: si ricarica comunque al primo uso
            log.warning("Riprendi: riscaldamento motori fallito", exc_info=True)
        # Come in main.py all'avvio: avatar caldo ma finestra nascosta.
        if (cfg.avatar_preload and avatar is not None and cfg.avatar_enabled
                and cfg.avatar_persona):
            try:
                if avatar.start(cfg.avatar_persona):
                    log.info("Riprendi: avatar '%s' di nuovo caldo", cfg.avatar_persona)
                else:
                    log.warning("Riprendi: avatar '%s' non partito (vedi log)",
                                cfg.avatar_persona)
            except Exception:  # noqa: BLE001
                log.warning("Riprendi: avatar non ripreso", exc_info=True)

    threading.Thread(target=_scalda, name="resume-warmup", daemon=True).start()
    log.warning("Sospensione terminata dalla GUI: motori in riscaldamento")
    return jsonify({"status": "ok"})


@bp.post("/components/main/restart")
def main_restart():
    app = current_app._get_current_object()
    STOP_SENTINEL.unlink(missing_ok=True)
    log.warning("Restart del main richiesto dalla GUI: exit(%d) tra 0.5s",
                RESTART_EXIT_CODE)
    _exit_soon(RESTART_EXIT_CODE, cleanup=lambda: _cleanup_children(app))
    return jsonify({"status": "restarting"})


@bp.post("/components/main/stop")
def main_stop():
    app = current_app._get_current_object()
    STOP_SENTINEL.write_text("stop richiesto dalla GUI\n", encoding="utf-8")
    log.warning("Stop del main richiesto dalla GUI: sentinella %s + exit",
                STOP_SENTINEL.name)
    _exit_soon(0, cleanup=lambda: _cleanup_children(app))
    return jsonify({"status": "stopping"})


def _run_detached(cmd: list[str]) -> None:
    subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.DETACHED_PROCESS)


@bp.post("/components/system/restart")
def system_restart():
    """Cold restart COMPLETO: lancia `restart-all.ps1` staccato e risponde subito.

    Lo script ferma le istanze vLLM (libera la VRAM), scarica i modelli di LM
    Studio, chiude worker/agente/bridge/GUI, rimuove le sentinelle e riavvia i
    servizi; bridge e Control Center li rilanciano i loro supervisori. Il
    processo che serve QUESTA richiesta e' tra quelli che verranno uccisi: per
    questo si risponde prima, e la GUI Tauri preferisce il proprio comando
    locale (funziona anche a bridge gia' spento).
    """
    script = Path(__file__).resolve().parents[1] / "restart-all.ps1"
    if not script.is_file():
        return jsonify({"error": f"script non trovato: {script}"}), 500
    try:
        _run_detached(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-WindowStyle", "Hidden", "-File", str(script)])
    except OSError as e:
        return jsonify({"error": f"avvio restart fallito: {e}"}), 500
    log.warning("Restart completo richiesto dalla GUI: %s", script)
    return jsonify({"status": "ok", "note": "restart completo avviato (~1-2 min)"})


@bp.post("/components/gateway/restart")
def gateway_restart():
    # run_gateway non ha uno stop-event: il riavvio del solo gateway non e'
    # possibile senza riavviare il processo. La GUI propone il restart del main.
    return jsonify({"error": "restart_main_instead",
                    "message": "il gateway vive nel processo main: "
                               "riavvia il componente main"}), 409
