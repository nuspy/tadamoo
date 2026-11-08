"""Calendari via OAuth: Google Calendar + Microsoft Outlook (Graph).

Sola lettura eventi. L'utente registra le PROPRIE app (vedi INSTALL.md):
  - Google Cloud Console -> OAuth client "Desktop app" -> client_id/secret
    in config (calendar_google_client_id/secret). Flusso InstalledAppFlow
    con redirect su localhost (apre il browser).
  - Azure App Registration -> "Mobile and desktop applications" + public
    client -> client_id in config (calendar_ms_client_id). Flusso
    device-code (codice da inserire su microsoft.com/devicelogin).

Token cache su disco in call-bridge/secrets/ (gitignorata).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from .config import Config

log = logging.getLogger("callbridge.calendar")

SECRETS_DIR = Path(__file__).resolve().parents[1] / "secrets"
GOOGLE_TOKEN = SECRETS_DIR / "google_token.json"
MS_TOKEN = SECRETS_DIR / "ms_token_cache.json"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
MS_SCOPES = ["Calendars.Read"]

# Estrazione del link riunione da location/description/body degli eventi.
_MEETING_URL_RE = re.compile(
    r"https://(?:meet\.google\.com/[a-z0-9-]+"
    r"|teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+"
    r"|[a-z0-9.]*zoom\.us/j/[^\s\"'<>]+)", re.I)


def extract_meeting_url(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = _MEETING_URL_RE.search(t)
        if m:
            return m.group(0)
    return None


# --------------------------------------------------------------------------
#  Google
# --------------------------------------------------------------------------
class GoogleCalendar:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._creds = None
        self._flow_thread: threading.Thread | None = None
        self.flow_error: str | None = None
        # `_load_creds` fa refresh + riscrittura di google_token.json: watcher e
        # endpoint lo chiamano da thread diversi -> senza lock, doppio refresh e
        # possibile file token corrotto.
        self._creds_lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self.cfg.calendar_google_client_id
                    and self.cfg.calendar_google_client_secret)

    def connected(self) -> bool:
        return self._load_creds() is not None

    def _load_creds(self):
        with self._creds_lock:
            if self._creds is not None and self._creds.valid:
                return self._creds
            if not GOOGLE_TOKEN.is_file():
                return None
            try:
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials
                creds = Credentials.from_authorized_user_file(
                    str(GOOGLE_TOKEN), GOOGLE_SCOPES)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    GOOGLE_TOKEN.write_text(creds.to_json(), encoding="utf-8")
                self._creds = creds if creds.valid else None
            except Exception as e:  # noqa: BLE001
                log.warning("Google: token non utilizzabile: %s", e)
                self._creds = None
            return self._creds

    def start_connect(self) -> dict:
        """Avvia il flusso OAuth in un thread (apre il browser).
        Ritorna subito; l'esito si osserva con connected()."""
        if not self.configured():
            return {"error": "calendar_google_client_id/secret non configurati"}
        if self._flow_thread is not None and self._flow_thread.is_alive():
            return {"status": "in_corso"}

        def _run() -> None:
            self.flow_error = None
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_config(
                    {"installed": {
                        "client_id": self.cfg.calendar_google_client_id,
                        "client_secret": self.cfg.calendar_google_client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}},
                    GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0, open_browser=True,
                                              timeout_seconds=300)
                SECRETS_DIR.mkdir(exist_ok=True)
                GOOGLE_TOKEN.write_text(creds.to_json(), encoding="utf-8")
                self._creds = creds
                log.info("Google Calendar collegato")
            except Exception as e:  # noqa: BLE001
                self.flow_error = str(e)
                log.error("Google OAuth fallito: %s", e)

        self._flow_thread = threading.Thread(target=_run, name="google-oauth",
                                             daemon=True)
        self._flow_thread.start()
        return {"status": "browser_aperto",
                "detail": "completa l'accesso nel browser"}

    def disconnect(self) -> None:
        self._creds = None
        GOOGLE_TOKEN.unlink(missing_ok=True)

    def events(self, days: int = 7) -> list[dict]:
        creds = self._load_creds()
        if creds is None:
            return []
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=creds,
                    cache_discovery=False)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmax = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() + days * 86400))
        res = svc.events().list(calendarId="primary", timeMin=now,
                                timeMax=tmax, singleEvents=True,
                                orderBy="startTime", maxResults=50).execute()
        out = []
        for ev in res.get("items", []):
            url = (ev.get("hangoutLink")
                   or extract_meeting_url(ev.get("location"),
                                          ev.get("description")))
            out.append({
                "id": "g:" + ev["id"],
                "provider": "google",
                "title": ev.get("summary") or "(senza titolo)",
                "start": (ev.get("start") or {}).get("dateTime")
                         or (ev.get("start") or {}).get("date"),
                "end": (ev.get("end") or {}).get("dateTime")
                       or (ev.get("end") or {}).get("date"),
                "organizer": ((ev.get("organizer") or {}).get("email")),
                "meeting_url": url,
            })
        return out


