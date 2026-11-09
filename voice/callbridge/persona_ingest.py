"""Pipeline OFFLINE documenti/libri -> personalita' + sapere di un avatar (Fase 5).

Separa ACQUISIZIONE (qui: pesante, async) da CONVERSAZIONE (LmStudioClient:
hot-path). Dato un documento:

  1. estrazione testo per formato (PDF/DOCX/EPUB/HTML/TXT/MD; OCR PaddleOCR
     locale OPZIONALE per le scansioni — best-effort, saltato se non installato);
  2. chunking a blocchi con overlap (su confini di paragrafo);
  3. RAG: embeddings dei chunk via LM Studio (/v1/embeddings) -> store locale
     `persona_rag` (fallback lessicale se manca il modello di embedding);
  4. estrazione LLM (map-reduce, diretta a LM Studio) della
     PERSONALITA' -> campo manifest `personality_from_docs`.

Nessuna dipendenza nuova obbligatoria: fitz/pypdf, python-docx, bs4, numpy,
openai sono gia' presenti. PaddleOCR e' l'unica opzionale (OCR scansioni).
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .persona_rag import PersonaRag, embed_texts

log = logging.getLogger("callbridge.persona_ingest")

SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".text",
                  ".pdf", ".docx", ".html", ".htm", ".epub"}

# Quante porzioni del documento usare per estrarre la personalita' (campionate
# uniformemente) sui documenti ENORMI, dove classificare ogni chunk costa troppo.
MAX_PERSONA_CHUNKS = 12
# Soglia sotto la quale si esamina OGNI chunk (classificazione di pertinenza +
# estrazione): cosi' le parti non pertinenti (dediche, prefazioni, presentazioni
# del curatore, indici, note editoriali) vengono SCARTATE da personalita' e RAG.
# Oltre la soglia si ripiega su un campione (solo personalita') + RAG su tutto.
MAX_CLASSIFY_CHUNKS = 60

ProgressCb = Callable[[str, float, str], None]


# --- estrazione testo -------------------------------------------------------

def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".markdown", ".text"):
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".docx":
        return _extract_docx(path)
    if ext in (".html", ".htm"):
        return _extract_html(path.read_text(encoding="utf-8", errors="replace"))
    if ext == ".epub":
        return _extract_epub(path)
    # sconosciuto: tenta come testo semplice
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_pdf(path: Path) -> str:
    import fitz  # PyMuPDF
    parts: list[str] = []
    empty = 0
    with fitz.open(str(path)) as doc:
        for page in doc:
            txt = page.get_text("text").strip()
            if not txt:
                txt = _ocr_pdf_page(page)  # "" se PaddleOCR non installato
                if not txt:
                    empty += 1
            if txt:
                parts.append(txt)
    if empty:
        log.info("PDF %s: %d pagine senza testo (scansioni? OCR non disponibile)",
                 path.name, empty)
    return "\n\n".join(parts)


def _extract_docx(path: Path) -> str:
    import docx
    d = docx.Document(str(path))
    return "\n".join(p.text for p in d.paragraphs if p.text)


def _extract_html(raw: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)


def _extract_epub(path: Path) -> str:
    from bs4 import BeautifulSoup
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith((".xhtml", ".html", ".htm"))]
        for n in sorted(names):  # ordine alfabetico ~ ordine di lettura
            try:
                html = z.read(n).decode("utf-8", "replace")
            except (KeyError, OSError):
                continue
            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            if text:
                out.append(text)
    return "\n\n".join(out)


# OCR opzionale (PaddleOCR locale). Istanza pigra e cache dell'esito del tentativo
# di import: se non e' installato, l'OCR resta disattivato senza rumore.
_ocr = None
_ocr_tried = False


def _get_ocr():
    global _ocr, _ocr_tried
    if _ocr_tried:
        return _ocr
    _ocr_tried = True
    try:
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(use_angle_cls=True, lang="it", show_log=False)
        log.info("PaddleOCR attivo (OCR scansioni)")
    except Exception as e:  # noqa: BLE001 - OCR opzionale
        log.info("PaddleOCR non disponibile (OCR disattivato): %s", e)
        _ocr = None
    return _ocr


def _ocr_pdf_page(page) -> str:
    ocr = _get_ocr()
    if ocr is None:
        return ""
    try:
        import numpy as np
        pix = page.get_pixmap(dpi=200)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        res = ocr.ocr(img, cls=True)
        lines: list[str] = []
        for block in (res or []):
            for line in (block or []):
                try:
                    lines.append(line[1][0])
                except (IndexError, TypeError):
                    pass
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 - OCR best-effort
        log.debug("OCR pagina fallito: %s", e)
        return ""


# --- chunking ---------------------------------------------------------------

def chunk_text(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    """Spezza il testo in blocchi ~chunk_chars su confini di paragrafo, con
    overlap (coda del blocco precedente). Char-based di proposito: nessun
    download di rete (a differenza di tiktoken) -> pipeline 100% offline."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for p in paras:
        if len(p) <= chunk_chars:
            units.append(p)
        else:  # paragrafo mostruoso (es. PDF senza a-capo): taglio duro
            for i in range(0, len(p), chunk_chars):
                units.append(p[i:i + chunk_chars])
    chunks: list[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + len(u) + 2 > chunk_chars:
            chunks.append(cur)
            tail = cur[-overlap_chars:] if overlap_chars > 0 else ""
            cur = (tail + "\n\n" + u) if tail else u
        else:
            cur = (cur + "\n\n" + u) if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def _sample(items: list, k: int) -> list:
    """Campiona fino a k elementi in modo uniforme (mantiene inizio e fine)."""
    if k <= 0 or len(items) <= k:
        return list(items)
    step = len(items) / k
    return [items[min(len(items) - 1, int(i * step))] for i in range(k)]


# --- LLM diretto LM Studio --------------------------------------------------

def _client(cfg: Config):
    from openai import OpenAI
    return OpenAI(base_url=cfg.lmstudio_base_url,
                  api_key=cfg.lmstudio_api_key or "lm-studio")


def _resolve_model(cfg: Config, client) -> str:
    want = (cfg.lmstudio_model or "").strip()
    if want:
        return want
    try:
        m = client.models.list()
        return m.data[0].id if m.data else "local-model"
    except Exception:  # noqa: BLE001 - LM Studio spento
        return "local-model"


def _llm(client, model: str, prompt: str, timeout: float) -> str:
    r = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], timeout=timeout)
    return (r.choices[0].message.content or "").strip()


