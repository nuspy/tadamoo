"""Brain "diretto LM Studio": conversazione vocale NON agentica near-real-time.

A differenza di `AcpAgentClient`/`HermesClient` (lo stato vive nel server),
`LmStudioClient` tiene la history IN PROCESSO e parla direttamente con un'API
OpenAI-compatibile (`/v1/chat/completions`, stream). E' il "cervello" delle
sessioni Ctrl+Shift+F4/F5.

Il backend e' scelto dalla GUI tra **LM Studio** e **vLLM** e viene reso pronto
da `llm_backend.resolve()` (avvio del server, caricamento del modello scelto se
nessuno e' caricato, riuso di quello gia' in memoria). Nome storico della classe
a parte, non e' legato a LM Studio.

Espone l'interfaccia brain duck-typed attesa da `StreamingSession`:
  respond(text) -> Iterator[str]          (obbligatorio)
  set_reply_language(name) / reset_session() / prime(message) / warm_up() / end()
  begin_session(persona)                  (hook per la memoria per-avatar)

Note di design:
- NON si pronuncia `reasoning_content` (chain-of-thought): al TTS va solo il
  testo `content`. Per modelli reasoning conviene usare un instruct o disabilitare
  il thinking lato LM Studio.
- La history e' protetta da `threading.Lock` (respond + compattazione concorrenti).
- Compattazione (rolling summary) e memoria per-avatar: vedi metodi dedicati.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("callbridge.lmstudio")


class LmStudioClient:
    def __init__(self, config, consumer: str = "chat", use_rag: bool = True) -> None:
        self.cfg = config
        # Quale voce del registro provider seguire ("chat", "copilot", …) e se
        # arricchire i turni col sapere della persona. Il copilota forzato su
        # un LLM esterno gira con `use_rag=False`: il RAG vive in locale, e
        # mandare fuori quei testi sarebbe un'altra cosa da quella chiesta.
        self.consumer = consumer
        self.use_rag = use_rag
        self._client = None                 # openai.OpenAI (lazy)
        self._client_base_url = config.lmstudio_base_url  # per rilevare cambi da GUI
        self._model: str | None = None      # risolto pigramente
        self._model_want = (config.lmstudio_model or "").strip()  # id richiesto
        # Backend in uso ("lmstudio"|"vllm") e scelta con cui e' stato creato il
        # client: se la GUI le cambia, il client va ricostruito (vedi begin_session).
        self._backend: str | None = None
        self._backend_want = ""
        # Preparazione del backend (avvio server + caricamento modello): parte a
        # inizio sessione in BACKGROUND, cosi' il cold start (fino a ~2 min per un
        # modello grande) si consuma mentre l'avatar carica invece di bloccare in
        # silenzio il primo turno. `_ready` = preparazione conclusa (anche male),
        # `_prepare_error` = messaggio da dire all'utente se e' andata male.
        self._ready = threading.Event()
        self._ready.set()                   # nessuna preparazione in corso
        self._prepare_error: str | None = None
        self._lock = threading.Lock()
        # Prompt di sistema, componibile: base + personalita' avatar + memoria +
        # istruzione lingua. Ricomposto da `_system_message()`.
        self._system_base = (config.lmstudio_system_prompt or "").strip()
        self._persona_prompt = ""           # personalita' avatar (begin_session)
        self._memory_prompt = ""            # profilo/memoria avatar (begin_session)
        self._reply_lang: str | None = None
        self._history: list[dict] = []      # [{role, content}, ...] senza il system
        self._token_est = 0                 # stima token della history
        # Modalita' "server stateful" (SPERIMENTALE, config.lmstudio_stateful_server):
        # dopo il primo turno inviato con contesto pieno, i turni successivi mandano
        # solo il nuovo messaggio utente (si assume che LM Studio ricordi il resto).
        self._primed = False                # contesto pieno gia' inviato in sessione
        self._compacting = False
        self._persona: str | None = None    # avatar corrente (memoria per-avatar)
        self._knowledge_project: str | None = None
        # Transcript integrale della sessione (NON compattato): serve al flush di
        # fine sessione (riassunto + memoria). Distinto da _history (compattata).
        self._transcript: list[dict] = []
        self._rag = None                    # PersonaRag (sapere della persona) o None

    # --- client / modello -------------------------------------------------

    def _prepare_backend(self) -> tuple[str, str]:
        """(base_url, modello) del backend scelto per la chat avatar, PRONTO:
        `llm_backend.resolve` avvia LM Studio se spento, carica il modello scelto
        se non ce n'e' nessuno e riusa quello gia' caricato quando c'e'."""
        from . import llm_backend
        backend, base, model = llm_backend.resolve(self.cfg, self.consumer)
        self._backend = backend
        return base, model

    def prepare_async(self) -> None:
        """Avvia in BACKGROUND la preparazione del backend (server + modello).

        Chiamata a inizio sessione: il cold start di un modello grande dura
        minuti e prima veniva pagato -- in silenzio -- al primo turno, con
        l'utente che parlava e l'avatar muto."""
        if not self._ready.is_set():
            return                          # preparazione gia' in corso
        self._ready.clear()
        self._prepare_error = None

        def _run() -> None:
            import time as _t
            t0 = _t.monotonic()
            try:
                self._ensure_client()       # risolve backend + modello, crea il client
                log.info("Chat avatar: backend %s pronto in %.0fs (modello %s)",
                         self._backend, _t.monotonic() - t0, self._model)
            except Exception as e:  # noqa: BLE001 - lo diciamo all'utente a voce
                self._prepare_error = str(e)
                log.error("Chat avatar: preparazione backend fallita: %s", e)
            finally:
                self._ready.set()

        threading.Thread(target=_run, name="chat-backend-warmup", daemon=True).start()

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            base_url, model = self._prepare_backend()
            self._client_base_url = base_url
            # Il modello effettivo lo decide il backend (regola "si usa quello
            # gia' caricato"), non il campo di config: memoizzalo qui.
            self._model = model or self._model
            # Con un provider REMOTO la chiave è la sua, non quella (fittizia)
            # di LM Studio: senza, ogni chiamata tornerebbe 401.
            from . import llm_backend
            remoto = llm_backend.remote_provider(self.cfg, self.consumer)
            api_key = (remoto or {}).get("api_key") or self.cfg.lmstudio_api_key \
                or "lm-studio"
            self._client = OpenAI(base_url=base_url, api_key=api_key)
        return self._client

    def _resolve_model(self) -> str:
        if self._model:
            return self._model
        # Non ancora risolto (client creato altrove): interroga il backend.
        _, model = self._prepare_backend()
        if model:
            self._model = model
            return model
        # Provider remoto: non c'è nulla "in memoria" da ispezionare, il modello
        # è quello scelto nel tab. Se manca, lo si dice invece di andare a
        # chiedere a LM Studio, che qui non c'entra.
        if self._backend not in ("lmstudio", "vllm", None):
            log.error("Chat: nessun modello scelto per il provider '%s'",
                      self._backend)
            return ""
        # "" -> si usa SOLO un modello davvero caricato. Qui prima si prendeva
        # `models.list().data[0]`, che in LM Studio e' il catalogo su disco: si
        # finiva a chiedere un modello non caricato e a farlo caricare JIT
        # accanto a quello caldo (VRAM esaurita). Se non c'e' nulla in memoria si
        # lascia "" e il turno lo dice a voce: meglio di un caricamento a caso.
        try:
            from . import llm_backend
            loaded = llm_backend.lmstudio_loaded(self.cfg) or []
        except Exception as e:  # noqa: BLE001 - LM Studio spento/non interrogabile
            log.warning("Chat avatar: modelli caricati non leggibili: %s", e)
            loaded = []
        if loaded:
            self._model = loaded[0]
            log.info("Chat avatar: uso il modello in memoria '%s'", self._model)
        else:
            log.error("Chat avatar: nessun modello in memoria e nessuno caricabile")
        return self._model or ""

    # --- composizione del system prompt ----------------------------------

    def _system_message(self) -> dict:
        parts = [self._system_base]
        if self._persona_prompt:
            parts.append(self._persona_prompt)
        if self._memory_prompt:
            parts.append(self._memory_prompt)
        if self._reply_lang:
            parts.append(f"Rispondi in {self._reply_lang}.")
        return {"role": "system", "content": "\n\n".join(p for p in parts if p)}

    @staticmethod
    def _delta_text(chunk) -> str:
        """Solo `content` (NON reasoning_content: non si pronuncia la CoT)."""
        if not chunk.choices:
            return ""
        return getattr(chunk.choices[0].delta, "content", None) or ""

    @staticmethod
    def _est_tokens(messages: list[dict]) -> int:
        """Stima grezza (char/4). Sufficiente per la sola soglia di compattazione."""
        return sum(len(m.get("content") or "") for m in messages) // 4

    # --- interfaccia brain -----------------------------------------------

    def respond(self, text: str) -> Iterator[str]:
        text = (text or "").strip()
        if not text:
            return
        # Modello ancora in caricamento: DILLO invece di restare muto. L'utente
        # sente "un attimo", poi la risposta vera arriva appena il backend e'
        # pronto (il turno non viene perso).
        if not self._ready.wait(timeout=2.0):
            yield "Un attimo, sto caricando il modello."
            timeout = float(getattr(self.cfg, "vllm_ensure_timeout_s", 300.0))
            if not self._ready.wait(timeout=timeout):
                yield " Ci sta mettendo troppo: controlla il motore dalla schermata Persone."
                return
        if self._prepare_error:
            yield ("Non riesco ad avviare il modello: " + self._prepare_error[:120])
            return
        try:
            client = self._ensure_client()
            model = self._resolve_model()
        except Exception as e:  # noqa: BLE001
            log.error("Chat avatar: backend non disponibile: %s", e)
            yield "Scusa, il modello non è al momento disponibile."
            return
        if not model:
            # Niente in memoria e niente di caricabile (tipicamente VRAM piena):
            # dirlo, invece di mandare all'API un nome che farebbe partire un
            # caricamento destinato a fallire.
            yield ("Non c'è nessun modello in memoria e non c'è spazio sulla "
                   "scheda per caricarlo. Libera la GPU e riprova.")
            return

        rag_ctx = self._rag_context(text)
        stateful = bool(getattr(self.cfg, "lmstudio_stateful_server", False))
        with self._lock:
            if stateful and self._primed:
                # Server "stateful": il contesto è già stato inviato al primo turno;
                # qui mandiamo SOLO il nuovo messaggio utente (+ eventuale RAG del
                # turno). Se LM Studio NON conserva lo stato, il modello perde il filo
                # → rimettere lmstudio_stateful_server=False.
                messages = []
                if rag_ctx:
                    messages.append({"role": "system", "content": rag_ctx})
                messages.append({"role": "user", "content": text})
            else:
                # Contesto pieno. Prefisso STABILE (system + history) per primo: così
                # LM Studio riusa la KV cache tra i turni. Il RAG cambia a ogni domanda,
                # quindi va in coda (dopo la history) per non invalidare quella cache.
                messages = [self._system_message()] + list(self._history)
                if rag_ctx:
                    messages.append({"role": "system", "content": rag_ctx})
                messages.append({"role": "user", "content": text})
                self._primed = True

        pieces: list[str] = []
        usage_total = None
        try:
            # "Reasoning" commutabile per-richiesta (checkbox "Chat Avatar
            # reasoning"). I due backend vogliono parametri DIVERSI:
            #
            #  - vLLM:      chat_template_kwargs.enable_thinking
            #  - LM Studio: reasoning_effort ("none" per spegnerlo). Qui
            #               chat_template_kwargs viene ignorato e "/no_think"
            #               non e' riconosciuto da Qwen 3.6 — misurato: senza
            #               questo parametro un "ciao" costa 15s di pensiero
            #               invisibile, con reasoning_effort="none" 0,5s.
            #
            # L'impostazione della GUI di LM Studio NON vale qui: governa solo
            # la sua chat interna, non le richieste su /v1/chat/completions.
            # Il parametro dipende dal backend CHE STA RISPONDENDO, non dal fatto
            # che in config ci sia un `vllm_model`: con un modello vLLM
            # configurato ma la conversazione servita da LM Studio si mandava
            # `chat_template_kwargs` (che LM Studio ignora) e il reasoning
            # restava acceso — 16-28s di pensiero invisibile prima del primo
            # token, misurati sui log.
            reasoning = bool(getattr(self.cfg, "avatar_chat_reasoning", False))
            extra = {}
            if self._backend == "vllm":
                extra["extra_body"] = {"chat_template_kwargs": {
                    "enable_thinking": reasoning}}
            elif self._backend == "lmstudio" and not reasoning:
                extra["reasoning_effort"] = "none"
            # Provider remoti (Gemini, Claude, OpenAI, …): nessuno dei due
            # parametri: ognuno ha la sua sintassi per il thinking e mandare
            # quella sbagliata fa fallire la richiesta con un 400.
            temperatura = getattr(self.cfg, "lmstudio_temperature", None)
            if temperatura is not None:
                extra["temperature"] = float(temperatura)
            stream = client.chat.completions.create(
                model=model, messages=messages, stream=True,
                stream_options={"include_usage": True},
                timeout=self.cfg.lmstudio_response_timeout_s,
                **extra,
            )
            for chunk in stream:
                # La usage arriva nell'ultimo chunk (choices vuoto).
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage_total = getattr(u, "total_tokens", None)
                tok = self._delta_text(chunk)
                if tok:
                    pieces.append(tok)
                    yield tok
        except Exception as e:  # noqa: BLE001 - errore di rete/modello
            log.error("Chat avatar: risposta fallita: %s", e)
            if not pieces:
                yield "Scusa, ho avuto un problema a rispondere."
            return

        reply = "".join(pieces).strip()
        now = time.time()
        with self._lock:
            self._history.append({"role": "user", "content": text})
            self._history.append({"role": "assistant", "content": reply})
            self._transcript.append({"ts": now, "speaker": "user", "text": text})
            self._transcript.append({"ts": now, "speaker": "assistant", "text": reply})
            if usage_total:
                self._token_est = int(usage_total)
            else:
                self._token_est = self._est_tokens(
                    [self._system_message()] + self._history)
        self._maybe_compact()

    def correggi_ultima_risposta(self, detto: str) -> bool:
        """Sostituisce l'ultima risposta con ciò che è stato DAVVERO pronunciato.

        Serve perché la storia della conversazione registra ciò che il modello
        ha generato, non ciò che l'utente ha sentito: appena c'è
        un'interruzione le due cose divergono, e da quel momento il modello dà
        per acquisite frasi che nessuno ha mai udito. Il sintomo tipico è un
        "come ti dicevo" che si riferisce a un pezzo troncato tre turni prima.

        Senza il Copione questa correzione non era scrivibile — nessuno sapeva
        dove fosse arrivata la voce. Ora sì, e questa riga chiude un difetto
        che esisteva da prima del full-duplex.
        """
        detto = (detto or "").strip()
        with self._lock:
            for msg in reversed(self._history):
                if msg.get("role") != "assistant":
                    continue
                if detto:
                    msg["content"] = detto
                else:
                    # Non è uscita una sillaba: il turno non è mai avvenuto,
                    # e va tolto insieme alla domanda che lo aveva prodotto.
                    self._history.remove(msg)
                return True
        return False

    def respond_continuazione(self, detto: str, previsto: str,
                              nuova: str) -> Iterator[str]:
        """Riscrive il seguito di una risposta in corso, tenendo conto
        dell'interruzione.

        Non è un turno nuovo: è la stessa frase che prosegue diversamente. Il
        prompt distingue in modo esplicito ciò che l'utente ha già sentito da
        ciò che era pronto ma modificabile, e vieta i tre difetti che
        altrimenti si presentano sempre: ripetere quel che si è appena detto,
        ricominciare da capo, e scusarsi dell'interruzione (che la fa notare
        invece di assorbirla).
        """
        istruzioni = (
            "Stai parlando a voce e l'utente ti ha interrotto.\n"
            f'Hai GIÀ DETTO ad alta voce, e lui lo ha sentito: "{detto[-400:]}"\n'
            f'Stavi per dire, ma NON è ancora uscito: "{previsto[:400]}"\n'
            f'Lui ti ha detto: "{nuova}"\n\n'
            "Scrivi come PROSEGUI da qui, tenendo conto di quello che ti ha "
            "detto. Non ripetere quello che hai già detto, non ricominciare "
            "da capo, non scusarti dell'interruzione e non dire che eri stato "
            "interrotto: continua e basta, come farebbe una persona.")
        return self.respond(istruzioni)

    def set_reply_language(self, name: str | None) -> None:
        self._reply_lang = (name or "").strip() or None

    def reset_session(self) -> bool:
        """Nuova conversazione: azzera la history (tiene system + memoria caricata)."""
        with self._lock:
            self._history = []
            self._token_est = 0
            self._primed = False   # la nuova conversazione ridà il contesto iniziale
        log.info("Chat avatar: nuova conversazione (history azzerata)")
        return True

    def prime(self, message: str) -> None:
        # In modalita' LM Studio il system prompt persona domina: il messaggio di
        # init (pensato per l'agente ACP) non va iniettato. No-op.
        return None

    def warm_up(self) -> None:
        """Scalda il modello in LM Studio con una micro-richiesta (best-effort)."""
        try:
            client = self._ensure_client()
            model = self._resolve_model()
            client.chat.completions.create(
                model=model, max_tokens=1,
                messages=[{"role": "user", "content": "ok"}],
            )
            log.info("Chat avatar: modello scaldato (%s)", model)
        except Exception as e:  # noqa: BLE001 - warm-up best-effort
            log.debug("Chat avatar: warm-up fallito: %s", e)

    # --- memoria per-avatar (Fase 3) -------------------------------------

    def _persona_root(self, persona: str) -> Path:
        import re
        safe = re.sub(r"\s+", "_", (persona or "").strip())
        return Path(self.cfg.avatar_dir) / "persone" / safe

    def _read_manifest(self, persona: str) -> dict:
        try:
            p = self._persona_root(persona) / "manifest.json"
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - manifest assente/illeggibile
            return {}

    def _memory_path(self, persona: str) -> Path:
        return self._persona_root(persona) / "memory.json"

    def _read_memory(self, persona: str) -> dict:
        try:
            return json.loads(self._memory_path(persona).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - prima sessione: nessuna memoria
            return {}

    def _write_memory(self, persona: str, mem: dict) -> None:
        try:
            p = self._memory_path(persona)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(mem, indent=2, ensure_ascii=False),
                         encoding="utf-8")
        except OSError as e:
            log.warning("Chat avatar: memory.json non scritto (%s): %s", persona, e)

    @staticmethod
    def _time_delta_phrase(last_seen_ts) -> str:
        if not last_seen_ts:
            return ""
        try:
            dt = time.time() - float(last_seen_ts)
        except (TypeError, ValueError):
            return ""
        if dt < 3600:
            when = "poco fa"
        elif dt < 86400:
            when = f"circa {int(dt // 3600)} ore fa"
        elif dt < 86400 * 30:
            when = f"{int(dt // 86400)} giorni fa"
        else:
            when = f"circa {int(dt // (86400 * 30))} mesi fa"
        return (f"L'ultima volta che avete parlato è stato {when}: tienine conto "
                "nel salutare e nel riprendere il discorso.")

    def begin_session(self, persona: str | None) -> None:
        """Inizio sessione (SINCRONO): azzera history/transcript e compone il
        system prompt con personalita' (manifest) + memoria (memory.json) +
        differenza di tempo dall'ultima conversazione."""
        with self._lock:
            self._history = []
            self._token_est = 0
            self._primed = False   # nuova sessione: il primo turno rimanda il contesto
            self._transcript = []
            self._persona = (persona or "").strip() or None
            self._persona_prompt = ""
            self._memory_prompt = ""
            self._knowledge_project = None
            self._rag = None
            # Rileggi i campi "di costruzione" dal config a ogni sessione: così le
            # modifiche fatte dalla GUI (base_url/model/system_prompt) valgono
            # dalla sessione successiva SENZA riavviare il bridge (altrimenti
            # resterebbero memoizzati da __init__/primo uso).
            self._system_base = (self.cfg.lmstudio_system_prompt or "").strip()
            # Reset del client quando la GUI cambia la scelta di backend o il
            # modello: senza questo il client memoizzato continuerebbe a parlare
            # col backend precedente fino al riavvio del bridge.
            from . import llm_backend
            backend_want = llm_backend.selected_backend(self.cfg)
            want = ((self.cfg.vllm_model if backend_want == "vllm"
                     else self.cfg.lmstudio_model) or "").strip()
            if backend_want != self._backend_want:
                self._client, self._model = None, None
                self._backend_want = backend_want
            elif backend_want == "lmstudio" and self._client_base_url != self.cfg.lmstudio_base_url:
                # Col backend vLLM il base_url e' dinamico (lo decide il manager):
                # ha senso confrontarlo solo in modalita' LM Studio.
                self._client, self._model = None, None
                self._client_base_url = self.cfg.lmstudio_base_url
            if want != self._model_want:
                # Anche il CLIENT va ricreato, non solo il nome del modello: uno
                # swap vLLM può mettere la nuova istanza su un'altra porta, e il
                # client memoizzato continuerebbe a parlare con quella vecchia.
                self._client, self._model = None, None
                self._model_want = want
        # NON si scalda il backend qui: aprire la sessione (identita' della
        # persona) non deve caricare alcun modello LLM. Il modello si risolve
        # pigramente alla prima `respond` (`_resolve_model`), cioe' solo quando
        # l'avatar deve davvero rispondere — e se in memoria c'e' gia' un modello
        # si usa quello, altrimenti si carica quello del config (vedi llm_backend).
        if not self._persona:
            return
        try:
            manifest = self._read_manifest(self._persona)
            personality = (manifest.get("personality") or "").strip()
            personality_docs = (manifest.get("personality_from_docs") or "").strip()
            sysp = (manifest.get("system_prompt") or "").strip()
            self._knowledge_project = (manifest.get("knowledge_project") or "").strip() or None
            mem = self._read_memory(self._persona)
            profile = (mem.get("rolling_profile") or "").strip()
            recent = mem.get("recent_sessions") or []
            pp = []
            if sysp:
                pp.append(sysp)
            if personality:
                pp.append("La tua personalità e il tuo modo di parlare: " + personality)
            if personality_docs:
                pp.append("Chi sei e come parli, dai testi acquisiti su di te: " + personality_docs)
            mp = []
            if profile:
                mp.append("Cosa ricordi delle conversazioni passate: " + profile)
            if recent:
                last = recent[-1].get("summary") or ""
                if last:
                    mp.append("Ultima conversazione: " + last[:600])
            delta = self._time_delta_phrase(mem.get("last_seen_ts"))
            if delta:
                mp.append(delta)
            with self._lock:
                self._persona_prompt = "\n\n".join(pp)
                self._memory_prompt = "\n\n".join(mp)
            self._load_rag()
            log.info("Chat avatar: sessione avviata (persona=%s, memoria=%s)",
                     self._persona, "sì" if (profile or recent) else "vuota")
        except Exception as e:  # noqa: BLE001 - non bloccare l'avvio
            log.warning("Chat avatar: caricamento memoria persona fallito: %s", e)

    # --- RAG: sapere della persona (Fase 5) ------------------------------

    def _load_rag(self) -> None:
        """Carica l'indice RAG della persona (se esiste), per il recall a
        conversazione. Best-effort: nessun indice o errore -> RAG spento."""
        self._rag = None
        if not self.use_rag or not self._persona:
            return
        try:
            from .persona_rag import PersonaRag
            store = PersonaRag(self._persona_root(self._persona))
            if store.exists():
                self._rag = store.load()
                log.info("Chat avatar: RAG persona '%s' caricato (%d chunk, %s)",
                         self._persona, len(self._rag.chunks),
                         "semantico" if self._rag.embeddings is not None else "lessicale")
        except Exception as e:  # noqa: BLE001 - RAG opzionale
            log.warning("Chat avatar: RAG non caricato: %s", e)
            self._rag = None

    def _rag_context(self, query: str) -> str:
        """Contesto RAG per il turno (best-effort): recupera i chunk pertinenti
        dal sapere della persona e li impacchetta come system message. '' se il
        RAG è spento, non trova nulla, o fallisce (non blocca mai il turno)."""
        rag = self._rag
        if rag is None:
            return ""
        try:
            from .persona_rag import make_embedder
            from . import llm_backend
            embed_fn = None
            # L'embedding può stare su un provider suo (consumatore "embed"):
            # il modello che vettorizza non è quello che conversa, e potrebbe
            # perfino essere su un altro servizio.
            ep = llm_backend.embed_endpoint(self.cfg)
            if ep and rag.embeddings is not None:
                base, key, model = ep
                from openai import OpenAI
                client = (self._ensure_client() if base == self._client_base_url
                          else OpenAI(base_url=base, api_key=key or "local"))
                embed_fn = make_embedder(client, model)
            top_k = int(getattr(self.cfg, "persona_rag_top_k", 4))
            hits = rag.search(query, top_k, embed_fn=embed_fn)
            if not hits:
                return ""
            joined = "\n\n".join(f"- {(h.get('text') or '')[:1500]}" for h in hits)
            return ("Contesto pertinente dal sapere di "
                    f"{self._persona or 'questa persona'} (usalo se utile, senza "
                    "citarlo alla lettera):\n" + joined)
        except Exception as e:  # noqa: BLE001 - RAG best-effort
            log.debug("Chat avatar: RAG retrieval fallito: %s", e)
            return ""

    def end(self) -> None:
        """Fine sessione: flush memoria in BACKGROUND (riassunto + reconcile del
        profilo locale + push async su wikibridge). Non blocca la chiusura."""
        with self._lock:
            transcript = list(self._transcript)
            persona = self._persona
            project = self._knowledge_project
        if not transcript or not persona:
            return
        threading.Thread(target=self._flush_worker,
                         args=(persona, project, transcript),
                         name="lmstudio-flush", daemon=True).start()

    def _llm_once(self, prompt: str) -> str:
        """Una singola risposta non-streaming da LM Studio (per riassunti/merge)."""
        client = self._ensure_client()
        model = self._resolve_model()
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            timeout=self.cfg.lmstudio_response_timeout_s)
        return (r.choices[0].message.content or "").strip()

    def _flush_worker(self, persona: str, project: str | None,
                      transcript: list[dict]) -> None:
        try:
            convo = "\n".join(
                f"{'Utente' if t['speaker'] == 'user' else persona}: {t['text']}"
                for t in transcript if t.get("text"))
            if not convo.strip():
                return
            summary = self._llm_once(
                "Riassumi questa conversazione in 3-5 frasi, mantenendo fatti, "
                "preferenze e temi in corso utili a riprenderla. Solo il "
                "riassunto, in italiano:\n\n" + convo)
            mem = self._read_memory(persona)
            old_profile = (mem.get("rolling_profile") or "").strip()
            # Reconcile del profilo durevole (stile Mem0, ma con 1 sola call LLM).
            new_profile = self._llm_once(
                "Profilo memoria precedente:\n" + (old_profile or "(vuoto)") +
                "\n\nRiassunto dell'ultima conversazione:\n" + summary +
                "\n\nAggiorna un PROFILO MEMORIA compatto (max ~200 parole) che "
                "conserva i fatti durevoli sull'interlocutore e la relazione, "
                "rimuovendo i dettagli effimeri. Solo il profilo aggiornato:")
            recent = (mem.get("recent_sessions") or [])
            recent.append({"ts": time.time(),
                           "title": time.strftime("%Y-%m-%d %H:%M"),
                           "summary": summary})
            mem.update({
                "version": 1,
                "last_seen_ts": time.time(),
                "rolling_profile": new_profile or old_profile,
                "recent_sessions": recent[-10:],  # tieni le ultime 10
                "stats": {"sessions": int((mem.get("stats") or {}).get("sessions", 0)) + 1},
            })
            self._write_memory(persona, mem)
            log.info("Chat avatar: memoria di '%s' aggiornata (profilo %d char)",
                     persona, len(new_profile or old_profile))
            # Tier-2 (async, grazioso): archivio ricercabile nel wiki dell'avatar.
            if project:
                self._push_wiki(persona, project, summary)
        except Exception as e:  # noqa: BLE001 - flush best-effort
            log.warning("Chat avatar: flush memoria fallito: %s", e)

    def _push_wiki(self, persona: str, project: str, summary: str) -> None:
        try:
            from . import knowledge_client
            import uuid
            knowledge_client.push_meeting(
                self.cfg, project,
                meeting_id=uuid.uuid4().hex[:12],
                title=f"Conversazione {time.strftime('%Y-%m-%d %H:%M')}",
                date=time.strftime("%Y-%m-%d"),
                source="avatar",
                participants=[persona, "utente"],
                analysis={"summary": summary, "highlights": [],
                          "agreements": [], "tasks": []})
        except Exception as e:  # noqa: BLE001 - wikibridge spento: grazioso
            log.debug("Chat avatar: push wiki fallito: %s", e)

    # --- compattazione contesto (Fase 2) ---------------------------------

    def _soglia_compattazione(self) -> int:
        """Token oltre i quali si compatta, DERIVATI dal contesto del modello
        caricato invece che cablati.

        Il modello lo sceglie l'utente di volta in volta e ognuno ha la sua
        finestra: una soglia fissa e' sbagliata per costruzione. Se e' piu'
        ALTA del contesto reale la compattazione non scatta mai e la
        conversazione satura (trovato dal vivo: soglia 131000 su un modello
        caricato a 32768). Si lascia margine per la risposta in arrivo e per
        il RAG del turno, che non sono ancora nella stima. Cosi' il contesto
        scorre da solo qualunque modello si carichi.
        """
        fisso = int(getattr(self.cfg, "lmstudio_context_compaction_tokens", 6000))
        try:
            from . import llm_backend
            reale = llm_backend.lmstudio_context(self.cfg)
        except Exception:  # noqa: BLE001 - backend non interrogabile
            reale = 0
        if reale <= 0:
            return fisso
        quota = float(getattr(self.cfg, "lmstudio_context_compaction_ratio", 0.6))
        derivata = int(reale * max(0.1, min(0.9, quota)))
        # Il valore in config resta un TETTO: chi lo abbassa vuole compattare
        # prima, e non deve vederselo alzare dal contesto del modello.
        return min(fisso, derivata) if fisso > 0 else derivata

    def _maybe_compact(self) -> None:
        """Se la history stimata supera la soglia, riassume in background i turni
        piu' vecchi (rolling summary) tenendo intatti gli ultimi N. Non blocca mai
        il turno: il riassunto avviene in un thread daemon e viene reiniettato
        sotto lock."""
        threshold = self._soglia_compattazione()
        if threshold <= 0 or self._compacting or self._token_est < threshold:
            return
        window = int(getattr(self.cfg, "lmstudio_history_window", 8))
        with self._lock:
            if len(self._history) <= window:
                return
            self._compacting = True
            old = list(self._history[:-window])  # da riassumere
        threading.Thread(target=self._compact_worker, args=(old, window),
                         name="lmstudio-compact", daemon=True).start()

    def _compact_worker(self, old: list[dict], window: int) -> None:
        try:
            client = self._ensure_client()
            model = self._resolve_model()
            convo = "\n".join(
                f"{'Utente' if m['role'] == 'user' else 'Assistente'}: {m['content']}"
                for m in old if m.get("content"))
            prompt = ("Riassumi in modo compatto la seguente conversazione, "
                      "mantenendo fatti, decisioni e temi in corso utili a "
                      "continuarla. Solo il riassunto, in italiano:\n\n" + convo)
            r = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                timeout=self.cfg.lmstudio_response_timeout_s,
            )
            summary = (r.choices[0].message.content or "").strip()
            if not summary:
                return
            with self._lock:
                recent = self._history[-window:]
                self._history = [
                    {"role": "system",
                     "content": "Riassunto della conversazione finora: " + summary}
                ] + recent
                self._token_est = self._est_tokens(
                    [self._system_message()] + self._history)
            log.info("Chat avatar: contesto compattato (%d msg -> riassunto + %d recenti)",
                     len(old), window)
        except Exception as e:  # noqa: BLE001 - compattazione best-effort
            log.warning("Chat avatar: compattazione fallita: %s", e)
        finally:
            self._compacting = False
