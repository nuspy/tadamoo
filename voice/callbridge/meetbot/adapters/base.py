"""Interfaccia degli adapter di piattaforma (Meet/Teams/Zoom).

Ogni adapter incapsula i selettori DOM e il flusso di ingresso della sua
piattaforma. I selettori sono CENTRALIZZATI in costanti di classe per
riparazioni rapide quando le web-app cambiano markup.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JoinResult:
    ok: bool
    state: str               # "in_call" | "waiting_admit" | "error"
    message: str | None = None


@dataclass
class SpeakerInfo:
    """Fotografia dell'attivita' parlato in un istante."""
    active: str | None = None          # nome del parlante attivo (se noto)
    participants: list[str] = field(default_factory=list)


class MeetingAdapter:
    """Contratto: tutte le operazioni ricevono la `page` Playwright."""

    platform = "generic"

    @staticmethod
    def matches(url: str) -> bool:
        raise NotImplementedError

    def join(self, page, display_name: str) -> JoinResult:
        """Naviga, disattiva cam/mic, inserisce il nome e chiede di entrare.
        Ritorna in_call o waiting_admit (sala d'attesa)."""
        raise NotImplementedError

    def in_call(self, page) -> bool:
        """True se la UI mostra una chiamata attiva."""
        raise NotImplementedError

    def speakers(self, page) -> SpeakerInfo:
        """Parlante attivo + partecipanti (best-effort, puo' tornare vuoto)."""
        return SpeakerInfo()

    def leave(self, page) -> None:
        raise NotImplementedError
