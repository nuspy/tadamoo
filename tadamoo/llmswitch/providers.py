"""Registro dei provider LLM: locali (LM Studio, vLLM, llama.cpp), remoti a
chiave (OpenAI, Claude, Gemini, Grok, Qwen, Z.AI, NVIDIA, DeepSeek, Kimi,
Nebius, generici OpenAI-compatibili) e ad abbonamento — sia quelli con un
endpoint HTTP (`llmswitch.oauth`) sia quelli che sono un programma da lanciare
(`llmswitch.agents`: Codex, Claude Code, Antigravity).

Un `Registry` è un file JSON con i provider configurati e a chi sono assegnati:

    reg = Registry(app_name="mioprogetto")
    reg.add({"type": "zai_coding", "api_key": "..."})
    reg.assign("chat", "zai_coding")
    base, key, model = reg.endpoint_for("chat")

Il file sta FUORI dalla cartella del progetto (in `%LOCALAPPDATA%` o
`~/.config`): contiene chiavi API, e i file di configurazione dei progetti
finiscono nei commit.

Sull'autenticazione, verificato ad agosto 2026:

- **OAuth utente**: xAI/Grok (OAuth 2.0 + PKCE su accounts.x.ai) e Google
  (authorization code, access token 24h + refresh token).
- **Solo API key**: Anthropic vieta dal 4 aprile 2026 i token OAuth delle
  subscription fuori dai propri client; la "Sign in with ChatGPT" di OpenAI non
  fa pagare le chiamate all'abbonamento dell'utente.
- **Abbonamento via CLI ufficiale**: è l'unico modo conforme di far pagare le
  chiamate a un abbonamento — il client ufficiale è autorizzato, un'app terza
  con le stesse credenziali no. Sono però AGENTI, non completion: misurato,
  "rispondi solo ok" costa ~6s a Claude Code, ~11s ad Antigravity e ~14s
  (14k token) a Codex, contro ~0,5s di un modello locale. Vanno bene in
  differita, non per la voce in tempo reale — vedi `realtime`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

from . import reasoning
from .local import LmStudioConfig, LocalEngines, VllmConfig

log = logging.getLogger("llmswitch.providers")

#: niente finestra console per i comandi ssh su Windows
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

TYPES: dict[str, dict] = {
    "lmstudio": {
        "label": "LM Studio", "auth": "none", "api": "lmstudio", "local": True,
        "base_url": "http://127.0.0.1:1234/v1",
        "note": "Server locale. Il modello già in memoria ha la precedenza.",
    },
    "vllm": {
        "label": "vLLM", "auth": "none", "api": "vllm", "local": True,
        "base_url": "",
        "note": "Istanze del manager vLLM: una viva si riusa com'è.",
    },
    "llamacpp": {
        "label": "llama.cpp (llama-server)", "auth": "none", "api": "openai",
        "local": False,
        "base_url": "http://127.0.0.1:8080/v1",
        # `local` è False perché la regola della VRAM non lo sa interrogare: llama-server
        # serve il modello che gli è stato dato all'avvio, uno solo, e non ha un'API di stato
        # come LM Studio né un manager come vLLM. Non c'è niente da risolvere.
        "note": "Server di llama.cpp, OpenAI-compatibile. Porta 8080 salvo `--port`. "
                "Serve il modello passato all'avvio: non se ne cambia da qui.",
    },
    # Server llama.cpp (fork PrismML) dedicato a Bonsai 2, di solito su un'altra macchina.
    # `local` è False come per llamacpp: serve un solo modello dato all'avvio e non ha un'API
    # di stato. La novità è `activation`: il server occupa ~15 GB di VRAM, quindi si accende
    # ON-DEMAND via SSH e si spegne quando non serve. Con `endpoint_for(carica=True)`
    # llmswitch lo accende da sé e attende /health; con `carica=False` lo lascia com'è — non
    # prende VRAM di iniziativa propria.
    #
    # Indirizzo, host SSH e comandi dipendono dalla macchina di chi lo ospita: non stanno qui
    # ma nella voce del registro, che è fuori dal progetto. Una voce tipica:
    #   {"type": "bonsai", "base_url": "http://<host>:7707/v1",
    #    "model": "bonsai-2-27b-abliterated",
    #    "activation": {"ssh_host": "<utente>@<host>", "start": "<comando>",
    #                   "stop": "<comando>", "health_url": "http://<host>:7707/health",
    #                   "wait_seconds": 120, "ssh_timeout": 60}}
    "bonsai": {
        "label": "Bonsai 2 27B (self-hosted)", "auth": "none", "api": "openai",
        "local": False,
        "base_url": "",
        "note": "Ternary-Bonsai-2-27B abliterated (PQ2_0), fork PrismML. Model id "
                "`bonsai-2-27b-abliterated`. Indirizzo e attivazione SSH (~15 GB VRAM) vanno "
                "nella voce del registro. Reasoning model: tieni max_tokens alto. UNCENSORED: "
                "nessun filtro contenuti.",
    },
    "openai": {
        "label": "OpenAI", "auth": "api_key", "api": "openai", "local": False,
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "console": "https://platform.openai.com/api-keys",
        "note": "API key (pay-per-use): l'abbonamento ChatGPT non copre le "
                "chiamate di app terze.",
    },
    "claude": {
        "label": "Claude (Anthropic)", "auth": "api_key", "api": "anthropic",
        "local": False, "base_url": "https://api.anthropic.com/v1",
        "chat_base_url": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
        "console": "https://console.anthropic.com/settings/keys",
        "note": "Solo API key: dal 4 aprile 2026 i token OAuth delle "
                "subscription non valgono fuori dai client Anthropic.",
    },
    "gemini": {
        "label": "Google Gemini", "auth": "either", "api": "gemini",
        "local": False,
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "chat_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "console": "https://aistudio.google.com/app/apikey",
        "cli_creds": "~/.gemini/oauth_creds.json",
        "note": "API key oppure OAuth Google. Da settembre 2026 le API key "
                "\"standard\" vengono rifiutate: servono le auth key.",
    },
    "grok": {
        "label": "Grok (xAI)", "auth": "either", "api": "openai", "local": False,
        "base_url": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
        "console": "https://console.x.ai/",
        "cli_creds": "~/.grok/auth.json",
        "note": "API key, o OAuth 2.0 PKCE per usare l'abbonamento SuperGrok / "
                "X Premium+.",
    },
    # Qwen ha due porte diverse, con conti diversi: la chiave di Model Studio (a consumo) e
    # l'abbonamento via OAuth, che passa per un endpoint tutto suo.
    "qwen": {
        "label": "Qwen (Alibaba Model Studio)", "auth": "api_key", "api": "openai",
        "local": False,
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "console": "https://modelstudio.console.alibabacloud.com/",
        "note": "Endpoint internazionale. Quello cinese è "
                "dashscope.aliyuncs.com/compatible-mode/v1, con chiavi separate.",
    },
    # --- abbonamenti -------------------------------------------------------------------
    # Si paga già un piano: lo si usa, invece di aprire un conto a consumo accanto. Le
    # credenziali arrivano da `llmswitch.oauth`, che le prende dallo strumento ufficiale già
    # autenticato e le rinnova. Gli id coincidono con quelli di `oauth.ABBONAMENTI`.
    "qwen_oauth": {
        "label": "Qwen", "auth": "oauth", "api": "openai",
        "local": False,
        "base_url": "https://portal.qwen.ai/v1",
        "cli_creds": "~/.qwen/oauth_creds.json",
        "token_url": "https://chat.qwen.ai/api/v1/oauth2/token",
        "console": "https://chat.qwen.ai/",
        "note": "Riusa il login del CLI `qwen`: niente da incollare, ma il CLI dev'essere "
                "già autenticato. Il tier gratuito da 2000 richieste/giorno è chiuso dal "
                "15 aprile 2026.",
    },
    "codex_oauth": {
        "label": "ChatGPT (piano Codex)", "auth": "oauth", "api": "openai",
        "local": False,
        "base_url": "https://chatgpt.com/backend-api/codex",
        "cli_creds": "~/.codex/auth.json",
        "token_url": "https://auth.openai.com/oauth/token",
        "console": "https://chatgpt.com/",
        "note": "Riusa il login del CLI `codex`. Il servizio pretende anche l'id dell'account, "
                "che sta nelle stesse credenziali.",
    },
    "grok_oauth": {
        "label": "Grok (SuperGrok / X Premium+)", "auth": "oauth", "api": "openai",
        "local": False,
        "base_url": "https://api.x.ai/v1",
        "cli_creds": "~/.grok/auth.json",
        "token_url": "https://auth.x.ai/oauth2/token",
        "console": "https://grok.com/",
        "note": "Riusa il login del CLI `grok`. I token durano circa sei ore e si rinnovano "
                "con un'ora di anticipo, per non spirare a metà di un lavoro lungo.",
    },
    "gemini_oauth": {
        "label": "Gemini (piano Google AI Pro)", "auth": "oauth", "api": "cloudcode",
        "local": False,
        "base_url": "https://cloudcode-pa.googleapis.com",
        "cli_creds": "~/.gemini/oauth_creds.json",
        "token_url": "https://oauth2.googleapis.com/token",
        "console": "https://gemini.google.com/",
        # `api` non è "openai" apposta: Cloud Code parla un protocollo suo — `v1internal` con
        # `:generateContent`, e /v1/chat/completions risponde 404 (misurato). Chi accetta solo
        # endpoint in stile OpenAI deve poterlo escludere guardando questo campo.
        #
        # Ma il vero ostacolo non è il protocollo: al 15 agosto 2026 `loadCodeAssist` risponde
        # che il piano dei privati NON è più ammesso —
        #   "reasonCode": "UNSUPPORTED_CLIENT", "tierId": "free-tier",
        #   "please migrate to the Antigravity suite of products"
        # — e lascia solo `standard-tier`, a pagamento e con un progetto Google Cloud proprio.
        # Un adattatore non servirebbe a niente: è il server a rifiutare, non il formato.
        "note": "Riusa il login del CLI `gemini`, ma dal 2026 Google non ammette più i piani "
                "individuali su Cloud Code (UNSUPPORTED_CLIENT): rimanda ad Antigravity, "
                "oppure al piano a pagamento con progetto Google Cloud. Per l'uso a consumo "
                "c'è il fornitore `gemini` con la chiave.",
    },
    # Z.AI ha DUE endpoint con piani e quote separati: stessa chiave non vale
    # su entrambi, quindi sono due provider e non un'opzione dello stesso.
    "zai": {
        "label": "Z.AI (piano API)", "auth": "api_key", "api": "openai",
        "local": False, "base_url": "https://api.z.ai/api/paas/v4",
        "key_env": "ZAI_API_KEY",
        "console": "https://z.ai/manage-apikey/apikey-list",
        "note": "Endpoint generale (GLM), OpenAI-compatibile.",
    },
    "zai_coding": {
        "label": "Z.AI (piano coding)", "auth": "api_key", "api": "openai",
        "local": False, "base_url": "https://api.z.ai/api/coding/paas/v4",
        "key_env": "GLM_API_KEY",
        "console": "https://z.ai/manage-apikey/apikey-list",
        "note": "Endpoint del piano coding (GLM-5.2 / GLM-5.1): quota separata "
                "da quella del piano API.",
    },
    "nvidia": {
        "label": "NVIDIA NIM", "auth": "api_key", "api": "openai", "local": False,
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_env": "NVIDIA_API_KEY",
        "console": "https://build.nvidia.com/settings/api-keys",
        "note": "Modelli di build.nvidia.com, OpenAI-compatibile.",
    },
    "deepseek": {
        "label": "DeepSeek", "auth": "api_key", "api": "openai", "local": False,
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "console": "https://platform.deepseek.com/api_keys",
        "note": "OpenAI-compatibile. Espone anche il dialetto Anthropic su /anthropic.",
    },
    "kimi": {
        "label": "Kimi (Moonshot AI)", "auth": "api_key", "api": "openai",
        "local": False, "base_url": "https://api.moonshot.ai/v1",
        "key_env": "KIMI_API_KEY",
        "console": "https://platform.moonshot.ai/console/api-keys",
        "note": "Endpoint internazionale. Quello cinese è api.moonshot.cn/v1, con "
                "chiavi e conto separati.",
    },
    "nebius": {
        "label": "Nebius Token Factory", "auth": "api_key", "api": "openai",
        "local": False, "base_url": "https://api.tokenfactory.nebius.com/v1",
        "key_env": "NEBIUS_API_KEY",
        "console": "https://tokenfactory.nebius.com/",
        "note": "Modelli aperti a consumo, OpenAI-compatibile.",
    },
    # Gira sulla macchina, ma NON è `local` per questa libreria: qui `local` significa "passa
    # dalla regola della VRAM", che si informa su `/api/v0/models` di LM Studio o sul manager
    # vLLM. Unsloth non ha nessuno dei due e il modello lo sceglie lui all'avvio
    # (`unsloth run --model …`), uno per volta: non c'è niente da risolvere, l'endpoint serve
    # quello che ha caricato.
    "unsloth": {
        "label": "Unsloth", "auth": "api_key",
        "api": "openai", "local": False,
        "base_url": "http://localhost:8000/v1",
        "key_env": "UNSLOTH_API_KEY",
        "note": "Server locale su llama.cpp. All'avvio stampa in console indirizzo e "
                "chiave (`sk-unsloth-…`); la porta è 8000 o 8888 secondo come è stato "
                "lanciato. Espone anche il dialetto Anthropic su /v1/messages.",
    },
    "openai_compat": {
        "label": "Generico (OpenAI-compatibile)", "auth": "api_key",
        "api": "openai", "local": False, "base_url": "",
        "note": "Qualunque server che espone /v1/chat/completions.",
    },
    "codex_cli": {
        "label": "OpenAI via Codex CLI (abbonamento)", "auth": "cli",
        "api": "cli", "local": True, "base_url": "",
        "command": "codex exec --skip-git-repo-check",
        "cli_creds": "~/.codex/auth.json",
        "note": "Abbonamento ChatGPT tramite il CLI ufficiale. ~14s a "
                "risposta: non adatto alla voce.",
    },
    "claude_cli": {
        "label": "Claude via Claude Code (abbonamento)", "auth": "cli",
        "api": "cli", "local": True, "base_url": "", "command": "claude -p",
        "cli_creds": "~/.claude/.credentials.json",
        "note": "Abbonamento Claude Pro/Max tramite il CLI ufficiale. Il più rapido dei tre: "
                "~4s a risposta (misurato).",
    },
    "antigravity": {
        "label": "Gemini via Antigravity CLI (abbonamento)", "auth": "cli",
        "api": "cli", "local": True, "base_url": "",
        "command": "agy --input-format stream-json --output-format stream-json",
        # Unico dei tre CLI a saper elencare i modelli: l'abbonamento non dà
        # solo Gemini, ma anche Claude e GPT-OSS.
        "models_command": "agy models",
        "note": "L'unica via rimasta per un abbonamento Google: dal 2026 Cloud Code rifiuta i "
                "piani individuali (UNSUPPORTED_CLIENT) e rimanda qui. ~10s a risposta. "
                "Il prompt viaggia come NDJSON su stdin (`stream-json`): passarlo come argomento "
                "di `-p` sfora la command line di Windows su un testo lungo ([WinError 206]).",
    },
}

#: consumatori predefiniti; ogni progetto può passare i propri
DEFAULT_CONSUMERS = ("chat", "copilot", "embed")


def default_registry_path(app_name: str = "llmswitch") -> Path:
    """File del registro, fuori dalla cartella del progetto."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME") \
        or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / app_name / "llm_providers.json"