def _parse_json(text: str) -> dict | None:
    """Primo oggetto JSON nella risposta (tollera i fence ```json)."""
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


def _llm_json(client, model: str, prompt: str, timeout: float) -> dict | None:
    for attempt in (1, 2):
        parsed = _parse_json(_llm(client, model, prompt, timeout))
        if parsed is not None:
            return parsed
        prompt = ("La risposta precedente non era JSON valido. Rispondi SOLO "
                  "con l'oggetto JSON richiesto.\n\n" + prompt)
    return None


_PERSONA_MAP = """[Questo è un brano tratto da un libro SU o DI {subject}. PRIMA stabilisci se è materiale utile a impersonarlo, POI (solo se lo è) estrai i tratti.

NON pertinente (relevant=false) è il materiale EDITORIALE che non è {subject}: dedica, presentazione o premessa del curatore/autore moderno, prefazione, introduzione critica in cui si parla dell'opera o di chi l'ha scritta, indice o sommario, bibliografia, note, ringraziamenti, copyright, dati dell'editore o della scuola.
Pertinente (relevant=true) è SOLO ciò che è scritto DA {subject} (lettere, discorsi in prima persona) o che racconta DIRETTAMENTE la sua vita, il suo carattere, le sue idee.

Rispondi SOLO con un oggetto JSON, senza testo attorno:
{{"relevant": true/false,
  "kind": "voce" | "biografia" | "editoriale" | "indice",
  "speech": ["modi di dire, registro, espressioni tipiche di {subject} (solo da brani in prima persona)"],
  "traits": ["tratti di carattere di {subject}"],
  "values": ["valori, credenze, motivazioni di {subject}"],
  "context": "epoca/ambiente/ruolo in una frase"}}
Se relevant=false lascia le liste vuote e context "". Non inventare. Scrivi in italiano.]

BRANO:
{text}"""

_PERSONA_REDUCE = """[Devi definire come IMPERSONARE {subject} in una conversazione vocale. Qui sotto: un eventuale profilo già esistente da integrare e delle note estratte dai testi (ognuna con un "kind"). Fondili in un PROFILO unico scritto in SECONDA PERSONA ("Sei {subject}. Parli così... Credi in..."), pronto come istruzione di sistema: identità, modo di parlare (registro ed espressioni), carattere, valori, epoca. Per il MODO DI PARLARE privilegia le note kind "voce" (prima persona). Coerente, non ridondante, max ~250 parole. Rispondi SOLO con il profilo, in italiano.]

PROFILO ESISTENTE:
{prior}

NOTE ESTRATTE (JSON):
{notes}"""


# --- manifest ---------------------------------------------------------------

def _persona_root(cfg: Config, persona: str) -> Path:
    safe = re.sub(r"\s+", "_", (persona or "").strip())
    return Path(cfg.avatars_dir) / safe


def _read_manifest(cfg: Config, persona: str) -> dict:
    try:
        p = _persona_root(cfg, persona) / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _update_manifest_field(cfg: Config, persona: str, key: str, value) -> None:
    p = _persona_root(cfg, persona) / "manifest.json"
    manifest = _read_manifest(cfg, persona)
    manifest[key] = value
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


