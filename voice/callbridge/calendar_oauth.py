"""Calendars through OAuth (Google Calendar, Microsoft Outlook), read only, for the auto-join of meetings.

Off and inert until the person has made their own OAuth apps and put their values in the credential store (nothing is shipped):
  `meetings/google`     JSON {"client_id", "client_secret"}; a desktop-app client of a Google Cloud project
  `meetings/microsoft`  JSON {"client_id", "tenant"?}; a public-client app registration with the delegated permission Calendars.Read
The tokens the sign-in produces are kept in the same entries ("token" for Google, "refresh_token" for Microsoft), never in a file.
Without the values `configured()` is false, nothing is imported and no network is touched. Not proven against the real services."""
from __future__ import annotations

import json
import logging
import re
import threading
import time

log = logging.getLogger("callbridge.calendar")

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
MS_SCOPES = ["Calendars.Read"]
NAMES = {"google": "meetings/google", "microsoft": "meetings/microsoft"}

_MEETING_URL_RE = re.compile(
    r"https://(?:meet\.google\.com/[a-z0-9-]+|teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+|[a-z0-9.]*zoom\.us/j/[^\s\"'<>]+)", re.I)


def extract_meeting_url(*texts: str | None) -> str | None:
    for t in texts:
        m = _MEETING_URL_RE.search(t or "")
        if m:
            return m.group(0)
    return None


class Vault:
    """The entries `meetings/<provider>` of the credential store; a store that cannot be reached reads as empty."""

    def __init__(self, store=None) -> None:
        self.store = store

    def get(self, provider: str) -> dict:
        if self.store is None:
            return {}
        try:
            data = json.loads(self.store.read(NAMES[provider]) or "{}")
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def put(self, provider: str, data: dict) -> None:
        if self.store is not None:
            self.store.write(NAMES[provider], json.dumps(data))

    def set_token(self, provider: str, key: str, value: str | None) -> None:
        data = self.get(provider)
        if value:
            data[key] = value
        else:
            data.pop(key, None)
        self.put(provider, data)


def default_vault() -> Vault:
    try:
        from tadamoo.llm.credentials import default_store
        return Vault(default_store())
    except Exception as e:  # noqa: BLE001 (TaDaMoo's package is not reachable from this process)
        log.info("the credential store is not reachable: %s", e)
        return Vault(None)


class _Provider:
    name = ""
    NOT_CONFIGURED = ""

    def __init__(self, vault: Vault | None = None) -> None:
        self.vault = vault if vault is not None else default_vault()
        self.flow_error: str | None = None
        self._thread: threading.Thread | None = None

    def values(self) -> dict:
        return self.vault.get(self.name)

    def disconnect(self) -> None:
        self.vault.set_token(self.name, self.token_key, None)


class GoogleCalendar(_Provider):
    name, token_key = "google", "token"
    NOT_CONFIGURED = "put client_id and client_secret of your own Google OAuth client in the credential store entry meetings/google"

    def configured(self) -> bool:
        v = self.values()
        return bool(v.get("client_id") and v.get("client_secret"))

    def _creds(self):
        token = self.values().get("token")
        if not token:
            return None
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_info(json.loads(token), GOOGLE_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.vault.set_token("google", "token", creds.to_json())
            return creds if creds.valid else None
        except Exception as e:  # noqa: BLE001
            log.warning("Google: the token cannot be used: %s", e)
            return None

    def connected(self) -> bool:
        return self.configured() and self._creds() is not None

    def start_connect(self) -> dict:
        """The sign-in runs in a thread and opens the person's browser; the outcome shows in `connected()`."""
        if not self.configured():
            return {"error": self.NOT_CONFIGURED}
        if self._thread is not None and self._thread.is_alive():
            return {"status": "in_progress"}
        v = self.values()

        def run() -> None:
            self.flow_error = None
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_config(
                    {"installed": {"client_id": v["client_id"], "client_secret": v["client_secret"], "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                   "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}, GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300)
                self.vault.set_token("google", "token", creds.to_json())
            except Exception as e:  # noqa: BLE001
                self.flow_error = str(e)

        self._thread = threading.Thread(target=run, name="google-oauth", daemon=True)
        self._thread.start()
        return {"status": "browser_opened"}

    def events(self, days: int = 7) -> list[dict]:
        creds = self._creds()
        if creds is None:
            return []
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        res = service.events().list(calendarId="primary", timeMin=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    timeMax=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400)),
                                    singleEvents=True, orderBy="startTime", maxResults=50).execute()
        out = []
        for ev in res.get("items", []):
            start, end = ev.get("start") or {}, ev.get("end") or {}
            out.append({"id": "g:" + ev["id"], "provider": "google", "title": ev.get("summary") or "(no title)",
                        "start": start.get("dateTime") or start.get("date"), "end": end.get("dateTime") or end.get("date"),
                        "organizer": (ev.get("organizer") or {}).get("email"),
                        "meeting_url": ev.get("hangoutLink") or extract_meeting_url(ev.get("location"), ev.get("description"))})
        return out


