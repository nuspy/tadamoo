"""Store RAG locale per-avatar — recall del "sapere" di una persona.

Ogni persona ha un piccolo indice in `avatar/persone/<safe>/rag/`:
  chunks.jsonl   un record per chunk: {"id", "source", "text"}
  embeddings.npy float32 [N, D] L2-normalizzati (assente in modalita' lessicale)
  meta.json      {"embed_model", "dim", "count"}

Il retrieval e' SEMANTICO se ci sono embeddings coerenti col modello corrente
(prodotto vettoriale = coseno, essendo gia' normalizzati); altrimenti ricade su
uno scoring LESSICALE (BM25-lite) sui soli testi — sempre disponibile, nessun
modello di embedding richiesto. La ricerca e' un brute-force numpy: per il
corpus di una persona (pochi libri = migliaia di chunk) resta sotto il ms.

NB: niente cognee/sqlite-vec qui (quello e' la Fase 6, il knowledge graph):
questo modulo copre il solo recall vettoriale/lessicale, near-real-time.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np

log = logging.getLogger("callbridge.persona_rag")

# embed_fn: prende una lista di testi e ritorna una matrice [len, D] float32,
# oppure None se gli embeddings non sono disponibili (LM Studio spento / nessun
# modello di embedding). Vedi make_embedder().
Embedder = Callable[[list[str]], "np.ndarray | None"]

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _normalize(mat: np.ndarray) -> np.ndarray:
    """L2-normalizza le righe (0 -> resta 0, niente divisione per zero)."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


