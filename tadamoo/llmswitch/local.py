"""Motori LLM locali: LM Studio e vLLM.

Il punto di questo modulo è una regola sola, che vale ovunque ci sia una GPU:

    **un modello già in memoria si usa e basta.**

In VRAM ci sta un modello grande per volta. Caricarne un secondo accanto al
primo non "raddoppia l'occupazione": fallisce, e porta giù la conversazione in
corso. Da qui discende tutto il resto — sapere con certezza cosa è caricato,
non fidarsi degli endpoint che rispondono altro, e non tentare caricamenti che
non entrano.

Trappole vere, trovate sul campo e qui già disinnescate:

- `GET /v1/models` di LM Studio elenca il catalogo su DISCO, non la memoria
  (misurato: 16 voci contro 1 caricata). Chi lo usa per sapere "cosa è caldo"
  finisce per passare all'API un modello non caricato, che viene caricato JIT
  accanto a quello vero → VRAM esaurita. La fonte giusta è `/api/v0/models`,
  che per ogni voce dà `state`.
- Lo stato non è binario: durante il caricamento di un modello da 20 GB vale
  `loading` per minuti. Trattarlo come "niente in memoria" fa partire un
  SECONDO caricamento accanto a quello in arrivo.
- Gli id portano il suffisso di quantizzazione (`nome@iq4_nl`) mentre le GUI
  mostrano il nome nudo. Passare il nome nudo fa risolvere a un'altra variante
  e la carica: id esatto, sempre.
- La CLI `lms` è un client dell'app: `lms ps` e `lms ls` **avviano LM Studio**
  se è spento. Si interrogano solo a server già acceso.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("llmswitch.local")

_LMS_QUICK_TIMEOUT = 25.0     # `lms ps`/`ls`: letture, devono rispondere subito
_LMS_START_TIMEOUT = 60.0     # `lms server start` non attende il boot completo
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # niente console su Windows
_CATALOG_TTL = 120.0          # il catalogo su disco cambia di rado


@dataclass
class LmStudioConfig:
    base_url: str = "http://127.0.0.1:1234/v1"
    #: modello preferito quando la memoria è vuota ("" = nessuna preferenza)
    model: str = ""
    autostart: bool = True
    #: percorso di `lms.exe`; vuoto = si cerca nel PATH e in ~/.lmstudio/bin
    cli_path: str = ""
    #: percorso dell'app, usato se la CLI non basta a far salire il server
    app_path: str = ""
    start_timeout_s: float = 90.0
    load_timeout_s: float = 300.0
    # --- come caricare, quando si carica -------------------------------------------------
    # Senza questi, `lms load` usa i valori di serie di LM Studio, che non sono quelli che
    # servono a un programma solo: `parallel` di serie è 4, e vuol dire che il server accetta
    # quattro richieste insieme. Con due processi vivi — o con un test lanciato di fianco —
    # si servono a vicenda e ciascuno va a metà velocità.
    #: Finestra di contesto. 0 = quella predefinita del modello.
    context_length: int = 0
    #: Richieste servite insieme. 1 = una per volta, le altre in fila.
    parallel: int = 1
    #: Quota da mettere sulla GPU: "max", "off", o una frazione. Vuoto = decide LM Studio.
    gpu: str = "max"
    #: Secondi di inattività dopo i quali scaricare. 0 = mai, e resta finché non lo scarichi tu.
    #: Un TTL scaduto, col caricamento a richiesta disattivato, lascia l'applicazione senza
    #: modello e senza nessuno che lo rimetta: è già successo.
    ttl_s: int = 0


@dataclass
class VllmConfig:
    manager_url: str = "http://127.0.0.1:7860"
    model: str = ""
    ensure_timeout_s: float = 300.0


@dataclass
class Resolution:
    """Esito di `LocalEngines.resolve`."""
    backend: str        #: "lmstudio" | "vllm"
    base_url: str       #: endpoint OpenAI-compatibile
    model: str          #: id ESATTO da passare all'API ("" = niente di usabile)
    #: perché si è scelto così — utile da loggare o mostrare
    reason: str = ""


def human_size(n) -> str:
    """Byte → "19,3 GB" (base 1024). "" se ignoto."""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return (f"{v:.0f} {unit}" if unit in ("B", "KB")
                    else f"{v:.1f} {unit}".replace(".", ","))
        v /= 1024
    return ""


def same_model(a: str, b: str) -> bool:
    """Stesso modello a meno della quantizzazione: `x` == `x@iq4_nl`."""
    na = (a or "").split("@", 1)[0].strip().lower()
    nb = (b or "").split("@", 1)[0].strip().lower()
    return bool(na) and na == nb


def vram_free_bytes(gpu: int = 0) -> int | None:
    """VRAM libera, o None se non misurabile (nvidia-smi assente)."""
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            return None
        righe = (p.stdout or "").strip().splitlines()
        return int(righe[gpu]) * 1024 * 1024
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


class LocalEngines:
    """Accesso ai motori locali con la regola della VRAM applicata.

        eng = LocalEngines(LmStudioConfig(model="qwen3.6-35b"))
        r = eng.resolve()
        client = OpenAI(base_url=r.base_url, api_key="x")
        client.chat.completions.create(model=r.model, ...)
    """

    def __init__(self, lmstudio: LmStudioConfig | None = None,
                 vllm: VllmConfig | None = None, prefer: str = "lmstudio") -> None:
        self.lm = lmstudio or LmStudioConfig()
        self.vllm = vllm or VllmConfig()
        #: quale backend provare per primo quando nessuno dei due è caldo
        self.prefer = prefer
        self._disk_cache: dict[str, dict] = {}
        self._disk_cache_ts = 0.0

    # --- LM Studio: sapere cosa c'è ---------------------------------------

    @property
    def _root(self) -> str:
        base = (self.lm.base_url or "").rstrip("/")
        return base[:-3].rstrip("/") if base.endswith("/v1") else base

    def lms_cli(self) -> str | None:
        if self.lm.cli_path:
            return self.lm.cli_path if Path(self.lm.cli_path).is_file() else None
        found = shutil.which("lms")
        if found:
            return found
        cand = Path.home() / ".lmstudio" / "bin" / "lms.exe"
        return str(cand) if cand.is_file() else None

    def lmstudio_up(self, timeout: float = 2.0) -> bool:
        import requests
        try:
            return requests.get(f"{self.lm.base_url.rstrip('/')}/models",
                                timeout=timeout).ok
        except requests.RequestException:
            return False

    def _states(self) -> dict[str, str] | None:
        """`id -> state` da `/api/v0/models`. None se non interrogabile."""
        meta = self._meta()
        return None if meta is None else {mid: st for mid, (st, _) in meta.items()}

    def _meta(self) -> dict[str, tuple[str, str]] | None:
        """`id -> (state, type)` da `/api/v0/models`. None se non interrogabile.

        Il TIPO serve quanto lo stato: `embeddings` sta in memoria come gli altri ma non
        genera testo, e mandargli una chat dà una risposta vuota o un errore."""
        if not self.lmstudio_up():
            return None
        import requests
        try:
            data = requests.get(self._root + "/api/v0/models", timeout=5).json() or {}
        except (requests.RequestException, ValueError) as e:
            log.debug("/api/v0/models non disponibile: %s", e)
            return None
        entries = data.get("data")
        if not isinstance(entries, list):
            return None
        return {str(m.get("id")).strip(): (str(m.get("state") or "").lower(),
                                           str(m.get("type") or "").lower())
                for m in entries if isinstance(m, dict) and m.get("id")}

    def loaded(self, kind: str = "") -> list[str] | None:
        """Modelli in memoria. **None** = non si è potuto sapere, ben diverso
        da `[]` = nessuno (che autorizzerebbe a caricarne uno).

        `kind="text"` esclude i modelli di embedding: chi cerca con chi CONVERSARE non deve
        ricevere un vettorizzatore, che in memoria ci sta quasi sempre — accanto all'altro,
        non al posto suo."""
        if not self.lmstudio_up():
            return None                      # e NON si sveglia la CLI
        meta = self._meta()
        if meta is not None:
            return [mid for mid, (st, tipo) in meta.items()
                    if st == "loaded" and not (kind == "text" and tipo == "embeddings")]
        cli = self.lms_cli()
        if not cli:
            return None
        if not self._cli_serves_this_address(cli):
            # L'API nativa non risponde e il server di LM Studio non è questo indirizzo: è un altro
            # programma (Basalt, un llama-server…). `lms ps` descriverebbe la memoria di LM Studio,
            # che non c'entra: «non so» (None), mai «nessun modello» — che autorizzerebbe a caricarne uno.
            return None
        data = _lms_json(cli, ["ps"], _LMS_QUICK_TIMEOUT)
        if isinstance(data, dict):
            data = data.get("models") or data.get("loaded") or []
        if data is None:
            return None                      # CLI muta o in timeout
        return _model_ids(data)

    def _cli_serves_this_address(self, cli: str) -> bool:
        """Il server locale di LM Studio ascolta sulla porta di ``base_url``?

        Serve solo quando ``/api/v0/models`` non risponde (LM Studio vecchio, o un altro server):
        ``lms server status`` è una lettura e non sveglia niente. Se la CLI non lo sa dire si
        resta sul comportamento di prima, cioè ci si fida della CLI.
        """
        data = _lms_json(cli, ["server", "status"], _LMS_QUICK_TIMEOUT)
        if not isinstance(data, dict) or "running" not in data:
            return True
        if not data.get("running"):
            return False
        porta = urlparse(self.lm.base_url or "").port
        return porta is None or not data.get("port") or int(data["port"]) == porta

    def busy(self) -> list[str]:
        """Modelli in stato transitorio: stanno già prendendosi la VRAM."""
        stati = self._states() or {}
        return [mid for mid, st in stati.items() if st not in ("loaded", "not-loaded")]

    def catalog(self, ask_cli: bool = True) -> dict[str, dict]:
        """`id -> {quant, size_bytes, params, arch}` dei modelli LM Studio.

        Il PESO lo dà solo `lms ls`, che costa secondi e avvia l'app se è
        spenta: si interroga a server già acceso e si memoizza."""
        meta: dict[str, dict] = {}
        if self.lmstudio_up():
            import requests
            try:
                data = requests.get(self._root + "/api/v0/models", timeout=5).json() or {}
                for m in data.get("data") or []:
                    mid = str((m or {}).get("id") or "").strip()
                    if mid:
                        meta[mid] = {"quant": str(m.get("quantization") or ""),
                                     "arch": str(m.get("arch") or ""),
                                     "size_bytes": 0, "params": ""}
            except (requests.RequestException, ValueError) as e:
                log.debug("catalogo non letto: %s", e)
        fresca = time.monotonic() - self._disk_cache_ts < _CATALOG_TTL
        if self._disk_cache and fresca:
            disco = self._disk_cache
        elif ask_cli and (cli := self.lms_cli()):
            data = _lms_json(cli, ["ls", "--llm"], _LMS_QUICK_TIMEOUT)
            if isinstance(data, dict):
                data = data.get("models") or []
            disco = {}
            for e in data or []:
                if not isinstance(e, dict):
                    continue
                mid = str(e.get("modelKey") or e.get("identifier") or "").strip()
                if not mid:
                    continue
                q = e.get("quantization")
                disco[mid] = {
                    "size_bytes": int(e.get("sizeBytes") or 0),
                    "params": str(e.get("paramsString") or ""),
                    "arch": str(e.get("architecture") or ""),
                    "quant": str((q or {}).get("name") if isinstance(q, dict) else (q or "")),
                }
            if disco:
                self._disk_cache = disco
                self._disk_cache_ts = time.monotonic()
        else:
            disco = self._disk_cache
        for mid, d in disco.items():
            e = meta.setdefault(mid, {"quant": "", "arch": "", "size_bytes": 0, "params": ""})
            e["size_bytes"] = e["size_bytes"] or d.get("size_bytes", 0)
            e["params"] = e["params"] or d.get("params", "")
            e["arch"] = e["arch"] or d.get("arch", "")
            e["quant"] = e["quant"] or d.get("quant", "")
        return meta

    def on_disk(self) -> list[str]:
        cli = self.lms_cli()
        if not cli:
            return []
        data = _lms_json(cli, ["ls", "--llm"], _LMS_QUICK_TIMEOUT)
        if isinstance(data, dict):
            data = data.get("models") or []
        return _model_ids(data)

    def exact_id(self, want: str) -> str:
        """Nome nudo → id con quantizzazione, preferendo quella nota e più
        leggera. Col nome nudo `lms load` prende "the first one" e l'API fa un
        JIT su una variante a caso."""
        want = (want or "").strip()
        if not want or "@" in want:
            return want
        meta = self.catalog()
        varianti = [m for m in meta if same_model(m, want)]
        if not varianti:
            return want
        if len(varianti) == 1:
            return varianti[0]

        def _ordine(mid: str) -> tuple:
            d = meta.get(mid) or {}
            return (0 if d.get("quant") else 1, int(d.get("size_bytes") or 0) or 1 << 62)

        scelto = sorted(varianti, key=_ordine)[0]
        log.info("'%s' ha %d varianti: uso '%s'", want, len(varianti), scelto)
        return scelto

    def fits_in_vram(self, model_id: str) -> tuple[bool, str]:
        """Il modello entra nella VRAM libera? Se peso o VRAM sono ignoti si
        lascia passare: bloccare alla cieca sarebbe peggio."""
        meta = self.catalog().get(model_id) or {}
        peso = int(meta.get("size_bytes") or 0)
        libera = vram_free_bytes()
        if not peso or libera is None:
            return True, ""
        serve = int(peso * 1.15)     # contesto KV e overhead del runtime
        if serve <= libera:
            return True, ""
        return False, (f"servono ~{human_size(serve)} ma restano "
                       f"{human_size(libera)}")

    # --- LM Studio: agire -------------------------------------------------

    def start_lmstudio(self) -> bool:
        if not self.lm.autostart:
            log.info("LM Studio spento e autostart disabilitato")
            return False
        deadline = time.monotonic() + float(self.lm.start_timeout_s)
        cli = self.lms_cli()
        if cli:
            port = ""
            if ":" in (self.lm.base_url or ""):
                port = self.lm.base_url.rsplit(":", 1)[-1].split("/")[0]
            args = [cli, "server", "start"] + (["-p", port] if port.isdigit() else [])
            try:
                subprocess.run(args, capture_output=True, text=True,
                               timeout=_LMS_START_TIMEOUT, creationflags=_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.warning("`lms server start` fallito: %s", e)
            if self._wait_up(deadline):
                return True
        app = self.lm.app_path or _default_app_path()
        if app and Path(app).is_file():
            try:
                subprocess.Popen([app], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                                 | subprocess.DETACHED_PROCESS)
            except OSError as e:
                log.error("avvio app LM Studio fallito: %s", e)
                return False
            return self._wait_up(deadline)
        log.error("LM Studio non avviabile: né CLI né app")
        return False

    def _wait_up(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            if self.lmstudio_up():
                return True
            time.sleep(2.0)
        return False

    def unload(self, model: str = "") -> tuple[bool, str]:
        """Scarica un modello (tutti se `model` è vuoto). Solo su richiesta
        esplicita: questa libreria non scarica mai di propria iniziativa."""
        cli = self.lms_cli()
        if not cli:
            return False, "CLI `lms` non disponibile"
        caldi = self.loaded()
        if caldi is None:
            return False, "stato dei modelli ignoto"
        bersagli = [m for m in caldi if same_model(m, model)] if model else list(caldi)
        if not bersagli:
            return True, "niente da scaricare"
        for m in bersagli:
            try:
                p = subprocess.run([cli, "unload", m], capture_output=True, text=True,
                                   timeout=_LMS_QUICK_TIMEOUT, creationflags=_NO_WINDOW)
                if p.returncode != 0:
                    return False, (p.stderr or p.stdout or "").strip()[:200]
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, str(e)
        return True, "scaricato: " + ", ".join(bersagli)

    def _argomenti_di_carico(self, model_id: str) -> list[str]:
        """`load <modello> --yes` con i parametri della configurazione, non quelli di serie."""
        fuori = ["load", model_id, "--yes"]
        if self.lm.gpu:
            fuori += ["--gpu", self.lm.gpu]
        if self.lm.context_length > 0:
            fuori += ["-c", str(self.lm.context_length)]
        if self.lm.parallel > 0:
            fuori += ["--parallel", str(self.lm.parallel)]
        if self.lm.ttl_s > 0:
            fuori += ["--ttl", str(self.lm.ttl_s)]
        return fuori

    def swap(self, model: str) -> tuple[bool, str]:
        """Scarica il caldo e carica `model` al suo posto (swap CONFERMATO)."""
        ok, msg = self.unload()
        if not ok:
            return False, msg
        cli = self.lms_cli()
        if not cli:
            return False, "CLI `lms` non disponibile"
        esatto = self.exact_id(model)
        try:
            p = subprocess.run([cli, "load", esatto, "--yes"], capture_output=True,
                               text=True, timeout=float(self.lm.load_timeout_s),
                               creationflags=_NO_WINDOW)
            if p.returncode != 0:
                return False, (p.stderr or p.stdout or "").strip()[:200]
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return True, f"in memoria ora c'è '{esatto}'"

    # --- vLLM --------------------------------------------------------------

    def vllm_instances(self) -> list[dict]:
        mgr = (self.vllm.manager_url or "").rstrip("/")
        if not mgr:
            return []
        import requests
        try:
            data = requests.get(mgr + "/api/instances", timeout=4).json() or {}
        except (requests.RequestException, ValueError):
            return []
        insts = [i for i in (data.get("instances") or []) if isinstance(i, dict)]
        active = data.get("active_id")
        insts.sort(key=lambda i: 0 if i.get("id") == active else 1)
        return insts

    @staticmethod
    def _instance_url(inst: dict) -> str:
        # Il manager registra 0.0.0.0, che non è connettibile: si ricostruisce.
        port = inst.get("port")
        return f"http://127.0.0.1:{port}" if port else str(inst.get("url") or "").rstrip("/")

    @staticmethod
    def _alive(url: str) -> bool:
        if not url:
            return False
        import requests
        try:
            return requests.get(url + "/health", timeout=3).ok
        except requests.RequestException:
            return False

    @staticmethod
    def _served(url: str) -> str:
        """Nome con cui l'istanza SERVE il modello: col caricamento da cartella
        `entry.model` è un PATH, e usarlo dà 404 model-not-found."""
        import requests
        try:
            data = requests.get(url + "/v1/models", timeout=5).json() or {}
        except (requests.RequestException, ValueError):
            return ""
        for m in data.get("data") or []:
            if str(m.get("id") or "").strip():
                return str(m["id"]).strip()
        return ""

    def vllm_loadable(self) -> list[str]:
        mgr = (self.vllm.manager_url or "").rstrip("/")
        if not mgr:
            return []
        import requests
        try:
            data = requests.get(mgr + "/api/models/local", timeout=4).json() or {}
        except (requests.RequestException, ValueError):
            return []
        return sorted({str(m.get("model")) for m in (data.get("models") or [])
                       if m.get("loadable") and m.get("model")})

    # --- la regola ---------------------------------------------------------

    def warm(self, prefer: str = "") -> Resolution | None:
        """Primo backend che ha GIÀ un modello in memoria, `prefer` per primo.

        Sonda di sola lettura: non avvia LM Studio e non chiede istanze vLLM."""
        def _lm() -> Resolution | None:
            caldi = self.loaded("text")
            if not caldi:
                return None
            want = (self.lm.model or "").strip()
            esatto = next((m for m in caldi if same_model(m, want)), "") if want else ""
            return Resolution("lmstudio", self.lm.base_url, esatto or caldi[0],
                              "modello già in memoria")

        def _vl() -> Resolution | None:
            for inst in self.vllm_instances():
                url = self._instance_url(inst)
                if not self._alive(url):
                    continue
                model = self._served(url) or str(inst.get("model") or "")
                if model:
                    return Resolution("vllm", f"{url}/v1", model, "istanza già viva")
            return None

        ordine = ("vllm", "lmstudio") if (prefer or self.prefer) == "vllm" \
            else ("lmstudio", "vllm")
        for name in ordine:
            got = _vl() if name == "vllm" else _lm()
            if got:
                return got
        return None

    def resolve(self, prefer: str = "") -> Resolution:
        """(backend, base_url, modello) pronti all'uso.

        1. un modello già in memoria — in QUALUNQUE backend — si usa e basta;
        2. memoria vuota → si carica quello scelto, sul backend preferito.
        """
        scelto = prefer or self.prefer
        caldo = self.warm(scelto)
        if caldo:
            if caldo.backend != scelto:
                caldo.reason = (f"{caldo.backend} ha già '{caldo.model}': si usa "
                                f"quello invece di caricare su {scelto} "
                                "(in VRAM non ci starebbero entrambi)")
            log.info("%s", caldo.reason)
            return caldo
        return self._ensure_vllm() if scelto == "vllm" else self._ensure_lmstudio()

    def _ensure_lmstudio(self) -> Resolution:
        want = (self.lm.model or "").strip()
        if not self.lmstudio_up() and not self.start_lmstudio():
            return Resolution("lmstudio", self.lm.base_url, want, "server non raggiungibile")
        caldi = self.loaded("text")
        if caldi is None:
            log.warning("stato dei modelli ignoto: non chiedo caricamenti")
            return Resolution("lmstudio", self.lm.base_url, want, "stato ignoto")
        if caldi:
            esatto = next((m for m in caldi if same_model(m, want)), "") if want else ""
            return Resolution("lmstudio", self.lm.base_url, esatto or caldi[0],
                              "modello già in memoria")
        in_arrivo = self.busy()
        if in_arrivo:
            # Sta già prendendosi la VRAM: si aspetta lui.
            log.info("LM Studio sta caricando '%s': aspetto", in_arrivo[0])
            deadline = time.monotonic() + float(self.lm.load_timeout_s)
            while time.monotonic() < deadline:
                time.sleep(2.0)
                pronti = self.loaded("text") or []
                if pronti:
                    return Resolution("lmstudio", self.lm.base_url, pronti[0],
                                      "caricamento concluso")
                if not self.busy():
                    break
            return Resolution("lmstudio", self.lm.base_url, in_arrivo[0],
                              "caricamento non concluso")
        if not want:
            return Resolution("lmstudio", self.lm.base_url, "", "nessun modello scelto")
        cli = self.lms_cli()
        if not cli:
            return Resolution("lmstudio", self.lm.base_url, want, "CLI `lms` assente")
        esatto = self.exact_id(want)
        ok, perche = self.fits_in_vram(esatto)
        if not ok:
            log.error("non carico '%s': %s", esatto, perche)
            return Resolution("lmstudio", self.lm.base_url, "", f"VRAM insufficiente: {perche}")
        argomenti = self._argomenti_di_carico(esatto)
        log.info("carico '%s' (%s)", esatto, " ".join(argomenti[2:]) or "valori predefiniti")
        try:
            subprocess.run([cli, *argomenti], capture_output=True, text=True,
                           timeout=float(self.lm.load_timeout_s), creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("`lms load` non completato: %s", e)
        # Si restituisce l'id REALMENTE in memoria: se il caricamento è fallito
        # o è arrivata un'altra variante, mandare quello richiesto ne farebbe
        # partire un secondo.
        dopo = self.loaded("text") or []
        if dopo:
            return Resolution("lmstudio", self.lm.base_url,
                              next((m for m in dopo if same_model(m, esatto)), dopo[0]),
                              "caricato")
        return Resolution("lmstudio", self.lm.base_url, "", "caricamento fallito")

    def _ensure_vllm(self) -> Resolution:
        want = (self.vllm.model or "").strip()
        if not want:
            return Resolution("vllm", "", "", "nessun modello scelto")
        mgr = (self.vllm.manager_url or "").rstrip("/")
        timeout_s = float(self.vllm.ensure_timeout_s)
        import requests
        try:
            body = requests.post(mgr + "/api/ensure", timeout=timeout_s + 15,
                                 json={"model": want, "wait": True,
                                       "timeout_s": timeout_s}).json() or {}
            url = str(body.get("url") or "").rstrip("/")
            if url:
                return Resolution("vllm", f"{url}/v1",
                                  str(body.get("model") or want), "istanza avviata")
        except (requests.RequestException, ValueError) as e:
            log.error("vLLM /ensure fallito: %s", e)
        return Resolution("vllm", "", want, "manager non raggiungibile")

    # --- per i menu --------------------------------------------------------

    def describe(self, model_id: str, meta: dict | None = None) -> dict:
        """Voce pronta per un menu: id + quantizzazione + peso.

        Serve perché lo STESSO modello sta su disco in più quantizzazioni, che
        pesano diversamente: col solo nome non si sa quale si sta scegliendo."""
        meta = meta if meta is not None else self.catalog().get(model_id) or {}
        quant = str(meta.get("quant") or "")
        size = int(meta.get("size_bytes") or 0)
        size_h = human_size(size)
        coda = " · ".join(p for p in (quant, size_h) if p)
        return {"id": model_id, "quant": quant, "size_bytes": size,
                "size_h": size_h, "params": str(meta.get("params") or ""),
                "arch": str(meta.get("arch") or ""),
                "label": f"{model_id} · {coda}" if coda else model_id}

    def menu(self, backend: str = "lmstudio") -> list[dict]:
        """Modelli selezionabili, i già-caldi per primi e marcati."""
        if backend == "vllm":
            vivi = []
            for inst in self.vllm_instances():
                url = self._instance_url(inst)
                if self._alive(url):
                    s = self._served(url) or str(inst.get("model") or "")
                    if s:
                        vivi.append(s)
            tutti = sorted(set(self.vllm_loadable()) | set(vivi))
            return [{"id": m, "label": m, "loaded": m in vivi} for m in tutti]
        caldi = self.loaded() or []
        meta = self.catalog(ask_cli=self.lmstudio_up())
        tutti = sorted(set(self.on_disk()) | set(caldi))
        voci = [dict(self.describe(m, meta.get(m)), loaded=m in caldi) for m in tutti]
        voci.sort(key=lambda v: (not v["loaded"], v["id"].lower()))
        return voci


# --- utilità CLI --------------------------------------------------------------

def _default_app_path() -> str:
    local = os.environ.get("LOCALAPPDATA", "")
    cand = Path(local) / "Programs" / "LM Studio" / "LM Studio.exe"
    return str(cand) if cand.is_file() else ""


def _lms_json(cli: str, args: list[str], timeout: float):
    """`lms <args> --json` → oggetto, o None se non ha risposto."""
    try:
        p = subprocess.run([cli, *args, "--json"], capture_output=True, text=True,
                           timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("lms %s fallito: %s", " ".join(args), e)
        return None
    out = (p.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        # Alcune versioni antepongono righe di log al JSON.
        start = min((i for i in (out.find("["), out.find("{")) if i >= 0), default=-1)
        if start < 0:
            return None
        try:
            return json.loads(out[start:])
        except ValueError:
            return None


def _model_ids(entries) -> list[str]:
    """Id modello da `lms ps/ls --json` (le chiavi cambiano per versione)."""
    ids: list[str] = []
    for e in entries or []:
        if isinstance(e, str):
            ids.append(e)
            continue
        if not isinstance(e, dict):
            continue
        for key in ("identifier", "modelKey", "model_key", "path", "address"):
            val = e.get(key)
            if isinstance(val, str) and val.strip():
                ids.append(val.strip())
                break
    return ids
