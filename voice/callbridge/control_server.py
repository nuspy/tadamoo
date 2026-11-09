"""Server HTTP di controllo dell'orchestrator.

L'orchestrator e' **server** per:
  - i comandi che partono dal server di TaDaMoo (e dall'agente)
  - le chiamate in uscita

Endpoint:
  GET  /health             - liveness (senza auth)
  GET  /status             - stato orchestrator + telefono + modelli Handy
  POST /prepare            - carica i modelli in vista di una chiamata
  POST /call               - chiamata in uscita {number, voice?, lang?, stt_model?}
  POST /hangup             - termina la chiamata corrente

Auth: header `Authorization: Bearer <control_auth_token>` (tranne /health).
"""

from __future__ import annotations

import logging
import threading
import time

from flask import Flask, jsonify, request

from .brains import make_brain
from .config import Config
from .handy_client import HandyError
from .orchestrator import CallBridge
from .ptt import PushToTalkManager
from .util import is_valid_phone_number, normalize_phone_number

log = logging.getLogger("callbridge.control")


def create_app(
    config: Config,
    bridge: CallBridge | None = None,
    ptt: PushToTalkManager | None = None,
    copilot=None,
    avatar=None,
    status_fn=None,
) -> Flask:
    """Costruisce l'app Flask del server di controllo.

    `status_fn`, se passato, e' il corpo di GET /status al posto di `bridge.status()`
    (il servizio vocale riporta i modelli scelti e installati, senza toccare telefono e Handy).

    `bridge` puo' essere passato dall'esterno; se assente ne viene creato uno
    con il cervello di processo (`brains.make_brain`).

    `ptt` e `copilot` sono il PushToTalkManager e il CopilotController
    dell'istanza unica (`main.py all`): servono agli endpoint /session/* e
    /copilot/* (GUI). Se assenti quegli endpoint rispondono 503.

    `avatar` e' l'AvatarClient CONDIVISO (uno solo per processo, vincolo VRAM):
    se passato, gli endpoint /avatar/* e /personas/* riusano questa istanza
    invece di crearne una lazy propria (evita un secondo worker MuseTalk).
    """
    app = Flask(__name__)
    # Limite di dimensione degli upload (clip persona mp4, media analisi): senza,
    # un body enorme puo' esaurire memoria/disco. 2 GiB copre i video lunghi.
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3
    bridge = bridge or CallBridge(config, make_brain(config))
    # Riferimenti condivisi per i blueprint (sessioni, copilota, avatar, diag).
    app.config["CB_CONFIG"] = config
    app.config["CB_BRIDGE"] = bridge
    app.config["CB_PTT"] = ptt
    app.config["CB_COPILOT"] = copilot
    if avatar is not None:
        app.config["CB_AVATAR"] = avatar

    # Blueprint della GUI (Control Center): sessioni voce, persone/avatar,
    # configurazione, stato/controllo componenti, diagnostica per i test guidati.
    from .api_analysis import bp as analysis_bp
    from .api_components import bp as components_bp
    from .api_config import bp as config_bp
    from .api_diag import bp as diag_bp
    from .api_external import bp as external_bp
    from .api_gpu import bp as gpu_bp
    from .api_knowledge import bp as knowledge_bp
    from .api_llm import bp as llm_bp
    from .api_meetings import bp as meetings_bp
    from .api_personas import bp as personas_bp
    from .api_session import bp as session_bp
    for blueprint in (session_bp, personas_bp, config_bp, components_bp,
                      diag_bp, gpu_bp, analysis_bp, meetings_bp, llm_bp,
                      external_bp, knowledge_bp):
        app.register_blueprint(blueprint)

    # --- Router trasporti: sceglie da solo GSM (preferito) vs Android (fallback)
    _gsm_brain_cache: dict = {}
    _gsm_brain_lock = threading.Lock()

    def _gsm_brain():
        # Lock: due /call concorrenti creerebbero due cervelli senza questa sezione critica.
        with _gsm_brain_lock:
            if "b" not in _gsm_brain_cache:
                _gsm_brain_cache["b"] = make_brain(config)
            return _gsm_brain_cache["b"]

    router = None
    if (config.call_transport or "auto").lower() != "off":
        from .transport import CallRouter
        router = CallRouter(config, bridge, _gsm_brain)
        try:
            router.setup()
        except Exception as e:  # noqa: BLE001 - il rilevamento non deve impedire l'avvio
            log.error("Router trasporti: setup fallito: %s", e)
    # per le chiamate pilotate dall'esterno (api_external)
    app.config["CB_ROUTER"] = router

    def _authorized() -> bool:
        if not config.control_auth_token:
            return True
        return request.headers.get("Authorization") == f"Bearer {config.control_auth_token}"

    @app.before_request
    def _auth_gate():
        # Preflight CORS: il browser non allega l'Authorization alle OPTIONS.
        if request.method == "OPTIONS":
            return ("", 204)
        if request.path == "/health":
            return None
        if not _authorized():
            return jsonify({"error": "unauthorized"}), 401
        return None

    # Da sospeso le azioni che accenderebbero un componente rispondono 409 col
    # messaggio «componenti sospesi: premi Riprendi» (vedi sospensione.py).
    from .sospensione import installa_blocco
    installa_blocco(app)

    @app.after_request
    def _cors(resp):
        # Il Control Center (webview Tauri) chiama queste API con fetch.
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return resp

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/status")
    def status():
        return jsonify(status_fn() if status_fn is not None else bridge.status())

    @app.post("/prepare")
    def prepare():
        """Chi sta per chiamare avvisa prima: carica i modelli e rispondi."""
        body = request.get_json(silent=True) or {}
        try:
            result = bridge.prepare(
                stt_model=body.get("stt_model", config.default_stt_model),
                tts=body.get("tts", True),
            )
        except HandyError as e:
            return jsonify({"status": "error", "message": str(e)}), 502
        return jsonify(result)

    @app.post("/call")
    def call():
        """Chiamata in uscita sul trasporto auto-selezionato (GSM o Android).

        Campi opzionali: voice, lang, greeting, e `transport` per forzare
        ("gsm"/"android") scavalcando la selezione automatica.
        """
        body = request.get_json(silent=True) or {}
        number = normalize_phone_number((body.get("number") or "").strip())
        if not number:
            return jsonify({"error": "campo 'number' mancante"}), 400
        if not is_valid_phone_number(number):
            return jsonify({"error": "numero non valido"}), 400

        voice = body.get("voice")
        lang = body.get("lang")
        if router is not None:
            return jsonify(router.place_call(
                number, voice=voice, lang=lang,
                greeting=body.get("greeting"), force=body.get("transport")))

        # legacy (call_transport=off): solo Android
        bridge.start_outgoing_call(number, voice=voice, lang=lang,
                                   stt_model=body.get("stt_model"))
        return jsonify({
            "status": "calling",
            "number": number,
            "voice": voice or config.default_voice,
            "lang": lang or config.default_lang,
        })

    @app.post("/hangup")
    def hangup():
        if router is not None:
            return jsonify(router.hangup())
        result = bridge.hangup()
        code = 200 if result.get("status") == "ok" else 502
        return jsonify(result), code

    @app.get("/engines/catalog")
    def engines_catalog():
        """Elenco dei motori TTS dal BRIDGE (sempre acceso), non dal server voci
        :8720: si vede anche da sospeso. Non crea ne' carica motori."""
        from .engines import catalogo_motori
        try:
            return jsonify({"models": catalogo_motori(config)})
        except Exception as e:  # noqa: BLE001
            log.warning("engines_catalog: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.get("/engines/status")
    def engines_status_route():
        """Dove sono caricati DAVVERO i motori STT/TTS (device, file, modello).

        `running_on` è il device del modello vivo; `would_load_on` quello che
        verrebbe usato al prossimo caricamento (le modifiche dalla GUI si
        applicano a caldo ma solo alla ricarica successiva).
        """
        from .engines import engines_status
        try:
            return jsonify(engines_status(config))
        except Exception as e:  # noqa: BLE001 - motori non inizializzati
            log.warning("engines_status: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.get("/session/language")
    def session_language_get():
        """Lingua della sessione voce in corso + elenco delle lingue offerte.

        `language` cambia anche da solo (comando vocale, auto-rilevamento):
        chi mostra un selettore deve rileggerlo periodicamente, non fidarsi
        dell'ultimo valore che ha impostato.
        """
        from .voice_commands import LANG_TABLE
        corrente = ptt.current_language() if ptt is not None else ""
        return jsonify({
            "language": corrente,
            "active": bool(corrente),
            "languages": [{"code": k, "label": v.get("reply") or k}
                          for k, v in LANG_TABLE.items()],
        })

    @app.post("/session/language")
    def session_language_set():
        """Cambia la lingua della sessione in corso (voce di sintesi + lingua in
        cui l'agente deve rispondere). 409 se non c'è una sessione attiva."""
        body = request.get_json(silent=True) or {}
        code = (body.get("language") or body.get("code") or "").strip()
        if not code:
            return jsonify({"error": "campo 'language' mancante"}), 400
        if ptt is None:
            return jsonify({"error": "push-to-talk non disponibile"}), 503
        if not ptt.set_language(code):
            if not ptt.current_language():
                return jsonify({"error": "nessuna sessione voce attiva",
                                "code": "no_session"}), 409
            return jsonify({"error": f"lingua '{code}' non supportata"}), 400
        log.info("Lingua sessione -> %s (dal pannello)", code)
        return jsonify({"status": "ok", "language": code})

    @app.post("/session/new")
    def session_new():
        """Nuova conversazione: azzera il contesto dei due cervelli."""
        brain = getattr(ptt, "brains", None) if ptt is not None else None
        fn = getattr(brain, "reset_session", None)
        reset = False
        if callable(fn):
            try:
                reset = bool(fn())
            except Exception as e:  # noqa: BLE001
                log.warning("session/new: reset fallito: %s", e)
        return jsonify({"status": "ok", "reset": reset})

    @app.get("/transport")
    def transport():
        """Stato del rilevamento + trasporto selezionato. `?refresh=1` ri-rileva."""
        if router is None:
            return jsonify({"selected": None, "mode": "off"})
        if request.args.get("refresh"):
            router.refresh()
        return jsonify(router.status())

    return app


def run_server(
    config: Config,
    bridge: CallBridge | None = None,
    start_ptt: bool = True,
    ptt: PushToTalkManager | None = None,
    copilot=None,
    avatar=None,
) -> None:
    """Avvia il server di controllo, il watcher delle chiamate in arrivo e il
    push-to-talk (bloccante).

    Se `bridge` non e' passato, ne viene creato uno con il cervello di processo
    (`brains.make_brain`).

    `start_ptt=False` quando il push-to-talk "ricco" (overlay, hotkey)
    e' gestito altrove nello stesso processo (vedi `main.py all`): evita la
    doppia registrazione delle hotkey.
    """
    brains = make_brain(config)
    if bridge is None:
        bridge = CallBridge(config, brains)
    # Mentre il server e' attivo, l'orchestrator vigila anche sulle chiamate
    # in arrivo (le accetta e le gestisce in autonomia — Task #11)
    bridge.start_incoming_watcher()
    # ...e (se abilitato e non gestito altrove) sulle hotkey push-to-talk per il
    # dialogo a voce con i due cervelli (harness F1/F2, diretto F4/F5).
    if not start_ptt:
        log.info("Push-to-talk gestito esternamente (start_ptt=False)")
    elif config.push_to_talk_enabled:
        manager = PushToTalkManager(config, brains)
        manager.start()
        ptt = ptt or manager  # esponi anche /session/* in modalita' standalone
    else:
        log.info("Push-to-talk disabilitato (push_to_talk_enabled=false)")

    # Motori voce caldi SUBITO, in background. Prima si caricavano alla prima
    # richiesta — cioe' quando qualcuno premeva per dettare o apriva l'avatar —
    # e whisper "medium" ci mette una decina di secondi: un'attesa a freddo
    # proprio nel momento in cui si vuole parlare. Qui il bridge e' appena
    # partito e non ha niente da fare: e' il momento giusto per pagarla.
    #
    # In un thread e non in linea: il control server deve rispondere subito,
    # e chi interroga /session/state mentre i modelli si scaldano deve vedere
    # un server vivo, non una connessione che pende.
    def _scalda_motori() -> None:
        try:
            from .handy_client import ensure_models_loaded
            t0 = time.monotonic()
            # keepalive=True: e' un giro AUTOMATICO. Se l'utente ha sospeso i
            # componenti (stato su disco, vale anche dopo un riavvio) non deve
            # riprenderli ne' caricarli: solo lo STT resta caldo.
            ok = ensure_models_loaded(config, keepalive=True)
            log.info("Motori voce %s in %.1fs",
                     "pronti" if ok else "NON pronti", time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001 - il bridge parte comunque
            log.warning("Scaldamento motori voce fallito: %s", e)

    threading.Thread(target=_scalda_motori, name="warmup-voce",
                     daemon=True).start()

    app = create_app(config, bridge, ptt=ptt, copilot=copilot, avatar=avatar)
    log.info(
        "Control server in ascolto su http://%s:%d",
        config.control_host,
        config.control_port,
    )
    app.run(host=config.control_host, port=config.control_port, threaded=True)
