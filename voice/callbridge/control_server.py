"""Server HTTP di controllo dell'orchestrator.

L'orchestrator e' **server** per:
  - i comandi che partono dal server di TaDaMoo (e dall'agente)
  - le chiamate in uscita

Endpoint:
  GET  /health             - liveness (senza auth)
  GET  /status             - stato orchestrator + telefono + modelli Handy
  POST /prepare            - carica i modelli in vista di una chiamata
  POST /call               - chiamata in uscita fatta a mano {number, voice?, lang?, greeting?, transport?}
  POST /hangup             - termina la chiamata corrente
  GET  /transport          - trasporto selezionato (?refresh=1 ri-rileva)
  GET  /call/approvals     - chiamate di un programma in attesa del via della persona
  POST /call/approvals/<id>/approve|deny
  GET  /call/audit         - ultime righe del registro delle chiamate
  (le chiamate di un programma o dell'agente: /external/*, vedi api_external.py)

Auth: header `Authorization: Bearer <control_auth_token>` (tranne /health).
"""

from __future__ import annotations

import hmac
import logging
import threading
import time

from flask import Flask, jsonify, request

from .brains import make_brain
from .config import Config
from .handy_client import HandyError
from .orchestrator import CallBridge
from .ptt import PushToTalkManager

log = logging.getLogger("callbridge.control")


def create_app(
    config: Config,
    bridge: CallBridge | None = None,
    ptt: PushToTalkManager | None = None,
    copilot=None,
    avatar=None,
    status_fn=None,
    policy=None,
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

    # --- Chiamate: la politica di sicurezza e il router dei trasporti (GSM preferito, Android di ripiego)
    from .call_policy import Policy
    from .call_service import CallService
    calls = CallService(config, bridge, policy or Policy(config))
    app.config["CB_CALLS"] = calls

    def _authorized() -> bool:
        if not config.control_auth_token:
            return True
        given = request.headers.get("Authorization", "")
        return hmac.compare_digest(given.encode("utf-8", "replace"), f"Bearer {config.control_auth_token}".encode("utf-8"))

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
        """Chiamata in uscita fatta a mano dalla persona, sul trasporto scelto (GSM o Android): senza approvazione, ma la prima frase dichiara
        l'IA (se l'impostazione e' accesa) e un numero di emergenza e' rifiutato.

        Campi: number; opzionali voice, lang, greeting e `transport` ("gsm"/"android") per scavalcare la scelta automatica.
        """
        body = request.get_json(silent=True) or {}
        code, out = calls.place("user", str(body.get("number") or ""), greeting=str(body.get("greeting") or ""), voice=body.get("voice"),
                                lang=body.get("lang"), transport=body.get("transport"))
        return jsonify(out), code

    @app.post("/hangup")
    def hangup():
        return jsonify(calls.hangup())

    @app.get("/call/approvals")
    def call_approvals():
        """Le chiamate di un programma che aspettano il via della persona."""
        return jsonify({"pending": calls.policy.approvals.pending(), "mode": calls.policy.approval_mode})

    @app.post("/call/approvals/<rid>/<verdict>")
    def call_decide(rid: str, verdict: str):
        if verdict not in ("approve", "deny"):
            return jsonify({"error": "not found"}), 404
        if not calls.policy.approvals.decide(rid, verdict == "approve"):
            return jsonify({"error": "no such request waiting", "code": "unknown"}), 404
        return jsonify({"status": "approved" if verdict == "approve" else "denied", "id": rid})

    @app.get("/call/audit")
    def call_audit():
        """Le ultime righe del registro delle chiamate (numero mascherato, nessuna trascrizione)."""
        limit = min(max(request.args.get("limit", default=20, type=int), 1), 200)
        return jsonify({"rows": calls.policy.audit.tail(limit)})

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
        from . import language_names
        status = ptt.language_status() if ptt is not None else {}
        corrente = status.get("language", "")
        return jsonify({
            "language": corrente,
            "active": bool(corrente),
            "mode": status.get("mode", ""),
            "follow": status.get("follow", False),
            "engine_fallback": status.get("engine_fallback", False),
            "warnings": status.get("warnings", []),
            "languages": [{"code": c, "label": language_names.label(c, "en")} for c in status.get("enabled", [])],
        })

    @app.post("/session/language")
    def session_language_set():
        """Cambia la lingua della sessione in corso (voce di sintesi + lingua in
        cui l'agente deve rispondere); "auto" la rimette automatica. 409 se non c'è una sessione attiva."""
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
            return jsonify({"error": f"language '{code}' is not enabled (Settings > Voice)", "code": "not_enabled"}), 400
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
        return jsonify(calls.transport_status(bool(request.args.get("refresh"))))

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
    app.config["CB_CALLS"].sync(eager=True)
    log.info(
        "Control server in ascolto su http://%s:%d",
        config.control_host,
        config.control_port,
    )
    app.run(host=config.control_host, port=config.control_port, threaded=True)
