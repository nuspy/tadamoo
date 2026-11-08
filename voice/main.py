"""Entrypoint del call-bridge — agente telefonico AI (host Windows).

Comandi:
  python main.py devices        elenca i device audio HFP rilevati
  python main.py ping           verifica la connessione con l'app Android
  python main.py call <numero>  chiama un numero e avvia la conversazione
  python main.py answer         attende una chiamata in arrivo e risponde
  python main.py serve          avvia il server di controllo (API per Hermes/skill)
  python main.py ptt            push-to-talk (hotkey) + voice gateway per l'app Android
  python main.py gateway        avvia solo il voice gateway (WebSocket per l'app Android)
  python main.py all            ISTANZA UNICA: control server + gateway + push-to-talk

STT/TTS sono i client WebSocket reali verso Handy. Hermes e' reale
(`HermesClient`, HTTP/SSE verso `hermes_base_url`) se `config.hermes_mode ==
"http"`, altrimenti uno stub a eco locale (`callbridge/stubs.py`).
"""

from __future__ import annotations

import argparse
import logging
import sys

# NB: NON precaricare qui le cuDNN dei wheel NVIDIA (per l'EP CUDA di
# onnxruntime). torch e onnxruntime-gpu vogliono cuDNN DIVERSE e Windows tiene
# una sola `cudnn64_9.dll` per processo: chi carica per primo vince e l'altro
# muore con "WinError 127 - procedura non trovata". Provato: anticipando
# onnxruntime, Whisper finisce su CPU (molto peggio del TTS su CPU).
# Priorità a torch => STT su CUDA; Kokoro resta su CPU (usare int8, più veloce
# lì di fp16). Per avere entrambi su GPU serve isolare Kokoro in un subprocess,
# come già fa Chatterbox.

from callbridge.config import Config  # noqa: E402
from callbridge.orchestrator import CallBridge  # noqa: E402
from callbridge.phone_client import PhoneControlClient  # noqa: E402
from callbridge.stubs import StubHermes  # noqa: E402


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-24s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _cmd_devices() -> None:
    import sounddevice as sd

    from callbridge.audio_hfp import find_hfp_devices

    in_idx, out_idx = find_hfp_devices()
    print(f"downlink (input)  [{in_idx}] {sd.query_devices(in_idx)['name']}")
    print(f"uplink   (output) [{out_idx}] {sd.query_devices(out_idx)['name']}")


def _cmd_ping(cfg: Config) -> None:
    phone = PhoneControlClient(cfg)
    if phone.health():
        print(f"OK — telefono raggiungibile su {cfg.base_url()}")
        print("status:", phone.status())
    else:
        print(f"FALLITO — nessuna risposta da {cfg.base_url()}")
        print("Verifica IP/porta/token nel config e che l'app Android sia avviata.")
        sys.exit(1)


def _cmd_call(cfg: Config, number: str) -> None:
    bridge = CallBridge(cfg, StubHermes())
    driver = bridge.start_outgoing_call(number)
    try:
        driver.join()
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        bridge.hangup()
        driver.join(timeout=3.0)


def _cmd_answer(cfg: Config) -> None:
    bridge = CallBridge(cfg, StubHermes())
    driver = bridge.start_incoming_answer()
    try:
        driver.join()
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        bridge.hangup()
        driver.join(timeout=3.0)


def _cmd_serve(cfg: Config) -> None:
    from callbridge.control_server import run_server

    run_server(cfg)


