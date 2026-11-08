"""Registro dei provider LLM: locali (LM Studio, vLLM) e remoti (Gemini,
Claude, OpenAI, Grok, Z.AI, NVIDIA, generici OpenAI-compatibili).

Il registro vive FUORI dal repository (`%LOCALAPPDATA%\\callAPIcall\\
llm_providers.json`): contiene chiavi API e token OAuth, e `config.json` è
tracciato da git — una chiave finita in un commit è una chiave da revocare.

Ogni voce del registro è un provider CONFIGURATO dall'utente:

    {"id": "gemini-1", "type": "gemini", "name": "Gemini",
     "model": "gemini-3-pro", "api_key": "...", "base_url": "",
     "oauth": {...}}

`type` sceglie come parlarci (vedi `TYPES`); `id` è stabile e lo usano i
consumatori (`assignments`) per dire "la chat avatar usa questo".

Autenticazione, allo stato di agosto 2026:
  - **OAuth utente**: Grok (xAI, OAuth 2.0 + PKCE su accounts.x.ai) e Gemini
    (authorization code Google, access token 24h + refresh token).
  - **Solo API key**: Claude — dal 4 aprile 2026 Anthropic vieta i token OAuth
    delle subscription fuori dai propri client — e OpenAI, la cui "Sign in with
    ChatGPT" non fa pagare le chiamate all'abbonamento dell'utente.
  - **Nessuna**: LM Studio e vLLM, che girano in locale.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("callbridge.llm_providers")

# Catalogo dei TIPI supportati. `auth`: "none" | "api_key" | "oauth" | "either".
# `api`: come si elencano i modelli e si parla col provider.
TYPES: dict[str, dict] = {
    "lmstudio": {
        "label": "LM Studio", "auth": "none", "api": "lmstudio", "local": True,
        "base_url": "http://127.0.0.1:1234/v1",
        "note": "Server locale. I modelli elencati sono quelli su disco; "
                "quello già in memoria ha la precedenza.",
    },
    "vllm": {
        "label": "vLLM", "auth": "none", "api": "vllm", "local": True,
        "base_url": "",
        "note": "Istanze gestite dal manager vLLM. Un'istanza viva si riusa "
                "com'è, senza caricarne una seconda.",
    },
    "openai": {
        "label": "OpenAI", "auth": "api_key", "api": "openai", "local": False,
        "base_url": "https://api.openai.com/v1",
        "note": "Serve una API key (pay-per-use): l'abbonamento ChatGPT non "
                "copre le chiamate di app terze.",
    },
    # `chat_base_url`: dove sta l'interfaccia OpenAI-compatibile, quando NON
    # coincide con quella che elenca i modelli. Anthropic e Google espongono le
    # due cose su path diversi, e usare il primo per chattare da 404.
    "claude": {
        "label": "Claude (Anthropic)", "auth": "api_key", "api": "anthropic",
        "local": False, "base_url": "https://api.anthropic.com/v1",
        "chat_base_url": "https://api.anthropic.com/v1",
        "note": "Solo API key: dal 4 aprile 2026 i token OAuth delle "
                "subscription Claude non sono utilizzabili fuori dai client "
                "Anthropic.",
    },
    "gemini": {
        "label": "Google Gemini", "auth": "either", "api": "gemini",
        "local": False,
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "chat_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "note": "API key oppure OAuth Google. Da settembre 2026 le API key "
                "\"standard\" vengono rifiutate: servono le auth key.",
    },
    "grok": {
        "label": "Grok (xAI)", "auth": "either", "api": "openai", "local": False,
        "base_url": "https://api.x.ai/v1",
        "note": "API key, oppure OAuth 2.0 PKCE per usare l'abbonamento "
                "SuperGrok / X Premium+.",
    },
    # Z.AI ha DUE endpoint distinti, con piani e quote separati: quello
    # generale e quello del piano coding. Stessa chiave non vale su entrambi,
    # quindi sono due provider e non un'opzione dello stesso.
    "zai": {
        "label": "Z.AI (piano API)", "auth": "api_key", "api": "openai",
        "local": False, "base_url": "https://api.z.ai/api/paas/v4",
        "note": "Endpoint generale (GLM). Interfaccia OpenAI-compatibile.",
    },
    "zai_coding": {
        "label": "Z.AI (piano coding)", "auth": "api_key", "api": "openai",
        "local": False, "base_url": "https://api.z.ai/api/coding/paas/v4",
        "note": "Endpoint del piano coding, quello usato da Hermes "
                "(GLM-5.2 / GLM-5.1). Quota separata dal piano API.",
    },
    "nvidia": {
        "label": "NVIDIA NIM", "auth": "api_key", "api": "openai", "local": False,
        "base_url": "https://integrate.api.nvidia.com/v1",
        "note": "Modelli di build.nvidia.com, interfaccia OpenAI-compatibile.",
    },
    "openai_compat": {
        "label": "Generico (OpenAI-compatibile)", "auth": "api_key",
        "api": "openai", "local": False, "base_url": "",
        "note": "Qualunque server che espone /v1/chat/completions.",
    },
    # --- subscription tramite il CLI ufficiale --------------------------------
    # È l'unico modo conforme di far pagare le chiamate a un ABBONAMENTO: il
    # client ufficiale è autorizzato a usarlo, una app terza con le stesse
    # credenziali no. Prezzo da pagare: sono agenti, non completion — misurato
    # su questa macchina, un "rispondi solo ok" costa 5,7s a Claude e 14,2s
    # (14k token) a Codex, contro 0,5s di un modello locale. Vanno bene per
    # analisi, riunioni e lavori in differita; NON per la voce in tempo reale.
    "codex_cli": {
        "label": "OpenAI via Codex CLI (abbonamento)", "auth": "cli",
        "api": "cli", "local": True, "base_url": "",
        "command": "codex exec --skip-git-repo-check",
        "note": "Usa l'abbonamento ChatGPT tramite il CLI Codex già "
                "autenticato. Lento (~14s a risposta): non adatto alla voce.",
    },
    "claude_cli": {
        "label": "Claude via Claude Code (abbonamento)", "auth": "cli",
        "api": "cli", "local": True, "base_url": "",
        "command": "claude -p",
        "note": "Usa l'abbonamento Claude Pro/Max tramite il CLI ufficiale. "
                "~6s a risposta: bene per i lavori in differita.",
    },
    "antigravity": {
        "label": "Gemini via Antigravity CLI (abbonamento)", "auth": "cli",
        "api": "cli", "local": True, "base_url": "",
        "command": "agy -p",
        # Unico dei tre CLI a saper elencare i modelli: l'abbonamento non dà
        # solo Gemini, ma anche Claude e GPT-OSS.
        "models_command": "agy models",
        "note": "Sostituisce il vecchio `gemini` CLI, che Google ha chiuso "
                "agli account individuali (IneligibleTierError). Autenticato "
                "una volta in modo interattivo; ~11s a risposta.",
    },
}

# Consumatori che possono avere un provider proprio. L'agente Hermes NON è qui:
# legge i suoi provider dal proprio config.yaml e resta padrone di quelli.
CONSUMERS = ("chat", "copilot", "embed")

_lock = threading.Lock()
_models_cache: dict[str, tuple[float, list[dict]]] = {}
_MODELS_TTL = 300.0


def registry_path() -> Path:
    """File del registro, fuori dal repo (contiene credenziali)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "callAPIcall" / "llm_providers.json"


