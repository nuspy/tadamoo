"""Client verso wikibridge (:8775) — conoscenza per progetto.

wikibridge è il repo separato C:\\Projects\\wikibridge: LLM Wiki locale
organizzata per progetti. Da qui callAPIcall:
- elenca i progetti (dropdown GUI via control server, se serve dal backend)
- dà al cervello la conoscenza del progetto collegato (project_context) —
  SOLO per quel progetto: l'isolamento tra progetti sta qui
- pusha le riunioni analizzate (contenuto, accordi, attività) nella
  sezione meetings/ del wiki del progetto
Il server wikibridge non ha un autostart: ensure_server() lo avvia
on-demand (processo staccato, invisibile) e attende che risponda.

Accesso alla conoscenza (knowledge_access):
- "path": il preamble punta alla wiki su disco e l'agente la legge da sé.
  Va bene solo single-user: leggere il disco scavalca ogni permesso.
- "api": per ogni richiesta si chiedono a wikibridge (/rag/context) gli
  estratti pertinenti, già filtrati per i permessi dell'utente per cui si
  fa inferenza (knowledge_username) e, nelle riunioni, dei partecipanti.
  Se wikibridge non risponde il cervello NON riceve conoscenza: mai un
  ripiego sul disco.
- "auto": "api" se wikibridge è in modalità multi-utente (identity provider).
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import Config
from .util import is_safe_artifact_id

log = logging.getLogger("callbridge.knowledge")

_start_lock = threading.Lock()

# Sessione wikibridge dell'utente (solo in memoria: il token non va su disco).
_session_lock = threading.Lock()
_login_lock = threading.Lock()          # un solo login (o finestra SSO) alla volta
_session: dict = {"token": None, "user": None}
_mode_cache: dict = {"multi_user": None, "login": None, "sso_login": None, "at": 0.0}
_MODE_TTL_S = 60.0
SSO_TIMEOUT_S = 300


def _headers(cfg: Config, token: str | None = None) -> dict:
    """Chiave applicazione in X-Wikibridge-Key (accettata da wikibridge sia
    single- sia multi-utente) + sessione utente se presente."""
    h = {}
    if cfg.knowledge_api_token:
        h["X-Wikibridge-Key"] = cfg.knowledge_api_token
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def available(cfg: Config) -> bool:
    try:
        r = requests.get(f"{cfg.knowledge_api_url}/health",
                         headers=_headers(cfg), timeout=3)
        return r.ok
    except requests.RequestException:
        return False


def multi_user(cfg: Config) -> bool:
    """wikibridge ha un identity provider (ACL per utente)? In cache 60 s."""
    now = time.monotonic()
    if _mode_cache["multi_user"] is not None and now - _mode_cache["at"] < _MODE_TTL_S:
        return _mode_cache["multi_user"]
    try:
        r = requests.get(f"{cfg.knowledge_api_url}/health",
                         headers=_headers(cfg), timeout=3)
        data = r.json() if r.ok else {}
        value = bool(data.get("multi_user"))
    except (requests.RequestException, ValueError):
        return bool(_mode_cache["multi_user"])
    _mode_cache.update(multi_user=value, login=data.get("login"),
                       sso_login=data.get("sso_login"), at=now)
    return value


def access_mode(cfg: Config) -> str:
    mode = (cfg.knowledge_access or "auto").lower()
    if mode in ("path", "api"):
        return mode
    return "api" if multi_user(cfg) else "path"


def login_sso(cfg: Config, open_url=webbrowser.open,
              timeout_s: float = SSO_TIMEOUT_S) -> dict:
    """Login SSO (OIDC o SAML) su wikibridge: apre il browser sul login
    aziendale e riceve il token di sessione su un mini-server loopback
    (porta effimera, solo 127.0.0.1)."""
    result: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - API di http.server
            token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if token:
                result["token"] = token
            ok = bool(token)
            page = ("Login wikibridge completato: puoi chiudere questa scheda."
                    if ok else "Login wikibridge non riuscito.")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        def log_message(self, *args):  # niente token nei log
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.timeout = 0.5
    return_to = f"http://127.0.0.1:{srv.server_port}/wb-callback"
    path = _mode_cache.get("sso_login") or "/identity/oidc/login"
    url = f"{cfg.knowledge_api_url}{path}?{urlencode({'return_to': return_to})}"
    log.info("login SSO wikibridge: apro il browser")
    threading.Thread(target=open_url, args=(url,), daemon=True).start()
    deadline = time.monotonic() + timeout_s
    try:
        while "token" not in result and time.monotonic() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()
    if "token" not in result:
        raise RuntimeError("login SSO wikibridge non completato")
    with _session_lock:
        _session.update(token=result["token"], user="sso")
    return {"user": "sso"}


def login(cfg: Config, username: str | None = None,
          password: str | None = None) -> dict:
    """Login su wikibridge per l'utente di inferenza: SSO nel browser se
    wikibridge usa OIDC o SAML, altrimenti LDAP/AD con le credenziali di config
    (password da CALLBRIDGE_KNOWLEDGE_PASSWORD)."""
    if _mode_cache.get("login") == "sso" and not (username and password):
        with _login_lock:
            if _session["token"]:
                return {"user": _session["user"]}
            return login_sso(cfg)
    username = username or cfg.knowledge_username
    password = password or cfg.knowledge_password
    if not username or not password:
        raise RuntimeError("wikibridge multi-utente: imposta knowledge_username "
                           "e CALLBRIDGE_KNOWLEDGE_PASSWORD")
    r = requests.post(f"{cfg.knowledge_api_url}/identity/login",
                      json={"username": username, "password": password},
                      headers=_headers(cfg), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"login wikibridge fallito ({r.status_code}): "
                           f"{r.json().get('error', '') if r.content else ''}")
    data = r.json()
    with _session_lock:
        _session.update(token=data["token"], user=username)
    log.info("sessione wikibridge aperta per %s", username)
    return {"user": username, "roles": data.get("roles", [])}


def whoami(cfg: Config) -> dict:
    """Chi è la sessione corrente per wikibridge (nome, ruoli, admin).
    {} se non c'è una sessione o wikibridge l'ha rifiutata."""
    token = _session["token"]
    if not token:
        return {}
    try:
        r = requests.get(f"{cfg.knowledge_api_url}/identity/whoami",
                         headers=_headers(cfg, token), timeout=5)
    except requests.RequestException:
        return {}
    if r.status_code == 401:
        with _session_lock:
            if _session["token"] == token:
                _session.update(token=None, user=None)   # scaduta o revocata
        return {}
    return r.json() if r.ok else {}


def session_info(cfg: Config) -> dict:
    """Stato per la GUI: modalità di wikibridge e sessione corrente. Il
    token e la chiave applicazione servono alla GUI per chiamare
    wikibridge direttamente con la stessa sessione (restano in memoria)."""
    multi = multi_user(cfg)
    info = {"multi_user": multi, "login": _mode_cache.get("login"),
            "sso_pending": _sso_state["pending"], "sso_error": _sso_state["error"],
            "app_key": cfg.knowledge_api_token or ""}
    me = whoami(cfg) if multi else {}
    if me.get("authenticated"):
        info.update(logged_in=True, token=_session["token"],
                    display_name=me.get("display_name") or _session["user"],
                    roles=me.get("roles", []), admin=bool(me.get("admin")),
                    clearance=me.get("clearance"))
    else:
        info["logged_in"] = False
    return info


_sso_state: dict = {"pending": False, "error": None}


def login_sso_async(cfg: Config) -> bool:
    """Avvia il login SSO in un thread (la GUI non resta appesa fino a 5
    minuti) e ritorna subito; l'esito si legge con session_info(). False se
    un login SSO è già in corso."""
    with _session_lock:          # non _login_lock: un login in corso lo tiene
        if _sso_state["pending"]:
            return False
        _sso_state.update(pending=True, error=None)

    def run():
        try:
            with _login_lock:
                login_sso(cfg)
        except Exception as e:  # noqa: BLE001 - riferito alla GUI
            _sso_state["error"] = str(e)
        finally:
            _sso_state["pending"] = False

    threading.Thread(target=run, daemon=True, name="wikibridge-sso").start()
    return True


def logout(cfg: Config) -> None:
    with _session_lock:
        token = _session["token"]
        _session.update(token=None, user=None)
    if token:
        try:
            requests.post(f"{cfg.knowledge_api_url}/identity/logout",
                          headers=_headers(cfg, token), timeout=5)
        except requests.RequestException:
            pass


def _request(cfg: Config, method: str, path: str, **kwargs) -> requests.Response:
    """Richiesta all'API con la sessione utente se wikibridge è multi-utente:
    login automatico se manca, un nuovo login se la sessione è scaduta."""
    user_session = multi_user(cfg)
    if user_session and not _session["token"]:
        login(cfg)
    r = requests.request(method, f"{cfg.knowledge_api_url}{path}",
                         headers=_headers(cfg, _session["token"] if user_session else None),
                         **kwargs)
    if user_session and r.status_code == 401:
        with _session_lock:
            _session.update(token=None)            # scaduta o revocata
        login(cfg)
        r = requests.request(method, f"{cfg.knowledge_api_url}{path}",
                             headers=_headers(cfg, _session["token"]), **kwargs)
    return r


def ensure_server(cfg: Config, timeout_s: float = 20.0) -> bool:
    """Avvia wikibridge se è giù e attende /health (avvio on-demand).

    Processo staccato e senza finestra: sopravvive a chi lo ha lanciato.
    Se la porta è già occupata da un'altra istanza, il doppione muore da
    solo sul bind e il polling vede comunque il server buono.
    """
    if available(cfg):
        return True
    with _start_lock:
        if available(cfg):
            return True
        cmd = shlex.split(cfg.knowledge_serve_cmd, posix=False)
        if not cmd or not Path(cmd[0].strip('"')).is_file():
            log.error("wikibridge non installato: %s", cfg.knowledge_serve_cmd)
            return False
        flags = 0
        si = None
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            # SOLO CREATE_NO_WINDOW: il processo ottiene una console nascosta
            # che i figli (stub pip -> python venvlauncher -> python base)
            # EREDITANO, quindi nessuna finestra in tutta la catena.
            # NON combinare con DETACHED_PROCESS: per i docs Microsoft
            # CREATE_NO_WINDOW viene IGNORATO se passato con DETACHED_PROCESS,
            # il processo resta senza console e il primo figlio console se ne
            # alloca una nuova VISIBILE -> era questo il terminale che appariva.
            flags = subprocess.CREATE_NO_WINDOW
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
        try:
            subprocess.Popen([cmd[0].strip('"'), *cmd[1:]],
                             creationflags=flags,
                             startupinfo=si,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as e:
            log.error("avvio wikibridge fallito: %s", e)
            return False
        log.info("wikibridge in avvio on-demand (%s)", cfg.knowledge_serve_cmd)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if available(cfg):
                return True
        log.error("wikibridge non risponde dopo %.0fs", timeout_s)
        return False


def list_projects(cfg: Config) -> list[dict]:
    try:
        r = _request(cfg, "GET", "/projects", timeout=5)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, RuntimeError) as e:
        log.warning("wikibridge non raggiungibile: %s", e)
        return []


def transcript_query(segments: list[dict], max_chars: int = 4000) -> str:
    """Testo della trascrizione da usare come domanda di ricerca (le parti
    finali, dove di solito si arriva al punto)."""
    text = " ".join((s.get("text") or "") for s in segments).strip()
    return text[-max_chars:]


def project_context(cfg: Config, project_id: str | None, query: str,
                    participants: list[str] | None = None,
                    unresolved: str = "public") -> str:
    """Conoscenza del progetto per il cervello, secondo knowledge_access.

    In modalità "api" restituisce gli estratti filtrati per l'utente (e per
    l'intersezione con i partecipanti, se indicati). unresolved="public":
    un partecipante non riconosciuto dal directory riduce ai soli contenuti
    pubblici (per ciò che viene detto ai presenti); "ignore": non conta (per
    ciò che vedranno solo i riconosciuti, come la pagina della riunione).
    Su qualunque errore ritorna una nota senza conoscenza: mai un ripiego
    sulla wiki su disco, che scavalcherebbe i permessi."""
    if not project_id:
        return ""
    if access_mode(cfg) == "path":
        return project_preamble(cfg, project_id)
    if not is_safe_artifact_id(project_id):
        log.warning("project_id non valido rifiutato: %r", project_id)
        return ""
    if not query.strip():
        return ""
    body = {"project": project_id, "query": query,
            "limit": int(cfg.knowledge_context_blocks),
            "max_chars": int(cfg.knowledge_context_chars)}
    if participants:
        body["participants"] = participants
        body["unresolved"] = unresolved
    try:
        if not ensure_server(cfg):
            raise RuntimeError("wikibridge non raggiungibile")
        r = _request(cfg, "POST", "/rag/context", json=body, timeout=15)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, RuntimeError, ValueError) as e:
        log.error("conoscenza del progetto %s non disponibile: %s", project_id, e)
        return (f"[Conoscenza del progetto '{project_id}' non disponibile in "
                f"questo momento: rispondi senza, e non cercarla altrove.]\n\n")
    blocks = data.get("blocks", [])
    scope = ("dell'utente e di tutti i partecipanti" if participants
             else "dell'utente")
    if not blocks:
        return (f"[Conoscenza del progetto '{project_id}': nessun contenuto "
                f"pertinente e accessibile coi permessi {scope}.]\n\n")
    parts = [f"[Conoscenza del progetto '{project_id}': estratti selezionati per "
             f"questa richiesta, filtrati sui permessi {scope}. Usa solo questi; "
             f"non leggere la wiki dal disco e non usare conoscenza di altri "
             f"progetti.]"]
    for b in blocks:
        where = b.get("title") or ""
        if b.get("heading"):
            where += f" › {b['heading']}"            # sezione del documento
        parts.append(f"--- {where} ({b.get('path')}, "
                     f"v{b.get('version') or '?'}) ---\n{b.get('text', '')}")
    return "\n\n".join(parts) + "\n\n"


def project_preamble(cfg: Config, project_id: str | None) -> str:
    """Preamble con il puntatore alla wiki del progetto ('' se nessuno).

    Il path è in forma WSL (/mnt/c/...) perché a leggerlo è l'agente
    Hermes che gira in WSL.
    """
    if not project_id:
        return ""
    if not is_safe_artifact_id(project_id):
        # project_id finisce in un path su disco E nel prompt dell'agente: un
        # "..\..\Users\atain" supererebbe la root. Rifiuta gli id non sicuri.
        log.warning("project_id non valido rifiutato: %r", project_id)
        return ""
    root = Path(cfg.knowledge_root)
    wiki = root / "projects" / project_id / "wiki"
    if not wiki.is_dir():
        log.warning("wiki del progetto %s non trovata (%s)", project_id, wiki)
        return ""
    win_path = str(wiki).replace("\\", "/")  # es. "C:/Projects/..."
    if cfg.acp_use_wsl:
        # forma WSL (/mnt/<drive>/...): a leggerlo è l'agente Hermes in WSL.
        # La lettera di drive si deriva dal path reale (non piu' "c" fissa: la
        # knowledge_root puo' stare su un altro disco, es. E:).
        drive = win_path[0].lower() if len(win_path) > 1 and win_path[1] == ":" else "c"
        agent_path = f"/mnt/{drive}" + win_path[2:]
    else:
        # Hermes nativo su Windows: path Windows (slash avanti, tollerati da Win)
        agent_path = win_path
    return (
        f"[Conoscenza del progetto '{project_id}': leggi {agent_path}/index.md "
        f"e le pagine collegate pertinenti prima di rispondere su questo "
        f"progetto. Non usare conoscenza di altri progetti.]\n\n"
    )


def push_meeting(cfg: Config, project_id: str, *, meeting_id: str,
                 title: str, date: str, source: str,
                 participants: list[str], analysis: dict,
                 transcript_path: str = "") -> str | None:
    """Inserisce la riunione analizzata nel wiki. Ritorna il path o None.

    Avvia il server on-demand se necessario (fine riunione a server giù)."""
    if not ensure_server(cfg):
        return None
    payload = {
        "project": project_id,
        "meeting_id": meeting_id,
        "title": title,
        "date": date,
        "source": source,
        "participants": participants,
        "summary": analysis.get("summary", ""),
        "highlights": analysis.get("highlights", []),
        "agreements": analysis.get("agreements", []),
        "tasks": analysis.get("tasks", []),
        "transcript_path": transcript_path,
    }
    try:
        r = _request(cfg, "POST", "/meetings", json=payload, timeout=30)
        r.raise_for_status()
        path = r.json().get("path")
        log.info("riunione %s nel wiki di '%s': %s",
                 meeting_id, project_id, path)
        return path
    except (requests.RequestException, RuntimeError) as e:
        log.error("push riunione nel wiki fallito: %s", e)
        return None