# --- attivazione SSH di un server on-demand ---------------------------------------
# Alcuni motori (es. Bonsai) girano su un'altra macchina e occupano molta VRAM: vanno
# accesi solo quando servono. I parametri stanno nella voce del provider
# (`prov["activation"]`); un tipo può dare un default in `TYPES[tipo]["activation"]`, ma
# solo se non contiene indirizzi o percorsi di una macchina precisa.

def activation_of(prov: dict, spec: dict) -> dict | None:
    """Parametri di attivazione SSH: quelli del provider hanno precedenza sul tipo."""
    act = prov.get("activation") or spec.get("activation")
    return act if isinstance(act, dict) and act.get("start") else None


def server_up(health_url: str, timeout: float = 3.0) -> bool:
    """True se /health risponde 200."""
    if not health_url:
        return False
    try:
        return requests.get(health_url, timeout=timeout).status_code == 200
    except requests.RequestException:
        return False


def _ssh(host: str, command: str, timeout: float) -> tuple[bool, str]:
    if not host or not command:
        return False, "attivazione: mancano ssh_host o il comando"
    try:
        p = subprocess.run(["ssh", host, command], capture_output=True, text=True,
                           timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"ssh fallito: {e}"
    if p.returncode != 0:
        return False, f"ssh uscito con {p.returncode}: {(p.stderr or p.stdout or '').strip()[:200]}"
    return True, (p.stdout or "").strip()


def activate_server(act: dict) -> tuple[bool, str]:
    """Accende il server via SSH (se non già su) e attende /health. Idempotente."""
    health = act.get("health_url") or ""
    if server_up(health):
        return True, "già attivo"
    ok, msg = _ssh(act.get("ssh_host") or "", act.get("start") or "",
                   float(act.get("ssh_timeout", 60)))
    if not ok:
        return False, f"avvio: {msg}"
    if not health:
        return True, "avviato (nessun health_url da verificare)"
    deadline = time.monotonic() + float(act.get("wait_seconds", 120))
    while time.monotonic() < deadline:
        if server_up(health):
            return True, "attivo"
        time.sleep(2.0)
    return False, "avviato ma /health non risponde entro il timeout"


def stop_server(act: dict) -> tuple[bool, str]:
    """Spegne il server via SSH per liberare la VRAM."""
    ok, msg = _ssh(act.get("ssh_host") or "", act.get("stop") or "",
                   float(act.get("ssh_timeout", 60)))
    return (True, "spento") if ok else (False, f"spegnimento: {msg}")


class Registry:
    """Provider configurati e loro assegnazioni, su file JSON."""

    def __init__(self, path: str | os.PathLike | None = None,
                 app_name: str = "llmswitch",
                 consumers: tuple[str, ...] = DEFAULT_CONSUMERS,
                 engines: LocalEngines | None = None,
                 models_ttl: float = 300.0) -> None:
        self.path = Path(path) if path else default_registry_path(app_name)
        self.consumers = tuple(consumers)
        #: motori locali; se assente se ne crea uno dai default al bisogno
        self.engines = engines
        self._lock = threading.Lock()
        self._models_cache: dict[str, tuple[float, list[dict]]] = {}
        self._ttl = models_ttl

    # --- persistenza -------------------------------------------------------

    def _empty(self) -> dict:
        return {"version": 1, "providers": [], "assignments": {}}

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("providers", [])
        data.setdefault("assignments", {})
        for prov in data["providers"]:
            if isinstance(prov, dict):
                _migra_ragionamento(prov)
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    def _find(self, data: dict, pid: str) -> dict | None:
        return next((x for x in data.get("providers", []) if x.get("id") == pid), None)

    # --- motori locali -----------------------------------------------------

    def _engines_for(self, prov: dict) -> LocalEngines:
        """`LocalEngines` configurato con l'endpoint/modello di questo provider."""
        if self.engines is not None:
            return self.engines
        spec = TYPES.get(prov.get("type") or "", {})
        base = (prov.get("base_url") or spec.get("base_url") or "").strip()
        model = (prov.get("model") or "").strip()
        if prov.get("type") == "vllm":
            return LocalEngines(vllm=VllmConfig(manager_url=base or
                                                VllmConfig.manager_url, model=model),
                                prefer="vllm")
        return LocalEngines(LmStudioConfig(base_url=base or
                                           LmStudioConfig.base_url, model=model))

    # --- elenco modelli ----------------------------------------------------

    def models(self, pid: str, force: bool = False) -> list[dict]:
        """Modelli del provider, chiesti al momento (cache per i remoti)."""
        data = self.load()
        prov = self._find(data, pid)
        if prov is None:
            raise KeyError(f"provider sconosciuto: {pid}")
        spec = TYPES.get(prov.get("type") or "", {})
        api = spec.get("api")
        if api == "cli":
            return _cli_models(prov, spec)
        if spec.get("local"):
            eng = self._engines_for(prov)
            return eng.menu("vllm" if prov.get("type") == "vllm" else "lmstudio")
        hit = self._models_cache.get(pid)
        if hit and not force and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        base = (prov.get("base_url") or spec.get("base_url") or "").strip()
        key = (prov.get("api_key") or "").strip()
        token = ((prov.get("oauth") or {}).get("access_token") or "").strip()
        if api == "anthropic":
            out = _anthropic_models(base, key)
        elif api == "gemini":
            out = _gemini_models(base, key, token)
        else:
            out = _openai_models(base, token or key)
        self._models_cache[pid] = (time.monotonic(), out)
        return out

    def test(self, pid: str) -> dict:
        try:
            found = self.models(pid, force=True)
            return {"ok": True, "models": len(found),
                    "sample": [m["id"] for m in found[:5]]}
        except Exception as e:  # noqa: BLE001 - qualunque errore è un esito
            return {"ok": False, "error": str(e)[:300]}

    # --- uso ---------------------------------------------------------------

    def api_key_for(self, tipo: str) -> str:
        """Chiave del primo provider configurato di quel TIPO; "" se non ce n'e' nessuno.

        Serve ai programmi che scelgono il fornitore per conto loro — in una configurazione di
        progetto, per esempio — ma non vogliono tenersi le chiavi in casa. La scelta di COSA
        usare resta loro; QUI ci sono solo le credenziali, in un posto solo, fuori dalle
        cartelle dei progetti e quindi fuori dai commit."""
        tipo = (tipo or "").strip()
        if not tipo:
            return ""
        for prov in self.load().get("providers", []):
            if prov.get("type") == tipo and (prov.get("api_key") or "").strip():
                return str(prov["api_key"]).strip()
        return ""

    def set_api_key_for(self, tipo: str, chiave: str) -> dict:
        """Registra la chiave di quel TIPO, creando la voce se non c'era. Ritorna la voce.

        Simmetrico di `api_key_for`. Una chiave vuota SVUOTA il campo senza rimuovere il
        provider: chi la cancella per errore non deve perdere anche l'indirizzo e il modello
        che aveva scelto."""
        tipo = (tipo or "").strip()
        if tipo not in TYPES:
            raise ValueError(f"tipo sconosciuto: {tipo}")
        esistente = next((p for p in self.load().get("providers", [])
                          if p.get("type") == tipo), None)
        if esistente is None:
            return self.add({"type": tipo, "api_key": chiave})
        return self.update(esistente["id"], {"api_key": chiave})

    def provider_for(self, consumer: str) -> dict | None:
        data = self.load()
        pid = (data.get("assignments") or {}).get(consumer) or ""
        return self._find(data, pid) if pid else None

    def endpoint_for(self, consumer: str, embedding: bool = False,
                     carica: bool = True) -> tuple[str, str, str] | None:
        """(base_url, api_key, modello) per un client OpenAI-compatibile.

        Con `embedding=True` torna `embed_model` invece di `model`: chi
        vettorizza non è chi conversa, e usare lo stesso campo manderebbe un
        LLM alla /embeddings.

        Sui provider LOCALI il modello di chat lo decide la regola della VRAM,
        e qui torna gia' risolto. Con `carica=False` la regola si limita a
        GUARDARE cosa c'e' in memoria (`warm`) invece di caricare il modello
        preferito quando la memoria e' vuota (`resolve`): serve ai programmi
        che non devono prendersi la VRAM di iniziativa propria — quella
        decisione e' dell'utente, non dell'applicazione. A memoria vuota si
        torna al modello configurato, e sara' il server a dire la sua."""
        prov = self.provider_for(consumer)
        if prov is None:
            return None
        spec = TYPES.get(prov.get("type") or "", {})
        if embedding:
            modello = (prov.get("embed_model") or "").strip()
            if not modello:
                return None
            base = ((prov.get("base_url") or "").strip()
                    or spec.get("base_url") or "").strip()
            return base, (prov.get("api_key") or "local").strip(), modello
        if spec.get("local") and spec.get("api") != "cli":
            eng = self._engines_for(prov)
            preferito = "vllm" if prov.get("type") == "vllm" else "lmstudio"
            chiave = (prov.get("api_key") or "local").strip()
            if carica:
                r = eng.resolve(preferito)
                return r.base_url, chiave, r.model
            caldo = eng.warm(preferito)
            if caldo is not None and caldo.model:
                return caldo.base_url, chiave, caldo.model
            base = ((prov.get("base_url") or "").strip()
                    or spec.get("base_url") or "").strip()
            return base, chiave, (prov.get("model") or "").strip()
        # Server on-demand (es. Bonsai): se il consumer è disposto a prendere VRAM
        # (`carica=True`), accendiamo il server via SSH e attendiamo /health. Con
        # `carica=False` non si tocca: quella decisione è dell'utente, non dell'app.
        act = activation_of(prov, spec) if carica else None
        if act:
            ok, perche = activate_server(act)
            if not ok:
                log.warning("attivazione '%s' non riuscita: %s", prov.get("id"), perche)
        # Per chattare serve l'endpoint OpenAI-compatibile, che su Gemini e
        # Anthropic non è quello che elenca i modelli.
        base = ((prov.get("base_url") or "").strip()
                or spec.get("chat_base_url") or spec.get("base_url") or "").strip()
        key = (self._token_abbonamento(prov)
               or (prov.get("oauth") or {}).get("access_token")
               or prov.get("api_key") or "").strip()
        # Server senza autenticazione (Bonsai, llama.cpp): il client OpenAI vuole comunque
        # una chiave non vuota, anche se non viene verificata.
        if not key and spec.get("auth") == "none":
            key = "local"
        return base, key, (prov.get("model") or "").strip()

    @staticmethod
    def _token_abbonamento(prov: dict) -> str:
        """Token dell'abbonamento, preso dallo strumento ufficiale e rinnovato se serve.

        Senza questo, un provider ad abbonamento uscirebbe da `endpoint_for` con la chiave
        vuota e la prima richiesta prenderebbe un 401 — con l'utente convinto di averlo
        configurato, perché nel registro la voce c'è.

        Il rinnovo silenzioso sì, il login interattivo no: `endpoint_for` viene chiamata anche
        da programmi che macinano lavori senza nessuno davanti allo schermo, e fermarli in
        attesa di una conferma nel browser li pianterebbe fino al timeout.
        """
        tipo = (prov.get("type") or "").strip()
        try:
            from .oauth import ABBONAMENTI, access_token

            if tipo not in ABBONAMENTI:
                return ""
            return access_token(tipo, consenti_login=False)
        except Exception as e:  # noqa: BLE001 — non collegato è un caso normale, non un guasto
            log.debug("%s: nessun token d'abbonamento disponibile (%s)", tipo, e)
            return ""

    def headers_for(self, consumer: str) -> dict[str, str]:
        """Intestazioni HTTP complete per parlare col provider assegnato.

        Serve perché `endpoint_for` restituisce una chiave sola, e a certi servizi non basta:
        il backend di Codex pretende anche l'id dell'account, e senza quello risponde picche
        pur avendo un token valido.
        """
        prov = self.provider_for(consumer)
        if prov is None:
            return {}
        tipo = (prov.get("type") or "").strip()
        try:
            from .oauth import ABBONAMENTI, intestazioni

            if tipo in ABBONAMENTI:
                return intestazioni(tipo, consenti_login=False)
        except Exception as e:  # noqa: BLE001
            log.debug("%s: intestazioni d'abbonamento non disponibili (%s)", tipo, e)
        risolto = self.endpoint_for(consumer, carica=False)
        chiave = risolto[1] if risolto else ""
        return {"Authorization": f"Bearer {chiave}"} if chiave else {}

    def reasoning_for(self, consumer: str, level: str = reasoning.INHERIT, *,
                      base: dict | None = None, max_tokens: int | None = None) -> dict:
        """Il ragionamento di una richiesta al fornitore assegnato a `consumer`: il punto unico in cui un livello
        diventa parametri (`llmswitch.reasoning`).

        `level` è quello della richiesta (`inherit`: quello del fornitore). Restituisce
        `{"level", "dialect", "params", "max_tokens"}`: `params` sono i campi da fondere nel corpo della richiesta
        (`base`, se c'è, senza le sue vecchie chiavi del ragionamento: mai due dialetti insieme); `max_tokens` è il tetto
        visibile più il margine del livello, se si è passato `max_tokens`. `level` nel risultato è `None` quando anche
        il fornitore eredita: non si manda niente e decide il server."""
        if not reasoning.valid(level):
            raise ValueError(f"livello di ragionamento sconosciuto: {level!r}")
        prov = self.provider_for(consumer) or {}
        spec = TYPES.get(prov.get("type") or "", {})
        dialetto = reasoning.dialect(prov.get("type") or "", spec.get("api", ""))
        livello = reasoning.resolve(prov.get("reasoning_level"), level)
        if dialetto == reasoning.NO_DIALECT:
            # Un CLI ad abbonamento non ha parametri per il ragionamento: niente da mandare.
            return {"level": livello, "dialect": dialetto, "params": dict(base or {}), "max_tokens": max_tokens}
        params = reasoning.params_for(livello, dialetto, base)
        tetto = reasoning.max_tokens_for(max_tokens, livello, params) if max_tokens else max_tokens
        return {"level": livello, "dialect": dialetto, "params": params, "max_tokens": tetto}

    # --- modifica ----------------------------------------------------------

    def add(self, prov: dict) -> dict:
        tipo = (prov.get("type") or "").strip()
        if tipo not in TYPES:
            raise ValueError(f"tipo sconosciuto: {tipo}")
        with self._lock:
            data = self.load()
            pid = (prov.get("id") or "").strip() or self._new_id(data, tipo)
            if self._find(data, pid) is not None:
                raise ValueError(f"esiste già un provider con id '{pid}'")
            voce = {"id": pid, "type": tipo,
                    "name": (prov.get("name") or TYPES[tipo]["label"]).strip(),
                    "base_url": (prov.get("base_url") or "").strip(),
                    "model": (prov.get("model") or "").strip(),
                    # Il modello che VETTORIZZA è un'altra cosa da quello che
                    # conversa: nello stesso campo, assegnare un provider sia
                    # alla chat sia al RAG manderebbe un LLM alla /embeddings.
                    "embed_model": (prov.get("embed_model") or "").strip(),
                    "api_key": (prov.get("api_key") or "").strip()}
            # Attivazione SSH per-provider (sovrascrive il default del tipo); assente = usa il tipo.
            if isinstance(prov.get("activation"), dict) and prov["activation"]:
                voce["activation"] = prov["activation"]
            # Il livello di ragionamento predefinito del fornitore (`inherit`: non si dice niente al server). Una voce
            # scritta col vecchio booleano (`ragiona`…) diventa `medium`/`none`.
            voce["reasoning_level"] = _livello_ammesso(prov.get("reasoning_level"))
            if "reasoning_level" not in prov:
                vecchio = {k: prov[k] for k in reasoning.LEGACY_BOOL_KEYS if isinstance(prov.get(k), bool)}
                if vecchio:
                    voce["reasoning_level"] = reasoning.from_bool(next(iter(vecchio.values())))
            data["providers"].append(voce)
            self.save(data)
            return voce

    @staticmethod
    def _new_id(data: dict, tipo: str) -> str:
        esistenti = {p.get("id") for p in data.get("providers", [])}
        if tipo not in esistenti:
            return tipo
        n = 2
        while f"{tipo}-{n}" in esistenti:
            n += 1
        return f"{tipo}-{n}"

    def update(self, pid: str, campi: dict) -> dict:
        with self._lock:
            data = self.load()
            prov = self._find(data, pid)
            if prov is None:
                raise KeyError(pid)
            for k in ("name", "base_url", "model", "embed_model", "api_key"):
                if k in campi:
                    prov[k] = (campi[k] or "").strip()
            if "reasoning_level" in campi:
                prov["reasoning_level"] = _livello_ammesso(campi["reasoning_level"])
            # `activation` è un dict, non una stringa: lo si imposta o lo si azzera.
            if "activation" in campi:
                act = campi["activation"]
                if isinstance(act, dict) and act:
                    prov["activation"] = act
                else:
                    prov.pop("activation", None)
            self.save(data)
            self._models_cache.pop(pid, None)
            return prov

    # --- attivazione on-demand (SSH) ---------------------------------------

    def _activation(self, pid: str) -> dict:
        prov = self._find(self.load(), pid)
        if prov is None:
            raise KeyError(pid)
        act = activation_of(prov, TYPES.get(prov.get("type") or "", {}))
        if act is None:
            raise ValueError(f"il provider '{pid}' non ha un'attivazione SSH")
        return act

    def activate(self, pid: str) -> tuple[bool, str]:
        """Accende via SSH il server del provider (idempotente) e attende /health."""
        return activate_server(self._activation(pid))

    def deactivate(self, pid: str) -> tuple[bool, str]:
        """Spegne via SSH il server del provider, liberando la VRAM."""
        return stop_server(self._activation(pid))

    def is_active(self, pid: str) -> bool:
        """True se il server del provider risponde su /health."""
        return server_up(self._activation(pid).get("health_url") or "")

    def remove(self, pid: str) -> None:
        with self._lock:
            data = self.load()
            data["providers"] = [p for p in data.get("providers", [])
                                 if p.get("id") != pid]
            # Nessun consumatore deve restare agganciato a un provider sparito.
            data["assignments"] = {k: v for k, v in (data.get("assignments") or {}).items()
                                   if v != pid}
            self.save(data)
            self._models_cache.pop(pid, None)

    def assign(self, consumer: str, pid: str | None) -> dict:
        if consumer not in self.consumers:
            raise ValueError(f"consumatore sconosciuto: {consumer}")
        with self._lock:
            data = self.load()
            if pid:
                if self._find(data, pid) is None:
                    raise KeyError(pid)
                data.setdefault("assignments", {})[consumer] = pid
            else:
                data.get("assignments", {}).pop(consumer, None)
            self.save(data)
            return data.get("assignments", {})

    # --- per la GUI --------------------------------------------------------

    def public(self) -> dict:
        """Registro SENZA segreti: le chiavi diventano un booleano."""
        data = self.load()
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
                "label": spec.get("label", p.get("type")),
                "note": spec.get("note", ""),
                "realtime": spec.get("api") != "cli",
                "command": spec.get("command", ""),
                "reasoning_level": reasoning.normalize(p.get("reasoning_level")),
                "reasoning_dialect": reasoning.dialect(p.get("type") or "", spec.get("api", "")),
            })
        return {"providers": fuori, "assignments": data.get("assignments", {}),
                "types": [{"type": k, "realtime": v.get("api") != "cli",
                           "reasoning_dialect": reasoning.dialect(k, v.get("api", "")),
                           **{kk: vv for kk, vv in v.items()
                              if kk in ("label", "auth", "local", "base_url", "note")}}
                          for k, v in TYPES.items()],
                "consumers": list(self.consumers),
                # Le scelte del menu a tendina del ragionamento (`static/livello-ragionamento.js`).
                "reasoning_levels": reasoning.choices("it")}