def _empty() -> dict:
    return {"version": 1, "providers": [], "assignments": {}}


def load() -> dict:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("providers", [])
    data.setdefault("assignments", {})
    return data


def save(data: dict) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _find(data: dict, pid: str) -> dict | None:
    return next((x for x in data.get("providers", []) if x.get("id") == pid), None)


def seed_from_config(cfg) -> dict:
    """Primo avvio: crea LM Studio e vLLM dai campi già in `config.json`.

    Così il tab nasce con dentro quello che il sistema sta già usando, invece
    di una lista vuota da ricompilare a mano."""
    data = load()
    if data["providers"]:
        return data
    base = (getattr(cfg, "lmstudio_base_url", "") or "").strip()
    data["providers"].append({
        "id": "lmstudio", "type": "lmstudio", "name": "LM Studio",
        "base_url": base or TYPES["lmstudio"]["base_url"],
        "model": (getattr(cfg, "lmstudio_model", "") or "").strip(),
        "api_key": "",
    })
    data["providers"].append({
        "id": "vllm", "type": "vllm", "name": "vLLM",
        "base_url": "", "model": (getattr(cfg, "vllm_model", "") or "").strip(),
        "api_key": "",
    })
    scelto = "vllm" if (getattr(cfg, "avatar_llm_backend", "") or "") == "vllm" \
        else "lmstudio"
    data["assignments"] = {c: scelto for c in CONSUMERS}
    save(data)
    log.info("Registro provider LLM creato da config.json (attivo: %s)", scelto)
    return data


