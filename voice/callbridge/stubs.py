"""Implementazione stub di Hermes.

Permette di far girare l'orchestratore end-to-end PRIMA che l'integrazione
vera con Hermes Agent sia pronta (Task #6). Verra' sostituita da un client
che parla col gateway di Hermes.

Nota: STT e TTS NON sono piu' stub — sono i client WebSocket reali verso
Handy (`stt_client.py` / `tts_client.py`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

log = logging.getLogger("callbridge.stubs")


class StubHermes:
    """Hermes finto: rimanda un'eco del testo, a pezzi (simula lo streaming
    token-per-token che fara' Hermes vero)."""

    def respond(self, text: str) -> Iterator[str]:
        reply = f"Ho sentito: {text}. "
        # Emette il testo a pezzi, come se arrivasse token per token dall'LLM.
        for word in reply.split(" "):
            if word:
                yield word + " "
