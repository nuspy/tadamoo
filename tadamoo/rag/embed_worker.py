"""Embeds passages and questions, and reranks passages, for the knowledge archives. Run by a Python that has torch and transformers, never imported by
the server (tests import it with a fake backend). It reads one JSON command per line on stdin and answers one JSON line on stdout; a closed stdin
ends it. Nothing here imports the rest of TaDaMoo.

Commands (each has an `id` that the answer repeats):
  {"cmd": "load", "role": "embed" | "rerank", "model_dir", "device": "cuda" | "cpu"}            -> {"ok", "device", "seconds"}
  {"cmd": "embed", "texts", "kind": "query" | "passage", "instruction", "batch", "max_len", "dim"?, "out"?}
        -> {"ok", "shape", "dtype": "float16", "data": <base64>}, or with `out` (a path) the same array written there as .npy and {"file"} instead of "data"
  {"cmd": "rerank", "query", "passages", "instruction", "batch", "max_len"}                      -> {"ok", "scores": [probability that each passage answers the query]}
  {"cmd": "unload", "role"}  {"cmd": "ping"}  {"cmd": "quit"}
A long `embed` or `rerank` also prints {"event": "progress", "id", "done", "total"} after each batch. A failed command answers {"ok": false, "error"} and
the worker keeps running. The vectors are L2-normalized (cut to `dim` first, then normalized again), the order of `texts` is kept."""
from __future__ import annotations

import base64
import json
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_MAX_LEN = 1400
DEFAULT_BATCH = 8
PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. "
          "Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
RERANK_INSTRUCTION = "Given a question, judge whether the passage answers it"


def query_text(instruction: str, text: str) -> str:
    return f"Instruct: {instruction}\nQuery: {text}"


