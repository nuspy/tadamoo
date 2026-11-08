"""Fabbrica dei "cervelli" + dispatch unificato di `respond()`.

La stessa cascata di scelte (agente completo via ACP -> Hermes HTTP -> stub)
era replicata in main.py, control_server.py e voice_gateway.py; il dispatch di
`respond()` (l'ACP accetta un `preamble`, l'`HermesClient` va avvolto in una
`HermesSession` con notifica outgoing/call-ended) era replicato in
voice_gateway.py e copilot.py. Questa e' l'unica copia.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator

from .config import Config

log = logging.getLogger("callbridge.brains")


def make_full_brain(cfg: Config, label: str = ""):
    """Cervello COMPLETO (F1/F2, gateway, GSM): agente ACP se abilitato,
    altrimenti Hermes HTTP, altrimenti stub (eco locale)."""
    tag = f" [{label}]" if label else ""
    if cfg.acp_enabled:
        from .acp_client import AcpAgentClient
        where = f"WSL:{cfg.acp_wsl_distro}" if cfg.acp_use_wsl else "nativo"
        log.info("Cervello%s: agente completo via ACP (%s)", tag, where)
        return AcpAgentClient(cfg)
    if cfg.hermes_mode == "http":
        from .hermes_client import HermesClient
        log.info("Cervello%s: Hermes HTTP (%s)", tag, cfg.hermes_base_url)
        return HermesClient(cfg)
    from .stubs import StubHermes
    log.info("Cervello%s: stub (eco locale)", tag)
    return StubHermes()


def make_lmstudio_brain(cfg: Config):
    """Cervello DIRETTO LM Studio (Ctrl+Shift+F4/F5): conversazione non-agentica
    near-real-time contro l'API OpenAI-compatibile, con history/compattazione/
    memoria per-avatar in processo. Nessun ACP/Hermes."""
    from .lmstudio_client import LmStudioClient
    log.info("Cervello [lmstudio]: diretto su %s", cfg.lmstudio_base_url)
    return LmStudioClient(cfg)


def make_limited_brain(cfg: Config):
    """Cervello LIMITATO (F3, simulazione telefonate).

    Dopo il cutover Hermes nativo il phone server HTTP (:8780) non gira piu':
    con l'agente ACP abilitato si usa quello (i chiamati dovrebbero pero'
    CONDIVIDERE l'istanza del full brain per non spawnare un 2o processo
    hermes — vedi main._build_brains / control_server). Altrimenti Hermes HTTP
    se configurato, poi stub."""
    if cfg.acp_enabled:
        from .acp_client import AcpAgentClient
        return AcpAgentClient(cfg)
    if cfg.hermes_mode == "http":
        from .hermes_client import HermesClient
        return HermesClient(cfg)
    from .stubs import StubHermes
    return StubHermes()


def respond_stream(
    brain,
    text: str,
    cfg: Config,
    *,
    preamble: str | None = None,
    session_tag: str = "app",
    lang: str | None = None,
    timeout_s: float | None = None,
) -> Iterator[str | dict]:
    """Interroga un cervello qualsiasi e yielda i chunk di risposta.

    Appiana le differenze di firma:
      - AcpAgentClient: respond(text, preamble=...); preamble=None usa quello
        vocale di default (cfg.acp_voice_preamble)
      - HermesClient:   serve una HermesSession (notifica outgoing/call-ended)
        e il preamble si antepone al testo
      - StubHermes:     respond(text)

    `timeout_s` serve a chi NON e' in conversazione (l'analisi di un transcript
    puo' macinare per parecchi minuti) e vale solo per l'agente ACP: gli altri
    cervelli hanno il proprio timeout HTTP.
    """
    from .acp_client import AcpAgentClient
    from .hermes_client import HermesClient, HermesSession

    if isinstance(brain, AcpAgentClient):
        yield from brain.respond(text, preamble=preamble, timeout_s=timeout_s)
        return
    if isinstance(brain, HermesClient):
        sid = f"{session_tag}-{uuid.uuid4().hex[:12]}"
        brain.notify_outgoing(sid, f"({session_tag})",
                              cfg.default_voice, lang or cfg.default_lang)
        session = HermesSession(brain, sid, "OUTGOING")
        try:
            yield from session.respond((preamble or "") + text)
        finally:
            session.end()
        return
    yield from brain.respond((preamble or "") + text)
