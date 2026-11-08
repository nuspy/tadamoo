"""Cervello ESTERNO: la conversazione la decide un altro programma, via REST.

Serve alle integrazioni tipo PGDCA (E:/Projects/OpenAGI): il chiamante
esterno piazza la chiamata con `POST /external/call`, spinge le battute con
`/say`, legge cio' che dice l'interlocutore con `/heard` e chiude con
`/hangup` (vedi api_external.py). Qui dentro c'e' solo il ponte: un "brain"
duck-typed (`respond(text) -> Iterator[str]`) che invece di interrogare un
agente aspetta la prossima battuta dalla coda REST.

Regole:
- il `greeting` (che per gli agenti conformi contiene la disclosure AI come
  prima frase) e' SEMPRE la prima cosa detta dall'AI: sul trasporto GSM lo
  pronuncia la sessione stessa (run_outgoing), su Android lo pronuncia
  questo brain al primo turno, prima di qualunque battuta dalla coda;
- le trascrizioni sono dati non fidati: qui si registrano e basta, le
  etichette di fiducia le mette il consumatore.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid

log = logging.getLogger("callbridge.external")

#: quanto il brain aspetta la prossima battuta dall'esterno prima di cedere
#: il turno (la sessione riascolta l'interlocutore e richiamera' respond)
DEFAULT_REPLY_WAIT_S = 25.0

#: le sessioni terminate restano leggibili (transcript) per questo tempo
ENDED_TTL_S = 3600.0


class ExternalCallSession:
    """Stato condiviso di una chiamata pilotata dall'esterno."""

    def __init__(self, number: str, greeting: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.number = number
        self.greeting = greeting
        self.status = "calling"            # calling | active | ended
        self.transport: str | None = None
        self.events: list[dict] = []       # [{"speaker","text"}] in ordine
        self.say_q: queue.Queue[str] = queue.Queue()
        self.cond = threading.Condition()
        self.created_at = time.time()
        self.ended_at: float | None = None

    def add(self, speaker: str, text: str) -> None:
        with self.cond:
            self.events.append({"speaker": speaker, "text": text})
            self.cond.notify_all()

    def wait_heard(self, after: int, timeout_s: float) -> tuple[dict | None, int]:
        """Prossimo turno del CHIAMATO con indice > `after` (blocca fino a
        `timeout_s`). Ritorna (evento|None, indice dell'ultimo evento visto)."""
        deadline = time.time() + max(0.0, timeout_s)
        with self.cond:
            while True:
                for i in range(max(0, after + 1), len(self.events)):
                    if self.events[i]["speaker"] != "assistant":
                        return dict(self.events[i]), i
                if self.status == "ended":
                    return None, len(self.events) - 1
                left = deadline - time.time()
                if left <= 0:
                    return None, len(self.events) - 1
                self.cond.wait(timeout=min(left, 1.0))

    def mark_ended(self) -> None:
        with self.cond:
            if self.status != "ended":
                self.status = "ended"
                self.ended_at = time.time()
            self.cond.notify_all()

    def snapshot(self) -> dict:
        with self.cond:
            dur = (self.ended_at or time.time()) - self.created_at
            return {"call_id": self.id, "number": self.number,
                    "status": self.status, "transport": self.transport,
                    "duration_s": round(dur, 1),
                    "transcript": [dict(e) for e in self.events]}


class ExternalBrain:
    """Brain duck-typed che gira le battute da/verso la coda REST."""

    def __init__(self, session: ExternalCallSession,
                 reply_wait_s: float = DEFAULT_REPLY_WAIT_S) -> None:
        self.session = session
        self.reply_wait_s = reply_wait_s
        # True quando il greeting NON e' pronunciato dalla sessione (Android):
        # va detto al primo turno, prima di ogni battuta dalla coda.
        self.greeting_pending = True

    def respond(self, text: str):
        s = self.session
        if s.status == "calling":
            s.status = "active"
        if text:
            s.add("callee", text)
        if self.greeting_pending:
            self.greeting_pending = False
            s.add("assistant", s.greeting)
            yield s.greeting
            return
        try:
            line = s.say_q.get(timeout=self.reply_wait_s)
        except queue.Empty:
            log.info("external[%s]: nessuna battuta entro %.0fs, cedo il turno",
                     s.id, self.reply_wait_s)
            return
        s.add("assistant", line)
        yield line

    def call_ended(self) -> None:
        self.session.mark_ended()


# --- registro delle sessioni -------------------------------------------------

_sessions: dict[str, ExternalCallSession] = {}
_lock = threading.Lock()


def new_session(number: str, greeting: str) -> ExternalCallSession:
    s = ExternalCallSession(number, greeting)
    with _lock:
        # spazzino: le sessioni terminate da piu' di ENDED_TTL_S non servono
        now = time.time()
        for cid in [c for c, x in _sessions.items()
                    if x.ended_at and now - x.ended_at > ENDED_TTL_S]:
            _sessions.pop(cid, None)
        _sessions[s.id] = s
    return s


def get_session(call_id: str) -> ExternalCallSession | None:
    with _lock:
        return _sessions.get(call_id)


def active_session() -> ExternalCallSession | None:
    """La sessione non terminata piu' recente (il bridge fa una chiamata per
    volta: al piu' una)."""
    with _lock:
        live = [s for s in _sessions.values() if s.status != "ended"]
    return max(live, key=lambda s: s.created_at) if live else None
