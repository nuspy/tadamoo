"""Helpers for the live tests against a running TaDaMoo server (tests/live/*.py): one request, the answer's tokens counted apart from the thinking,
the GPU's power limit, and a results file. Nothing here starts, stops or reconfigures the server."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))                      # the harness code of this tree, not an installed one
ENDPOINT = os.environ.get("LIVE_ENDPOINT", "http://127.0.0.1:7232")
OUT = Path(os.environ.get("LIVE_OUT", "D:/tmp/live_2026_11_26"))
OUT.mkdir(parents=True, exist_ok=True)


def _post(path: str, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = 3600.0) -> Dict[str, Any]:
    req = urllib.request.Request(ENDPOINT + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **(headers or {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def status() -> Dict[str, Any]:
    return json.load(urllib.request.urlopen(ENDPOINT + "/v1/status", timeout=10))


def tokens(text: str) -> int:
    """Tokens of `text` by the server's own tokenizer."""
    if not text:
        return 0
    return len(_post("/tokenize", {"content": text}, timeout=120)["tokens"])


def chat(messages: List[Dict[str, Any]], *, max_tokens: int, thinking: Optional[bool] = None, session: str = "", extra: Optional[Dict[str, Any]] = None,
         temperature: Optional[float] = None, timeout: float = 3600.0) -> Dict[str, Any]:
    """One non-streamed request. `answer_tokens` is the tokens of the answer text alone (the thinking is not counted); `completion_tokens` is what the
    server counts, thinking included. `thinking` None leaves the model's own default."""
    body: Dict[str, Any] = {"model": "live", "messages": messages, "max_tokens": max_tokens, **(extra or {})}
    if temperature is not None:
        body["temperature"] = temperature
    if thinking is not None:
        body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    t0 = time.time()
    d = _post("/v1/chat/completions", body, {"X-Session-Id": session} if session else None, timeout)
    seconds = time.time() - t0
    ch = d["choices"][0]
    msg = ch["message"]
    content, thought = msg.get("content") or "", msg.get("reasoning_content") or msg.get("reasoning") or ""
    usage = d.get("usage") or {}
    return {"finish": ch.get("finish_reason"), "content": content, "thinking_chars": len(thought), "answer_tokens": tokens(content),
            "thinking_tokens": tokens(thought), "completion_tokens": usage.get("completion_tokens"), "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"), "words": len(content.split()), "seconds": round(seconds, 1),
            "answer_capped": d.get("answer_capped"), "repeated": d.get("repeated")}


def gpu() -> Dict[str, str]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,power.limit,power.draw,memory.used,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
        name, limit, draw, used, total = [x.strip() for x in out.split(",")]
        return {"gpu": name, "power_limit": limit, "power_draw": draw, "vram": f"{used} of {total}"}
    except Exception as exc:                                  # a missing nvidia-smi is not a reason to stop a test
        return {"gpu": f"unknown ({type(exc).__name__})"}


def record(name: str, row: Dict[str, Any]) -> None:
    """Appends one JSON line to <OUT>/<name>.jsonl and prints it."""
    row = {"at": time.strftime("%H:%M:%S"), **row}
    with open(OUT / f"{name}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False)[:600], flush=True)