class PersonaRag:
    """Indice RAG di UNA persona. Costruito da `PersonaRag(persona_root)`."""

    def __init__(self, persona_root: Path) -> None:
        self.dir = Path(persona_root) / "rag"
        self.chunks: list[dict] = []
        self.embeddings: np.ndarray | None = None   # [N, D] normalizzati
        self.embed_model: str = ""

    # --- persistenza ------------------------------------------------------

    @property
    def _chunks_path(self) -> Path:
        return self.dir / "chunks.jsonl"

    @property
    def _emb_path(self) -> Path:
        return self.dir / "embeddings.npy"

    @property
    def _meta_path(self) -> Path:
        return self.dir / "meta.json"

    def exists(self) -> bool:
        return self._chunks_path.is_file()

    def load(self) -> PersonaRag:
        """Carica chunk + embeddings in memoria (idempotente, best-effort)."""
        self.chunks = []
        self.embeddings = None
        self.embed_model = ""
        if not self.exists():
            return self
        try:
            with self._chunks_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.chunks.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("RAG: chunks.jsonl illeggibile (%s): %s", self.dir, e)
            self.chunks = []
            return self
        try:
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            self.embed_model = str(meta.get("embed_model") or "")
        except (OSError, json.JSONDecodeError):
            meta = {}
        if self._emb_path.is_file():
            try:
                emb = np.load(self._emb_path)
                # Coerenza: una riga per chunk, altrimenti scarta (lessicale).
                if emb.ndim == 2 and emb.shape[0] == len(self.chunks):
                    self.embeddings = emb.astype(np.float32)
                else:
                    log.warning("RAG: embeddings incoerenti (%d vs %d chunk) -> lessicale",
                                emb.shape[0] if emb.ndim == 2 else -1, len(self.chunks))
            except (OSError, ValueError) as e:
                log.warning("RAG: embeddings.npy illeggibile: %s", e)
        return self

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._chunks_path.open("w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        if self.embeddings is not None:
            np.save(self._emb_path, self.embeddings)
        elif self._emb_path.is_file():
            # Passato a lessicale (modello cambiato): rimuovi i vecchi vettori.
            try:
                self._emb_path.unlink()
            except OSError:
                pass
        self._meta_path.write_text(json.dumps({
            "embed_model": self.embed_model,
            "dim": int(self.embeddings.shape[1]) if self.embeddings is not None else 0,
            "count": len(self.chunks),
        }, indent=2), encoding="utf-8")

    # --- costruzione ------------------------------------------------------

    def add(self, source: str, texts: Iterable[str],
            embeddings: np.ndarray | None, embed_model: str) -> int:
        """Accoda i chunk di un documento (carica lo stato esistente prima).

        Regola embeddings: si mantengono i vettori solo se COERENTI su tutto lo
        store (stesso modello e stessa dimensione fra vecchi e nuovi); appena la
        coerenza si rompe (modello cambiato, un lato senza vettori) si ricade
        sul lessicale per l'intero indice — semplice e sempre corretto.
        """
        self.load()
        new_texts = [t for t in texts if t and t.strip()]
        if not new_texts:
            return 0
        base = len(self.chunks)
        for i, t in enumerate(new_texts):
            self.chunks.append({"id": base + i, "source": source, "text": t})

        new_emb = None
        if embeddings is not None and len(embeddings) == len(new_texts):
            new_emb = _normalize(np.asarray(embeddings, dtype=np.float32))

        coherent = (
            new_emb is not None
            and (base == 0 or self.embeddings is not None)
            and (self.embeddings is None or self.embeddings.shape[1] == new_emb.shape[1])
            and (not self.embed_model or self.embed_model == embed_model)
        )
        if coherent:
            self.embeddings = (new_emb if self.embeddings is None
                               else np.vstack([self.embeddings, new_emb]))
            self.embed_model = embed_model
        else:
            # Incoerente -> lessicale su tutto (scarta ogni vettore).
            self.embeddings = None
            self.embed_model = ""
        self._save()
        log.info("RAG '%s': +%d chunk (tot %d, %s)", source, len(new_texts),
                 len(self.chunks), "semantico" if self.embeddings is not None else "lessicale")
        return len(new_texts)

    # --- retrieval --------------------------------------------------------

    def search(self, query: str, top_k: int,
               embed_fn: Embedder | None = None) -> list[dict]:
        """Ritorna i top_k chunk piu' pertinenti a `query` (semantico se
        possibile, altrimenti lessicale). Mai solleva: [] su input vuoto."""
        query = (query or "").strip()
        if not query or not self.chunks or top_k <= 0:
            return []
        if self.embeddings is not None and embed_fn is not None:
            try:
                qv = embed_fn([query])
            except Exception as e:  # noqa: BLE001 - embedding best-effort
                log.debug("RAG: embed query fallito, uso lessicale: %s", e)
                qv = None
            if qv is not None and len(qv):
                q = _normalize(np.asarray(qv, dtype=np.float32))[0]
                scores = self.embeddings @ q
                idx = np.argsort(-scores)[:top_k]
                return [self.chunks[i] for i in idx]
        return self._lexical(query, top_k)

    def _lexical(self, query: str, top_k: int) -> list[dict]:
        q = set(_tokens(query))
        if not q:
            return []
        scored: list[tuple[float, dict]] = []
        for c in self.chunks:
            toks = _tokens(c.get("text") or "")
            if not toks:
                continue
            tf = Counter(toks)
            # Somma delle TF dei termini della query, smorzata dalla lunghezza
            # del chunk (BM25-lite): favorisce match densi in testi non enormi.
            hit = sum(tf[t] for t in q)
            if hit:
                scored.append((hit / math.sqrt(len(toks)), c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]


# --- embeddings via LM Studio (/v1/embeddings) ---------------------------

def embed_texts(client, model: str, texts: list[str],
                batch: int = 64) -> np.ndarray | None:
    """Calcola gli embeddings con l'API OpenAI-compatibile di LM Studio.

    Ritorna [len(texts), D] float32, o None se il modello non e' impostato o la
    chiamata fallisce (LM Studio spento / nessun modello di embedding)."""
    model = (model or "").strip()
    if not model or not texts:
        return None
    try:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            resp = client.embeddings.create(model=model, input=chunk)
            # L'ordine dei data segue quello dell'input (spec OpenAI).
            vectors.extend(d.embedding for d in resp.data)
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] != len(texts):
            return None
        return arr
    except Exception as e:  # noqa: BLE001 - embeddings best-effort
        log.warning("RAG: embeddings non disponibili (%s): %s", model, e)
        return None


def make_embedder(client, model: str) -> Embedder:
    """Chiusura per il retrieval a conversazione: embed della sola query."""
    return lambda texts: embed_texts(client, model, texts)