def _build_brains(cfg: Config, log: logging.Logger):
    """Crea (brain_full, brain_limited, brain_copilot) per il push-to-talk.

    - COMPLETO (F1/F2): agente Hermes via ACP (tool/azioni) se abilitato,
      altrimenti il modello scelto da hermes_mode.
    - LIMITATO (F3, simulazione telefonate): dopo il cutover nativo il phone
      server HTTP (:8780) non gira piu'; con l'agente ACP abilitato F3 riusa la
      STESSA istanza del full brain (un solo processo hermes, non due),
      altrimenti Hermes HTTP / stub.
    - COPILOTA (Shift+F4): istanza dedicata (l'ACP parte lazy alla 1a
      attivazione) per non mischiare il contesto col PTT.
    """
    from callbridge.brains import (make_full_brain, make_limited_brain,
                                    make_lmstudio_brain)

    brain_full = make_full_brain(cfg, label="ptt")
    # Con ACP abilitato, condividi l'unico agente col full brain: crearne uno
    # nuovo via make_limited_brain(ACP) spawnerebbe un secondo processo hermes.
    brain_limited = brain_full if cfg.acp_enabled else make_limited_brain(cfg)
    brain_copilot = None
    if cfg.copilot_enabled:
        if getattr(cfg, "copilot_force_external", False):
            # "Forza LLM esterna senza RAG": si rinuncia al sapere locale delle
            # persone (che resta in casa) e agli strumenti dell'agente, in
            # cambio della conoscenza generale di un modello grande.
            from callbridge.lmstudio_client import LmStudioClient
            brain_copilot = LmStudioClient(cfg, consumer="copilot", use_rag=False)
            log.info("Copilota: LLM esterna forzata (provider 'copilot', RAG spento)")
        else:
            brain_copilot = (make_full_brain(cfg, label="copilota")
                             if cfg.copilot_use_acp else make_limited_brain(cfg))
    # LM Studio diretto (Ctrl+Shift+F4/F5): istanza dedicata, non agentica.
    brain_lmstudio = make_lmstudio_brain(cfg) if cfg.avatar_chat_enabled else None
    return brain_full, brain_limited, brain_copilot, brain_lmstudio


def _start_gateway_thread(cfg: Config, log: logging.Logger,
                          critical: bool = False) -> None:
    """Avvia il voice gateway (app Android) in un thread daemon, se abilitato.

    `critical=True` (servizio headless, dove il gateway E' la funzione primaria):
    se il thread muore il processo esce col codice di restart, cosi' NSSM lo
    rilancia pulito invece di restare vivo ma monco (senza gateway). In
    `main.py all` il gateway e' secondario rispetto al PTT (main thread): li'
    `critical=False`, si logga soltanto per non abbattere il PTT funzionante."""
    if not cfg.gateway_enabled:
        return
    import os
    import threading

    from callbridge.voice_gateway import run_gateway

    # Codice di restart condiviso col comando GUI di restart (api_components).
    RESTART_EXIT_CODE = 42

    def _run() -> None:
        try:
            run_gateway(cfg)
        except Exception as e:  # noqa: BLE001 - non far cadere il processo
            log.error("Voice gateway terminato con errore: %s", e)
        else:
            log.error("Voice gateway terminato inaspettatamente")
        if critical:
            log.error("Gateway critico giu': esco con %d per il restart del servizio",
                      RESTART_EXIT_CODE)
            os._exit(RESTART_EXIT_CODE)

    threading.Thread(target=_run, name="voice-gateway", daemon=True).start()
    log.info("Voice gateway avviato (ws://%s:%d)", cfg.gateway_host, cfg.gateway_port)


