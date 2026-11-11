"""A stand-in for the embedding worker: the real command loop (tadamoo/rag/embed_worker.py `serve`) over a fake backend that needs no model.
A text becomes the counts of a few topic words, so meaning can be tested; a reranker scores the words a passage shares with the question.
The environment steers it: STUB_LOG (a file that gets one JSON line per command), STUB_NO_CUDA (a load on cuda answers cpu), STUB_DIE_AFTER (exit after
that many embed commands), STUB_SLOW (seconds each embed command takes)."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from tadamoo.rag import embed_worker  # noqa: E402

TOPICS = (("boat", "sail", "yacht", "mast", "keel", "sloop"), ("rice", "stock", "risotto", "butter", "parmesan"), ("cat", "gatti", "divano"))


def vector(text):
    low = text.lower()
    out = np.zeros(len(TOPICS) + 1, dtype=np.float32)
    for c, words in enumerate(TOPICS):
        out[c] = sum(low.count(w) for w in words)
    out[-1] = 0.01
    return out


class Fake:
    def __init__(self):
        self.embeds = 0

    def note(self, **row):
        if os.environ.get("STUB_LOG"):
            with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def load(self, role, model_dir, device):
        got = "cpu" if device == "cuda" and os.environ.get("STUB_NO_CUDA") else device
        self.note(cmd="load", role=role, model=Path(model_dir).name, device=got)
        return got

    def unload(self, role):
        self.note(cmd="unload", role=role)

    def embed(self, texts, kind, instruction, batch, max_len, dim, progress):
        self.embeds += 1
        self.note(cmd="embed", kind=kind, n=len(texts), batch=batch, instruction=instruction[:20])
        time.sleep(float(os.environ.get("STUB_SLOW", "0")))
        if os.environ.get("STUB_DIE_AFTER") and self.embeds > int(os.environ["STUB_DIE_AFTER"]):
            os._exit(7)
        out = np.vstack([vector(t) for t in texts]) if texts else np.zeros((0, len(TOPICS) + 1), dtype=np.float32)
        out = out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
        progress(len(texts), len(texts))
        return out[:, :dim] if dim else out

    def rerank(self, query, passages, instruction, batch, max_len, progress):
        self.note(cmd="rerank", n=len(passages))
        words = set(query.lower().split())
        return [len(words & set(p.lower().split())) / max(1, len(words)) for p in passages]


if __name__ == "__main__":
    embed_worker.emit({"event": "ready"})
    raise SystemExit(embed_worker.serve(Fake(), sys.stdin))