def _livello_ammesso(valore) -> str:
    """Il livello di ragionamento di una voce: vuoto o assente → `inherit`; uno sconosciuto è un errore."""
    if valore is None or (isinstance(valore, str) and not valore.strip()):
        return reasoning.INHERIT
    pulito = valore.strip().lower() if isinstance(valore, str) else valore
    if not reasoning.valid(pulito):
        raise ValueError(f"livello di ragionamento sconosciuto: {valore!r} "
                         f"(ammessi: {', '.join(reasoning.CHOICES)})")
    return pulito


def _migra_ragionamento(prov: dict) -> None:
    """Una voce scritta prima dei livelli: il vecchio booleano (`ragiona`, `reasoning`, `thinking`) diventa
    `reasoning_level` (`medium`/`none`) e sparisce. Si salva alla prossima scrittura del registro."""
    vecchie = [k for k in reasoning.LEGACY_BOOL_KEYS if isinstance(prov.get(k), bool)]
    if not vecchie:
        return
    if not reasoning.valid(prov.get("reasoning_level")):
        prov["reasoning_level"] = reasoning.from_bool(prov[vecchie[0]])
    for k in vecchie:
        prov.pop(k)


# --- elenchi modelli per famiglia di API --------------------------------------

def _openai_models(base_url: str, api_key: str, extra: dict | None = None) -> list[dict]:
    import requests
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers.update(extra or {})
    r = requests.get(base_url.rstrip("/") + "/models", headers=headers, timeout=20)
    r.raise_for_status()
    return [{"id": str(m["id"]), "label": str(m["id"])}
            for m in (r.json() or {}).get("data") or [] if m.get("id")]