# --------------------------------------------------------------------------
#  Microsoft (Graph)
# --------------------------------------------------------------------------
class MicrosoftCalendar:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._app = None
        self._cache = None
        self._flow: dict | None = None
        self._flow_thread: threading.Thread | None = None
        self.flow_error: str | None = None

    def configured(self) -> bool:
        return bool(self.cfg.calendar_ms_client_id)

    def _get_app(self):
        import msal
        if self._app is None:
            self._cache = msal.SerializableTokenCache()
            if MS_TOKEN.is_file():
                self._cache.deserialize(MS_TOKEN.read_text(encoding="utf-8"))
            self._app = msal.PublicClientApplication(
                self.cfg.calendar_ms_client_id,
                authority=("https://login.microsoftonline.com/"
                           + (self.cfg.calendar_ms_tenant or "common")),
                token_cache=self._cache)
        return self._app

    def _save_cache(self) -> None:
        if self._cache is not None and self._cache.has_state_changed:
            SECRETS_DIR.mkdir(exist_ok=True)
            MS_TOKEN.write_text(self._cache.serialize(), encoding="utf-8")

    def _token(self) -> str | None:
        if not self.configured():
            return None
        app = self._get_app()
        accounts = app.get_accounts()
        if accounts:
            res = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
            self._save_cache()
            if res and "access_token" in res:
                return res["access_token"]
        return None

    def connected(self) -> bool:
        try:
            return self._token() is not None
        except Exception:  # noqa: BLE001
            return False

    def start_connect(self) -> dict:
        """Device-code flow: ritorna user_code + verification_uri; il
        completamento avviene in background."""
        if not self.configured():
            return {"error": "calendar_ms_client_id non configurato"}
        if self._flow_thread is not None and self._flow_thread.is_alive():
            # Evita di aprire un secondo device-flow (thread bloccante fino a
            # ~15 min) mentre uno e' gia' in attesa di completamento.
            return {"status": "in_corso"}
        app = self._get_app()
        flow = app.initiate_device_flow(scopes=MS_SCOPES)
        if "user_code" not in flow:
            return {"error": f"device flow fallito: {flow.get('error_description')}"}
        self._flow = flow

        def _wait() -> None:
            self.flow_error = None
            try:
                res = app.acquire_token_by_device_flow(flow)  # bloccante
                self._save_cache()
                if "access_token" in res:
                    log.info("Microsoft Calendar collegato")
                else:
                    self.flow_error = res.get("error_description", "sconosciuto")
            except Exception as e:  # noqa: BLE001
                self.flow_error = str(e)

        self._flow_thread = threading.Thread(target=_wait, name="ms-oauth",
                                             daemon=True)
        self._flow_thread.start()
        return {"user_code": flow["user_code"],
                "verification_uri": flow["verification_uri"],
                "detail": flow.get("message")}

    def disconnect(self) -> None:
        self._app = None
        self._cache = None
        MS_TOKEN.unlink(missing_ok=True)

    def events(self, days: int = 7) -> list[dict]:
        import requests as rq
        token = self._token()
        if token is None:
            return []
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        tmax = time.strftime("%Y-%m-%dT%H:%M:%S",
                             time.gmtime(time.time() + days * 86400))
        r = rq.get(
            "https://graph.microsoft.com/v1.0/me/calendarView",
            params={"startDateTime": now, "endDateTime": tmax,
                    "$orderby": "start/dateTime", "$top": "50",
                    "$select": "subject,start,end,organizer,location,"
                               "onlineMeeting,bodyPreview"},
            headers={"Authorization": f"Bearer {token}",
                     "Prefer": 'outlook.timezone="UTC"'},
            timeout=20)
        if not r.ok:
            log.warning("Graph calendarView HTTP %s", r.status_code)
            return []
        out = []
        for ev in r.json().get("value", []):
            url = ((ev.get("onlineMeeting") or {}).get("joinUrl")
                   or extract_meeting_url(
                       (ev.get("location") or {}).get("displayName"),
                       ev.get("bodyPreview")))
            out.append({
                "id": "m:" + ev["id"],
                "provider": "microsoft",
                "title": ev.get("subject") or "(senza titolo)",
                "start": (ev.get("start") or {}).get("dateTime"),
                "end": (ev.get("end") or {}).get("dateTime"),
                "organizer": ((ev.get("organizer") or {})
                              .get("emailAddress") or {}).get("address"),
                "meeting_url": url,
            })
        return out