def _build_ptt(cfg: Config, log: logging.Logger):
    """Costruisce overlay + copilota + manager PTT (senza avviare nulla).

    Separato da `_run_ptt_mainloop` cosi' `_cmd_all` puo' passare manager e
    copilota al control server (endpoint /session/* e /copilot/* della GUI)
    PRIMA di bloccarsi sul mainloop tkinter.
    """
    from callbridge.ptt import PushToTalkManager
    from callbridge.ptt_overlay import BrainOverlay

    brain_full, brain_limited, brain_copilot, brain_lmstudio = _build_brains(cfg, log)
    # NIENTE warm-up dell'agente ACP all'avvio: non si carica alcun modello LLM
    # finche' non serve. L'agente si avvia alla prima richiesta (F1/telefonata/
    # avatar) via `respond` -> `_ensure_started`; il primo turno paga l'avvio.
    # L'overlay (tkinter) DEVE girare sul thread principale: gli prendiamo il
    # mainloop. Hotkey/sessioni girano su thread separati e aggiornano lo stato.
    overlay = BrainOverlay()
    # AvatarClient CONDIVISO: uno solo per processo (vincolo VRAM: un worker
    # MuseTalk per volta). Lo usano PTT-avatar (Ctrl+Shift+F1/F2), copilota
    # (Shift+F4) e gli endpoint /avatar//personas della GUI.
    avatar = None
    if cfg.avatar_enabled:
        from callbridge.avatar_client import AvatarClient
        avatar = AvatarClient(cfg)
    copilot = None
    if brain_copilot is not None:
        from callbridge.copilot import CopilotController
        copilot = CopilotController(cfg, brain_copilot, overlay, avatar=avatar)
    manager = PushToTalkManager(cfg, brain_full, brain_limited,
                                overlay=overlay, copilot=copilot, avatar=avatar,
                                brain_lmstudio=brain_lmstudio)
    return overlay, manager, copilot, avatar


def _run_ptt_mainloop(cfg: Config, log: logging.Logger, prebuilt=None) -> None:
    """PTT ricco (overlay + F1/F2/F3) sul thread principale (bloccante)."""
    overlay, manager, copilot, _avatar = prebuilt or _build_ptt(cfg, log)
    manager.start()
    print(f"Push-to-talk: {cfg.push_to_talk_hotkey}=agente completo (casse), "
          f"{cfg.push_to_talk_hotkey_fullduplex}=completo (cuffie), "
          f"{cfg.push_to_talk_hotkey_limited}=limitato"
          + (f", {cfg.push_to_talk_hotkey_copilot}=copilota" if copilot else "")
          + ". Ctrl+C per uscire.")
    try:
        overlay.mainloop()  # bloccante (thread principale)
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
    finally:
        # Anche a mainloop terminato normalmente (root Tk distrutto) le hotkey
        # globali vanno de-registrate e la sessione chiusa.
        manager.stop()


def _cmd_ptt(cfg: Config) -> None:
    """Solo push-to-talk + overlay (sessione utente).

    Architettura "split": control server, voice gateway e media server girano
    nel servizio headless (`server`); qui resta SOLO cio' che richiede la
    sessione interattiva (hotkey, mic/casse, overlay, copilota). Niente gateway
    qui, altrimenti farebbe doppio bind sulla :gateway_port col servizio.
    """
    log = logging.getLogger("callbridge")
    _run_ptt_mainloop(cfg, log)


