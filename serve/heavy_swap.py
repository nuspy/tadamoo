"""A heavy job that needs the whole GPU (a video, a big image) can put the local model to sleep and send the conversations to a provider the person
chose, for as long as the job runs. Only with the person's yes, and never silently.

    begin(kind, purpose, minutes, wait_s)   {"ok": True, "swap": {...}} or {"ok": False, "reason", "message", "message_it"}
    touch(id), end(id), state(), answer(id, allow, always)

The person's level (`mode`): `ask` (every job asks, the default), `allow` (image and video jobs go ahead; any other kind still asks), `never`.
A job that asks waits for the answer on a card the pages show (`pending()`), up to `wait_s`. With the yes: the registry of providers swaps (every function on
the loaded model reads as the chosen provider, `SecureRegistry.begin_swap`), the engine sleeps (`Service.sleep`: its VRAM is free, its RAM kept) and the
server refuses to wake it for anyone else until `end`. A swap not touched for `hold_s` ends by itself, as a lease does.

It exists only for a local Basalt or Strata engine, the two that have the VRAM, SLEEP and WAKE commands: with no local model (a cloud service answers, or
none is loaded) there is nothing to put to sleep, and llama.cpp or vLLM cannot be put to sleep from here.

Refusal codes: not_allowed (the level is never, or the person said no), no_provider, bad_provider, busy (another swap), timeout (nobody answered),
no_local_engine, unsupported_engine, cannot_sleep (the engine cannot sleep now), bad_request."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

MODES = ("ask", "allow", "never")
ALLOWED_KINDS = ("image", "video")                 # what the level `allow` covers
HOLD_DEFAULT_S = 90
ASK_WAIT_S = 600.0
SLEEP_TRIES = 60                                   # one try a second: a request the engine still runs has to finish first
TICK_S = 1.0
MESSAGES = {
    "not_allowed": ("The person did not allow the local model to sleep for this job.", "Non hai permesso di mettere a riposo il modello locale per questo lavoro."),
    "no_provider": ("No provider is chosen for heavy jobs: choose one in Settings, Heavy jobs.",
                    "Per i lavori pesanti non è scelto nessun provider: sceglilo in Impostazioni, Lavori pesanti."),
    "bad_provider": ("The provider chosen for heavy jobs cannot be used.", "Il provider scelto per i lavori pesanti non si può usare."),
    "busy": ("Another heavy job already holds the local model.", "Un altro lavoro pesante tiene già il modello locale."),
    "timeout": ("Nobody answered the question in time.", "Nessuno ha risposto alla domanda in tempo."),
    "cannot_sleep": ("The local model cannot go to sleep now.", "Il modello locale non può mettersi a riposo ora."),
    "no_local_engine": ("No local model is running, so there is nothing to put to sleep.", "Nessun modello locale è in funzione: non c'è nulla da mettere a riposo."),
    "unsupported_engine": ("Only Basalt and Strata can be put to sleep from here; this engine cannot.",
                           "Solo Basalt e Strata si possono mettere a riposo da qui; questo motore no."),
    "bad_request": ("Send kind (text) and purpose (text).", "Manda kind (testo) e purpose (testo)."),
    "not_found": ("No such swap: it ended.", "Nessun passaggio con questo id: è finito."),
}


def refusal(reason: str, detail: str = "") -> dict:
    en, it = MESSAGES[reason]
    return {"ok": False, "reason": reason, "message": en + (f" ({detail})" if detail else ""), "message_it": it + (f" ({detail})" if detail else "")}


class HeavyPrefs:
    """{"mode": "ask" | "allow" | "never", "provider": id of a provider} in one small file of the user data folder."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None

    def read(self) -> dict:
        data = {}
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        data = data if isinstance(data, dict) else {}
        provider = data.get("provider")
        return {"mode": data.get("mode") if data.get("mode") in MODES else "ask", "provider": provider if isinstance(provider, str) else ""}

    def write(self, changes: dict) -> dict:
        now = self.read()
        if "mode" in changes:
            if changes["mode"] not in MODES:
                raise ValueError("mode is ask, allow or never")
            now["mode"] = changes["mode"]
        if "provider" in changes:
            if not isinstance(changes["provider"], str):
                raise ValueError("provider is the id of a provider, or empty")
            now["provider"] = changes["provider"]
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(now, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        return now


class HeavySwap:
    def __init__(self, service, registry, prefs: HeavyPrefs, ledger=None, clock=time.monotonic, hold_s: float = HOLD_DEFAULT_S,
                 sleep_tries: int = SLEEP_TRIES, pause=time.sleep) -> None:
        self.service, self.registry, self.prefs, self.ledger = service, registry, prefs, ledger
        self.clock, self.hold_s, self.sleep_tries, self.pause = clock, hold_s, sleep_tries, pause
        self._cv = threading.Condition()
        self._active: dict | None = None
        self._pending: dict[str, dict] = {}
        self._timer: threading.Thread | None = None
        self._closed = False

    def engine_problem(self) -> str:
        """"" when the model of this server is a local engine that can be put to sleep, else the refusal code."""
        if not self.service.loaded():
            return "no_local_engine"
        engine = getattr(self.service, "engine", None)
        if not (hasattr(engine, "sleep") and hasattr(engine, "can_sleep")):
            return "unsupported_engine"
        return ""

    # ---- the question
    def _card(self, kind: str, purpose: str, minutes: float, cfg: dict) -> dict:
        from tadamoo.llm import functions
        data = self.registry.load()
        prov = self.registry._find(data, cfg["provider"]) or {}
        name = prov.get("name") or cfg["provider"]
        moving = [f for f in functions.names() if functions.spec(f)["kind"] == "chat"]
        used = self.ledger.used(cfg["provider"]) if self.ledger is not None else 0
        cap = int(prov.get("daily_token_cap") or 0)
        return {"id": uuid.uuid4().hex[:16], "kind": kind, "purpose": purpose[:200], "minutes": round(float(minutes), 1), "provider": name,
                "provider_id": cfg["provider"], "functions": moving,
                "leaves": "the text of the conversations that use these functions while the job runs, the files the model reads in them and the results of its tools",
                "leaves_it": "il testo delle conversazioni che usano queste funzioni finché il lavoro dura, i file che il modello vi legge e i risultati dei suoi strumenti",
                "cost": {"tokens_used_today": used, "daily_cap": cap,
                         "note": f"what it costs depends on your plan with {name}; the tokens used are counted here",
                         "note_it": f"quanto costa dipende dal tuo piano con {name}; i token usati sono contati qui"},
                "answer": None, "always": False}

    def pending(self) -> list[dict]:
        with self._cv:
            return [dict(c) for c in self._pending.values() if c["answer"] is None]

    def answer(self, card_id: str, allow: bool, always: bool = False) -> dict:
        with self._cv:
            card = self._pending.get(card_id)
            if card is None or card["answer"] is not None:
                return refusal("not_found")
            card["answer"], card["always"] = bool(allow), bool(allow and always)
            self._cv.notify_all()
        if allow and always:
            self.prefs.write({"mode": "allow"})
        return {"ok": True}

    # ---- begin / touch / end
    def begin(self, kind, purpose, minutes=5, wait_s=ASK_WAIT_S) -> dict:
        try:
            return self._begin(kind, purpose, minutes, wait_s)
        except Exception as e:  # noqa: BLE001 - the caller gets a result, never an exception
            self._leave()
            self.registry.end_swap()
            self.service.heavy_hold = None
            return refusal("cannot_sleep", f"{e.__class__.__name__}: {e}")

    def _begin(self, kind, purpose, minutes, wait_s) -> dict:
        if not isinstance(kind, str) or not kind.strip() or not isinstance(purpose, str):
            return refusal("bad_request")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 < minutes <= 24 * 60:
            minutes = 5
        cfg = self.prefs.read()
        problem = self.engine_problem()
        if problem:
            return refusal(problem)
        if cfg["mode"] == "never":
            return refusal("not_allowed")
        if not cfg["provider"]:
            return refusal("no_provider")
        with self._cv:
            if self._active is not None or self._closed:
                return refusal("busy")
            self._active = {"id": None}                  # held while the question is open: a second job waits for its own turn at the engine, not here
        try:
            card = self._card(kind.strip(), purpose, minutes, cfg)
            if cfg["mode"] != "allow" or kind.strip() not in ALLOWED_KINDS:
                with self._cv:
                    self._pending[card["id"]] = card
                    deadline = self.clock() + float(wait_s)
                    while card["answer"] is None:
                        left = deadline - self.clock()
                        if left <= 0 or self._closed:
                            break
                        self._cv.wait(min(left, 0.5))
                    answer = card["answer"]
                    self._pending.pop(card["id"], None)
                if answer is None:
                    return self._fail("timeout")
                if not answer:
                    return self._fail("not_allowed")
            try:
                swap = self.registry.begin_swap(cfg["provider"])
            except ValueError as e:
                return self._fail("bad_provider", str(e))
            self.service.heavy_hold = (f"the local model sleeps while a {kind.strip()} job runs ({purpose[:80]}); the conversations use {swap['name']} "
                                       "meanwhile. Try again when the job has finished")
            why = self._sleep()
            if why:
                self.registry.end_swap()
                self.service.heavy_hold = None
                return self._fail("cannot_sleep", why)
            now = self.clock()
            with self._cv:
                self._active = {"id": uuid.uuid4().hex[:16], "kind": kind.strip(), "purpose": purpose[:200], "provider": swap["name"], "began": now, "last": now}
                out = {"ok": True, "swap": {k: self._active[k] for k in ("id", "kind", "purpose", "provider")} | {"hold_s": self.hold_s}}
            self._start_timer()
            return out
        except BaseException:
            self._leave()
            raise

    def _fail(self, reason: str, detail: str = "") -> dict:
        self._leave()
        return refusal(reason, detail)

    def _leave(self) -> None:
        with self._cv:
            if self._active is not None and self._active.get("id") is None:
                self._active = None

    def _sleep(self) -> str:
        """"" when the engine sleeps (or there is none to put to sleep), else why not."""
        last = ""
        for _ in range(max(1, self.sleep_tries)):
            last = self.service.sleep()
            if last in ("asleep", "not loaded"):
                return ""
            if last != "busy":
                return last
            self.pause(1.0)
        return "a request is still running"

    def touch(self, swap_id) -> dict:
        with self._cv:
            if self._active is None or self._active.get("id") != swap_id:
                return refusal("not_found")
            self._active["last"] = self.clock()
        self.registry.touch_swap()
        return {"ok": True}

    def end(self, swap_id) -> dict:
        with self._cv:
            if self._active is None or self._active.get("id") != swap_id:
                return refusal("not_found")
            self._active = None
        self._finish()
        return {"ok": True}

    def _finish(self) -> None:
        """The conversations come back first, then the engine wakes: nothing asks it for an answer while it fills its cache."""
        self.registry.end_swap()
        try:
            self.service.heavy_hold = None
            self.service.wake_now()
        except Exception as e:  # noqa: BLE001 - the next request wakes it, or loads it again
            print(f"[server] warning: waking the model after a heavy job failed ({e})", flush=True)

    # ---- the safety net
    def _start_timer(self) -> None:
        if self._timer is None or not self._timer.is_alive():
            self._timer = threading.Thread(target=self._tick, name="heavy-swap-timer", daemon=True)
            self._timer.start()

    def _tick(self) -> None:
        while True:
            time.sleep(TICK_S)
            with self._cv:
                if self._closed or self._active is None:
                    return
                lapsed = self._active.get("id") and self.clock() - self._active["last"] > self.hold_s
                if lapsed:
                    self._active = None
            if lapsed:
                self._finish()
                return

    def shutdown(self) -> None:
        with self._cv:
            self._closed = True
            was = self._active is not None and self._active.get("id") is not None
            self._active = None
            self._cv.notify_all()
        if was:
            self.registry.end_swap()

    def state(self) -> dict:
        with self._cv:
            active = {k: v for k, v in (self._active or {}).items() if k in ("id", "kind", "purpose", "provider")} if self._active and self._active.get("id") else None
        return {**self.prefs.read(), "active": active, "pending": self.pending(), "engine": self.engine_problem()}
