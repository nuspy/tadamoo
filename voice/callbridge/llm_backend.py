"""Prontezza del backend LLM della CHAT AVATAR (Ctrl+Shift+F4/F5).

Due backend alternativi, scelti dalla GUI (`avatar_llm_backend`):

  - **lmstudio** — server OpenAI-compatibile di LM Studio su `lmstudio_base_url`.
  - **vllm**     — istanza vLLM avviata/riusata dal manager (`vllm_manager_url`).

Regole applicate a ogni warm-up/inizio sessione, in quest'ordine:

  1. un modello e' GIA' in memoria -> **si usa quello**, qualunque sia e in
     QUALUNQUE dei due backend stia (anche se in GUI ne risulta un altro, e
     anche se il backend caldo non e' quello selezionato).
  2. nessun modello in memoria     -> si carica quello scelto in GUI
     (`lmstudio_model` / `vllm_model`) sul backend selezionato, avviandolo se
     serve (LM Studio: `lms server start`; vLLM: il manager e' sempre su).

La regola 1 e' una scelta esplicita: la VRAM tiene UN modello grande per volta,
quindi caricarne un secondo accanto a uno gia' caldo non "raddoppia" niente —
fallisce, e porta giu' la conversazione. Chi vuole cambiare modello lo scarica
dal backend (o usa il pulsante di swap della GUI), non se lo vede imporre
all'inizio di una sessione vocale.

Corollario pratico: quale modello sia caricato lo si deve sapere con CERTEZZA.
Vedi `lmstudio_loaded`: `/v1/models` NON e' una fonte valida (elenca il catalogo
su disco, non la memoria) e prenderlo per tale e' costato caricamenti JIT a
vuoto.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("callbridge.llm_backend")

# Comandi CLI di sola lettura (ps/ls/status): devono rispondere subito.
_LMS_QUICK_TIMEOUT = 25.0
# `lms server start` non attende il boot completo, ma nemmeno deve appendersi.
_LMS_START_TIMEOUT = 60.0

# Su Windows i subprocess CLI non devono far lampeggiare una console.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- selezione backend --------------------------------------------------------

def selected_backend(cfg, consumer: str = "chat") -> str:
    """"lmstudio" | "vllm". Campo vuoto = auto, retrocompatibile col
    comportamento storico (vLLM se `vllm_model` e' valorizzato)."""
    # Il tab "LLM Providers" e' la fonte piu' recente: se ha assegnato a questo
    # consumatore un provider LOCALE, comanda quello. I remoti passano da
    # `remote_provider()` e non da qui.
    try:
        from . import llm_providers
        prov = llm_providers.provider_for(cfg, consumer)
        if prov and prov.get("type") in ("lmstudio", "vllm"):
            return str(prov["type"])
    except Exception as e:  # noqa: BLE001 - registro assente/illeggibile
        log.debug("registro provider non letto: %s", e)
    choice = (getattr(cfg, "avatar_llm_backend", "") or "").strip().lower()
    if choice in ("lmstudio", "vllm"):
        return choice
    return "vllm" if (getattr(cfg, "vllm_model", "") or "").strip() else "lmstudio"


def remote_provider(cfg, consumer: str = "chat") -> dict | None:
    """Provider REMOTO assegnato a `consumer` (Gemini, Claude, OpenAI, …).

    None quando il consumatore usa un motore locale: in quel caso decide
    `resolve()`, con la sua regola sulla VRAM. I remoti non hanno quel
    problema — non c'e' niente da caricare — quindi si usano e basta.
    """
    try:
        from . import llm_providers
        prov = llm_providers.provider_for(cfg, consumer)
        if not prov:
            return None
        spec = llm_providers.TYPES.get(prov.get("type") or "", {})
        if spec.get("local"):
            return None
        # Per chattare serve l'endpoint OpenAI-compatibile, che su Gemini e
        # Anthropic non e' quello che elenca i modelli.
        base = ((prov.get("base_url") or "").strip()
                or spec.get("chat_base_url") or spec.get("base_url") or "").strip()
        key = ((prov.get("oauth") or {}).get("access_token")
               or prov.get("api_key") or "").strip()
        return {"id": prov.get("id"), "type": prov.get("type"), "base_url": base,
                "api_key": key, "model": (prov.get("model") or "").strip(),
                "api": spec.get("api", "openai")}
    except Exception as e:  # noqa: BLE001
        log.debug("provider remoto non risolto: %s", e)
        return None


# --- LM Studio ----------------------------------------------------------------

def lms_cli(cfg) -> str | None:
    """Percorso di `lms.exe` (CLI di LM Studio) o None se non installata."""
    explicit = (getattr(cfg, "lmstudio_lms_cli", "") or "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = shutil.which("lms")
    if found:
        return found
    cand = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    return str(cand) if cand.is_file() else None


def _lms_app(cfg) -> str | None:
    """Percorso dell'app LM Studio (fallback se la CLI non basta)."""
    explicit = (getattr(cfg, "lmstudio_app_path", "") or "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    local = os.environ.get("LOCALAPPDATA", "")
    cand = Path(local) / "Programs" / "LM Studio" / "LM Studio.exe"
    return str(cand) if cand.is_file() else None


def _lms_json(cli: str, args: list[str], timeout: float) -> list | dict | None:
    """Esegue `lms <args> --json` e ne restituisce il JSON, o None."""
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
            log.debug("lms %s: output non JSON", " ".join(args))
            return None


def _model_ids(entries) -> list[str]:
    """Estrae gli id modello da `lms ps/ls --json` (chiavi variabili per versione)."""
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


def lmstudio_up(cfg, timeout: float = 2.0) -> bool:
    """True se il server LM Studio risponde su `lmstudio_base_url`."""
    import requests
    try:
        r = requests.get(f"{cfg.lmstudio_base_url.rstrip('/')}/models", timeout=timeout)
        return r.ok
    except requests.RequestException:
        return False


def _lmstudio_root(cfg) -> str:
    """Radice del server LM Studio senza il suffisso OpenAI `/v1`."""
    base = (cfg.lmstudio_base_url or "").rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def _lmstudio_states(cfg) -> dict[str, str] | None:
    """`id -> state` da `/api/v0/models`. None se non interrogabile."""
    if not lmstudio_up(cfg):
        return None
    import requests
    try:
        data = requests.get(_lmstudio_root(cfg) + "/api/v0/models", timeout=5).json() or {}
    except (requests.RequestException, ValueError) as e:
        log.debug("/api/v0/models non disponibile: %s", e)
        return None
    entries = data.get("data")
    if not isinstance(entries, list):
        return None
    return {str(m.get("id")).strip(): str(m.get("state") or "").lower()
            for m in entries if isinstance(m, dict) and m.get("id")}


def lmstudio_busy(cfg) -> list[str]:
    """Modelli in stato TRANSITORIO (tipicamente in caricamento).

    Durante il caricamento di un modello da 20 GB lo stato non e' ancora
    `loaded`: leggerlo come "memoria vuota" faceva partire un SECONDO
    caricamento accanto al primo. Qualunque stato diverso da `not-loaded` e
    `loaded` vale come "sta gia' occupando la VRAM, non toccare".
    """
    stati = _lmstudio_states(cfg) or {}
    return [mid for mid, st in stati.items() if st not in ("loaded", "not-loaded")]


def lmstudio_context(cfg) -> int:
    """Finestra di contesto del modello CARICATO, in token. 0 se ignota.

    Serve a chi deve compattare la conversazione: la soglia non puo' essere
    un numero fisso, perche' il modello lo sceglie l'utente di volta in volta
    e ognuno viene caricato con la sua finestra. Con una soglia cablata piu'
    ALTA del contesto reale la compattazione non scatta mai e la
    conversazione satura (visto: soglia 131000 token su un modello caricato
    a 32768).
    """
    import requests
    try:
        data = requests.get(_lmstudio_root(cfg) + "/api/v0/models", timeout=5).json() or {}
    except Exception as e:  # noqa: BLE001 - server giu'/API muta
        log.debug("contesto del modello non leggibile: %s", e)
        return 0
    for m in (data.get("data") or []):
        if str(m.get("state") or "") == "loaded":
            try:
                return int(m.get("loaded_context_length") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def lmstudio_loaded(cfg) -> list[str] | None:
    """Modelli attualmente CARICATI in memoria, o **None** se non si e' riusciti
    a saperlo (server giu', API muta): None = "non lo so", ben diverso da []
    = "nessuno caricato", che autorizzerebbe a caricarne uno.

    Fonte: `GET /api/v0/models` (REST nativo di LM Studio), che per ogni voce
    da' `state: loaded | not-loaded`. Fallback `lms ps` per le versioni senza
    `/api/v0`.

    `/v1/models` NON e' una fonte valida e non va reintrodotto: elenca l'intero
    catalogo su DISCO (misurato: 16 voci contro 1 sola caricata). Usarlo faceva
    risultare "gia' caldo" il modello scelto in GUI, che veniva quindi passato a
    `/v1/chat/completions` -> LM Studio provava a caricarlo JIT ACCANTO a quello
    davvero in memoria -> "Failed to load model ... Engine protocol startup was
    aborted" e conversazione a terra.
    """
    if not lmstudio_up(cfg):
        # Server giu': niente da sapere. E soprattutto NON si interroga la CLI,
        # che essendo un client dell'app la farebbe partire.
        return None
    stati = _lmstudio_states(cfg)
    if stati is not None:
        return [mid for mid, st in stati.items() if st == "loaded"]
    cli = lms_cli(cfg)
    if not cli:
        return None
    data = _lms_json(cli, ["ps"], _LMS_QUICK_TIMEOUT)
    if isinstance(data, dict):
        data = data.get("models") or data.get("loaded") or []
    if data is None:
        return None            # CLI muta o in timeout: "non lo so"
    return _model_ids(data)


def _same_model(a: str, b: str) -> bool:
    """Stesso modello a meno del suffisso di quantizzazione (`@iq4_nl`, `@?`).

    In GUI si sceglie `qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive`, in
    memoria l'id e' `...@iq4_nl`: senza questo confronto sono due stringhe
    diverse e il modello caldo non verrebbe riconosciuto.
    """
    na = (a or "").split("@", 1)[0].strip().lower()
    nb = (b or "").split("@", 1)[0].strip().lower()
    return bool(na) and na == nb


def lmstudio_on_disk(cfg) -> list[str]:
    """Modelli LLM presenti su disco (`lms ls --llm`), per il dropdown GUI."""
    cli = lms_cli(cfg)
    if not cli:
        return []
    data = _lms_json(cli, ["ls", "--llm"], _LMS_QUICK_TIMEOUT)
    if isinstance(data, dict):
        data = data.get("models") or []
    return _model_ids(data)


# --- metadati per i menu a tendina --------------------------------------------
#
# Nei selettori il solo id non basta: lo STESSO modello e' presente in piu'
# quantizzazioni (`...aggressive@iq4_nl` accanto a `...aggressive@?`), che
# pesano diversamente e non ci stanno tutte in VRAM. Quantizzazione e peso
# viaggiano quindi accanto a ogni voce.

def human_size(n) -> str:
    """Byte -> "19,3 GB" (base 1024, come Esplora risorse). "" se ignoto."""
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


def _quant_from_name(name: str) -> str:
    """Quantizzazione dedotta dal NOME del repo (vLLM non la espone altrove):
    `...-AWQ-W4A16`, `...-AWQ-4bit`, `...-GPTQ-Int4`, `...-FP8`."""
    import re
    tokens = re.findall(
        r"(?i)\b(AWQ(?:[-_]?W\d+A\d+)?|GPTQ(?:[-_]?Int\d+)?|W\d+A\d+|"
        r"FP8|FP16|BF16|INT8|INT4|\d+bit|[IQ]Q?\d[_A-Z0-9]*)\b",
        name or "")
    return "-".join(dict.fromkeys(t.upper() for t in tokens)) if tokens else ""


_disk_cache: dict[str, dict] = {}       # `lms ls` memoizzato (vedi _CATALOG_TTL)
_disk_cache_ts = 0.0
_CATALOG_TTL = 120.0                    # il catalogo su disco cambia di rado


def lmstudio_catalog(cfg, ask_cli: bool = True) -> dict[str, dict]:
    """`id -> {quant, size_bytes, params, arch}` per i modelli LM Studio.

    `lms ls` da' il PESO (l'unica fonte che ce l'ha) ma costa secondi e, essendo
    un client dell'app, la AVVIA se e' spenta: si interroga solo con LM Studio
    gia' acceso, e il risultato si tiene in cache per `_CATALOG_TTL`.
    `/api/v0/models` da' quantizzazione e stato (senza peso) a ogni chiamata.
    """
    global _disk_cache_ts
    meta: dict[str, dict] = {}
    if lmstudio_up(cfg):
        import requests
        try:
            data = requests.get(_lmstudio_root(cfg) + "/api/v0/models", timeout=5).json() or {}
            for m in data.get("data") or []:
                mid = str((m or {}).get("id") or "").strip()
                if mid:
                    meta[mid] = {"quant": str(m.get("quantization") or ""),
                                 "arch": str(m.get("arch") or ""),
                                 # "llm" | "vlm" | "embeddings": serve a NON
                                 # proporre un modello di chat dove ne va uno
                                 # che vettorizza, e viceversa.
                                 "kind": str(m.get("type") or ""),
                                 "size_bytes": 0,
                                 "params": ""}
        except (requests.RequestException, ValueError) as e:
            log.debug("catalogo /api/v0/models non letto: %s", e)
    fresca = time.monotonic() - _disk_cache_ts < _CATALOG_TTL
    if _disk_cache and fresca:
        disco = _disk_cache
    elif ask_cli and (cli := lms_cli(cfg)):
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
            _disk_cache.clear()
            _disk_cache.update(disco)
            _disk_cache_ts = time.monotonic()
    else:
        disco = _disk_cache          # eventuale cache scaduta: meglio di niente
    for mid, d in disco.items():
        entry = meta.setdefault(mid, {"quant": "", "arch": "", "size_bytes": 0,
                                      "params": "", "kind": ""})
        entry["size_bytes"] = entry["size_bytes"] or d.get("size_bytes", 0)
        entry["params"] = entry["params"] or d.get("params", "")
        entry["arch"] = entry["arch"] or d.get("arch", "")
        entry["quant"] = entry["quant"] or d.get("quant", "")
    return meta


def _dir_size(path: str) -> int:
    """Peso di una cartella modello (vLLM tiene i pesi in piu' safetensors)."""
    import os
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        return 0
    return total


_vllm_size_cache: dict[str, int] = {}


def vllm_catalog(cfg) -> dict[str, dict]:
    """`modello -> {quant, size_bytes}` per i modelli locali del manager vLLM.

    Il manager non espone ne' peso ne' quantizzazione: il peso si misura dalla
    cartella (memoizzato: e' una scansione di disco) e la quantizzazione si
    legge dal nome del repo, dove sta per convenzione (`...-AWQ-W4A16`).
    """
    mgr = (getattr(cfg, "vllm_manager_url", "") or "").rstrip("/")
    if not mgr:
        return {}
    import requests
    try:
        data = requests.get(mgr + "/api/models/local", timeout=4).json() or {}
    except (requests.RequestException, ValueError):
        return {}
    out: dict[str, dict] = {}
    for m in data.get("models") or []:
        name = str((m or {}).get("model") or "").strip()
        if not name:
            continue
        path = str(m.get("path") or "")
        if path and path not in _vllm_size_cache:
            _vllm_size_cache[path] = _dir_size(path)
        out[name] = {"quant": _quant_from_name(name),
                     "size_bytes": _vllm_size_cache.get(path, 0),
                     "arch": "", "params": ""}
    return out


def describe(model_id: str, meta: dict | None) -> dict:
    """Voce pronta per un menu a tendina: id + quantizzazione + peso.

    `label` e' gia' componibile a video ("modello · IQ4_NL · 19,3 GB"); i campi
    grezzi restano accanto per chi vuole formattare da se' (la GUI web, che
    localizza i numeri)."""
    meta = meta or {}
    quant = str(meta.get("quant") or "") or _quant_from_name(model_id)
    size = int(meta.get("size_bytes") or 0)
    size_h = human_size(size)
    coda = " · ".join(p for p in (quant, size_h) if p)
    return {
        "id": model_id,
        "quant": quant,
        "size_bytes": size,
        "size_h": size_h,
        "params": str(meta.get("params") or ""),
        "arch": str(meta.get("arch") or ""),
        "kind": str(meta.get("kind") or ""),
        "label": f"{model_id} · {coda}" if coda else model_id,
    }


def start_lmstudio(cfg) -> bool:
    """Avvia il server LM Studio (`lms server start`) e attende che risponda.
    Se la CLI manca o il server non sale, ripiega sull'app. False = non partito."""
    if not getattr(cfg, "lmstudio_autostart", True):
        log.info("LM Studio spento e autostart disabilitato: non lo avvio")
        return False
    deadline = time.monotonic() + float(getattr(cfg, "lmstudio_start_timeout_s", 90.0))
    cli = lms_cli(cfg)
    if cli:
        # Porta esplicita: l'API deve rispondere dove punta lmstudio_base_url.
        port = ""
        base = cfg.lmstudio_base_url or ""
        if ":" in base:
            tail = base.rsplit(":", 1)[-1]
            port = tail.split("/")[0]
        args = [cli, "server", "start"]
        if port.isdigit():
            args += ["-p", port]
        log.info("LM Studio spento: avvio `%s`", " ".join(args[1:]))
        try:
            subprocess.run(args, capture_output=True, text=True,
                           timeout=_LMS_START_TIMEOUT, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("`lms server start` fallito: %s", e)
        if _wait_lmstudio(cfg, deadline):
            return True
    app = _lms_app(cfg)
    if app:
        log.info("Server non su: lancio l'app LM Studio (%s)", app)
        try:
            subprocess.Popen([app], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                             | subprocess.DETACHED_PROCESS)
        except OSError as e:
            log.error("Avvio dell'app LM Studio fallito: %s", e)
            return False
        return _wait_lmstudio(cfg, deadline)
    log.error("LM Studio non avviabile: ne' CLI `lms` ne' app trovate")
    return False


def _wait_lmstudio(cfg, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if lmstudio_up(cfg):
            log.info("LM Studio pronto su %s", cfg.lmstudio_base_url)
            return True
        time.sleep(2.0)
    return False


def ensure_lmstudio(cfg, want: str = "") -> tuple[str, str]:
    """(base_url, modello). Avvia il server se serve; carica il modello scelto
    in GUI se non e' gia' in memoria. Campo vuoto = usa quello caldo.

    `want` sovrascrive il modello della GUI (`lmstudio_model`)."""
    base = cfg.lmstudio_base_url
    want = (want or cfg.lmstudio_model or "").strip()
    if not lmstudio_up(cfg) and not start_lmstudio(cfg):
        return base, want
    loaded = lmstudio_loaded(cfg)
    if loaded is None:
        # Non sappiamo cosa sia in memoria: NON e' il momento di far caricare
        # qualcosa (un JIT alla cieca e' esattamente cio' che svuota la VRAM).
        log.warning("LM Studio: stato dei modelli caricati ignoto; uso '%s' "
                    "senza chiedere caricamenti", want or "(nessuno)")
        return base, want
    if loaded:
        # C'e' gia' un modello caldo: si usa QUELLO. Si passa il suo id ESATTO
        # (con il suffisso di quantizzazione): passare il nome "nudo" della GUI
        # farebbe risolvere LM Studio a un'altra variante dello stesso modello e
        # caricarla JIT accanto a questa.
        esatto = next((m for m in loaded if _same_model(m, want)), "") if want else ""
        scelto = esatto or loaded[0]
        if want and not esatto:
            log.info("LM Studio ha gia' '%s' caricato: uso quello (in GUI c'e' "
                     "'%s', ma non si scomoda un secondo modello)", scelto, want)
        elif esatto and esatto != want:
            log.info("LM Studio ha '%s' caricato: uso questo id esatto (in GUI "
                     "c'e' '%s', stesso modello senza quantizzazione)", esatto, want)
        return base, scelto
    # Nessuno `loaded`, ma potrebbe essercene uno IN CARICAMENTO: quello sta già
    # prendendosi la VRAM. Si aspetta lui invece di affiancargliene un altro.
    busy = lmstudio_busy(cfg)
    if busy:
        log.info("LM Studio sta caricando '%s': aspetto quello invece di "
                 "caricarne un altro", busy[0])
        deadline = time.monotonic() + float(getattr(cfg, "lmstudio_load_timeout_s", 300.0))
        while time.monotonic() < deadline:
            time.sleep(2.0)
            caldi = lmstudio_loaded(cfg) or []
            if caldi:
                log.info("LM Studio: '%s' è pronto, uso quello", caldi[0])
                return base, caldi[0]
            if not lmstudio_busy(cfg):
                break
        log.warning("LM Studio: il caricamento di '%s' non si è concluso", busy[0])
        return base, busy[0]
    if not want:
        log.warning("LM Studio senza modelli caricati e nessun modello scelto in GUI")
        return base, ""
    cli = lms_cli(cfg)
    if not cli:
        log.error("Nessun modello caricato e CLI `lms` assente: non posso caricare '%s'", want)
        return base, want
    # Id ESATTO: col nome nudo `lms` prende "the first one" fra le varianti e
    # l'API ne fa un JIT su una a caso.
    esatto = resolve_exact_id(cfg, want)
    ok, perche = cabe_in_vram(cfg, esatto)
    if not ok:
        # Meglio dirlo che tentare: un load che non entra in VRAM fallisce e si
        # porta dietro il turno. Modello vuoto -> il brain lo riferisce a voce.
        log.error("Non carico '%s': %s. Libera la GPU o scegli un modello più "
                  "piccolo dalla GUI.", esatto, perche)
        return base, ""
    log.info("LM Studio: carico '%s'", esatto)
    try:
        p = subprocess.run([cli, "load", esatto, "--yes"], capture_output=True, text=True,
                           timeout=float(getattr(cfg, "lmstudio_load_timeout_s", 300.0)),
                           creationflags=_NO_WINDOW)
        if p.returncode != 0:
            log.error("`lms load %s` fallito (%s): %s", esatto, p.returncode,
                      (p.stderr or p.stdout or "").strip()[:300])
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error("`lms load %s` non completato: %s", esatto, e)
    # Si restituisce l'id REALMENTE in memoria, non quello richiesto: se il
    # caricamento è fallito o LM Studio ha scelto un'altra variante, mandare il
    # nome richiesto all'API ne innescherebbe un secondo caricamento.
    dopo = lmstudio_loaded(cfg) or []
    if dopo:
        return base, next((m for m in dopo if _same_model(m, esatto)), dopo[0])
    log.error("Dopo il caricamento LM Studio non ha nessun modello in memoria")
    return base, ""


# --- vLLM ---------------------------------------------------------------------

def vllm_instances(cfg) -> list[dict]:
    """Istanze note al manager, l'attiva per prima. [] se il manager e' giu'."""
    mgr = (getattr(cfg, "vllm_manager_url", "") or "").rstrip("/")
    if not mgr:
        return []
    import requests
    try:
        data = requests.get(mgr + "/api/instances", timeout=4).json() or {}
    except (requests.RequestException, ValueError) as e:
        log.debug("manager vLLM non raggiungibile: %s", e)
        return []
    insts = [i for i in (data.get("instances") or []) if isinstance(i, dict)]
    active = data.get("active_id")
    insts.sort(key=lambda i: 0 if i.get("id") == active else 1)
    return insts


def _instance_url(inst: dict) -> str:
    """URL client-usabile dell'istanza: il manager registra 0.0.0.0 per le
    subprocess, che non e' connettibile -> si ricostruisce su 127.0.0.1."""
    port = inst.get("port")
    if port:
        return f"http://127.0.0.1:{port}"
    return str(inst.get("url") or "").rstrip("/")


def _vllm_alive(url: str) -> bool:
    if not url:
        return False
    import requests
    try:
        return requests.get(url + "/health", timeout=3).ok
    except requests.RequestException:
        return False


def _vllm_served_model(url: str) -> str:
    """Nome con cui l'istanza SERVE il modello (`GET /v1/models`).

    Non coincide con `entry.model` del registry: quando il manager carica da
    cartella locale, li' c'e' il PATH (E:\\AI\\Models\\vllm\\...) mentre l'API
    risponde solo al `--served-model-name` (il repo id). Usare il path da'
    404 model-not-found.
    """
    import requests
    try:
        data = requests.get(url + "/v1/models", timeout=5).json() or {}
    except (requests.RequestException, ValueError):
        return ""
    for m in data.get("data") or []:
        mid = str(m.get("id") or "").strip()
        if mid:
            return mid
    return ""


def vllm_loadable(cfg) -> list[str]:
    """Modelli caricabili dal manager (cartella locale), per il dropdown GUI."""
    mgr = (getattr(cfg, "vllm_manager_url", "") or "").rstrip("/")
    if not mgr:
        return []
    import requests
    try:
        data = requests.get(mgr + "/api/models/local", timeout=4).json() or {}
    except (requests.RequestException, ValueError):
        return []
    return sorted({str(m.get("model")) for m in (data.get("models") or [])
                   if m.get("loadable") and m.get("model")})


def ensure_vllm(cfg) -> tuple[str, str]:
    """(base_url, modello).

    Un'istanza viva si riusa SEMPRE, col modello che sta gia' servendo, anche se
    in GUI ne risulta un altro: e' la stessa regola di LM Studio. Il manager si
    scomoda solo quando non c'e' nessuna istanza viva.
    """
    want = (getattr(cfg, "vllm_model", "") or "").strip()
    for inst in vllm_instances(cfg):
        url = _instance_url(inst)
        if not _vllm_alive(url):
            continue
        model = _vllm_served_model(url) or str(inst.get("model") or want)
        if want and model != want:
            log.info("vLLM serve gia' '%s': uso quello (in GUI c'e' '%s', ma "
                     "non si fa lo swap di un'istanza viva)", model, want)
        return f"{url}/v1", model
    if not want:
        log.warning("Nessuna istanza vLLM viva e nessun modello scelto in GUI")
        return cfg.lmstudio_base_url, ""
    mgr = (getattr(cfg, "vllm_manager_url", "") or "").rstrip("/")
    timeout_s = float(getattr(cfg, "vllm_ensure_timeout_s", 300.0))
    log.info("Nessuna istanza vLLM viva: chiedo al manager di avviare '%s'", want)
    import requests
    try:
        # Solo il MODELLO: i parametri di caricamento (compute mode, VRAM,
        # max-model-len, eager) li decide il manager dalle sue ricette. Passarli
        # da qui li scavalcherebbe (`pick()` nel manager da' priorita' al body).
        r = requests.post(
            mgr + "/api/ensure",
            json={"model": want, "wait": True, "timeout_s": timeout_s},
            timeout=timeout_s + 15,
        )
        body = r.json() or {}
        url = str(body.get("url") or "").rstrip("/")
        if url:
            log.info("vLLM pronto su %s (modello %s)", url, body.get("model") or want)
            return f"{url}/v1", str(body.get("model") or want)
    except (requests.RequestException, ValueError) as e:
        log.error("vLLM /ensure fallito (%s): resta il base_url di LM Studio", e)
    # Manager giu'/errore: fallback esplicito a LM Studio (loggato sopra), cosi'
    # la conversazione parte comunque se LM Studio e' acceso.
    return cfg.lmstudio_base_url, want


# --- API usata dal brain e dalla GUI ------------------------------------------

def resolve_exact_id(cfg, want: str) -> str:
    """Nome scelto in GUI -> id ESATTO con quantizzazione (`nome@iq4_nl`).

    Lo stesso modello sta su disco in piu' varianti: passare il nome nudo fa
    rispondere a `lms load` "2 models match the provided model key on the same
    device. Loading the first one" e a `/v1/chat/completions` un JIT su una
    variante a caso. Si preferisce la variante con quantizzazione NOTA (non
    `@?`) e, a parita', la piu' leggera.
    """
    want = (want or "").strip()
    if not want or "@" in want:
        return want
    meta = lmstudio_catalog(cfg, ask_cli=True)
    varianti = [m for m in meta if _same_model(m, want)]
    if not varianti:
        return want
    if len(varianti) == 1:
        return varianti[0]

    def _ordine(mid: str) -> tuple:
        d = meta.get(mid) or {}
        quant = str(d.get("quant") or "")
        size = int(d.get("size_bytes") or 0)
        return (0 if quant else 1, size or 1 << 62)

    scelto = sorted(varianti, key=_ordine)[0]
    log.info("'%s' ha %d varianti in catalogo: uso '%s' (%s)", want, len(varianti),
             scelto, human_size((meta.get(scelto) or {}).get("size_bytes")) or "peso ignoto")
    return scelto


def vram_free_bytes() -> int | None:
    """VRAM libera sulla GPU 0, o None se non misurabile (niente nvidia-smi)."""
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            return None
        return int((p.stdout or "").strip().splitlines()[0]) * 1024 * 1024
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def cabe_in_vram(cfg, model_id: str) -> tuple[bool, str]:
    """Il modello ci sta nella VRAM libera adesso? (ok, spiegazione).

    Ultima rete di sicurezza: qualunque cosa occupi la GPU — un modello caricato
    da fuori, l'avatar MuseTalk, un caricamento in corso — un `lms load` che non
    ci sta fallisce e si porta dietro la conversazione. Meglio non tentarlo.
    Se il peso o la VRAM non sono misurabili si lascia passare (nessuna prova
    contraria, e bloccare alla cieca sarebbe peggio).
    """
    meta = lmstudio_catalog(cfg, ask_cli=True).get(model_id) or {}
    peso = int(meta.get("size_bytes") or 0)
    libera = vram_free_bytes()
    if not peso or libera is None:
        return True, ""
    # I pesi non sono tutta l'occupazione: contesto KV e overhead del runtime
    # chiedono altro. 15% di margine e' quanto basta a non illudersi.
    serve = int(peso * 1.15)
    if serve <= libera:
        return True, ""
    return False, (f"servono ~{human_size(serve)} ma sulla GPU ne restano "
                   f"{human_size(libera)}")


def unload_lmstudio(cfg, model: str = "") -> tuple[bool, str]:
    """Scarica dalla memoria un modello LM Studio (tutti se `model` e' vuoto).

    (ok, messaggio). Serve allo SWAP esplicito: la VRAM tiene un modello grande
    per volta, quindi per caricarne un altro il primo va tolto — ma solo su
    richiesta esplicita dell'utente, mai d'iniziativa.
    """
    cli = lms_cli(cfg)
    if not cli:
        return False, "CLI `lms` non disponibile: scarica il modello da LM Studio"
    caldi = lmstudio_loaded(cfg)
    if caldi is None:
        return False, "stato dei modelli caricati ignoto"
    bersagli = ([m for m in caldi if _same_model(m, model)] if model else list(caldi))
    if not bersagli:
        return True, "nessun modello da scaricare"
    for m in bersagli:
        log.info("LM Studio: scarico '%s' (swap richiesto)", m)
        try:
            p = subprocess.run([cli, "unload", m], capture_output=True, text=True,
                               timeout=_LMS_QUICK_TIMEOUT, creationflags=_NO_WINDOW)
            if p.returncode != 0:
                msg = (p.stderr or p.stdout or "").strip()[:200]
                log.error("`lms unload %s` fallito: %s", m, msg)
                return False, f"scarico di '{m}' fallito: {msg}"
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("`lms unload %s` non completato: %s", m, e)
            return False, f"scarico di '{m}' non completato: {e}"
    return True, "scaricato: " + ", ".join(bersagli)


def swap_lmstudio(cfg, model: str, provider: str = "") -> tuple[bool, str]:
    """Scarica il modello caldo e mette `model` al suo posto (swap CONFERMATO).

    Da chiamare solo dopo un sì esplicito dell'utente: vedi
    `/avatar/llm/swap`.

    Se il bersaglio e' un modello vLLM ci si ferma allo SCARICO: caricarlo con
    `lms load` non avrebbe senso (non e' un modello di LM Studio) — liberata la
    VRAM, ci pensa il manager vLLM ad avviare l'istanza al primo uso.
    """
    model = (model or "").strip()
    if not model:
        return False, "nessun modello indicato"
    ok, msg = unload_lmstudio(cfg)
    if not ok:
        return False, msg
    if (provider or "").strip().lower() == "vllm":
        log.info("VRAM liberata: '%s' lo caricherà il manager vLLM", model)
        return True, f"memoria liberata; '{model}' verrà avviato da vLLM al primo turno"
    cli = lms_cli(cfg)
    if not cli:
        return False, "CLI `lms` non disponibile: carica il modello da LM Studio"
    log.info("LM Studio: carico '%s' dopo lo scarico (swap confermato)", model)
    try:
        p = subprocess.run([cli, "load", model, "--yes"], capture_output=True, text=True,
                           timeout=float(getattr(cfg, "lmstudio_load_timeout_s", 300.0)),
                           creationflags=_NO_WINDOW)
        if p.returncode != 0:
            err = (p.stderr or p.stdout or "").strip()[:200]
            return False, f"caricamento di '{model}' fallito: {err}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"caricamento di '{model}' non completato: {e}"
    return True, f"in memoria ora c'e' '{model}'"


def swap_check(cfg, model: str, provider: str = "") -> dict:
    """Cosa comporta passare a `model`: serve una conferma di swap?

    Ritorna `{needs_confirm, loaded, loaded_label, target_label, reason}`. La VRAM
    non tiene due modelli grandi: se in memoria ce n'e' gia' uno DIVERSO, chi
    sceglie deve sapere che il primo verra' scaricato, e poter dire di no (in
    quel caso resta quello caldo).
    """
    model = (model or "").strip()
    prov = (provider or "").strip().lower()
    vuoto = {"needs_confirm": False, "loaded": "", "loaded_label": "",
             "target_label": model, "reason": ""}
    # Provider remoti (zai, openrouter, ...): nessuna VRAM locale in gioco.
    if prov and prov not in ("lmstudio", "vllm", "diffusiongemma"):
        return vuoto
    caldi = lmstudio_loaded(cfg)
    if not caldi:
        return vuoto                     # memoria vuota o ignota: nulla da scaricare
    if any(_same_model(m, model) for m in caldi):
        return vuoto                     # e' gia' lui (magari altra quantizzazione)
    caldo = caldi[0]
    meta = lmstudio_catalog(cfg, ask_cli=True)   # LM Studio è su: `lms ps` l'ha detto
    return {
        "needs_confirm": True,
        "loaded": caldo,
        "loaded_label": describe(caldo, meta.get(caldo))["label"],
        "target_label": describe(model, meta.get(model))["label"],
        "reason": ("In memoria c'è già un modello e la VRAM ne tiene uno solo: "
                   "per caricare quello scelto va prima scaricato l'altro."),
    }


def embed_endpoint(cfg) -> tuple[str, str, str] | None:
    """(base_url, api_key, modello) per gli EMBEDDING, o None se non configurati.

    Il modello di embedding non è quello di chat: per usarne uno diverso basta
    un secondo provider nel registro (stesso tipo, altro modello) assegnato al
    consumatore "embed". Senza assegnazione vale il campo storico
    `lmstudio_embed_model`, così le configurazioni esistenti non cambiano.
    """
    try:
        from . import llm_providers
        prov = llm_providers.provider_for(cfg, "embed")
        # `embed_model`, NON `model`: sono due cose diverse, e assegnare lo
        # stesso provider a chat ed embedding manderebbe altrimenti un LLM da
        # 35B alla /embeddings.
        modello = (prov or {}).get("embed_model", "").strip() if prov else ""
        if prov and modello:
            spec = llm_providers.TYPES.get(prov.get("type") or "", {})
            base = ((prov.get("base_url") or "").strip()
                    or spec.get("chat_base_url") or spec.get("base_url") or "")
            key = ((prov.get("oauth") or {}).get("access_token")
                   or prov.get("api_key") or cfg.lmstudio_api_key or "local")
            return base, key, modello
    except Exception as e:  # noqa: BLE001 - registro assente
        log.debug("provider di embedding non risolto: %s", e)
    storico = (getattr(cfg, "lmstudio_embed_model", "") or "").strip()
    if not storico:
        return None
    return (cfg.lmstudio_base_url, cfg.lmstudio_api_key or "lm-studio", storico)


def warm_backend(cfg, prefer: str = "") -> tuple[str, str, str] | None:
    """(backend, base_url, modello) del primo backend che ha GIA' un modello in
    memoria, `prefer` per primo. None se nessuno dei due ne ha.

    Sonda di sola lettura: non avvia LM Studio (solo HTTP se gia' risponde) e
    non chiede al manager di tirare su istanze vLLM.
    """
    def _lmstudio() -> tuple[str, str, str] | None:
        loaded = lmstudio_loaded(cfg)
        if not loaded:
            return None
        want = (cfg.lmstudio_model or "").strip()
        esatto = next((m for m in loaded if _same_model(m, want)), "") if want else ""
        return "lmstudio", cfg.lmstudio_base_url, (esatto or loaded[0])

    def _vllm() -> tuple[str, str, str] | None:
        for inst in vllm_instances(cfg):
            url = _instance_url(inst)
            if not _vllm_alive(url):
                continue
            model = _vllm_served_model(url) or str(inst.get("model") or "")
            if model:
                return "vllm", f"{url}/v1", model
        return None

    ordine = ("vllm", "lmstudio") if prefer == "vllm" else ("lmstudio", "vllm")
    for name in ordine:
        found = _vllm() if name == "vllm" else _lmstudio()
        if found:
            return found
    return None


def resolve(cfg, consumer: str = "chat") -> tuple[str, str, str]:
    """(backend, base_url, modello) pronti all'uso per `consumer`.

    `consumer` distingue chi sta chiedendo (chat avatar, copilota, …): ognuno
    può avere un provider suo nel registro."""
    # Provider REMOTO scelto nel tab: niente da avviare né da caricare, si usa.
    remoto = remote_provider(cfg, consumer)
    if remoto:
        log.info("%s: provider remoto '%s' (modello %s)", consumer, remoto["id"],
                 remoto["model"] or "predefinito")
        return remoto["type"], remoto["base_url"], remoto["model"]
    backend = selected_backend(cfg, consumer)
    # Regola 1: un modello gia' in memoria si usa e basta — anche se sta nel
    # backend NON selezionato. Prima questo controllo era per-backend, cosi'
    # con un modello caldo in LM Studio e la GUI su vLLM (o viceversa) si
    # finiva a caricarne un secondo: in VRAM non ci stanno.
    warm = warm_backend(cfg, prefer=backend)
    if warm:
        got, base, model = warm
        if got != backend:
            log.info("%s ha gia' '%s' in memoria: uso quello invece di caricare "
                     "un modello su %s (in VRAM non ci starebbero entrambi)",
                     got, model, backend)
        return got, base, model
    # Regola 2: memoria vuota -> si carica quello scelto in GUI, sul backend scelto.
    if backend == "vllm":
        base, model = ensure_vllm(cfg)
    else:
        base, model = ensure_lmstudio(cfg)
    return backend, base, model


def list_models(cfg, include: str = "") -> dict:
    """Modelli selezionabili dalla GUI per la chat avatar, per backend.

    `loaded` = gia' in memoria (quello che verrebbe usato adesso); `available` =
    caricabile su richiesta; `queried` = False quando l'elenco non e' stato
    chiesto al backend (vedi sotto) e quindi e' vuoto per scelta, non perche'
    manchino modelli.

    ATTENZIONE: la CLI `lms` AVVIA LM Studio se e' spento (e' un client dell'app,
    non un lettore di file). Percio' la si interroga solo quando LM Studio e' il
    backend scelto, e' gia' acceso, o la GUI lo chiede esplicitamente con
    `include` — altrimenti aprire la tab Personas accenderebbe LM Studio anche a
    chi usa vLLM.
    """
    selected = selected_backend(cfg)
    want = (include or "").strip().lower()
    lm_up = lmstudio_up(cfg)
    ask_lm = lm_up or selected == "lmstudio" or want in ("lmstudio", "all")
    # `lmstudio_loaded` e' innocuo (HTTP, e con server giu' torna subito None):
    # e' `lmstudio_on_disk` (`lms ls`) che accenderebbe l'app, quindi solo quello
    # resta dietro `ask_lm`.
    lm_loaded = lmstudio_loaded(cfg) or []
    lm_disk = lmstudio_on_disk(cfg) if ask_lm else []
    insts = vllm_instances(cfg)
    vllm_live = []
    for i in insts:
        url = _instance_url(i)
        if _vllm_alive(url):
            # Nome SERVITO, non entry.model (che con i modelli locali e' un path).
            served = _vllm_served_model(url) or str(i.get("model") or "")
            if served:
                vllm_live.append(served)
    lm_meta = lmstudio_catalog(cfg, ask_cli=ask_lm)
    vl_meta = vllm_catalog(cfg)
    # Anche le chiavi del catalogo: `lms ls --llm` esclude per definizione i
    # modelli di EMBEDDING, che invece servono al RAG e devono comparire.
    lm_avail = sorted(set(lm_disk) | set(lm_loaded) | set(lm_meta))
    vl_avail = sorted(set(vllm_loadable(cfg)) | set(vllm_live))

    def _voci(ids: list[str], meta: dict, caldi: list[str]) -> list[dict]:
        """`available` arricchito: quantizzazione, peso e chi e' gia' in memoria.
        Senza, due quantizzazioni dello stesso modello sono indistinguibili."""
        return [dict(describe(m, meta.get(m)), loaded=m in caldi) for m in ids]

    return {
        "selected": selected,
        "backends": {
            "lmstudio": {
                "up": lm_up,
                "queried": ask_lm,
                "loaded": lm_loaded,
                "available": lm_avail,
                "models": _voci(lm_avail, lm_meta, lm_loaded),
                "chosen": (cfg.lmstudio_model or "").strip(),
            },
            "vllm": {
                "up": bool(insts) or _manager_up(cfg),
                "queried": True,
                "loaded": vllm_live,
                "available": vl_avail,
                "models": _voci(vl_avail, vl_meta, vllm_live),
                "chosen": (getattr(cfg, "vllm_model", "") or "").strip(),
            },
        },
    }


def _manager_up(cfg) -> bool:
    mgr = (getattr(cfg, "vllm_manager_url", "") or "").rstrip("/")
    if not mgr:
        return False
    import requests
    try:
        return requests.get(mgr + "/api/status", timeout=3).ok
    except requests.RequestException:
        return False