def _anthropic_models(base_url: str, api_key: str) -> list[dict]:
    import requests
    r = requests.get(base_url.rstrip("/") + "/models", timeout=20, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    return [{"id": str(m["id"]), "label": str(m.get("display_name") or m["id"])}
            for m in (r.json() or {}).get("data") or [] if m.get("id")]


def _gemini_models(base_url: str, api_key: str, token: str = "") -> list[dict]:
    import requests
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params = {} if token else {"key": api_key}
    r = requests.get(base_url.rstrip("/") + "/models", headers=headers,
                     params=params, timeout=20)
    r.raise_for_status()
    out = []
    for m in (r.json() or {}).get("models") or []:
        name = str(m.get("name") or "").split("/")[-1]
        metodi = m.get("supportedGenerationMethods") or []
        # Solo chi sa generare testo: l'elenco include embedding e altro, che
        # in un menu "modello di chat" sono rumore.
        if name and (not metodi or "generateContent" in metodi):
            out.append({"id": name, "label": str(m.get("displayName") or name)})
    return out


def _cli_models(prov: dict, spec: dict) -> list[dict]:
    """Provider "CLI ufficiale": nessun endpoint da interrogare, si chiede al programma.

    L'interrogazione vera sta in `agents`, che è dove i CLI vengono eseguiti: tenerne due
    copie significherebbe correggere ogni difetto due volte — ed è il difetto che questa
    libreria esiste per togliere agli altri progetti."""
    from .agents import AGENTI, MODELLO_SEGNAPOSTO, disponibile
    from .agents import modelli as _modelli_agente

    tipo = (prov.get("type") or "").strip()
    if tipo in AGENTI:
        if not disponibile(tipo):
            raise RuntimeError(f"comando '{AGENTI[tipo].comando[0]}' non trovato nel PATH")
        trovati = _modelli_agente(tipo)
        if trovati:
            return trovati
    else:
        exe = (prov.get("base_url") or spec.get("command") or "").split()
        if not exe or shutil.which(exe[0]) is None:
            raise RuntimeError(f"comando '{exe[0] if exe else '?'}' non trovato nel PATH")
    # Chi non sa elencare i suoi modelli usa quello che ha configurato al proprio interno.
    scelto = (prov.get("model") or "").strip()
    return [{"id": scelto or MODELLO_SEGNAPOSTO,
             "label": scelto or "(quello configurato nel CLI)"}]