def _cmd_all(cfg: Config, no_gateway: bool = False) -> None:
    """Istanza unica: control server (chiamate) + voice gateway (app) + PTT.

    Tutto nello stesso processo, in sessione utente (il PTT richiede hotkey,
    audio e overlay). Il control server gira in un thread (Flask), con il suo
    PTT interno disattivato (`start_ptt=False`) per non duplicare le hotkey;
    il gateway in un altro thread; il PTT ricco prende il thread principale.

    `no_gateway=True` (architettura split): NON avvia il voice gateway qui —
    gira nel servizio headless `callAPIcall-orchestrator` (main.py gateway).
    Restano control server (chiamate, con audio HFP che richiede la sessione
    utente) + PTT/overlay + controllo PTT dalla GUI.
    """
    import threading

    from callbridge.control_server import run_server
    from callbridge.engines import in_sospensione

    log = logging.getLogger("callbridge")

    # Il PTT viene costruito PRIMA del control server: cosi' gli endpoint
    # /session/* e /copilot/* della GUI operano sulla stessa istanza delle
    # hotkey (overlay incluso), non su un duplicato.
    prebuilt = _build_ptt(cfg, log)
    overlay, manager, copilot, avatar = prebuilt

    def _serve() -> None:
        try:
            run_server(cfg, start_ptt=False, ptt=manager, copilot=copilot,
                       avatar=avatar)
        except Exception as e:  # noqa: BLE001 - non far cadere il processo
            log.error("Control server terminato con errore: %s", e)

    threading.Thread(target=_serve, name="control-server", daemon=True).start()
    log.info("Control server avviato (http://%s:%d)", cfg.control_host, cfg.control_port)

    # Keep-alive modelli Handy: precarica all'avvio e ripinga periodicamente, cosi'
    # restano SEMPRE in VRAM (niente unload da inattivita' a 900s -> niente ricarica
    # lenta quando parte la conversazione col PTT).
    def _models_keepalive() -> None:
        import time

        from callbridge.handy_client import ensure_models_loaded
        while True:
            ensure_models_loaded(cfg, stt_model=cfg.default_stt_model, tts=True,
                                 keepalive=True)
            time.sleep(600)  # < idle_unload (900s)

    threading.Thread(target=_models_keepalive, name="models-keepalive", daemon=True).start()
    log.info("Keep-alive modelli avviato (STT=%s, TTS=%s; precarico + ping 600s)",
             cfg.stt_backend, cfg.tts_backend)

    # Preload avatar: carica i modelli MuseTalk in VRAM e prepara la persona
    # (cache) in background, cosi' la prima Ctrl+Shift+F1 e' immediata (il worker
    # resta poi caldo tra le sessioni). Il minuto di "reload" e' proprio questo
    # caricamento modelli in VRAM, che non e' serializzabile; qui lo si paga una
    # volta all'avvio, fuori dal percorso critico.
    if in_sospensione() and cfg.avatar_preload:
        log.info("Avatar: preload NON eseguito, i componenti sono sospesi "
                 "(si toglie con Riprendi)")
    if (cfg.avatar_preload and avatar is not None and cfg.avatar_enabled
            and cfg.avatar_persona and not in_sospensione()):
        def _avatar_preload() -> None:
            persona = cfg.avatar_persona
            log.info("Avatar: preload di '%s' in background...", persona)
            try:
                if avatar.start(persona):
                    avatar.set_preview(False)  # caldo ma finestra nascosta
                    log.info("Avatar: '%s' precaricato e caldo", persona)
                else:
                    log.warning("Avatar: preload di '%s' fallito (vedi log)", persona)
            except Exception as e:  # noqa: BLE001 - preload best-effort
                log.warning("Avatar: preload fallito: %s", e)
        threading.Thread(target=_avatar_preload, name="avatar-preload",
                         daemon=True).start()

    # Server Handy-compatibile (nativo) su :8720 per i consumatori esterni che
    # parlavano con Handy (es. la skill Hermes kokoro_tts.py). Solo se abilitato
    # e con Handy spento (stessa porta).
    if cfg.handy_compat_enabled:
        if in_sospensione():
            log.info("Handy-compat NON avviato: i componenti sono sospesi "
                     "(parte con Riprendi)")
        else:
            from callbridge.handy_compat import avvia_compat
            avvia_compat(cfg)
            log.info("Handy-compat abilitato su :%d (Handy deve essere spento)",
                     cfg.handy_compat_port)

    # Dettatura globale (Alt+Spazio): STT nativo -> incolla nel testo attivo.
    # Sostituisce la dettatura di Handy. Tenuto vivo dal mainloop PTT sottostante.
    from callbridge.dictation import DictationManager
    _dictation = DictationManager(cfg, overlay=overlay)
    _dictation.start()
    # L'STT e' sempre attivo e caldo: ogni 30 s si controlla che il processo sia
    # vivo e il modello ancora in VRAM (anche da sospeso). Vedi stt_shared.controlla.
    from callbridge.engines import avvia_controllo_stt
    avvia_controllo_stt(cfg)

    # Cronologia appunti (Ctrl+Alt+frecce): componente fisso, senza parametri.
    from callbridge.clipboard_history import ClipboardHistory
    try:
        ClipboardHistory(overlay=overlay).start()
    except Exception as e:  # noqa: BLE001 - accessorio: non deve fermare il bridge
        log.error("Cronologia appunti non avviata: %s", e)

    if not no_gateway:
        _start_gateway_thread(cfg, log)
    else:
        log.info("Voice gateway NON avviato qui (gira nel servizio headless)")
    _run_ptt_mainloop(cfg, log, prebuilt=prebuilt)


