"""Server HTTP di controllo dell'orchestrator.

L'orchestrator e' **server** per:
  - i comandi che partono da Hermes (e dalla skill per agenti)
  - le chiamate in uscita

Hermes e la skill sono **client** di questa API. Le chiamate in *arrivo*
seguono il verso opposto (l'orchestrator notifica Hermes — vedi Task #11).

Endpoint:
  GET  /health             - liveness (senza auth)
  GET  /status             - stato orchestrator + telefono + modelli Handy
  POST /prepare            - carica i modelli in vista di una chiamata
  POST /call               - chiamata in uscita {number, voice?, lang?, stt_model?}
  POST /hangup             - termina la chiamata corrente
  GET  /models/available   - voci/lingue disponibili            (proxy -> Handy)
  POST /models/load        - carica modelli in VRAM             (proxy -> Handy)
  POST /models/unload      - scarica modelli dalla VRAM         (proxy -> Handy)
  GET  /models/status      - stato dei modelli in VRAM          (proxy -> Handy)

Auth: header `Authorization: Bearer <control_auth_token>` (tranne /health).
"""

from __future__ import annotations

import logging
import threading
import time

from flask import Flask, jsonify, request

from .config import Config
from .handy_client import HandyError
from .hermes_client import HermesClient
from .orchestrator import CallBridge
from .ptt import PushToTalkManager
from .stubs import StubHermes
from .util import is_valid_phone_number, normalize_phone_number

log = logging.getLogger("callbridge.control")


def _hermes_models(config: Config) -> dict:
    """Modelli LLM di Hermes RAGGRUPPATI per provider (per il dropdown a rami).

    Fonte: sezione `providers` del config.yaml di Hermes (autorevole) + merge dei
    `/v1/models` live per i provider LOCALI (lmstudio/diffusiongemma) cosi' i
    modelli appena caricati appaiono. `current` = modello/provider ACP attuale.
    """
    import os

    providers = []
    current = {"model": config.acp_model, "provider": config.acp_provider}

    # Modelli CARICABILI dal manager vLLM (lazy-start: l'istanza puo' essere spenta,
    # ma il selettore deve comunque mostrare cosa e' disponibile in E:\AI\Models\vllm).
    vllm_loadable: set[str] = set()
    mgr = (getattr(config, "vllm_manager_url", "") or "").rstrip("/")
    if mgr:
        try:
            import requests
            r = requests.get(mgr + "/api/models/local", timeout=2)
            for m in (r.json() or {}).get("models", []):
                if m.get("loadable") and m.get("source") == "dir" and m.get("model"):
                    vllm_loadable.add(str(m["model"]))
        except Exception:  # noqa: BLE001 - manager non raggiungibile
            pass

    def _is_vllm(prov: str, base: str) -> bool:
        # provider vLLM per nome o per porta nell'intervallo istanze (8000-8100)
        if prov == "vllm":
            return True
        import re
        m = re.search(r":(\d+)", base or "")
        return bool(m and 8000 <= int(m.group(1)) <= 8100)

    path = os.path.join(config.acp_hermes_home or "", "config.yaml")
    try:
        import yaml
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
        # Modello EFFETTIVO dell'agente = model.default/provider (acp ignora -m).
        md = data.get("model") or {}
        current = {"model": md.get("default"), "provider": md.get("provider")}
        for prov, pd in (data.get("providers") or {}).items():
            pd = pd or {}
            models = set((pd.get("models") or {}).keys())
            base = (pd.get("base_url") or "").rstrip("/")
            if base and ("127.0.0.1" in base or "localhost" in base):
                try:
                    import requests
                    r = requests.get(base + "/models", timeout=2)
                    for m in (r.json() or {}).get("data", []):
                        mid = str(m.get("id") or "")
                        if mid and "embed" not in mid.lower():
                            models.add(mid)
                except Exception:  # noqa: BLE001 - provider locale non pronto
                    pass
            # vLLM: aggiungi i modelli caricabili dal manager (visibili anche a istanza spenta)
            if _is_vllm(prov, base):
                models |= vllm_loadable
            if models:
                providers.append({"provider": prov, "models": sorted(models),
                                  "local": _is_vllm(prov, base) or bool(
                                      base and ("127.0.0.1" in base or "localhost" in base))})
    except Exception as e:  # noqa: BLE001 - config.yaml assente/illeggibile
        log.warning("hermes_models: lettura config.yaml fallita: %s", e)
    # Quantizzazione e peso accanto a ogni voce: nei menu ci sono modelli con lo
    # STESSO nome e quantizzazioni diverse, che pesano (e stanno in VRAM)
    # diversamente. `models` resta la lista di id di prima, per i consumatori
    # che non leggono `models_info`.
    from . import llm_backend
    try:
        # `ask_cli` solo se LM Studio è già acceso: `lms ls` (unica fonte del
        # peso) altrimenti lo farebbe partire solo per riempire un menu.
        lm_meta = llm_backend.lmstudio_catalog(
            config, ask_cli=llm_backend.lmstudio_up(config))
        vl_meta = llm_backend.vllm_catalog(config)
        caldi = llm_backend.lmstudio_loaded(config) or []
        for p in providers:
            meta = vl_meta if _is_vllm(p["provider"], "") else lm_meta
            p["models_info"] = [
                dict(llm_backend.describe(m, meta.get(m)), loaded=m in caldi)
                for m in p["models"]]
        current["loaded"] = caldi
    except Exception as e:  # noqa: BLE001 - metadati sono un di piu', mai bloccanti
        log.debug("hermes_models: metadati non disponibili: %s", e)
    # `override` = checkbox "Override Hermes model": a False la GUI mostra il
    # modello di Hermes in sola lettura (callAPIcall non lo decide piu').
    return {"providers": providers, "current": current,
            "override": bool(getattr(config, "acp_model_override", True))}