# --- orchestrazione ---------------------------------------------------------

def run_ingest(cfg: Config, persona: str, doc_path: Path,
               progress: ProgressCb | None = None) -> dict:
    """Esegue l'intera pipeline su UN documento. Solleva RuntimeError con un
    messaggio leggibile su errore fatale (testo vuoto). Ritorna un riepilogo."""
    def report(phase: str, pct: float, detail: str = "") -> None:
        if progress:
            progress(phase, pct, detail)

    report("extract", 3.0, doc_path.name)
    text = extract_text(doc_path)
    if not text.strip():
        raise RuntimeError(
            "nessun testo estratto dal documento (se è una scansione serve "
            "PaddleOCR, non installato)")

    report("chunk", 12.0, "")
    chunk_chars = max(500, int(cfg.persona_ingest_chunk_tokens) * 4)
    overlap = max(0, int(cfg.persona_ingest_overlap_tokens) * 4)
    chunks = chunk_text(text, chunk_chars, overlap)
    if not chunks:
        raise RuntimeError("nessun blocco di testo prodotto")
    log.info("ingest '%s': %d char -> %d chunk (%s)", persona, len(text),
             len(chunks), doc_path.name)

    client = _client(cfg)
    model = _resolve_model(cfg, client)

    # 3) Analisi per-chunk: classifica la PERTINENZA + estrai i tratti. Sui
    #    documenti piccoli (<= MAX_CLASSIFY_CHUNKS) esamina OGNI chunk e SCARTA le
    #    parti non pertinenti (dediche, presentazioni/premesse del curatore,
    #    introduzioni critiche, indici, note editoriali). Sui documenti enormi
    #    ripiega su un campione (solo per la personalità): classificare ogni
    #    chunk costerebbe troppo.
    full = len(chunks) <= MAX_CLASSIFY_CHUNKS
    to_process = chunks if full else _sample(chunks, MAX_PERSONA_CHUNKS)
    report("analyze", 25.0, f"{len(to_process)} blocchi")
    notes: list[dict] = []
    relevant_texts: list[str] = []
    dropped = 0
    for i, ch in enumerate(to_process):
        report("analyze", 25.0 + 50.0 * i / max(1, len(to_process)),
               f"{i + 1}/{len(to_process)}")
        j = _llm_json(client, model,
                      _PERSONA_MAP.format(subject=persona, text=ch),
                      cfg.lmstudio_response_timeout_s)
        if j is None:
            relevant_texts.append(ch)   # non classificato: in dubbio, tienilo
            continue
        if j.get("relevant"):
            notes.append(j)
            relevant_texts.append(ch)
        else:
            dropped += 1

    # 4) RAG: sui documenti piccoli indicizza SOLO i chunk pertinenti (indice
    #    pulito, niente dediche/indici); sui grandi indicizza tutto (non li
    #    abbiamo classificati tutti).
    rag_texts = relevant_texts if full else chunks
    report("embed", 78.0, f"{len(rag_texts)} blocchi")
    # Provider di embedding dal registro (consumatore "embed"), con ripiego sul
    # campo storico: indicizzare e interrogare DEVONO usare lo stesso modello,
    # quindi la scelta passa da qui come in `_rag_context`.
    from . import llm_backend
    ep = llm_backend.embed_endpoint(cfg)
    embed_model = ep[2] if ep else ""
    embed_client = client
    if ep and ep[0] and ep[0] != getattr(client, "base_url", None):
        from openai import OpenAI
        embed_client = OpenAI(base_url=ep[0], api_key=ep[1] or "local")
    embeddings = embed_texts(embed_client, embed_model, rag_texts) if embed_model else None
    rag = PersonaRag(_persona_root(cfg, persona))
    rag.add(doc_path.name, rag_texts, embeddings, embed_model)
    rag_mode = "semantic" if embeddings is not None else "lexical"

    # 5) Personalità: sintesi dai soli chunk pertinenti.
    personality_updated = False
    if notes:
        report("personality", 90.0, "sintesi")
        prior = (_read_manifest(cfg, persona).get("personality_from_docs") or "").strip()
        profile = _llm(client, model, _PERSONA_REDUCE.format(
            subject=persona, prior=prior or "(nessuno)",
            notes=json.dumps(notes[:40], ensure_ascii=False)),
            cfg.lmstudio_response_timeout_s)
        if profile:
            _update_manifest_field(cfg, persona, "personality_from_docs", profile)
            personality_updated = True

    report("done", 100.0, "")
    result = {
        "chunks": len(chunks),
        "analyzed": len(to_process),
        "relevant": len(relevant_texts) if full else None,
        "dropped": dropped,
        "rag": rag_mode,
        "rag_indexed": len(rag_texts),
        "personality_updated": personality_updated,
        "notes": len(notes),
    }
    log.info("ingest '%s' completato: %s", persona, result)
    return result