def _cmd_server(cfg: Config) -> None:
    """Server HEADLESS per il servizio (Session 0): control server + voice
    gateway + media server, SENZA push-to-talk/overlay.

    Le parti che NON richiedono la sessione utente (orchestrazione chiamate,
    app Android, streaming media) -> resilienti come servizio NSSM. Il PTT
    locale (hotkey/audio/overlay/copilota) gira a parte nel task utente (`ptt`).

    NB: gli endpoint GUI /session/* e /copilot/* che pilotano la sessione PTT
    dal vivo NON operano qui (il manager PTT vive nell'altro processo); le
    hotkey Shift+F1/F2/F4 restano pienamente funzionanti.
    """
    import threading

    from callbridge.control_server import run_server

    log = logging.getLogger("callbridge")

    # Voice gateway (app Android) + media server, in thread daemon. Qui e' la
    # funzione primaria del servizio: se muore, esci per far rilanciare NSSM.
    _start_gateway_thread(cfg, log, critical=True)

    # Keep-alive modelli Handy: restano caldi in VRAM (gateway e PTT li usano).
    def _models_keepalive() -> None:
        import time

        from callbridge.handy_client import ensure_models_loaded
        while True:
            ensure_models_loaded(cfg, stt_model=cfg.default_stt_model, tts=True,
                                 keepalive=True)
            time.sleep(600)

    threading.Thread(target=_models_keepalive, name="models-keepalive", daemon=True).start()
    log.info("Server headless avviato: control + gateway + media (no PTT)")

    # Control server sul thread principale (bloccante); PTT interno disattivato.
    run_server(cfg, start_ptt=False)


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Call-bridge — agente telefonico AI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="elenca i device audio HFP rilevati")
    sub.add_parser("ping", help="verifica la connessione con l'app Android")
    p_call = sub.add_parser("call", help="chiama un numero e avvia la conversazione")
    p_call.add_argument("number", help="numero da chiamare, es. +39333xxxxxxx")
    sub.add_parser("answer", help="attende una chiamata in arrivo e risponde")
    sub.add_parser("serve", help="avvia il solo control server dell'orchestrator")
    sub.add_parser("server", help="servizio headless: control + gateway + media (senza PTT)")
    sub.add_parser("ptt", help="avvia solo il push-to-talk/overlay (sessione utente)")
    sub.add_parser("gateway", help="avvia il voice gateway per l'app Android (WebSocket)")
    p_all = sub.add_parser("all", help="istanza unica: control server + voice gateway + push-to-talk")
    p_all.add_argument("--no-gateway", action="store_true",
                       help="non avviare il voice gateway (gira nel servizio headless)")
    args = parser.parse_args()

    cfg = Config.load()

    if args.cmd == "devices":
        _cmd_devices()
    elif args.cmd == "ping":
        _cmd_ping(cfg)
    elif args.cmd == "call":
        _cmd_call(cfg, args.number)
    elif args.cmd == "answer":
        _cmd_answer(cfg)
    elif args.cmd == "serve":
        _cmd_serve(cfg)
    elif args.cmd == "server":
        _cmd_server(cfg)
    elif args.cmd == "ptt":
        _cmd_ptt(cfg)
    elif args.cmd == "gateway":
        from callbridge.voice_gateway import run_gateway
        run_gateway(cfg)
    elif args.cmd == "all":
        _cmd_all(cfg, no_gateway=args.no_gateway)


if __name__ == "__main__":
    main()
