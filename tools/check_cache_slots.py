#!/usr/bin/env python3
"""How a server handles the slot a /v1/chat/completions request is pinned to: `cache_slot` (Basalt's) and `id_slot` (llama-server's).

Run it against a server that is up with its model loaded, when the GPU is free (it sends real requests; the prefix probes need the real
model to show a cache):

    python tools/check_cache_slots.py --base http://127.0.0.1:7232 --server basalt --model qwen3.8-flash-next
    python tools/check_cache_slots.py --base http://127.0.0.1:8080 --server llama --model <alias> --slots 2

`--server` is `basalt` (a Basalt server, ideally started with `--parallel 2 --side-slot 30000`) or `llama` (llama-server, ideally with
`-np 2`). Each probe prints PASS, FAIL, INFO (a behaviour recorded, neither right nor wrong) or SKIP, with the evidence. Exit code 1 if any FAIL.

What is asked, per probe:
  accepts_other_key   the other server's slot key is not an error (Basalt ignores id_slot; llama-server ignores cache_slot)
  bad_value           a bad value of the server's own key: Basalt answers 400, llama-server's behaviour is recorded
  same_slot_reuse     the same long prompt twice in one slot: the second reads (almost) all of it from the cache
  other_slot_kept     prompt A, then B in another slot, then A again in A's slot: A is still cached (the other slot did not evict it)
  side_session        (Basalt) cache_slot "side" is served by the session when it has one, and "side_only" is refused when it has none
  slots_listing       (llama-server) GET /slots lists the slots and the number agrees with --slots
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

PASS, FAIL, INFO, SKIP = "PASS", "FAIL", "INFO", "SKIP"
FILLER = ("The quick brown fox jumps over the lazy dog while the engine counts tokens and the cache keeps the prefix of this prompt. ")
REUSE_MIN = 0.85                         # the share of the prompt the second call must read from the cache


class Transport:
    """The two calls the probes make; tests replace it."""

    def __init__(self, base: str, api_key: str = "", timeout: float = 600.0) -> None:
        self.base, self.key, self.timeout = base.rstrip("/"), api_key, timeout

    def _open(self, req: urllib.request.Request) -> Tuple[int, Any]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                code = r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read().decode("utf-8", "replace"), e.code
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": {"message": str(e)}}
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, {"raw": raw[:300]}

    def post(self, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
        headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + self.key} if self.key else {})}
        return self._open(urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers))

    def get(self, path: str) -> Tuple[int, Any]:
        return self._open(urllib.request.Request(self.base + path, headers={"Authorization": "Bearer " + self.key} if self.key else {}))


def cached_tokens(reply: Any) -> Optional[int]:
    """Prompt tokens a reply says came from the cache: OpenAI's field (Basalt, recent llama-server) or llama-server's own `timings.cache_n`."""
    if not isinstance(reply, dict):
        return None
    details = (reply.get("usage") or {}).get("prompt_tokens_details") or {}
    if isinstance(details.get("cached_tokens"), int):
        return details["cached_tokens"]
    timings = reply.get("timings") or {}
    return timings["cache_n"] if isinstance(timings.get("cache_n"), int) else None


def prompt_tokens(reply: Any) -> int:
    return int(((reply or {}).get("usage") or {}).get("prompt_tokens") or 0)


def prompt(tag: str, paragraphs: int) -> List[Dict[str, str]]:
    """A long user message that begins with `tag`, so two tags give prompts that share nothing but the filler."""
    return [{"role": "user", "content": f"[{tag}] " + FILLER * paragraphs + "Say ok."}]


class Checker:
    def __init__(self, transport: Any, server: str, model: str, slots: int = 2, paragraphs: int = 60) -> None:
        if server not in ("basalt", "llama"):
            raise ValueError("server: basalt or llama")
        self.t, self.server, self.model, self.slots, self.paragraphs = transport, server, model, slots, paragraphs
        self.results: List[Dict[str, str]] = []

    # -- helpers ------------------------------------------------------------------------------------------

    def note(self, name: str, status: str, detail: str) -> None:
        self.results.append({"probe": name, "status": status, "detail": detail})

    @property
    def own_key(self) -> str:
        return "cache_slot" if self.server == "basalt" else "id_slot"

    def ask(self, messages: List[Dict[str, str]], extra: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        body = {"model": self.model, "messages": messages, "max_tokens": 4, "temperature": 0, **({"cache_prompt": True} if self.server == "llama" else {}),
                **(extra or {})}
        if self.server == "basalt":
            body["reasoning_effort"] = "none"
        return self.t.post("/v1/chat/completions", body)

    def slot_args(self, which: int) -> Dict[str, Any]:
        """The server's own way of pinning a request: llama-server's id_slot, or Basalt's cache_slot ("main", then "side")."""
        if self.server == "llama":
            return {"id_slot": which}
        return {"cache_slot": "main" if which == 0 else "side"}

    # -- probes --------------------------------------------------------------------------------------------

    def accepts_other_key(self) -> None:
        other = {"id_slot": 0} if self.server == "basalt" else {"cache_slot": "main"}
        code, reply = self.ask(prompt("other", 2), other)
        self.note("accepts_other_key", PASS if code == 200 else FAIL, f"{other} -> HTTP {code}" + ("" if code == 200 else f": {json.dumps(reply)[:200]}"))

    def bad_value(self) -> None:
        bad = {"cache_slot": "elsewhere"} if self.server == "basalt" else {"id_slot": 999}
        code, reply = self.ask(prompt("bad", 2), bad)
        text = f"{bad} -> HTTP {code}: {json.dumps(reply)[:160]}"
        if self.server == "basalt":
            self.note("bad_value", PASS if code == 400 else FAIL, text)
        else:
            self.note("bad_value", INFO, text)                               # llama-server may serve it from any slot or refuse: recorded

    def same_slot_reuse(self) -> None:
        messages = prompt("A", self.paragraphs)
        pin = self.slot_args(0)
        code1, first = self.ask(messages, pin)
        code2, second = self.ask(messages, pin)
        if code1 != 200 or code2 != 200:
            return self.note("same_slot_reuse", FAIL, f"HTTP {code1} then {code2}")
        got, total = cached_tokens(second), prompt_tokens(second)
        self.note("same_slot_reuse", *self.reuse_verdict(got, total, f"{pin}: the second call"))

    def other_slot_kept(self) -> None:
        if self.server == "llama" and self.slots < 2:
            return self.note("other_slot_kept", SKIP, "needs --slots 2 or more (start llama-server with -np 2)")
        a, b = prompt("A", self.paragraphs), prompt("B", self.paragraphs)
        results = [self.ask(a, self.slot_args(0)), self.ask(b, self.slot_args(1)), self.ask(a, self.slot_args(0))]
        if any(code != 200 for code, _ in results):
            return self.note("other_slot_kept", FAIL, "HTTP " + ", ".join(str(c) for c, _ in results) + ": "
                             + json.dumps([r for c, r in results if c != 200][:1])[:200])
        again = results[2][1]
        served = (results[1][1] or {}).get("cache_slot")
        extra = f" (the second prompt was served by the {served} slot)" if served else ""
        self.note("other_slot_kept", *self.reuse_verdict(cached_tokens(again), prompt_tokens(again), "A after B in another slot" + extra))

    def reuse_verdict(self, got: Optional[int], total: int, what: str) -> Tuple[str, str]:
        if got is None:
            return INFO, f"{what}: the reply does not say how much came from the cache"
        share = got / total if total else 0.0
        return (PASS if share >= REUSE_MIN else FAIL), f"{what} read {got} of {total} prompt tokens from the cache ({share:.0%}, least {REUSE_MIN:.0%})"

    def side_session(self) -> None:
        if self.server != "basalt":
            return self.note("side_session", SKIP, "Basalt only")
        code, status = self.t.get("/v1/status")
        side = ((status or {}).get("concurrency") or {}).get("side_slot") if code == 200 and isinstance(status, dict) else None
        code1, reply = self.ask(prompt("side", 2), {"cache_slot": "side"})
        served = (reply or {}).get("cache_slot") if code1 == 200 else None
        code2, strict = self.ask(prompt("side", 2000), {"cache_slot": "side_only"})
        if side:
            ok = served == "side" and code2 == 400
            return self.note("side_session", PASS if ok else FAIL, f"session {side}: 'side' served by {served!r}; an oversized 'side_only' -> HTTP {code2}")
        ok = served in ("main", None) and code2 == 400
        self.note("side_session", PASS if ok else FAIL, f"no session: 'side' served by {served!r} (an ordinary slot); 'side_only' -> HTTP {code2}")

    def slots_listing(self) -> None:
        if self.server != "llama":
            return self.note("slots_listing", SKIP, "llama-server only")
        code, listing = self.t.get("/slots")
        if code != 200 or not isinstance(listing, list):
            return self.note("slots_listing", INFO, f"GET /slots -> HTTP {code} (it may be disabled with --no-slots)")
        ids = sorted(s.get("id") for s in listing if isinstance(s, dict))
        self.note("slots_listing", PASS if len(ids) == self.slots else FAIL, f"slots {ids}, expected {self.slots}")

    def run(self) -> List[Dict[str, str]]:
        for probe in (self.accepts_other_key, self.bad_value, self.same_slot_reuse, self.other_slot_kept, self.side_session, self.slots_listing):
            try:
                probe()
            except Exception as exc:                                  # one probe failing to run is a result, not the end of the check
                self.note(probe.__name__, FAIL, f"the probe could not run: {exc}")
        return self.results


def report(results: List[Dict[str, str]]) -> str:
    return "\n".join(f"{r['status']:<5} {r['probe']:<18} {r['detail']}" for r in results)


def check(base: str, server: str, model: str, api_key: str = "", slots: int = 2, paragraphs: int = 60,
          transport: Any = None) -> List[Dict[str, str]]:
    return Checker(transport or Transport(base, api_key), server, model, slots, paragraphs).run()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="the server's address, e.g. http://127.0.0.1:7232")
    ap.add_argument("--server", required=True, choices=("basalt", "llama"))
    ap.add_argument("--model", required=True, help="the model name the server answers to")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--slots", type=int, default=2, help="llama-server: the number of slots it was started with (-np)")
    ap.add_argument("--paragraphs", type=int, default=60, help="the length of the test prompt (about 30 tokens each)")
    a = ap.parse_args(argv)
    results = check(a.base, a.server, a.model, a.api_key, a.slots, a.paragraphs)
    print(report(results))
    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