def create_app(
    config: Config,
    bridge: CallBridge | None = None,
    ptt: PushToTalkManager | None = None,
    copilot=None,
    avatar=None,
) -> Flask:
    """Costruisce l'app Flask del server di controllo.

    `bridge` puo' essere passato dall'esterno (es. per condividere Hermes
    vero); se assente ne viene creato uno con lo StubHermes.

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
    bridge = bridge or CallBridge(config, StubHermes())
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
        # Lock: due /call concorrenti creerebbero due AcpAgentClient (= due
        # processi hermes) senza questa sezione critica.
        with _gsm_brain_lock:
            if "b" not in _gsm_brain_cache:
                from .brains import make_full_brain
                _gsm_brain_cache["b"] = make_full_brain(config, label="gsm")
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
        return jsonify(bridge.status())

    @app.post("/prepare")
    def prepare():
        """Hermes avvisa che sta per chiamare: carica i modelli e rispondi."""
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

    # --- proxy verso il canale di controllo di Handy --------------------

    @app.get("/models/available")
    def models_available():
        try:
            return jsonify(bridge.handy.models_available())
        except HandyError as e:
            return jsonify({"error": str(e)}), 502

    @app.post("/models/load")
    def models_load():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(
                bridge.handy.load_models(
                    stt_model=body.get("stt_model"), tts=body.get("tts", True)
                )
            )
        except HandyError as e:
            return jsonify({"error": str(e)}), 502

    @app.post("/models/unload")
    def models_unload():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(bridge.handy.unload_models(body.get("component", "all")))
        except HandyError as e:
            return jsonify({"error": str(e)}), 502

    @app.get("/models/status")
    def models_status():
        try:
            return jsonify(bridge.handy.models_status())
        except HandyError as e:
            return jsonify({"error": str(e)}), 502

    # --- LLM di Hermes: modello per sessione + nuova conversazione -------

    @app.get("/hermes/models")
    def hermes_models():
        """Modelli disponibili raggruppati per provider + modello ACP corrente."""
        return jsonify(_hermes_models(config))

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

    @app.get("/avatar/llm/models")
    def avatar_llm_models():
        """Modelli selezionabili per la CHAT AVATAR (F4/F5), per backend.

        Per LM Studio e vLLM: quali sono gia' caricati (`loaded` -> sono quelli
        che verrebbero usati adesso) e quali si possono caricare (`available`).
        `?include=lmstudio|vllm|all` interroga un backend anche se spento — per
        LM Studio significa ACCENDERLO (lo fa la sua CLI), quindi la GUI lo passa
        solo quando l'utente sceglie quel backend.
        """
        from .llm_backend import list_models
        try:
            return jsonify(list_models(config, request.args.get("include", "")))
        except Exception as e:  # noqa: BLE001 - backend giu'/CLI assente
            log.warning("avatar_llm_models: %s", e)
            return jsonify({"error": str(e)}), 502

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

    @app.post("/avatar/llm/swap")
    def avatar_llm_swap():
        """Carica per la CHAT AVATAR il modello scelto AL POSTO di quello caldo.

        Stessa regola di `/hermes/model`: senza `confirm: true` risponde 409
        `confirm_required` dicendo cosa verrebbe scaricato. Senza questa chiamata
        esplicita il modello già in memoria resta e viene usato così com'è.
        """
        body = request.get_json(silent=True) or {}
        model = (body.get("model") or "").strip()
        if not model:
            return jsonify({"error": "campo 'model' mancante"}), 400
        from . import llm_backend
        check = llm_backend.swap_check(config, model, "lmstudio")
        if check["needs_confirm"] and not bool(body.get("confirm")):
            return jsonify({"code": "confirm_required", **check}), 409
        ok, msg = llm_backend.swap_lmstudio(config, model)
        if not ok:
            return jsonify({"error": msg, "code": "swap_failed"}), 502
        return jsonify({"status": "ok", "model": model, "note": msg})

    @app.post("/hermes/model")
    def hermes_set_model():
        """Cambia il modello LLM dell'agente ACP (riavvia l'agente col nuovo -m).

        Se in memoria c'è GIÀ un altro modello, non si scarica nulla di
        iniziativa: si risponde 409 `confirm_required` con cosa verrebbe
        scaricato. Il chiamante ripete con `confirm: true` per procedere allo
        swap, oppure lascia perdere e resta caldo quello di prima.
        """
        body = request.get_json(silent=True) or {}
        model = (body.get("model") or "").strip()
        provider = body.get("provider")
        if not model:
            return jsonify({"error": "campo 'model' mancante"}), 400
        if not bool(getattr(config, "acp_model_override", True)):
            return jsonify({
                "error": "override del modello Hermes disattivato: il modello "
                         "dell'agente lo decide Hermes (config.yaml)",
                "code": "override_disabled",
            }), 409
        brain = getattr(ptt, "brain_full", None) if ptt is not None else None
        fn = getattr(brain, "set_model", None)
        if not callable(fn):
            return jsonify({"error": "brain non supporta il cambio modello"}), 400
        from . import llm_backend
        check = llm_backend.swap_check(config, model, provider or "")
        if check["needs_confirm"]:
            if not bool(body.get("confirm")):
                return jsonify({"code": "confirm_required", **check}), 409
            ok, msg = llm_backend.swap_lmstudio(config, model, provider or "")
            if not ok:
                return jsonify({"error": msg, "code": "swap_failed"}), 502
            log.info("Hermes: swap confermato -> %s (%s)", model, msg)
        fn(model, provider)
        return jsonify({"status": "ok", "model": model, "provider": provider,
                        "note": "agente ACP in riavvio col nuovo modello"})

    @app.post("/session/new")
    def session_new():
        """Nuova conversazione Hermes: azzera il contesto dell'agente ACP."""
        brain = getattr(ptt, "brain_full", None) if ptt is not None else None
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

    Se `bridge` non e' passato, ne viene creato uno con il client Hermes scelto
    in base a `config.hermes_mode`:
      - `"http"`  -> `HermesClient` verso `hermes_base_url` (vedi skill/SKILL.md)
      - altro     -> `StubHermes` (eco locale, utile per i test)

    `start_ptt=False` quando il push-to-talk "ricco" (overlay, F1/F2/F3, ACP)
    e' gestito altrove nello stesso processo (vedi `main.py all`): evita la
    doppia registrazione delle hotkey.
    """
    if bridge is None:
        if config.acp_enabled:
            # Agente ACP nativo: dopo il cutover il phone server HTTP (:8780) non
            # gira piu'. AcpAgentClient parte lazy (nessun processo finche' non
            # arriva una chiamata), quindi non prolifera processi hermes.
            from .brains import make_full_brain
            log.info("Hermes integration: agente ACP (nativo)")
            hermes = make_full_brain(config, label="orchestrator")
        elif config.hermes_mode == "http":
            log.info("Hermes integration: HTTP (%s)", config.hermes_base_url)
            hermes = HermesClient(config)
        else:
            log.info("Hermes integration: stub (eco locale)")
            hermes = StubHermes()
        bridge = CallBridge(config, hermes)
    # Mentre il server e' attivo, l'orchestrator vigila anche sulle chiamate
    # in arrivo (le accetta e le gestisce in autonomia — Task #11)
    bridge.start_incoming_watcher()
    # ...e (se abilitato e non gestito altrove) sulla hotkey push-to-talk per il
    # dialogo diretto utente <-> Hermes. Disabilitato quando la combinazione e'
    # gia' registrata da un altro programma (es. Handy possiede Ctrl+Space).
    #
    # Il PTT usa due "cervelli":
    #   brain_full    (F1/F2) = agente completo via ACP (con tools/skills) se
    #                 acp_enabled=true, altrimenti HermesClient/stub.
    #   brain_limited (F3)    = HermesClient (phone server, senza tools).
    if not start_ptt:
        log.info("Push-to-talk gestito esternamente (start_ptt=False)")
    elif config.push_to_talk_enabled:
        from .brains import (make_full_brain, make_limited_brain,
                             make_lmstudio_brain)
        brain_full = make_full_brain(config, label="ptt")
        # Con ACP abilitato F3 condivide l'unico agente col full brain (dopo il
        # cutover nativo il phone server :8780 non gira; evita un 2o processo).
        brain_limited = (brain_full if config.acp_enabled
                         else make_limited_brain(config))
        brain_lmstudio = (make_lmstudio_brain(config)
                          if config.avatar_chat_enabled else None)
        # Avvia warm-up ACP in background (non blocca il server).
        # NIENTE `import threading` qui: il modulo lo importa gia' in cima, e
        # un import LOCALE rende `threading` una variabile locale per tutta la
        # funzione — quindi ogni uso PRECEDENTE esplode con
        # "local variable 'threading' referenced before assignment", e il
        # control server non parte affatto. Successo davvero.
        # NIENTE warm-up dell'agente ACP all'avvio: non si carica alcun modello
        # LLM finche' non serve. L'agente si avvia pigramente alla prima richiesta
        # (telefonata, avatar Hermes, chat) via `respond` -> `_ensure_started`;
        # il primo turno di quella funzione paga l'avvio dell'agente.
        manager = PushToTalkManager(config, brain_full, brain_limited,
                                    brain_lmstudio=brain_lmstudio)
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