class MicrosoftCalendar(_Provider):
    name, token_key = "microsoft", "refresh_token"
    NOT_CONFIGURED = "put client_id (and tenant, if not common) of your own Azure app registration in the credential store entry meetings/microsoft"

    def configured(self) -> bool:
        return bool(self.values().get("client_id"))

    def _app(self):
        import msal
        v = self.values()
        return msal.PublicClientApplication(v["client_id"], authority="https://login.microsoftonline.com/" + (v.get("tenant") or "common"))

    def _access_token(self) -> str | None:
        refresh = self.values().get("refresh_token")
        if not (self.configured() and refresh):
            return None
        try:
            res = self._app().acquire_token_by_refresh_token(refresh, MS_SCOPES)
        except Exception as e:  # noqa: BLE001
            log.warning("Microsoft: the token cannot be used: %s", e)
            return None
        if "access_token" not in res:
            return None
        if res.get("refresh_token") and res["refresh_token"] != refresh:
            self.vault.set_token("microsoft", "refresh_token", res["refresh_token"])
        return res["access_token"]

    def connected(self) -> bool:
        return self._access_token() is not None

    def start_connect(self) -> dict:
        """The device-code sign-in: returns the code and the page to type it on; the rest happens in a thread."""
        if not self.configured():
            return {"error": self.NOT_CONFIGURED}
        if self._thread is not None and self._thread.is_alive():
            return {"status": "in_progress"}
        app = self._app()
        flow = app.initiate_device_flow(scopes=MS_SCOPES)
        if "user_code" not in flow:
            return {"error": f"the sign-in could not start: {flow.get('error_description')}"}

        def wait() -> None:
            self.flow_error = None
            try:
                res = app.acquire_token_by_device_flow(flow)
                if res.get("refresh_token"):
                    self.vault.set_token("microsoft", "refresh_token", res["refresh_token"])
                else:
                    self.flow_error = res.get("error_description", "unknown")
            except Exception as e:  # noqa: BLE001
                self.flow_error = str(e)

        self._thread = threading.Thread(target=wait, name="ms-oauth", daemon=True)
        self._thread.start()
        return {"user_code": flow["user_code"], "verification_uri": flow["verification_uri"], "detail": flow.get("message")}

    def events(self, days: int = 7) -> list[dict]:
        token = self._access_token()
        if token is None:
            return []
        import requests
        r = requests.get("https://graph.microsoft.com/v1.0/me/calendarView",
                         params={"startDateTime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                                 "endDateTime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + days * 86400)),
                                 "$orderby": "start/dateTime", "$top": "50", "$select": "subject,start,end,organizer,location,onlineMeeting,bodyPreview"},
                         headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'}, timeout=20)
        if not r.ok:
            log.warning("Graph calendarView: HTTP %s", r.status_code)
            return []
        out = []
        for ev in r.json().get("value", []):
            out.append({"id": "m:" + ev["id"], "provider": "microsoft", "title": ev.get("subject") or "(no title)",
                        "start": (ev.get("start") or {}).get("dateTime"), "end": (ev.get("end") or {}).get("dateTime"),
                        "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address"),
                        "meeting_url": (ev.get("onlineMeeting") or {}).get("joinUrl")
                        or extract_meeting_url((ev.get("location") or {}).get("displayName"), ev.get("bodyPreview"))})
        return out
