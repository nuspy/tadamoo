"""Client verso l'agente Hermes COMPLETO via ACP (Agent Client Protocol).

A differenza di `HermesClient` (che parla col phone-server limitato su HTTP),
questo dialoga con l'**agente completo** — quello con tutti i tool/azioni
(calendario, ricerche, fatture, ecc.). Lancia `hermes acp` in WSL e scambia
JSON-RPC newline-delimited sullo stdio del processo.

Espone `respond(text) -> Iterator[str]` (stessa firma di StubHermes /
HermesSession), quindi `StreamingSession` lo usa come "cervello" senza modifiche.

Flusso ACP (validato): initialize -> session/new -> session/prompt; gli update
`agent_message_chunk` portano il testo della risposta in streaming. Le richieste
inverse dell'agente vengono gestite: `session/request_permission` -> auto-allow
(autonomia), `fs/*` e `terminal/*` -> declinate (l'agente usa i propri tool).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shlex
import subprocess
import threading
import time
from collections.abc import Iterator

from .config import Config

log = logging.getLogger("callbridge.acp")


class AcpAgentClient:
    """Connessione persistente all'agente Hermes completo via ACP/stdio."""

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self._proc: subprocess.Popen[str] | None = None
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        self._next_id = 0
        self._session_id: str | None = None
        self._started = False
        self._lock = threading.Lock()        # un prompt per volta sulla sessione
        self._start_lock = threading.Lock()  # serializza gli avvii (warm-up/respond)
        self._last_fail = 0.0                 # cooldown dopo un avvio fallito
        # Lingua in cui l'agente deve rispondere (nome umano, es. "English").
        # None = usa il preambolo di default (`acp_voice_preamble`, italiano).
        # Impostata dalla sessione PTT al cambio lingua (voce/auto-rilevamento).
        self._reply_lang: str | None = None
        # Modello/provider LLM iniettati nello spawn (`-m MODEL --provider PROV`),
        # ma SOLO con l'override attivo: il flag e' riletto a ogni spawn, cosi'
        # spegnerlo dalla GUI ha effetto senza riavviare il bridge.
        self._model: str | None = (config.acp_model or "").strip() or None
        self._provider: str | None = (config.acp_provider or "").strip() or None
        # Ultimo preambolo (istruzione voce/lingua) gia' comunicato all'agente:
        # si re-invia SOLO quando cambia (primo turno o cambio lingua), non a ogni
        # messaggio -> meno token, contesto pulito, KV cache non invalidata.
        self._last_preamble_sent: str | None = None

    # --- modello: vince quello già in memoria -----------------------------

    @staticmethod
    def _stesso_modello(a: str, b: str) -> bool:
        """Confronto che ignora il suffisso di quantizzazione (`@iq4_nl`)."""
        return (a or "").split("@", 1)[0].strip().lower() == \
               (b or "").split("@", 1)[0].strip().lower()

    def _modello_caldo(self) -> str:
        """Id ESATTO del modello già caricato in LM Studio, "" se non ce n'è.

        Con più modelli caldi si preferisce quello già scelto per l'agente.
        Best-effort: un errore qui non deve impedire l'avvio dell'agente.
        """
        try:
            from . import llm_backend
            caldi = llm_backend.lmstudio_loaded(self.cfg) or []
            if not caldi:
                return ""
            voluto = self._model or ""
            return next((m for m in caldi if self._stesso_modello(m, voluto)), caldi[0])
        except Exception as e:  # noqa: BLE001 - LM Studio spento/non interrogabile
            log.debug("ACP: modelli caricati non leggibili: %s", e)
            return ""

    def _carica_modello(self, voluto: str) -> str:
        """Memoria VUOTA: carica in LM Studio il modello `voluto` e ne
        restituisce l'id ESATTO ("" se non ci si riesce).

        Senza, l'agente parte lo stesso e LM Studio — che non fa JIT — risponde
        `HTTP 400: No models loaded` a ogni turno: la voce tace e l'analisi
        muore sul primo chunk. Caricare qui è sicuro proprio perché in memoria
        non c'è nulla: non si scomoda il modello di nessuno (quello è lo swap, e
        resta una scelta esplicita dell'utente). `ensure_lmstudio` porta con sé
        i controlli già scritti: id esatto, capienza in VRAM, attesa di un
        caricamento altrui già in corso."""
        voluto = (voluto or "").strip()
        if not voluto:
            return ""
        try:
            from . import llm_backend
            llm_backend.ensure_lmstudio(self.cfg, want=voluto)
        except Exception as e:  # noqa: BLE001 - LM Studio spento/non caricabile
            log.warning("ACP: caricamento di '%s' non riuscito: %s", voluto, e)
            return ""
        # Si riguarda cosa c'è DAVVERO in memoria: `ensure_lmstudio` restituisce
        # il modello richiesto anche quando non è riuscita a caricarlo.
        caricato = self._modello_caldo()
        if caricato:
            log.info("ACP: LM Studio era senza modelli: caricato '%s'", caricato)
        else:
            log.error("ACP: LM Studio non ha caricato '%s': l'agente risponderà "
                      "a vuoto finché non c'è un modello in memoria", voluto)
        return caricato

    def _modello_atteso(self) -> str:
        """Modello che l'agente chiederà davvero.

        Con l'override acceso è quello iniettato allo spawn; altrimenti è
        `model.default` del config.yaml di Hermes, che si legge e basta."""
        if bool(getattr(self.cfg, "acp_model_override", True)) and self._model:
            return self._model.strip()
        return ((self.current_model() or {}).get("model") or "").strip()

    def _base_url_del_provider(self, provider: str) -> str:
        """URL che l'agente userebbe davvero con `provider`: quello del provider
        nel registro di Hermes se c'è, altrimenti `model.base_url` (il caso di
        `custom`). "" se non è deducibile — e Hermes non sempre scrive entrambi."""
        import re

        def _campo(blocco: str) -> str:
            fine = re.search(r"^\S", blocco, re.M)
            b = re.search(r"^\s+base_url:\s*(\S+)",
                          blocco[:fine.start() if fine else len(blocco)], re.M)
            return b.group(1).strip("'\"") if b else ""

        try:
            testo = open(self._hermes_config_path(), encoding="utf-8").read()
        except Exception as e:  # noqa: BLE001 - senza config non si giudica
            log.debug("_base_url_del_provider: config.yaml illeggibile: %s", e)
            return ""
        if provider:
            m = re.search(rf"^  {re.escape(provider)}:\s*$", testo, re.M)
            if m:
                trovato = _campo(testo[m.end():])
                if trovato:
                    return trovato
        m = re.search(r"^model:\s*$", testo, re.M)
        return _campo(testo[m.end():]) if m else ""

    def _provider_locale(self, provider: str) -> bool:
        """True SOLO con prova certa che il provider parla con QUESTA macchina.

        Nel dubbio si risponde False e non si carica niente: un provider senza
        base_url deducibile è un builtin di Hermes (z.ai, openrouter, …), cioè
        cloud — mettergli accanto un modello in VRAM sarebbe spreco puro. I
        provider locali (`custom` via `model.base_url`, o uno del registro) un
        base_url ce l'hanno sempre, altrimenti nemmeno Hermes saprebbe dove
        mandare le richieste."""
        base = self._base_url_del_provider(provider)
        if not base:
            return False
        return any(h in base for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))

    def _prepara_modello(self) -> None:
        """Allinea agente e LM Studio PRIMA dello spawn, secondo una sola
        regola: comanda il modello gia' in memoria.

        Del config.yaml di Hermes si tocca il solo `model.default`, e solo
        quando l'override e' acceso: e' il campo che decide cosa l'agente
        chiedera', e tenerlo fermo su un id scritto a mano e' la causa dei
        doppi caricamenti. Il resto del file resta di Hermes.

          - memoria VUOTA  -> si carica il modello che l'agente chiederà;
          - modello CALDO  -> è quello che verrà usato;
          - modello caldo DIVERSO da quello dell'agente -> con l'override ACCESO
            si riallinea `model.default` al caldo (vedi sotto); con l'override
            spento lo si dice nel log e basta, perché il modello dell'agente in
            quel caso lo decide Hermes.

        Perché riallineare invece di avvisare e basta: l'agente `acp` ignora
        `-m`, quindi l'unico modo di farlo seguire il modello in memoria è
        `model.default`. Lasciato fisso, quel campo è una CABLATURA: si carica
        un modello diverso dalla GUI e l'agente continua a chiedere il vecchio,
        così LM Studio ne carica un SECONDO e la VRAM finisce (è esattamente
        come `gemma-4-e2b-it` restava caldo accanto al modello voluto). Con il
        riallineamento vale la stessa regola della chat avatar: si usa quello
        già in memoria, e `acp_model` conta solo a memoria vuota.

        Con un provider remoto non si carica nulla: l'agente parla con un
        servizio esterno e la VRAM locale non c'entra."""
        prov = (self.current_model() or {}).get("provider") or ""
        if not self._provider_locale(prov):
            log.info("ACP: provider '%s' non locale: nessun modello da preparare "
                     "in LM Studio", prov or "?")
            return
        # Cosa l'agente chiederà DAVVERO: `model.default` del config.yaml, non la
        # preferenza di callAPIcall. Confrontare la preferenza sarebbe un errore
        # gia' pagato: con `acp_model` allineato al caldo ma `model.default`
        # fermo su un altro id, il controllo passava e l'agente faceva caricare
        # a LM Studio un SECONDO modello (visto dal vivo: gemma-4-12b caldo
        # accanto a qwen3.5, 12,7 GB + 22 GB).
        chiedera = ((self.current_model() or {}).get("model") or "").strip()
        caldo = self._modello_caldo()
        override = bool(getattr(self.cfg, "acp_model_override", True))
        # Il modello da usare: quello in memoria se c'e', altrimenti la
        # preferenza della GUI — che a memoria vuota si carica.
        target = caldo or self._carica_modello(self._modello_atteso())
        if not target:
            return
        if self._stesso_modello(chiedera, target):
            return
        if not override:
            log.warning("ACP: in memoria c'è '%s' ma l'agente chiederà '%s': o "
                        "LM Studio ne carica un secondo (in VRAM non ci stanno) "
                        "o risponde in errore. Accendi l'override del modello "
                        "nella GUI per riallinearlo da solo, oppure carica '%s' "
                        "in LM Studio.", target, chiedera, chiedera)
            return
        # Riallineamento: si scrive PRIMA dello spawn, così l'agente nasce già
        # puntato al modello giusto. `self._model` segue, altrimenti il prossimo
        # `_modello_atteso()` tornerebbe a proporre quello vecchio.
        if self._write_hermes_model(target, self._provider):
            self._model = target
            log.info("ACP: l'agente chiedeva '%s' ma in memoria c'è '%s': "
                     "riallineato model.default", chiedera or "(vuoto)", target)
        else:
            log.warning("ACP: riallineamento a '%s' non riuscito: l'agente "
                        "chiederà '%s' e LM Studio potrebbe caricarne un secondo",
                        target, chiedera)

    # --- ciclo di vita ----------------------------------------------------

    def warm_up(self) -> None:
        """Avvia l'agente in anticipo (best-effort) E scalda il modello.

        Oltre allo startup (~90s: tool/MCP), manda un MINI-prompt: LM Studio
        carica il modello solo alla PRIMA richiesta (swap ~15-30s se e' caricato
        un altro modello) e il KV cache del system prompt si popola solo allora.
        Pagando questo qui, il primo turno vocale reale parte in ~0.5s invece di
        ~30s."""
        if not self._ensure_started():
            return
        try:
            t0 = time.monotonic()
            n = 0
            for _ in self.respond("Rispondi solo con: pronto.",
                                  preamble="[Avvio a caldo.]\n\n"):
                n += 1
            log.info("ACP: modello scaldato (%d chunk, %.1fs)", n,
                     time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001 - warm-up best-effort
            log.debug("ACP warm-up prompt fallito: %s", e)

    def prime(self, message: str) -> None:
        """Invia un messaggio di INIT/contesto di sessione e SCARTA la risposta:
        avvisa l'agente del modo (vocale/avatar/chiamata) e delle skill da usare,
        senza che nulla venga pronunciato dal TTS. Best-effort."""
        if not message or not message.strip():
            return
        try:
            n = 0
            for _ in self.respond(message,
                                  preamble="[Setup sessione, rispondi solo: ok.]\n\n"):
                n += 1
            log.info("ACP: sessione inizializzata (init inviato, risposta scartata)")
        except Exception as e:  # noqa: BLE001
            log.debug("ACP prime fallito: %s", e)

    @property
    def is_ready(self) -> bool:
        """True se l'agente e' avviato e il processo WSL e' vivo (per /diag/acp)."""
        return self._started and self._proc is not None and self._proc.poll() is None

    def _start_locked(self) -> None:
        popen_kw: dict = {}
        if self.cfg.acp_use_wsl:
            cmd = ["wsl.exe", "-d", self.cfg.acp_wsl_distro, "--",
                   "bash", "-lc", self.cfg.acp_wsl_command]
            session_cwd = self.cfg.acp_cwd
            log.info("ACP: avvio agente in WSL (%s) — fino a %.0fs",
                     self.cfg.acp_wsl_distro, self.cfg.acp_init_timeout_s)
        else:
            # Hermes nativo su Windows: lancio diretto dell'eseguibile, niente
            # wsl.exe. shlex(posix=False) tiene i backslash dei path Windows.
            cmd = [t.strip('"') for t in
                   shlex.split(self.cfg.acp_native_command, posix=False)]
            # Inietta modello/provider come flag GLOBALI, prima del subcomando
            # `acp` (usage: hermes [-m MODEL] [--provider PROV] {acp,...}).
            # Con l'override spento non si inietta nulla: il modello dell'agente
            # e' quello del config.yaml di Hermes.
            # Si prepara LM STUDIO al modello che l'agente chiederà (memoria
            # vuota -> lo si carica). Il config.yaml di Hermes si legge soltanto:
            # è suo, non nostro. Se in memoria c'è un modello DIVERSO da quello
            # dell'agente resta il rischio noto — LM Studio ne caricherebbe un
            # secondo e in VRAM non ci stanno — ma la scelta di riallineare i due
            # è dell'utente, dal pannello modelli della GUI.
            self._prepara_modello()
            extra: list[str] = []
            override = bool(getattr(self.cfg, "acp_model_override", True))
            if override:
                if self._model:
                    extra += ["-m", self._model]
                if self._provider:
                    extra += ["--provider", self._provider]
            else:
                log.info("ACP: override del modello spento, avvio senza -m/--provider")
            if extra and cmd:
                cmd = [cmd[0]] + extra + cmd[1:]
            session_cwd = self.cfg.acp_cwd_native
            # hermes.exe e' una console-app: lanciata dal supervisore PTT
            # (nascosto/senza console) aprirebbe un terminale -> CREATE_NO_WINDOW.
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            env = os.environ.copy()
            if self.cfg.acp_hermes_home:
                env["HERMES_HOME"] = self.cfg.acp_hermes_home
            popen_kw["env"] = env
            if self.cfg.acp_cwd_native:
                popen_kw["cwd"] = self.cfg.acp_cwd_native
            log.info("ACP: avvio agente nativo (%s) — fino a %.0fs",
                     cmd[0] if cmd else "?", self.cfg.acp_init_timeout_s)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            **popen_kw,
        )
        self._inbox = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

        init = self._rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "callapicall", "version": "0.1"},
        }, timeout=self.cfg.acp_init_timeout_s)
        if init is None:
            raise RuntimeError("ACP initialize fallito/timeout")
        sess = self._rpc("session/new", {"cwd": session_cwd, "mcpServers": []},
                         timeout=self.cfg.acp_init_timeout_s)
        self._session_id = str((sess or {}).get("sessionId") or "").strip()
        if not self._session_id:
            raise RuntimeError("ACP session/new senza sessionId")
        # Agente appena (ri)nato: nessun preambolo ancora comunicato.
        self._last_preamble_sent = None
        self._started = True
        log.info("ACP: sessione agente pronta (%s)", self._session_id)

    def _ensure_started(self) -> bool:
        if self._started and self._proc is not None and self._proc.poll() is None:
            return True
        from .sospensione import in_sospensione
        if in_sospensione():
            return False         # da sospeso l'agente non si avvia (Riprendi)
        # Cooldown: dopo un avvio fallito non ritentare ad ogni enunciato
        # (ogni tentativo costa fino a acp_init_timeout_s).
        if time.monotonic() - self._last_fail < 30.0:
            return False
        with self._start_lock:
            if self._started and self._proc is not None and self._proc.poll() is None:
                return True
            self.close()
            try:
                self._start_locked()
                return True
            except Exception as e:  # noqa: BLE001 - agente non avviabile
                log.error("ACP: avvio agente fallito: %s", e)
                self._last_fail = time.monotonic()
                # Reap del processo eventualmente spawnato ma non inizializzato:
                # senza, un hermes.exe resterebbe orfano in RAM fino al prossimo
                # tentativo (che chiama close() solo allora).
                self.close()
                return False

    def close(self) -> None:
        self._started = False
        self._session_id = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
            self._proc = None

    def end(self) -> None:
        """Compat con l'interfaccia delle sessioni: la sessione ACP e'
        persistente (mantiene il contesto), quindi qui non si chiude nulla."""
        return None

    # --- controllo runtime dalla sessione voce ---------------------------

    def set_reply_language(self, name: str | None) -> None:
        """Imposta la lingua in cui l'agente deve rispondere (nome umano, es.
        "English", "français"). None = torna al preambolo di default (italiano).

        Serve perche' il preambolo vocale di default e' cablato sull'italiano:
        al cambio lingua (comando o auto-rilevamento) la sessione lo aggiorna,
        cosi' l'agente risponde nella lingua giusta e non solo la voce TTS."""
        self._reply_lang = (name or "").strip() or None

    def _hermes_config_path(self) -> str:
        return os.path.join(self.cfg.acp_hermes_home or "", "config.yaml")

    def current_model(self) -> dict:
        """Modello/provider LLM EFFETTIVI dell'agente = `model.default`/`provider`
        del config.yaml di Hermes (l'agente `acp` IGNORA il flag `-m`)."""
        import re
        model, provider = self._model, self._provider
        try:
            text = open(self._hermes_config_path(), encoding="utf-8").read()
            in_model = False
            for ln in text.splitlines():
                if re.match(r"^model:\s*$", ln):
                    in_model = True
                    continue
                if in_model:
                    if re.match(r"^\S", ln):
                        break
                    m = re.match(r"^\s+default:\s*(.+?)\s*$", ln)
                    if m:
                        model = m.group(1).strip().strip("'\"")
                    p = re.match(r"^\s+provider:\s*(.+?)\s*$", ln)
                    if p:
                        provider = p.group(1).strip().strip("'\"")
        except Exception as e:  # noqa: BLE001
            log.debug("current_model: lettura config.yaml fallita: %s", e)
        return {"model": model, "provider": provider}

    def _write_hermes_model(self, model: str, provider: str | None) -> bool:
        """Scrive `model.default` (e `provider`) nel config.yaml di Hermes: è la
        VERA fonte del modello per l'agente `acp` (che ignora `-m`). Edit mirato
        per riga, così i commenti/il resto restano intatti."""
        import re
        path = self._hermes_config_path()
        try:
            lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
        except Exception as e:  # noqa: BLE001
            log.warning("_write_hermes_model: config.yaml illeggibile: %s", e)
            return False
        in_model, done = False, False
        for i, ln in enumerate(lines):
            if re.match(r"^model:\s*$", ln):
                in_model = True
                continue
            if in_model:
                if re.match(r"^\S", ln):
                    break
                if re.match(r"^\s+default:", ln):
                    ind = ln[: len(ln) - len(ln.lstrip())]
                    lines[i] = f"{ind}default: {model}\n"
                    done = True
                elif provider and re.match(r"^\s+provider:", ln):
                    ind = ln[: len(ln) - len(ln.lstrip())]
                    lines[i] = f"{ind}provider: {provider}\n"
        if done:
            try:
                open(path, "w", encoding="utf-8").write("".join(lines))
            except Exception as e:  # noqa: BLE001
                log.warning("_write_hermes_model: scrittura fallita: %s", e)
                return False
        return done

    def set_model(self, model: str, provider: str | None = None) -> bool:
        """Cambia il modello LLM dell'agente: scrive `config.yaml model.default`
        (fonte reale per `acp`, il `-m` è ignorato) e RIAVVIA l'agente (close ->
        respawn al prossimo turno). Ritorna True se il config è stato aggiornato.

        No-op con l'override spento: il modello dell'agente lo decide Hermes, e
        scrivergli il config.yaml alle spalle sarebbe proprio cio' che l'utente
        ha chiesto di non fare."""
        if not bool(getattr(self.cfg, "acp_model_override", True)):
            log.info("ACP: cambio modello ignorato (override del modello Hermes spento)")
            return False
        model = (model or "").strip()
        with self._start_lock:
            self._model = model or None
            if provider is not None:
                self._provider = (provider or "").strip() or None
            ok = self._write_hermes_model(self._model, self._provider)
            log.info("ACP: modello -> %s (provider=%s) in config.yaml=%s; "
                     "riavvio agente", self._model, self._provider, ok)
            self.close()
        return ok

    def reset_session(self) -> bool:
        """Apre una nuova sessione ACP sullo stesso processo agente: azzera il
        contesto della conversazione (comando vocale "nuova conversazione").
        Ritorna True se e' stata creata una nuova sessione."""
        with self._lock:
            if not (self._started and self._proc is not None
                    and self._proc.poll() is None):
                return False
            self._drain_stale()
            cwd = self.cfg.acp_cwd if self.cfg.acp_use_wsl else self.cfg.acp_cwd_native
            try:
                sess = self._rpc("session/new", {"cwd": cwd, "mcpServers": []},
                                 timeout=self.cfg.acp_init_timeout_s)
            except Exception as e:  # noqa: BLE001 - processo morto
                log.warning("ACP reset_session fallito: %s", e)
                return False
            sid = str((sess or {}).get("sessionId") or "").strip()
            if not sid:
                log.warning("ACP reset_session: session/new senza sessionId")
                return False
            self._session_id = sid
            # Contesto azzerato: l'agente non ricorda piu' il preambolo -> va
            # ri-comunicato al primo turno della nuova sessione.
            self._last_preamble_sent = None
            log.info("ACP: nuova sessione (contesto azzerato) %s", sid)
            return True

    def _voice_preamble(self) -> str:
        """Preambolo vocale, adattato alla lingua di risposta se impostata."""
        if self._reply_lang:
            return (f"[Voice conversation: reply in {self._reply_lang}, "
                    "short and natural.]\n\n")
        return self.cfg.acp_voice_preamble

    # --- I/O di basso livello --------------------------------------------

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except Exception:  # noqa: BLE001 - riga non-JSON (log dell'agente)
                pass

    def _stderr_reader(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            log.debug("acp[stderr] %s", line.rstrip())

    def _nid(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, method: str, params: dict, mid: int | None = None) -> None:
        obj: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if mid is not None:
            obj["id"] = mid
        self._proc.stdin.write(json.dumps(obj) + "\n")  # type: ignore[union-attr]
        self._proc.stdin.flush()  # type: ignore[union-attr]

    def _reply(self, mid, result=None, error=None) -> None:
        obj: dict = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            obj["error"] = error
        else:
            obj["result"] = result
        self._proc.stdin.write(json.dumps(obj) + "\n")  # type: ignore[union-attr]
        self._proc.stdin.flush()  # type: ignore[union-attr]

    def _handle_reverse(self, msg: dict) -> bool:
        """Gestisce le richieste inverse dell'agente. True se gestita."""
        method = msg.get("method")
        mid = msg.get("id")
        if method == "session/request_permission":
            opts = (msg.get("params") or {}).get("options") or []
            allow = next(
                (o for o in opts
                 if "allow" in str(o.get("kind", "")).lower()
                 or "allow" in str(o.get("optionId", "")).lower()),
                None,
            )
            if allow:
                self._reply(mid, {"outcome": {"outcome": "selected",
                                              "optionId": allow.get("optionId")}})
            else:
                self._reply(mid, {"outcome": {"outcome": "cancelled"}})
            return True
        if method and (method.startswith("fs/") or method.startswith("terminal/")):
            # Il client non fornisce fs/terminal: l'agente usa i propri tool.
            self._reply(mid, error={"code": -32601,
                                    "message": "client non fornisce fs/terminal"})
            return True
        return False

    def _rpc(self, method: str, params: dict, timeout: float):
        """Richiesta bloccante: ritorna `result` (gestendo le richieste inverse
        e ignorando gli update); None su errore/timeout/processo morto."""
        rid = self._nid()
        self._send(method, params, mid=rid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return None
            try:
                msg = self._inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if msg.get("method") == "session/update":
                continue  # nessun testo da raccogliere durante init/new
            if self._handle_reverse(msg):
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    log.error("ACP %s: %s", method, msg["error"])
                    return None
                return msg.get("result")
        log.warning("ACP: timeout su %s", method)
        return None

    # --- API "cervello" ---------------------------------------------------

    def respond(self, text: str, preamble: str | None = None,
                timeout_s: float | None = None) -> Iterator[str]:
        """Invia l'enunciato all'agente e yielda i chunk di risposta (testo).

        `preamble` sovrascrive il preambolo vocale di default (usato es. dal
        copilota, che ha istruzioni diverse). None = `cfg.acp_voice_preamble`.

        `timeout_s` sovrascrive `acp_response_timeout_s`, che è tarato sulla
        VOCE: in conversazione 5 minuti di silenzio sono un guasto, mentre a
        un'analisi che macina un transcript con un modello "thinking" possono
        non bastare — e allo scadere si esce con le mani vuote, che a chi
        chiama sembra un modello che non risponde niente.

        Se il chiamante abbandona l'iteratore (barge-in/stop) o si va in
        timeout, il turno viene CANCELLATO lato agente (`session/cancel`):
        senza, i chunk del turno vecchio (che non hanno un id di richiesta)
        finirebbero pronunciati nel turno successivo.
        """
        with self._lock:
            if not self._ensure_started():
                # Non restare muti: un messaggio parlabile fa capire all'utente
                # (voce/telefono) e alla GUI che l'agente non e' disponibile.
                yield "Scusa, l'assistente non è al momento disponibile."
                return
            self._drain_stale()
            if preamble is None:
                # Preambolo voce/lingua di default: si comunica SOLO quando cambia
                # (primo turno o cambio lingua). L'agente lo mantiene per tutta la
                # sessione (e compatta il contesto da solo), quindi ripeterlo a
                # ogni turno sprecherebbe token, ridurrebbe il contesto libero e
                # invaliderebbe il KV cache di LM Studio.
                pre = self._voice_preamble()
                if pre == self._last_preamble_sent:
                    pre = ""
                else:
                    self._last_preamble_sent = pre
            else:
                # Preambolo esplicito (copilota, prime): sempre inviato, non tocca
                # il tracciamento del preambolo vocale.
                pre = preamble
            prompt_text = (pre or "") + text
            rid = self._nid()
            self._send("session/prompt", {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": prompt_text}],
            }, mid=rid)

            finished = False
            limite = float(timeout_s or self.cfg.acp_response_timeout_s)
            try:
                deadline = time.monotonic() + limite
                while time.monotonic() < deadline:
                    if self._proc is None or self._proc.poll() is not None:
                        log.error("ACP: processo terminato durante la risposta")
                        finished = True
                        return
                    try:
                        msg = self._inbox.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if msg.get("method") == "session/update":
                        upd = (msg.get("params") or {}).get("update") or {}
                        if upd.get("sessionUpdate") == "agent_message_chunk":
                            content = upd.get("content") or {}
                            t = str(content.get("text") or "") if isinstance(content, dict) else ""
                            if t:
                                yield t
                        continue
                    if self._handle_reverse(msg):
                        continue
                    if msg.get("id") == rid:
                        if "error" in msg:
                            log.error("ACP prompt: %s", msg["error"])
                        finished = True
                        return  # fine turno (stopReason)
                log.warning("ACP: timeout risposta dopo %.0fs", limite)
            finally:
                if not finished:
                    self._cancel_turn(rid)

    def _drain_stale(self) -> None:
        """Scarta update/risposte rimasti in coda da un turno precedente
        (rispondendo comunque alle eventuali richieste inverse)."""
        n = 0
        try:
            while True:
                msg = self._inbox.get_nowait()
                if not self._handle_reverse(msg):
                    n += 1
        except queue.Empty:
            pass
        if n:
            log.debug("ACP: scartati %d messaggi stantii del turno precedente", n)

    def _cancel_turn(self, rid: int) -> None:
        """Turno abbandonato (barge-in/stop/timeout): chiede all'agente di
        interrompere e attende (breve) la chiusura del prompt in corso."""
        try:
            self._send("session/cancel", {"sessionId": self._session_id})
        except Exception as e:  # noqa: BLE001 - processo morto
            log.debug("ACP cancel: %s", e)
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return
            try:
                msg = self._inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._handle_reverse(msg):
                continue
            if msg.get("id") == rid:
                return  # stopReason (cancelled): turno chiuso pulito