# --- elenco modelli -----------------------------------------------------------

def _openai_models(base_url: str, api_key: str, extra: dict | None = None) -> list[dict]:
    """`GET {base}/models` (OpenAI, Grok, Z.AI, NVIDIA, generici)."""
    import requests
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers.update(extra or {})
    r = requests.get(base_url.rstrip("/") + "/models", headers=headers, timeout=20)
    r.raise_for_status()
    out = []
    for m in (r.json() or {}).get("data") or []:
        mid = str(m.get("id") or "").strip()
        if mid:
            out.append({"id": mid, "label": mid})
    return out


def _anthropic_models(base_url: str, api_key: str) -> list[dict]:
    import requests
    r = requests.get(base_url.rstrip("/") + "/models", timeout=20, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    return [{"id": str(m.get("id")), "label": str(m.get("display_name") or m.get("id"))}
            for m in (r.json() or {}).get("data") or [] if m.get("id")]


def _gemini_models(base_url: str, api_key: str, token: str = "") -> list[dict]:
    import requests
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params = {} if token else {"key": api_key}
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    out = []
    for m in (r.json() or {}).get("models") or []:
        name = str(m.get("name") or "").split("/")[-1]
        # Solo i modelli che sanno generare testo: l'elenco include embedding
        # e altro, che in un menu "modello di chat" sono solo rumore.
        metodi = m.get("supportedGenerationMethods") or []
        if name and (not metodi or "generateContent" in metodi):
            out.append({"id": name, "label": str(m.get("displayName") or name)})
    return out


def _cli_models(prov: dict, spec: dict) -> list[dict]:
    """Provider "CLI ufficiale": non c'è un endpoint da interrogare.

    Se il CLI sa elencare i suoi modelli (`models_command`) si usa quello —
    Antigravity, per esempio, dà accesso non solo a Gemini ma anche a Claude e
    GPT-OSS, e sceglierli a mano sarebbe indovinare. Altrimenti si verifica solo
    che il comando esista: il modello lo decide il CLI dalla sua config.
    """
    import shutil
    import subprocess
    cmd = (prov.get("base_url") or spec.get("command") or "").split()
    exe = cmd[0] if cmd else ""
    if not exe or shutil.which(exe) is None:
        raise RuntimeError(f"comando '{exe or '?'}' non trovato nel PATH")
    elenco = (spec.get("models_command") or "").split()
    if elenco:
        try:
            p = subprocess.run(elenco, capture_output=True, text=True, timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = []
            for riga in (p.stdout or "").splitlines():
                # "id<TAB>Etichetta leggibile"; righe di servizio senza tab.
                if "\t" not in riga:
                    continue
                mid, _, label = riga.partition("\t")
                if mid.strip():
                    out.append({"id": mid.strip(), "label": label.strip() or mid.strip()})
            if out:
                return out
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("`%s` non ha risposto: %s", " ".join(elenco), e)
    scelto = (prov.get("model") or "").strip()
    return [{"id": scelto or "default",
             "label": scelto or "(quello configurato nel CLI)"}]


def _local_models(cfg, prov: dict) -> list[dict]:
    """LM Studio / vLLM: l'elenco arriva da `llm_backend`, e ciò che è GIÀ in
    memoria viene marcato — è quello che verrebbe usato adesso."""
    from . import llm_backend
    tipo = prov.get("type")
    d = llm_backend.list_models(cfg, include=tipo)
    b = (d.get("backends") or {}).get(tipo) or {}
    caldi = set(b.get("loaded") or [])
    out = []
    for m in b.get("models") or []:
        out.append({"id": m["id"], "label": m.get("label") or m["id"],
                    "loaded": m["id"] in caldi,
                    "size_bytes": m.get("size_bytes", 0),
                    "quant": m.get("quant", ""),
                    # "llm" | "vlm" | "embeddings": la GUI se ne serve per non
                    # offrire un modello di chat dove va un embedder.
                    "kind": m.get("kind", "")})
    # I caricati per primi: sono quelli che non costano un'attesa.
    out.sort(key=lambda m: (not m.get("loaded"), m["id"].lower()))
    return out


def models(cfg, pid: str, force: bool = False) -> list[dict]:
    """Modelli del provider `pid`, chiesti al momento e memoizzati per 5'."""
    data = load()
    prov = _find(data, pid)
    if prov is None:
        raise KeyError(f"provider sconosciuto: {pid}")
    spec = TYPES.get(prov.get("type") or "", {})
    if spec.get("api") == "cli":
        return _cli_models(prov, spec)
    if spec.get("local"):
        return _local_models(cfg, prov)          # sempre fresco: cambia spesso
    hit = _models_cache.get(pid)
    if hit and not force and time.monotonic() - hit[0] < _MODELS_TTL:
        return hit[1]
    base = (prov.get("base_url") or spec.get("base_url") or "").strip()
    key = (prov.get("api_key") or "").strip()
    token = ((prov.get("oauth") or {}).get("access_token") or "").strip()
    api = spec.get("api")
    if api == "anthropic":
        out = _anthropic_models(base, key)
    elif api == "gemini":
        out = _gemini_models(base, key, token)
    else:
        out = _openai_models(base, token or key)
    _models_cache[pid] = (time.monotonic(), out)
    return out


def test(cfg, pid: str) -> dict:
    """Prova il provider: (ok, quanti modelli, messaggio d'errore)."""
    try:
        found = models(cfg, pid, force=True)
        return {"ok": True, "models": len(found),
                "sample": [m["id"] for m in found[:5]]}
    except Exception as e:  # noqa: BLE001 - qualunque errore è un esito
        return {"ok": False, "error": str(e)[:300]}


# --- risoluzione per i consumatori --------------------------------------------

def provider_for(cfg, consumer: str) -> dict | None:
    """Provider assegnato a `consumer` ("chat" | "copilot" | "embed")."""
    data = load()
    pid = (data.get("assignments") or {}).get(consumer) or ""
    return _find(data, pid) if pid else None


def endpoint_for(cfg, consumer: str) -> tuple[str, str, str] | None:
    """(base_url, api_key, modello) pronti per un client OpenAI-compatibile.

    None se non c'è un provider assegnato. Per i locali la scelta del modello
    resta a `llm_backend` (regola "si usa quello già in memoria")."""
    prov = provider_for(cfg, consumer)
    if prov is None:
        return None
    spec = TYPES.get(prov.get("type") or "", {})
    base = (prov.get("base_url") or spec.get("base_url") or "").strip()
    key = ((prov.get("oauth") or {}).get("access_token")
           or prov.get("api_key") or "").strip()
    return base, key, (prov.get("model") or "").strip()


def public(data: dict | None = None) -> dict:
    """Registro senza segreti, per la GUI: le chiavi diventano un booleano."""
    data = data or load()
    fuori = []
    for p in data.get("providers", []):
        spec = TYPES.get(p.get("type") or "", {})
        oauth = p.get("oauth") or {}
        fuori.append({
            "id": p.get("id"), "type": p.get("type"), "name": p.get("name"),
            "base_url": p.get("base_url") or spec.get("base_url", ""),
            "model": p.get("model", ""),
            "embed_model": p.get("embed_model", ""),
            "has_key": bool((p.get("api_key") or "").strip()),
            "oauth_account": oauth.get("account", ""),
            "oauth_connected": bool(oauth.get("access_token")),
            "local": bool(spec.get("local")),
            "auth": spec.get("auth", "api_key"),
            # I provider "CLI" sono agenti: latenza di secondi, quindi non
            # proponibili per la conversazione vocale.
            "realtime": spec.get("api") != "cli",
            "command": spec.get("command", ""),
            "label": spec.get("label", p.get("type")),
            "note": spec.get("note", ""),
        })
    return {"providers": fuori, "assignments": data.get("assignments", {}),
            "types": [{"type": k, "realtime": v.get("api") != "cli",
                       **{kk: vv for kk, vv in v.items()
                          if kk in ("label", "auth", "local", "base_url", "note")}}
                      for k, v in TYPES.items()],
            "consumers": list(CONSUMERS)}


# --- modifica del registro ----------------------------------------------------

def add(prov: dict) -> dict:
    tipo = (prov.get("type") or "").strip()
    if tipo not in TYPES:
        raise ValueError(f"tipo sconosciuto: {tipo}")
    with _lock:
        data = load()
        pid = (prov.get("id") or "").strip() or _new_id(data, tipo)
        if _find(data, pid) is not None:
            raise ValueError(f"esiste già un provider con id '{pid}'")
        voce = {
            "id": pid, "type": tipo,
            "name": (prov.get("name") or TYPES[tipo]["label"]).strip(),
            "base_url": (prov.get("base_url") or "").strip(),
            "model": (prov.get("model") or "").strip(),
            # Il modello che VETTORIZZA non è quello che conversa: tenerli nello
            # stesso campo faceva passare un LLM da 35B alla /embeddings.
            "embed_model": (prov.get("embed_model") or "").strip(),
            "api_key": (prov.get("api_key") or "").strip(),
        }
        data["providers"].append(voce)
        save(data)
        return voce


def _new_id(data: dict, tipo: str) -> str:
    esistenti = {p.get("id") for p in data.get("providers", [])}
    if tipo not in esistenti:
        return tipo
    n = 2
    while f"{tipo}-{n}" in esistenti:
        n += 1
    return f"{tipo}-{n}"


def update(pid: str, campi: dict) -> dict:
    with _lock:
        data = load()
        prov = _find(data, pid)
        if prov is None:
            raise KeyError(pid)
        for k in ("name", "base_url", "model", "embed_model", "api_key"):
            if k in campi:
                prov[k] = (campi[k] or "").strip()
        save(data)
        _models_cache.pop(pid, None)
        return prov


def remove(pid: str) -> None:
    with _lock:
        data = load()
        data["providers"] = [p for p in data.get("providers", []) if p.get("id") != pid]
        # Un consumatore non deve restare agganciato a un provider che non c'è più.
        data["assignments"] = {k: v for k, v in (data.get("assignments") or {}).items()
                               if v != pid}
        save(data)
        _models_cache.pop(pid, None)


def assign(consumer: str, pid: str | None) -> dict:
    if consumer not in CONSUMERS:
        raise ValueError(f"consumatore sconosciuto: {consumer}")
    with _lock:
        data = load()
        if pid:
            if _find(data, pid) is None:
                raise KeyError(pid)
            data.setdefault("assignments", {})[consumer] = pid
        else:
            data.get("assignments", {}).pop(consumer, None)
        save(data)
        return data.get("assignments", {})
