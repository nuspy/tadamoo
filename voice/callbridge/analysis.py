"""Analisi di trascrizioni (riunioni, video, audio) — modulo CONDIVISO.

Usato sia dalla sezione "Analisi" (file/URL caricati) sia dal meetbot a fine
riunione. Dato un transcript (lista di segmenti, opzionalmente con speaker),
produce con il cervello diretto (il modello caricato, senza strumenti):

  {
    "summary":    riassunto globale esaustivo,
    "highlights": [{"title", "summary"}],   # punti salienti: titolo +
                                            # riassunto MOLTO esaustivo
    "tasks":      [{"assignee", "task"}],   # attivita' per partecipante
  }

Trascrizioni lunghe: map-reduce. Il testo viene spezzato in chunk (~10k
char, su confini di segmento); per ogni chunk si chiede un JSON parziale
(riassunto + candidati salienti/task), poi una passata finale fonde il tutto.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable

from .brains import make_brain
from .config import Config

log = logging.getLogger("callbridge.analysis")

# Cervello dedicato all'analisi, creato pigramente e condiviso: una sola istanza per processo.
_brain = None
_brain_lock = threading.Lock()


def _get_brain(cfg: Config):
    global _brain
    with _brain_lock:
        if _brain is None:
            # Sempre il cervello DIRETTO: la trascrizione e' contenuto non fidato e non deve arrivare a un agente con strumenti.
            _brain = make_brain(cfg).direct
        return _brain


def _ask(cfg: Config, brain, prompt: str,
         preamble: str | None = None) -> str:
    """Interroga il cervello e raccoglie la risposta completa (testo, non il
    parlato: `ask_stream`). Una trascrizione e' contenuto non fidato: si legge e
    si riassume, non la si esegue."""
    out: list[str] = []
    for chunk in brain.ask_stream((preamble or "") + prompt):
        if isinstance(chunk, str):
            out.append(chunk)
    return "".join(out)


def _parse_json(text: str) -> dict | None:
    """Estrae il primo oggetto JSON dalla risposta (tollera i fence)."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`")
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _ask_json(cfg: Config, brain, prompt: str,
              preamble: str | None = None) -> tuple[dict | None, str]:
    """Domanda con risposta JSON; un retry se il parsing fallisce.

    Ritorna `(json, ultima risposta grezza)`. La risposta serve al chiamante
    per riferire la CAUSA vera: quando il cervello e' rotto a monte (LM Studio
    senza modelli caricati, agente in errore) la risposta non e' "quasi JSON",
    e' un messaggio d'errore — dirlo vale piu' di "non ha prodotto JSON"."""
    reply = ""
    for attempt in (1, 2):
        reply = _ask(cfg, brain, prompt, preamble=preamble)
        parsed = _parse_json(reply)
        if parsed is not None:
            return parsed, reply
        log.warning("analisi: risposta non-JSON (tentativo %d): %.200s",
                    attempt, reply)
        prompt = ("La risposta precedente non era JSON valido. Rispondi "
                  "SOLO con l'oggetto JSON richiesto, senza testo attorno.\n\n"
                  + prompt)
    return None, reply


def _perche(reply: str) -> str:
    """Risposta grezza del cervello ridotta a una riga citabile nell'errore."""
    testo = " ".join((reply or "").split())
    if not testo:
        return "risposta vuota"
    return testo[:200] + ("…" if len(testo) > 200 else "")


def _segments_text(segments: list[dict]) -> str:
    lines = []
    for s in segments:
        spk = s.get("speaker")
        t = s.get("start_s")
        prefix = ""
        if t is not None:
            m, sec = divmod(int(t), 60)
            prefix += f"[{m:02d}:{sec:02d}] "
        if spk:
            prefix += f"{spk}: "
        lines.append(prefix + (s.get("text") or ""))
    return "\n".join(lines)