class TorchBackend:
    """The models, one per role. Needs torch, transformers and numpy."""

    def __init__(self) -> None:
        import numpy
        import torch
        from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer
        self.np, self.torch = numpy, torch
        self.classes = {"embed": AutoModel, "rerank": AutoModelForCausalLM}
        self.tokenizer_of = AutoTokenizer
        self.models: Dict[str, Any] = {}

    def load(self, role: str, model_dir: str, device: str) -> str:
        torch = self.torch
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        self.unload(role)
        tok = self.tokenizer_of.from_pretrained(model_dir, padding_side="left")
        try:
            model = self.classes[role].from_pretrained(model_dir, dtype=torch.bfloat16)
        except TypeError:                                  # an older transformers names it torch_dtype
            model = self.classes[role].from_pretrained(model_dir, torch_dtype=torch.bfloat16)
        model = model.to(device).eval()
        extra: Dict[str, Any] = {}
        if role == "rerank":
            extra = {"yes": tok.convert_tokens_to_ids("yes"), "no": tok.convert_tokens_to_ids("no"),
                     "pre": tok.encode(PREFIX, add_special_tokens=False), "suf": tok.encode(SUFFIX, add_special_tokens=False)}
        self.models[role] = {"model": model, "tok": tok, "device": device, **extra}
        return device

    def unload(self, role: str) -> None:
        if self.models.pop(role, None) is not None:
            import gc
            gc.collect()
            if self.torch.cuda.is_available():
                self.torch.cuda.empty_cache()

    def embed(self, texts: Sequence[str], kind: str, instruction: str, batch: int, max_len: int, dim: Optional[int],
              progress: Callable[[int, int], None]):
        torch, np = self.torch, self.np
        m = self.models["embed"]
        strings = [query_text(instruction, t) if kind == "query" else t for t in texts]
        order = sorted(range(len(strings)), key=lambda i: len(strings[i]))          # similar lengths together waste less padding
        out: List[Any] = [None] * len(strings)
        for start in range(0, len(order), max(1, batch)):
            ids = order[start:start + batch]
            enc = m["tok"]([strings[i] for i in ids], padding=True, truncation=True, max_length=max_len, return_tensors="pt").to(m["device"])
            with torch.no_grad():
                h = m["model"](**enc).last_hidden_state
            mask = enc["attention_mask"]
            last = h[:, -1] if bool(mask[:, -1].sum() == mask.shape[0]) else h[torch.arange(h.shape[0], device=h.device), mask.sum(dim=1) - 1]
            vec = torch.nn.functional.normalize(last.float(), dim=1).cpu().numpy()
            for j, i in enumerate(ids):
                out[i] = vec[j]
            progress(min(start + batch, len(order)), len(order))
        return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)

    def rerank(self, query: str, passages: Sequence[str], instruction: str, batch: int, max_len: int, progress: Callable[[int, int], None]) -> List[float]:
        torch = self.torch
        m = self.models["rerank"]
        tok, pre, suf = m["tok"], m["pre"], m["suf"]
        scores: List[float] = []
        for start in range(0, len(passages), max(1, batch)):
            texts = [f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {d}" for d in passages[start:start + batch]]
            enc = tok(texts, padding=False, truncation="longest_first", return_attention_mask=False, max_length=max_len - len(pre) - len(suf))
            ids = [pre + e + suf for e in enc["input_ids"]]
            padded = tok.pad({"input_ids": ids}, padding=True, return_tensors="pt").to(m["device"])
            with torch.no_grad():
                logits = m["model"](**padded).logits[:, -1, :]
            two = torch.stack([logits[:, m["no"]], logits[:, m["yes"]]], dim=1)
            scores += torch.nn.functional.log_softmax(two.float(), dim=1)[:, 1].exp().tolist()
            progress(min(start + batch, len(passages)), len(passages))
        return scores


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _vectors(backend: Any, cmd: Dict[str, Any], progress: Callable[[int, int], None]) -> Dict[str, Any]:
    import numpy as np
    dim = int(cmd["dim"]) if cmd.get("dim") else None
    matrix = np.asarray(backend.embed([str(t) for t in cmd["texts"]], str(cmd.get("kind") or "passage"), str(cmd.get("instruction") or ""),
                                       int(cmd.get("batch") or DEFAULT_BATCH), int(cmd.get("max_len") or DEFAULT_MAX_LEN), dim, progress), dtype=np.float32)
    if dim and matrix.ndim == 2 and matrix.shape[1] > dim:
        matrix = matrix[:, :dim]
        matrix = matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    half = matrix.astype(np.float16)
    reply: Dict[str, Any] = {"shape": list(half.shape), "dtype": "float16"}
    if cmd.get("out"):
        with open(str(cmd["out"]), "wb") as f:
            np.save(f, half, allow_pickle=False)
        reply["file"] = str(cmd["out"])
    else:
        reply["data"] = base64.b64encode(half.tobytes()).decode("ascii")
    return reply


def serve(backend: Any, lines, send: Callable[[dict], None] = emit) -> int:
    """Runs the commands of `lines` (an iterable of text lines; its end is the order to stop) with `backend`."""
    for raw in lines:
        if not raw.strip():
            continue
        try:
            cmd = json.loads(raw)
            if not isinstance(cmd, dict):
                raise ValueError("a command is a JSON object")
        except ValueError as e:
            send({"ok": False, "error": f"bad command: {e}"})
            continue
        cid, name = cmd.get("id"), cmd.get("cmd")
        if name == "quit":
            send({"id": cid, "ok": True})
            return 0

        def progress(done: int, total: int, cid=cid) -> None:
            send({"event": "progress", "id": cid, "done": done, "total": total})
        try:
            started = time.monotonic()
            reply: Dict[str, Any] = {}
            if name == "load":
                reply = {"device": backend.load(str(cmd["role"]), str(cmd["model_dir"]), str(cmd.get("device") or "cuda"))}
            elif name == "unload":
                backend.unload(str(cmd["role"]))
            elif name == "embed":
                reply = _vectors(backend, cmd, progress)
            elif name == "rerank":
                reply = {"scores": [float(s) for s in backend.rerank(str(cmd["query"]), [str(p) for p in cmd["passages"]], str(cmd.get("instruction") or RERANK_INSTRUCTION),
                                                                       int(cmd.get("batch") or 4), int(cmd.get("max_len") or DEFAULT_MAX_LEN), progress)]}
            elif name != "ping":
                raise ValueError(f"unknown command: {name}")
            send({"id": cid, "ok": True, "seconds": round(time.monotonic() - started, 3), **reply})
        except Exception as e:                              # the caller is told; the worker stays up for the next command
            send({"id": cid, "ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"})
    return 0


def main() -> int:
    try:
        backend = TorchBackend()                            # the libraries load first, in the main thread, before anything reads stdin
    except Exception as e:
        emit({"event": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"})
        return 2
    emit({"event": "ready"})
    return serve(backend, sys.stdin)


if __name__ == "__main__":
    raise SystemExit(main())
