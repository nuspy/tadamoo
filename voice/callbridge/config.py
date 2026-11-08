"""Configurazione del call-bridge.

I valori si possono sovrascrivere con un file JSON (`config.json` accanto a
`main.py`) o con variabili d'ambiente `CALLBRIDGE_*`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, fields
from pathlib import Path


@dataclass
class Config:
    # --- App Android di controllo ---
    phone_host: str = "192.168.1.50"       # IP del telefono sulla LAN
    phone_port: int = 8080
    # Deve combaciare con ControlService.AUTH_TOKEN lato Android.
    auth_token: str = "change-me-shared-token"
    # L'IP qui sopra e' solo il punto di partenza: cambia a ogni riconnessione
    # al Wi-Fi, quindi se non risponde il telefono viene cercato da solo
    # (USB/adb, mDNS, broadcast, scansione). Vedi phone_discovery.py.
    phone_auto_discover: bool = True
    phone_id: str = ""                     # ANDROID_ID: fissa QUALE telefono usare
    phone_bt_name: str = ""                # forza il nome BT dell'endpoint HFP
    adb_exe: str = ""                      # percorso di adb, se non e' nel PATH

    # --- Audio HFP (endpoint Bluetooth del telefono su Windows) ---
    # None = autodetect (cerca "Hands-Free HF Audio" del telefono target).
    hfp_input_device: int | None = None    # downlink: voce dell'interlocutore
    hfp_output_device: int | None = None   # uplink: voce sintetizzata da iniettare
    sample_rate: int = 16000               # 16k (mSBC wideband); fallback 8k
    block_ms: int = 100                    # dimensione blocco audio

    # --- Handy esteso (motore STT/TTS, su host Windows) ---
    handy_host: str = "127.0.0.1"
    handy_port: int = 8720                 # porta API di Handy (HANDY_API_PORT)

    # --- Server di controllo dell'orchestrator ---
    # L'orchestrator e' SERVER per i comandi da Hermes e per le chiamate in uscita.
    control_host: str = "127.0.0.1"
    control_port: int = 8770
    control_auth_token: str = "change-me-control-token"

    # --- Default di chiamata ---
    default_voice: str = "if_sara"
    default_lang: str = "it"
    default_stt_model: str | None = None   # None = usa il modello di default di Handy

    # --- Push-to-talk utente (Task #12) ---
    # Registra la hotkey globale nell'orchestrator. Lascia False se la
    # combinazione e' gia' posseduta da un altro programma (es. Handy ha gia'
    # registrato Ctrl+Space): evita il conflitto di registrazione.
    push_to_talk_enabled: bool = True
    # Hotkey half-duplex: durante il TTS il microfono viene ignorato (no eco
    # con le casse). Hotkey full-duplex: ascolta sempre (ideale con le cuffie).
    push_to_talk_hotkey: str = "shift+f1"            # agente completo, casse (half-duplex)
    push_to_talk_hotkey_fullduplex: str = "shift+f2"  # agente completo, cuffie (full-duplex)
    push_to_talk_hotkey_limited: str = "shift+f3"     # modello limitato (simulazione chiamate)
    # Varianti con avatar video (MuseTalk): stessa conversazione completa ma con
    # il labiale sincronizzato sull'avatar, come nelle video call. Registrate
    # solo se avatar_enabled=true. Convivono con shift+f1/f2 (match esatto dei
    # modificatori in pynput). La persona mostrata e' `avatar_persona`.
    push_to_talk_hotkey_avatar: str = "ctrl+shift+f1"       # completo + avatar, half-duplex
    push_to_talk_hotkey_avatar_full: str = "ctrl+shift+f2"  # completo + avatar, full-duplex
    # Modalita' voce DIRETTA su LM Studio (NON agentica, near-real-time): stessa
    # sessione avatar/voce ma il "cervello" e' l'LLM di LM Studio via API
    # OpenAI-compatibile, senza Hermes/ACP. Memoria e personalita' sono per-avatar
    # (vedi lmstudio_* piu sotto). Registrate solo se avatar_chat_enabled=true.
    push_to_talk_hotkey_lmstudio: str = "ctrl+shift+f4"       # LM Studio diretto, half-duplex
    push_to_talk_hotkey_lmstudio_full: str = "ctrl+shift+f5"  # LM Studio diretto, full-duplex
    # Pausa di silenzio (s) che chiude il turno parlato in half-duplex: gli
    # spezzoni STT separati da meno di tanto vengono uniti in un solo enunciato.
    ptt_turn_pause_s: float = 1.5
    # Attesa ESTESA quando l'enunciato pare interrotto a meta' pensiero (finisce
    # con "e", "perche'", "il", una virgola...): vedi `_sembra_sospeso`. Con la
    # sola soglia fissa si doveva scegliere tra rispondere in fretta e non
    # tagliare chi si ferma a pensare; cosi' una frase conclusa parte subito e
    # un pensiero a meta' ottiene piu' tempo. 0 = disattiva l'adattivita'.
    ptt_turn_pause_max_s: float = 2.5
    # Pausa post-TTS (s): dopo che il TTS ha finito di parlare, il microfono
    # resta MUTO per questo tempo. Aspetta che l'eco del TTS si spenga
    # completamente dalle casse prima di riattivare il mic.
    ptt_post_tts_pause_s: float = 1.0
    # Warmup di ascolto (s): dopo la pausa post-TTS, il microfono viene
    # riattivato (audio va allo STT) ma il brain SCARTA gli enunciati per
    # questo tempo. Serve a: 1) far calibrare lo STT sul rumore di fondo
    # reale 2) catturare eventuali residui di eco 3) evitare falsi trigger.
    # Il primo enunciato valido viene accettato solo dopo questo periodo.
    ptt_listen_warmup_s: float = 0.8
    # Device audio per il push-to-talk: None = device di default dell'OS.
    ptt_input_device: int | None = None
    ptt_output_device: int | None = None

    # --- Copilota da conversazione (Task #22) ---
    # Hotkey Shift+F4: apre una finestra che ascolta l'INTERLOCUTORE di una
    # call/riunione SUL PC (cattura il loopback = audio in uscita del PC) e
    # suggerisce contesto + risposte. La voce dell'utente (microfono) NON viene
    # mai catturata, quindi non innesca risposte. Niente TTS (output solo testo).
    copilot_enabled: bool = True
    push_to_talk_hotkey_copilot: str = "shift+f4"
    # True = usa l'agente completo ACP (con tool/ricerche) anche per il copilota
    # (istanza dedicata, avviata alla prima attivazione). False = modello HTTP/stub.
    copilot_use_acp: bool = True
    # "Forza LLM esterna senza RAG": il copilota lascia l'agente locale e parla
    # col provider assegnato a `copilot` nel tab Provider LLM. Si RINUNCIA al
    # sapere delle persone (che vive in locale e non va spedito fuori) e agli
    # strumenti dell'agente: in cambio si ha una conoscenza generale piu' ampia.
    # Default False: il copilota resta locale, che e' cio' che abilita il RAG.
    copilot_force_external: bool = False
    copilot_loopback_samplerate: int = 48000  # sample rate del device di uscita
    copilot_lang: str = "it"
    copilot_num_responses: int = 3            # n. di risposte suggerite (regolabile in GUI)
    # Rispondi con audio: sintetizza (Handy TTS) la risposta scelta e la riproduce
    # sul device scelto. Per parlare DENTRO la call serve un cavo audio virtuale
    # (es. VB-CABLE) impostato come microfono dell'app di chiamata.
    copilot_audio_reply: bool = False         # default off (attivabile da checkbox)
    copilot_tts_output_device: int | None = None  # None = device di default dell'OS
    copilot_font_size: int = 22               # dimensione testo iniziale (regolabile)
    copilot_scroll_speed: float = 1.0         # autoscroll: frazione di vista/sec (0=off)
    # Nasconde la finestra dalla condivisione schermo / registrazione
    # (SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE, Win10 2004+): Zoom/Teams/
    # Meet/OBS non la catturano, ma resta visibile a te. Non protegge da una FOTO
    # dello schermo. Regolabile a runtime con il pulsante nella finestra.
    copilot_hide_from_capture: bool = True
    copilot_preamble: str = (
        "[Stai assistendo l'utente DURANTE una chiamata o riunione. Il testo qui "
        "sotto e' l'ultimo enunciato dell'INTERLOCUTORE (NON dell'utente): non "
        "rispondere come se fossi tu a parlare, ma aiuta l'utente a capire e a "
        "rispondere. Scrivi in italiano, conciso e leggibile a colpo d'occhio, in "
        "questo formato:\n"
        "CONTESTO: una riga che spiega cosa intende o chiede l'interlocutore.\n"
        "RISPOSTE: esattamente {n} risposte brevi che l'utente puo' dire, una per "
        "riga, ognuna su una riga che inizia con '- '.\n"
        "DATI: se servono fatti/numeri/ricerche, usa i tuoi strumenti e aggiungili "
        "qui appena disponibili; altrimenti ometti questa sezione. Non inventare. "
        "Non usare strumenti di sintesi vocale/TTS.]\n\n"
    )

    # --- Avatar video del copilota (worker MuseTalk) ---
    # L'avatar mostra una persona (foto/clip in avatar/persone/<nome>/) su una
    # webcam virtuale e ne sincronizza il labiale con le risposte TTS.
    # Si attiva dal copilota quando avatar_persona e' impostata.
    avatar_enabled: bool = False
    avatar_dir: str = "C:\\Projects\\callAPIcall\\avatar"
    avatar_persona: str = ""               # nome in avatar/persone/ ("" = disattivo)
    # Precarica il worker avatar all'avvio del bridge (in background): carica i
    # modelli MuseTalk in VRAM e prepara la persona (cache su disco) PRIMA della
    # prima Ctrl+Shift+F1, cosi' l'attivazione e' immediata invece di ~1 min. Il
    # worker resta poi caldo tra le sessioni (set_preview(False) non lo uccide).
    # Costo: ~3-4 GB di VRAM occupati sempre (contendono con LM Studio). Il
    # caricamento modelli in VRAM NON e' serializzabile: la preparazione dei clip
    # (landmark/latenti/maschere) e' invece gia' in cache su disco e riusata.
    avatar_preload: bool = False
    # Device audio su cui il worker riproduce la voce dell'avatar (None =
    # default OS; per parlare DENTRO la call: indice del cavo virtuale usato
    # come microfono dall'app di chiamata).
    avatar_audio_device: int | None = None
    # Finestra video locale ("callAPIcall — Avatar") con l'avatar che parla,
    # in aggiunta alla webcam virtuale OBS. Effetto al prossimo avvio del worker.
    avatar_preview_window: bool = True
    # GPU del worker MuseTalk (indice nvidia-smi; None = default). Effetto al
    # prossimo (ri)avvio dell'avatar. Picker alimentato da GET /gpu/list.
    avatar_gpu_index: int | None = None
    # DEPRECATO (non piu' usato): la preparazione di un clip non ha piu'
    # timeout — si attende finche' il worker e' vivo, col progresso visibile.
    # Streaming frase-per-frase dell'avatar: ogni frase completa viene
    # sintetizzata e accodata subito al worker, che la rende mentre il bridge
    # prepara la successiva. False = si aspetta l'INTERA risposta prima di
    # aprire bocca (comportamento storico): audio senza tagli fra le frasi, ma
    # con un TTS lento sono secondi di silenzio a ogni turno.
    avatar_stream_sentences: bool = True
    # Fotogrammi al secondo dell'avatar. E' la leva piu' diretta sul carico
    # GPU: il lavoro di UNet/VAE e' proporzionale al numero di fotogrammi,
    # quindi 16 al posto di 25 toglie il 36% del calcolo. Per una testa che
    # parla restano fluidi. Vale sia per il worker sia per la traccia WebRTC
    # del gateway, che devono combaciare.
    avatar_fps: int = 25
    # Voce dell'avatar dentro il flusso WebRTC come traccia Opus, invece di
    # frame PCM sul WebSocket. Si usa solo se l'app la chiede nella sua offerta
    # SDP, quindi le app vecchie continuano col PCM. False = sempre PCM.
    avatar_audio_webrtc: bool = True
    # Diagnostica: profila i primi batch del labiale col profiler di torch e
    # scrive nel log quali kernel consumano il tempo. Serve per il primo batch
    # che costa venti volte i successivi. Rallenta, quindi si accende solo per
    # indagare.
    avatar_profile: bool = False
    avatar_prepare_timeout_s: float = 600.0
    # Watchdog "nessun progresso": se il worker resta vivo ma non avanza per
    # questo tempo (hang GPU), start() esce con errore invece di bloccarsi.
    avatar_prepare_stall_s: float = 300.0

    # --- Hermes ---
    # Hermes e' SERVER per le chiamate (vedi skill/SKILL.md per la spec).
    # L'orchestrator chiama: /incoming-call, /outgoing-call, /utterance (SSE),
    # /call-ended, /health su hermes_base_url.
    hermes_base_url: str = "http://127.0.0.1:8780"
    hermes_mode: str = "stub"              # "stub" = no-op locale | "http" = HermesClient

    # --- Agente Hermes COMPLETO via ACP (con tool/azioni) ---
    # Per la voce interna (F1/F2) e le chiamate vere: dialogo col full agent.
    # L'orchestrator lancia `hermes acp` in WSL e parla JSON-RPC su stdio.
    acp_enabled: bool = True
    acp_wsl_distro: str = "Ubuntu"
    # Comando eseguito in WSL (login shell). `--accept-hooks` auto-approva i
    # hook shell (autonomia). Aggiungere `-m MODEL --provider PROVIDER` se serve
    # forzare il modello/provider (es. LM Studio locale).
    acp_wsl_command: str = (
        "cd /home/ataini && exec /home/ataini/.hermes/hermes-agent/venv/bin/python "
        "-m hermes_cli.main acp --accept-hooks"
    )
    acp_cwd: str = "/home/ataini"          # cwd passato a session/new (modo WSL)
    # --- Modo NATIVO (Hermes installato su Windows, niente WSL) ---
    # Se acp_use_wsl=False si lancia direttamente l'eseguibile Hermes nativo
    # (nessun wsl.exe). Path tipico post-installer: %LOCALAPPDATA%\hermes.
    # Stringa stile shell (gli argomenti extra tipo `-m MODEL` si aggiungono qui);
    # viene splittata con shlex(posix=False) per gestire i path Windows.
    acp_use_wsl: bool = False
    acp_native_command: str = (
        r'"C:\Users\atain\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe" '
        "acp --accept-hooks"
    )
    acp_cwd_native: str = r"C:\Users\atain"          # cwd passato a session/new (nativo)
    # HERMES_HOME passato al processo nativo (config/skills/cron migrati qui).
    acp_hermes_home: str = r"C:\Users\atain\AppData\Local\hermes"
    # Modello/provider LLM dell'agente ACP: iniettati come `-m MODEL --provider
    # PROVIDER` PRIMA del subcomando `acp` (percio' acp_native_command NON deve
    # contenerli). "" = usa il default di config.yaml di Hermes. Cambiabili a
    # runtime (GUI/pannello avatar): il cambio RIAVVIA l'agente ACP.
    acp_model: str = "gemma-4-12b-obliterated"
    acp_provider: str = "lmstudio"
    # Checkbox GUI "Override Hermes model". False = callAPIcall NON decide il
    # modello dell'agente: niente `-m/--provider` allo spawn e nessuna scrittura
    # di `model.default` nel config.yaml di Hermes -> vale il default di Hermes.
    # True = comportamento storico (acp_model/acp_provider imposti dalla GUI).
    # Persistito in config.json come ogni altro campo: lo stato scelto resta.
    acp_model_override: bool = True
    # Avvio di `hermes acp` lento (carica tool/MCP, anche server MCP che falliscono
    # in retry): timeout di init generoso.
    acp_init_timeout_s: float = 240.0
    acp_response_timeout_s: float = 300.0  # tetto attesa risposta (ricerche lunghe)
    # Istruzione iniettata nel prompt vocale: l'agente deve rispondere SOLO con
    # testo (l'audio lo genera l'orchestratore via Handy/Kokoro), niente tool TTS.
    # NB: un preamble "pesante" (regole tassative, "solo testo", ecc.) DISTURBA il
    # tool-calling dell'agente (gli fa emettere le chiamate-tool come testo non
    # eseguito). Tenerlo MINIMALE: i tool restano funzionanti come da tastiera e la
    # pulizia del parlato (markdown/markup/marcatori) la fa gia' il sanitizer TTS.
    acp_voice_preamble: str = (
        "[Conversazione a voce: rispondi in italiano, breve e naturale.]\n\n"
    )
    # Messaggio di INIT inviato all'agente all'apertura di una sessione (una
    # volta, la risposta NON viene pronunciata dal TTS): avvisa che e' in modo
    # vocale/avatar/chiamata e di usare le skill dedicate. Configurabile in GUI.
    # "" = niente init.
    voice_session_init: str = (
        "[SETUP SESSIONE: da ora sei in conversazione VOCALE in tempo reale, con "
        "AVATAR video e sintesi vocale (TTS). Applica la skill 'voice-mode': "
        "rispondi in testo semplice (niente markdown/elenchi/simboli), breve e "
        "naturale come si parla. Usa pure gli altri strumenti/skill quando "
        "servono. Rispondi solo: ok.]"
    )
    call_session_init: str = (
        "[SETUP SESSIONE: da ora sei in una TELEFONATA in tempo reale (solo voce). "
        "Applica la skill 'voice-mode': frasi brevi e parlate, testo semplice, "
        "niente markdown. Usa gli strumenti/skill quando servono. Rispondi solo: ok.]"
    )

    # --- Modalita' voce DIRETTA su LM Studio (Ctrl+Shift+F4/F5) ---
    # Conversazione NON agentica near-real-time contro l'API OpenAI-compatibile di
    # LM Studio, senza Hermes/ACP. Il "cervello" e' LmStudioClient (history in
    # processo, compattazione a rolling summary, memoria/personalita' per-avatar).
    # Accende la CHAT AVATAR (hotkey Ctrl+Shift+F4/F5 + il suo brain non
    # agentico). NON ha nulla a che vedere con quale backend risponde: quello lo
    # decide `avatar_llm_backend` (LM Studio o vLLM). Si chiamava
    # `lmstudio_enabled` — nome storico che faceva credere il contrario, al punto
    # che spegnerlo "perché uso vLLM" spegneva l'intera modalità: il vecchio nome
    # resta accettato in config.json per compatibilità (vedi `load`).
    avatar_chat_enabled: bool = True
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = ""                # "" = primo modello caricato in LM Studio
    lmstudio_api_key: str = "lm-studio"     # LM Studio ignora la chiave ma openai la vuole
    # System prompt base (la personalita' dell'avatar, se presente, si aggiunge a
    # questo a inizio sessione).
    lmstudio_system_prompt: str = (
        "Sei un interlocutore vocale in tempo reale. Rispondi in italiano, breve e "
        "naturale come si parla, in testo semplice (niente markdown, elenchi o simboli). "
        "Frasi corte con punteggiatura per aiutare la sintesi vocale."
    )
    # Compattazione: quando la history stimata supera questa soglia di token, i
    # turni piu' vecchi vengono riassunti in background; gli ultimi N restano interi.
    lmstudio_context_compaction_tokens: int = 6000
    # Frazione del contesto REALE del modello caricato oltre la quale si
    # compatta. La soglia effettiva e' il minimo fra questa e il tetto qui
    # sopra: cosi' il contesto scorre da solo qualunque modello si scelga,
    # senza dover riconfigurare nulla a ogni cambio.
    lmstudio_context_compaction_ratio: float = 0.6
    lmstudio_history_window: int = 8        # turni recenti tenuti verbatim
    lmstudio_response_timeout_s: float = 120.0
    # SPERIMENTALE: se True, dentro la STESSA sessione manda system+history solo al
    # PRIMO turno; ai turni successivi invia SOLO il nuovo messaggio utente,
    # assumendo che LM Studio conservi il contesto tra le richieste. ATTENZIONE:
    # /v1/chat/completions e' stateless per contratto -> se il server NON mantiene
    # lo stato, il modello perde memoria dei turni precedenti. Rimettere a False
    # (comportamento classico: riinvio dell'intero contesto a ogni turno) se la
    # conversazione perde il filo.
    lmstudio_stateful_server: bool = False
    # --- Personalita'/sapere da documenti (Fase 5) + RAG per-battuta ---
    # Pipeline OFFLINE (persona_ingest): documenti/libri -> testo -> chunk ->
    # embeddings (RAG locale) + estrazione LLM della personalita'. A conversazione
    # LmStudioClient recupera i chunk pertinenti e li inietta (RAG).
    # embed_model = id del modello di embedding caricato in LM Studio; "" =
    # nessun semantico -> il retrieval usa il fallback lessicale (sempre attivo).
    lmstudio_embed_model: str = ""
    # --- Backend LLM della chat avatar: LM Studio OPPURE vLLM ---
    # Scelta ESPLICITA dalla GUI. "" = auto (retrocompatibile: vLLM se
    # `vllm_model` e' valorizzato, altrimenti LM Studio). Vale SOLO per la chat
    # avatar (F4/F5): l'agente Hermes ha il suo provider (`acp_provider`).
    #
    # Regole di prontezza applicate a ogni warm-up/inizio sessione (llm_backend.py):
    #  - backend giu'            -> lo si avvia (LM Studio: `lms server start`)
    #  - nessun modello caricato -> si carica quello scelto qui sotto
    #  - modello gia' caricato   -> si usa QUELLO (nessuno swap forzato)
    avatar_llm_backend: str = ""             # "lmstudio" | "vllm" | "" (auto)
    # Avvio automatico di LM Studio quando e' il backend scelto ed e' spento.
    lmstudio_autostart: bool = True
    lmstudio_lms_cli: str = ""               # "" = `lms` dal PATH / ~/.lmstudio/bin
    lmstudio_app_path: str = ""              # "" = LM Studio.exe in %LOCALAPPDATA%
    lmstudio_start_timeout_s: float = 90.0   # attesa che :1234 risponda
    lmstudio_load_timeout_s: float = 300.0   # attesa di `lms load` (modello grande)
    # --- Backend vLLM via manager (servizio "vLLM Manager" su :7860) ---
    # Il MANAGER e' il gestore: avvia/ferma le istanze e decide i parametri di
    # caricamento (eager, max-model-len, VRAM, swap) dalle sue ricette per-modello.
    # callAPIcall gli dice solo QUALE modello vuole (`POST /api/ensure {model}`) e
    # usa l'URL che ritorna; se c'e' gia' un'istanza attiva usa quella col SUO
    # modello. Nessun tuning da qui: passarlo scavalcherebbe le ricette del manager.
    vllm_manager_url: str = "http://127.0.0.1:7860"
    vllm_model: str = ""                     # es. sahilchachra/Qwen3.6-35B-A3B-uncensored-heretic-AWQ
    vllm_ensure_timeout_s: float = 300.0     # quanto attendiamo il cold start (pesi + JIT)
    # Checkbox GUI "Chat Avatar reasoning": True = l'avatar ragiona (enable_thinking),
    # False = risposta diretta a bassa latenza (default per la voce).
    avatar_chat_reasoning: bool = False
    persona_rag_top_k: int = 4               # chunk iniettati per battuta
    persona_ingest_chunk_tokens: int = 1800  # ~dimensione blocco (stima 4 char/token)
    persona_ingest_overlap_tokens: int = 200

    # --- Turn-taking / barge-in (Task #7) ---
    # Se l'interlocutore parla mentre il TTS sta suonando, la risposta in corso
    # viene interrotta. L'energia minima (RMS, audio float32) per considerare
    # l'audio "voce" e non eco residua del TTS.
    barge_in_enabled: bool = True
    barge_in_energy_threshold: float = 0.02

    # --- Voice gateway per l'app Android (Task #20) ---
    # WebSocket che riceve audio dall'app remota (via Tailscale/LAN), fa
    # STT -> Hermes -> TTS e rimanda testo+audio. Un turno per volta (viva voce)
    # o full-duplex (auricolare/bluetooth).
    gateway_enabled: bool = True
    gateway_host: str = "0.0.0.0"          # 0.0.0.0 per essere raggiungibile via Tailscale
    gateway_port: int = 8771
    gateway_auth_token: str = "change-me-control-token"  # token condiviso con l'app
    # Server HTTP per i media (immagini/video generati da Hermes): l'app li
    # scarica via URL (meglio del base64 sul WebSocket, soprattutto per i video).
    gateway_media_port: int = 8772

    # --- Modem GSM/GPRS USB (SIM800C su porta seriale, via CH340) ---
    # Chiamate cellulari dirette dal PC via comandi AT (alternativa al giro
    # Bluetooth-HFP). Su SIM800C l'audio e' solo a file/turn-based (CMEDPLAY/CREC);
    # lo stesso driver controlla anche i moduli 4G (EC25/SIM7600) che hanno audio
    # USB. Vedi callbridge/gsm_modem.py.
    # Selezione automatica del trasporto chiamate (callbridge/transport.py):
    #   "auto"    = rileva e sceglie (preferisce il chip GSM, fallback Android)
    #   "gsm"     = forza il modem SIM800C
    #   "android" = forza il telefono Android (HFP)
    #   "off"     = nessun routing automatico (comportamento legacy)
    call_transport: str = "auto"
    gsm_port: str = "COM6"                 # porta seriale del modem (CH340)
    gsm_baud: int = 115200
    # Conversazione turn-based a file (SIM800C): finestra di registrazione per
    # turno (niente VAD live -> finestra fissa), file su flash del modulo, volume
    # di riproduzione, attesa risposta dopo il dial. Da rifinire col test SIM.
    gsm_record_window_s: float = 8.0       # quanto registra l'interlocutore per turno
    gsm_answer_wait_s: float = 6.0         # attesa che l'altro risponda dopo ATD
    gsm_tts_file: str = "C:\\tts.amr"      # path (sulla flash del modulo) del TTS da riprodurre
    gsm_record_id: int = 1                 # id file per AT+CREC (1-99; file in C:\REC\<id>.DVI)
    gsm_play_volume: int = 80              # volume CMEDPLAY (0-100 circa)
    gsm_auto_answer: bool = True           # risponde da solo alle chiamate in arrivo
    gsm_max_call_s: float = 600.0          # durata massima chiamata (paracadute anti-loop)
    gsm_max_empty_turns: int = 3           # turni vuoti di fila prima di chiudere

    # --- Meeting bot (endpoint /meetbot/* e /meetings/*) ---
    meetbot_enabled: bool = True
    meetbot_backend: str = "windows"       # "windows" (loopback) | "wsl" (futuro)
    meetbot_display_name: str = "Hermes Bot"
    meetbot_artifacts_dir: str = "artifacts/meetings"

    # --- Calendari (auto-join riunioni: OAuth, vedi INSTALL.md) ---
    # Google Cloud Console -> OAuth client "Desktop app".
    calendar_google_client_id: str = ""
    calendar_google_client_secret: str = ""
    # Azure App Registration (public client, delegated Calendars.Read).
    calendar_ms_client_id: str = ""
    calendar_ms_tenant: str = "common"
    calendar_poll_s: int = 300             # frequenza poll calendari

    # --- Conoscenza per progetto (wikibridge, repo separato) ---
    # Server wikibridge (LLM Wiki per progetti) e radice dei suoi dati.
    # Il server NON ha autostart: viene avviato on-demand da chi lo usa
    # (GUI e push riunioni) con il comando qui sotto.
    knowledge_api_url: str = "http://127.0.0.1:8775"
    knowledge_api_token: str = ""
    knowledge_root: str = r"C:\Projects\wikibridge\data"
    knowledge_serve_cmd: str = r"C:\Projects\wikibridge\.venv\Scripts\wikibridge.exe serve"
    # Come il cervello riceve la conoscenza del progetto:
    #  "path" = puntatore alla wiki su disco (solo single-user: scavalca le ACL)
    #  "api"  = estratti già filtrati per i permessi dell'utente (/rag/context)
    #  "auto" = "api" se wikibridge ha un identity provider (multi-utente)
    knowledge_access: str = "auto"
    # Utente wikibridge per cui si fa inferenza. Con wikibridge su LDAP/AD:
    # username qui e password nella variabile d'ambiente
    # CALLBRIDGE_KNOWLEDGE_PASSWORD (non qui). Con wikibridge su SSO (OIDC)
    # non servono: al primo bisogno si apre il browser sul login aziendale.
    knowledge_username: str = ""
    knowledge_password: str = ""
    knowledge_context_chars: int = 12000   # budget di testo per risposta
    knowledge_context_blocks: int = 8

    # --- Analisi video/audio (endpoint /analysis/*) ---
    # Cartella degli artifact (relativa a call-bridge/ se non assoluta).
    analysis_artifacts_dir: str = "artifacts/analyses"
    analysis_ffmpeg_path: str = "ffmpeg"
    analysis_ytdlp_path: str = "yt-dlp"
    analysis_segment_s: int = 300          # durata segmenti per /transcribe
    analysis_chunk_chars: int = 10000      # chunk del map-reduce di analisi
    # Attesa della risposta del cervello per OGNI chunk. Non e' quella della voce
    # (`acp_response_timeout_s`, 300 s): li' cinque minuti di silenzio sono un
    # guasto, qui il modello sta leggendo un pezzo di trascrizione e con uno
    # "thinking" 300 s non bastano — e allo scadere il turno esce VUOTO.
    analysis_response_timeout_s: float = 1800.0

    # --- Diarizzazione: chi parla quando ("Speaker 1", "Speaker 2", ...) ---
    # Modelli ONNX in models/diarization/ via sherpa-onnx (vedi engines/diarize.py).
    # E' un di piu': se il pacchetto o i modelli mancano, la trascrizione esce
    # comunque, senza etichette.
    diarization_enabled: bool = True
    diarization_model_dir: str = ""        # "" = call-bridge/models/diarization
    # "cuda" richiede un wheel sherpa-onnx build CUDA: con quello di pip (CPU) il
    # provider viene ignorato. Su CPU sono comunque ~19x realtime.
    diarization_device: str = "cpu"
    diarization_num_threads: int = 4
    # Numero di voci: 0 = lo decide il clustering. Per il singolo file lo
    # sovrascrive `speakers` in POST /analysis/start — saperlo (un'intervista e'
    # a due) e' molto piu' affidabile che indovinarlo.
    diarization_num_speakers: int = 0
    # Soglia del clustering automatico. TARATA SUL MODELLO DI EMBEDDING: con
    # CAM++ (il predefinito) 1.0 separa le voci, 0.8 le frantuma e 1.2 le fonde.
    diarization_threshold: float = 1.0
    # Oltre questa durata si salta: l'audio va tutto in RAM in float32
    # (~230 MB/ora) e il beneficio non vale il rischio di far fuori il bridge.
    diarization_max_minutes: int = 240

    # --- Controllo componenti dalla GUI (endpoint /components/*) ---
    # Comandi (eseguiti in `wsl -d acp_wsl_distro -- bash -lc ...`) per avviare/
    # fermare il servizio Hermes ("" = controllo disabilitato, solo monitoraggio).
    hermes_start_cmd: str = ""
    hermes_stop_cmd: str = ""

    # --- Motore STT/TTS NATIVO (integrazione di Handy in-process) ---
    # Elimina la dipendenza dal processo Handy portando STT/TTS dentro il bridge.
    # Backend selezionabile per convivenza/rollback: "handy" (WS a :8720) o
    # "native" (in-process). Default "handy" = rete di sicurezza durante la migrazione.
    stt_backend: str = "handy"             # "handy" | "native"
    tts_backend: str = "handy"             # "handy" | "native"
    # STT nativo (openai-whisper / torch). Distribuibile: gira su CUDA o CPU.
    stt_model: str = "small"               # tiny|base|small|medium|large-v3
    stt_device: str = "cuda"               # "cuda" | "cpu" (fallback auto a cpu se CUDA assente)
    stt_gpu_memory: str = "shared"         # "shared" | "dedicated" (solo cuda; shared: impl. Fase 2)
    stt_compute_type: str = "auto"         # "auto" | "float16" | "float32" | "int8"
    # Tutta l'inferenza whisper in UN SOLO thread (coda di lavori) invece di
    # un thread per chiamante. PyTorch tiene le cache dei piani cuDNN
    # thread_local, quindi un thread nuovo li ricostruisce: la dettatura ne
    # lanciava uno per ogni micro-pausa. False = comportamento storico.
    stt_single_thread: bool = True
    # Motore di conversazione continua: l'audio resta nel gateway e viene
    # consegnato a tempo invece di essere spinto appena pronto, cosi' cio' che
    # non e' ancora uscito si puo' ancora tagliare o cambiare. Interruttore
    # TEMPORANEO, finche' il percorso nuovo non ha sostituito il vecchio.
    duplex_motore_nuovo: bool = False
    # Quando l'utente dice qualcosa che riguarda cio' che l'avatar STA PER
    # dire, si riscrive il seguito invece di accodare una risposta a parte.
    # Costa un taglio e una generazione in piu', quindi si concede solo se
    # c'e' abbastanza parlato non ancora uscito da coprire l'attesa.
    duplex_integra: bool = False
    duplex_integra_da_s: float = 2.5
    # Cancellazione d'eco sul PC (AEC3 di WebRTC in un venv a parte, perche' la
    # ruota non esiste per il 3.10 su cui gira il bridge). Senza, in vivavoce
    # il microfono risente le casse: misurato, 9,3 dB di attenuazione naturale
    # e la frase dell'avatar trascritta per intero a qualunque volume. Se il
    # venv non c'e' l'audio passa com'era, quindi accenderlo non e' rischioso.
    aec_pc: bool = True
    aec_ritardo_ms: int = 60
    # Dove si renderizza il lipsync dell'avatar. Non e' una preferenza del
    # server ma una capacita' del client, che la dichiara: il PC la ricorda per
    # sapere se aprire il video WebRTC (musetalk) o mandare invece i dati per il
    # rendering sul dispositivo (wav2lip / visemi). Il default e' il
    # comportamento storico.
    #   musetalk = il PC genera il video (fotorealistico, non scala: un flusso
    #              per utente)
    #   wav2lip  = il telefono genera il lipsync on-device dalla foto + audio
    #              (fotorealistico, scala)
    #   visemi   = il telefono anima la foto sui tempi dei visemi che il PC
    #              calcola dal testo (leggerissimo, scala a molti utenti)
    render_mode_default: str = "musetalk"
    # espeak-ng per la fonemizzazione multilingue dei visemi (testo->IPA). Vuoto
    # = si cerca nel PATH; se manca del tutto, i visemi ripiegano sulle regole
    # italiane. La data dir va indicata a parte perche' la build Windows
    # segfaulta senza ESPEAK_DATA_PATH.
    espeak_ng_exe: str = ""
    espeak_data_path: str = ""
    # Temperatura della chat avatar. Non era impostata affatto: si prendeva il
    # default del server (0,7-0,8), e a quella temperatura i modelli
    # multilingue cambiano alfabeto in mezzo alla frase — misurato su
    # ternary-bonsai-27b: "un sistema di обработка audio", "in inglês", frasi
    # in cinese dentro una risposta italiana. A 0,3 il fenomeno e' sparito
    # (0 caratteri estranei su 1373), a 0,7 restavano 4-8. Per una voce conta
    # piu' la tenuta della lingua che la varieta' delle parole.
    lmstudio_temperature: float = 0.3
    stt_language: str = ""                 # "" = autodetect; es. "it", "en"
    # Endpointing del client STT nativo. Usa il VAD SILERO (modello, robusto ai
    # mic a basso livello: non taglia la voce continua) se disponibile; altrimenti
    # ripiega sull'energia (stt_vad_threshold).
    stt_vad_model: str = ""                # path silero onnx ("" = autodetect)
    stt_vad_speech_prob: float = 0.5       # soglia probabilita' di voce (silero)
    stt_vad_threshold: float = 0.01        # RMS minimo (fallback energia)
    stt_endpoint_silence_s: float = 0.75   # silenzio che CHIUDE il turno
    # Chunking del parlato: a ogni micro-pausa di questa durata il pezzo
    # gia' detto parte in trascrizione mentre l'utente continua, cosi' quando
    # scatta l'endpoint resta da trascrivere solo la coda. 0 = disattivato.
    stt_chunk_silence_s: float = 0.35
    stt_chunk_min_s: float = 2.0      # sotto questo parlato non si spezza
    stt_min_utterance_s: float = 0.3       # scarta enunciati piu' corti
    # Cartella modelli Kokoro per il TTS nativo ("" = autodetect da Handy/AppData).
    kokoro_model_dir: str = ""
    # Device del TTS nativo (Kokoro via onnxruntime). NB: la GPU richiede
    # onnxruntime-gpu (di default onnxruntime espone solo CPU) -> "cpu" e' il
    # default effettivo; lo switch resta per coerenza con lo STT e uso futuro.
    tts_device: str = "cpu"                # "cuda" | "cpu"
    tts_gpu_memory: str = "shared"         # "shared" | "dedicated" (solo cuda)
    # Precisione del modello Kokoro da caricare: "fp32" (kokoro-v1.0.onnx, qualita'
    # max), "fp16" (kokoro-v1.0.fp16.onnx, default), "int8" (kokoro-v1.0.int8.onnx,
    # piu' leggero/veloce). Il file deve essere presente in kokoro_model_dir.
    kokoro_precision: str = "fp16"         # "fp32" | "fp16" | "int8"
    # Voci CLONATE (Chatterbox Multilingual, come Handy): quando la voce richiesta
    # corrisponde a un file in <kokoro_model_dir>/cloned_voices/, la sintesi passa
    # all'helper Chatterbox (subprocess, protocollo stdio binario) invece di Kokoro.
    # I path vuoti fanno autodetect dal venv/helper lasciati da Handy.
    clone_tts_enabled: bool = True         # False = ignora le voci clonate (solo Kokoro)
    chatterbox_venv_python: str = ""       # "" = autodetect chatterbox-venv di Handy
    chatterbox_helper: str = ""            # "" = autodetect chatterbox_tts_helper.py
    # Piper: TTS per le lingue assenti da Kokoro (Ungherese, Polacco, ...). Le voci
    # sono file <piper_model_dir>/<voce>.onnx (+ .onnx.json) da rhasspy/piper-voices.
    # Quando la voce richiesta corrisponde a un file Piper, la sintesi usa Piper.
    piper_enabled: bool = True
    piper_model_dir: str = ""              # "" = autodetect <kokoro_model_dir>/piper_voices
    # Device/memoria per Piper e per la voce clonata (Chatterbox), come Kokoro.
    # device: "cpu"|"cuda"; memory: "dedicated"|"shared". cuda+dedicated=GPU VRAM;
    # cuda+shared=CPU (shared VRAM reale non ottenibile: shared = CPU, 0 VRAM
    # dedicata, per non contendere con altri sistemi). Kokoro usa tts_device/
    # tts_gpu_memory; Whisper stt_device/stt_gpu_memory.
    piper_device: str = "cpu"              # "cpu" | "cuda"
    piper_gpu_memory: str = "dedicated"    # "dedicated" | "shared"
    chatterbox_device: str = "cuda"        # "cpu" | "cuda"
    chatterbox_gpu_memory: str = "dedicated"
    # Motore TTS scelto nella GUI (id del catalogo in engines/catalog.py:
    # kokoro | piper | chatterbox | xtts | cosyvoice | orpheus | dia).
    # Persistente: prima viveva solo in memoria e ogni riavvio tornava a Kokoro.
    tts_engine: str = "kokoro"
    # Radice dei venv dei motori pesanti, uno per motore ("" = <repo>/tts-venvs).
    # Ognuno gira come subprocess perche' hanno stack torch/CUDA incompatibili
    # fra loro e con onnxruntime del bridge.
    tts_venvs_dir: str = ""
    xtts_device: str = "cuda"              # "cpu" | "cuda"
    xtts_gpu_memory: str = "dedicated"     # "dedicated" | "shared"
    cosyvoice_device: str = "cuda"
    cosyvoice_gpu_memory: str = "dedicated"
    orpheus_device: str = "cuda"
    orpheus_gpu_memory: str = "dedicated"
    dia_device: str = "cuda"
    dia_gpu_memory: str = "dedicated"
    # Qwen3-TTS (helper nel venv `dia`, runtime faster-qwen3-tts). Taglia dei
    # modelli ufficiali: "0.6B" (piu' veloce, ~4 GB VRAM) o "1.7B" (qualita',
    # istruzioni di stile sulle voci preimpostate). I due campi *_model
    # sostituiscono il modello ufficiale con una variante compatibile (repo
    # Hugging Face o cartella locale): Base per le voci clonate, CustomVoice per
    # le 9 voci preimpostate. backend "ggml" = GGUF quantizzati via qwentts.cpp
    # (quant "Q8_0" | "Q4_K_M"), serve il pacchetto qwentts-cpp-python.
    qwen3tts_device: str = "cuda"
    qwen3tts_gpu_memory: str = "dedicated"
    qwen3tts_size: str = "0.6B"            # "0.6B" | "1.7B"
    qwen3tts_clone_model: str = ""         # "" = Qwen/Qwen3-TTS-12Hz-<size>-Base
    qwen3tts_preset_model: str = ""        # "" = Qwen/Qwen3-TTS-12Hz-<size>-CustomVoice
    qwen3tts_backend: str = "auto"         # "auto" | "torch" | "ggml"
    qwen3tts_quant: str = "Q8_0"           # solo backend ggml
    # Streaming TTS: i motori che lo sanno fare (catalogo, `streaming=True`)
    # consegnano l'audio A PEZZI mentre generano la frase, invece di attendere
    # la frase intera. Adattivo: si va a pezzi solo quando l'audio gia' pronto
    # davanti all'ascoltatore e' meno di `tts_stream_margin_s` secondi (prima
    # frase, o sintesi rimasta indietro); altrimenti la frase si genera intera,
    # come prima, e non si spezzetta nulla per niente.
    tts_streaming: bool = True
    tts_stream_margin_s: float = 2.0
    # Precedenza engine per l'auto-selezione voce nelle lingue presenti su
    # ENTRAMBI i modelli (Kokoro e Piper). False = Kokoro ha la precedenza,
    # True = Piper. Le lingue presenti su uno solo (es. Ungherese=solo Piper)
    # usano quella disponibile a prescindere.
    tts_prefer_piper: bool = False
    # Server Handy-compatibile: replica l'API di Handy (HTTP + WS /tts/stream e
    # /stt/stream) sui motori nativi, cosi' i consumatori esterni che parlavano
    # con Handy su :8720 (es. la skill Hermes kokoro_tts.py) funzionano invariati.
    # Va abilitato SOLO con Handy spento (stessa porta). Default off in convivenza.
    handy_compat_enabled: bool = False
    handy_compat_host: str = "127.0.0.1"
    handy_compat_port: int = 8720          # identica a Handy: "esattamente com'era"
    # Dettatura globale (come Handy): hotkey PUSH-TO-HOLD -> registra dal mic
    # mentre premuta -> al rilascio STT nativo -> incolla nell'app in primo piano
    # (clipboard + Ctrl+V). Alt+Spazio e' soppressa (niente menu di sistema).
    dictation_enabled: bool = True
    dictation_hotkey: str = "alt+space"
    # Dopo l'incolla il testo dettato resta negli appunti (come un Ctrl+C) e
    # compare nella cronologia; false = si rimette il contenuto di prima.
    dictation_keep_clipboard: bool = True

    # --- Comportamento ---
    poll_status_interval_s: float = 1.0    # frequenza polling /status del telefono
    tts_speed: float = 1.2
    # Respiro imposto DOPO la punteggiatura, in secondi: le pause naturali di
    # Kokoro sono brevi e irregolari, queste rendono il parlato dell'avatar più
    # naturale. 0 = nessuna pausa aggiunta (solo quella del modello).
    # Normalizzazione del testo prima della sintesi (solo italiano): espande
    # numeri, unita' di misura, sigle, date e orari in forma pronunciabile.
    # Senza, il TTS legge "15 km" come "quindici kappa emme". Vedi text_normalize.
    tts_normalize_text: bool = True
    tts_pause_sentence_s: float = 0.5      # dopo . ! ? …
    tts_pause_clause_s: float = 0.25       # dopo , ; :                 # velocita' di sintesi TTS (1.0 = normale, >1 = piu' veloce)

    # --- Bridge testuale Telegram (on-demand: non parte da solo con CallAPICall,
    # si avvia a mano con `python -m callbridge.telegram_text_bridge` quando serve) ---
    telegram_wrapper_url: str = "http://127.0.0.1:8888"
    telegram_wrapper_token: str = ""        # deve combaciare con rest.auth_token nel settings.json del wrapper
    telegram_bridge_host: str = "127.0.0.1"
    telegram_bridge_port: int = 8790        # il wrapper deve avere acp.server_port su questo stesso valore

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        cfg = cls()
        # 1) file JSON
        json_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config.json"
        if json_path.exists():
            # utf-8-SIG, non utf-8: un config.json scritto da PowerShell 5.1
            # (`Set-Content -Encoding UTF8`) o dal Blocco note comincia con il
            # BOM, e con `utf-8` json.loads muore su "Unexpected UTF-8 BOM"
            # prima ancora che il bridge parta. Il BOM si mangia e si prosegue.
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
            # Nomi storici ancora presenti nelle config esistenti: si accettano
            # solo se il nome nuovo non c'è, così un file già migrato vince.
            for vecchio, nuovo in (("lmstudio_enabled", "avatar_chat_enabled"),):
                if vecchio in data and nuovo not in data:
                    data[nuovo] = data[vecchio]
            for f in fields(cls):
                if f.name in data:
                    setattr(cfg, f.name, data[f.name])
        # 2) variabili d'ambiente (priorita' massima)
        for f in fields(cls):
            env = os.environ.get(f"CALLBRIDGE_{f.name.upper()}")
            if env is not None:
                current = getattr(cfg, f.name)
                if isinstance(current, bool):
                    setattr(cfg, f.name, env.lower() in ("1", "true", "yes"))
                elif isinstance(current, int) or (current is None and f.name.endswith("device")):
                    setattr(cfg, f.name, int(env))
                elif isinstance(current, float):
                    setattr(cfg, f.name, float(env))
                else:
                    setattr(cfg, f.name, env)
        return cfg

    def base_url(self) -> str:
        return f"http://{self.phone_host}:{self.phone_port}"

    def handy_base_url(self) -> str:
        return f"http://{self.handy_host}:{self.handy_port}"

    def handy_stt_url_lang(self, lang: str = "") -> str:
        """URL dello STT con la lingua della sessione (`?lang=it`).

        Passarla evita che Whisper la indovini: su enunciati brevi ci prende
        poco, e una trascrizione nella lingua sbagliata manda fuori strada
        anche l'agente e la voce di risposta."""
        base = self.handy_stt_url()
        codice = (lang or "").strip().lower().split("-")[0]
        return f"{base}?lang={codice}" if codice else base

    def handy_stt_url(self) -> str:
        return f"ws://{self.handy_host}:{self.handy_port}/stt/stream"

    def handy_tts_url(self) -> str:
        return f"ws://{self.handy_host}:{self.handy_port}/tts/stream"

    def as_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str | os.PathLike[str] | None = None) -> None:
        """Scrive la configurazione corrente su file JSON."""
        json_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config.json"
        json_path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