def _chunk(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks, cur = [], []
    size = 0
    for line in text.split("\n"):
        if size + len(line) > max_chars and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


_MAP_PROMPT = """[Analizza questo SEGMENTO di trascrizione ({part}/{total}).
Rispondi SOLO con JSON:
{{"partial_summary": "riassunto denso del segmento",
  "candidate_highlights": [{{"title": "titolo breve", "summary": "riassunto del punto"}}],
  "candidate_agreements": [{{"title": "accordo/decisione presa", "detail": "dettaglio"}}],
  "candidate_tasks": [{{"assignee": "nome o 'non specificato'", "task": "cosa deve fare"}}]}}
Scrivi in italiano. Non inventare nulla che non sia nel testo.]

{text}"""

_REDUCE_PROMPT = """[Hai analizzato una {kind} in {total} segmenti. Qui sotto i
risultati parziali. Fondili in un'analisi FINALE. Rispondi SOLO con JSON:
{{"summary": "riassunto globale MOLTO esaustivo (piu' paragrafi: contesto, temi
trattati, decisioni, conclusioni)",
  "highlights": [{{"title": "titolo che identifica il punto saliente",
                   "summary": "riassunto MOLTO esaustivo del punto: cosa e' stato
detto, da chi (se noto), dettagli, numeri, decisioni collegate"}}],
  "agreements": [{{"title": "accordo o decisione presa",
                   "detail": "dettaglio dell'accordo: cosa e' stato deciso, da chi, condizioni"}}],
  "tasks": [{{"assignee": "partecipante (usa i nomi reali se disponibili{participants_hint})",
              "task": "descrizione completa dell'attivita', con scadenze se citate"}}]}}
Unisci i duplicati, ordina i salienti per importanza. Includi in agreements
solo decisioni/accordi effettivi (non temi discussi senza esito). Scrivi in italiano.]

{partials}"""


def analyze_transcript(
    cfg: Config,
    segments: list[dict],
    *,
    participants: list[str] | None = None,
    progress: Callable[[str, float], None] | None = None,
    kind: str = "riunione",
    preamble: str | None = None,
) -> dict:
    """Analizza il transcript. `segments`: [{text, speaker?, start_s?}].

    `progress(fase, percent)` viene chiamata durante l'elaborazione.
    Ritorna {"summary", "highlights": [...], "tasks": [...]}; su fallimento
    del cervello solleva RuntimeError con un messaggio leggibile.
    """
    brain = _get_brain(cfg)
    text = _segments_text(segments)
    if not text.strip():
        raise RuntimeError("trascrizione vuota: nulla da analizzare")
    chunks = _chunk(text, max(2000, int(cfg.analysis_chunk_chars)))
    total = len(chunks)
    log.info("analisi: %d caratteri in %d chunk", len(text), total)

    partials: list[dict] = []
    for i, ch in enumerate(chunks):
        if progress:
            progress("analyzing", 100.0 * i / (total + 1))
        res, reply = _ask_json(cfg, brain, _MAP_PROMPT.format(
            part=i + 1, total=total, text=ch), preamble=preamble)
        if res is None:
            raise RuntimeError(f"il cervello non ha prodotto JSON sul chunk "
                               f"{i+1} — ha risposto: {_perche(reply)}")
        partials.append(res)

    # Caso semplice: un solo chunk -> chiedi direttamente l'analisi finale
    # (il reduce serve solo a fondere piu' parziali).
    if progress:
        progress("analyzing", 100.0 * total / (total + 1))
    hint = ""
    if participants:
        hint = "; partecipanti: " + ", ".join(participants)
    reduced, reply = _ask_json(cfg, brain, _REDUCE_PROMPT.format(
        kind=kind, total=total, participants_hint=hint,
        partials=json.dumps(partials, ensure_ascii=False, indent=1)),
        preamble=preamble)
    if reduced is None:
        raise RuntimeError("il cervello non ha prodotto il JSON finale — "
                           f"ha risposto: {_perche(reply)}")

    return {
        "summary": str(reduced.get("summary") or ""),
        "highlights": [
            {"title": str(h.get("title") or "Punto saliente"),
             "summary": str(h.get("summary") or "")}
            for h in (reduced.get("highlights") or [])
            if isinstance(h, dict)
        ],
        "agreements": [
            {"title": str(a.get("title") or ""),
             "detail": str(a.get("detail") or "")}
            for a in (reduced.get("agreements") or [])
            if isinstance(a, dict) and a.get("title")
        ],
        "tasks": [
            {"assignee": str(t.get("assignee") or "non specificato"),
             "task": str(t.get("task") or "")}
            for t in (reduced.get("tasks") or [])
            if isinstance(t, dict) and t.get("task")
        ],
    }


def analysis_to_markdown(title: str, analysis: dict) -> str:
    """Render del risultato in summary.md (artifact leggibile)."""
    lines = [f"# {title}", "", "## Riassunto", "", analysis.get("summary", ""), ""]
    if analysis.get("highlights"):
        lines += ["## Punti salienti", ""]
        for h in analysis["highlights"]:
            lines += [f"### {h['title']}", "", h["summary"], ""]
    if analysis.get("agreements"):
        lines += ["## Accordi", ""]
        for a in analysis["agreements"]:
            detail = a.get("detail", "")
            lines.append(f"- **{a['title']}**" + (f": {detail}" if detail else ""))
        lines.append("")
    if analysis.get("tasks"):
        lines += ["## Attività", ""]
        for t in analysis["tasks"]:
            lines.append(f"- **{t['assignee']}**: {t['task']}")
        lines.append("")
    return "\n".join(lines)
