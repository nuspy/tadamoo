"""Watcher dei calendari: auto-join delle riunioni selezionate.

Polla Google/Microsoft ogni `calendar_poll_s`; per gli eventi con auto-join
attivo (persistito in artifacts/meetings/autojoin.json) programma il join
~1 minuto prima dell'inizio. Il join effettivo passa dalla stessa strada
della GUI (callback `join_cb(url, title)`) cosi' vale il vincolo
una-riunione-alla-volta del backend loopback.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("callbridge.meetbot.calendar")

JOIN_EARLY_S = 60          # entra ~1 min prima dell'inizio
JOIN_LATE_TOLERANCE_S = 900  # entra comunque se l'evento e' iniziato da <15 min


def _parse_dt(value: str | None) -> float | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class AutojoinStore:
    """Selezioni per-evento, persistite (sopravvivono ai riavvii)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self._data: dict[str, bool] = json.loads(
                path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def get(self, event_id: str) -> bool:
        return bool(self._data.get(event_id))

    def set(self, event_id: str, enabled: bool) -> None:
        with self._lock:
            if enabled:
                self._data[event_id] = True
            else:
                self._data.pop(event_id, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8")


class CalendarWatcher:
    def __init__(self, cfg, google, microsoft, store: AutojoinStore,
                 join_cb) -> None:
        self.cfg = cfg
        self.google = google
        self.microsoft = microsoft
        self.store = store
        self.join_cb = join_cb        # (url, title) -> bool
        self._stop = threading.Event()
        self._joined: set[str] = set()  # eventi gia' processati
        self.last_events: list[dict] = []
        self.last_poll: float = 0.0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="calendar-watch",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def fetch_events(self, days: int = 7) -> list[dict]:
        events: list[dict] = []
        for prov in (self.google, self.microsoft):
            try:
                events.extend(prov.events(days=days))
            except Exception as e:  # noqa: BLE001
                log.warning("lettura calendario fallita: %s", e)
        events.sort(key=lambda e: e.get("start") or "")
        for ev in events:
            ev["autojoin"] = self.store.get(ev["id"])
        self.last_events = events
        self.last_poll = time.time()
        return events

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # days=7 come la vista GUI: il poll interno non deve restringere
                # `last_events` (che la GUI mostra) alla sola finestra di auto-join.
                events = self.fetch_events(days=7)
                now = time.time()
                for ev in events:
                    if not ev.get("autojoin") or not ev.get("meeting_url"):
                        continue
                    if ev["id"] in self._joined:
                        continue
                    start = _parse_dt(ev.get("start"))
                    if start is None:
                        continue
                    if (start - JOIN_EARLY_S) <= now <= (start + JOIN_LATE_TOLERANCE_S):
                        title = ev.get("title", "")
                        log.info("auto-join: '%s' (%s)", title, ev["id"])
                        ok = False
                        try:
                            # Marca "processato" SOLO se il join riesce: un
                            # fallimento va ritentato al prossimo poll (nella
                            # finestra di 15 min), non scartato per sempre.
                            ok = bool(self.join_cb(ev["meeting_url"], title))
                        except Exception as e:  # noqa: BLE001
                            log.error("auto-join fallito: %s", e)
                        if ok:
                            self._joined.add(ev["id"])
                # Limita la crescita di `_joined` agli eventi ancora visibili.
                self._joined &= {ev["id"] for ev in events}
            except Exception as e:  # noqa: BLE001
                log.error("calendar watch: %s", e)
            self._stop.wait(float(self.cfg.calendar_poll_s))
