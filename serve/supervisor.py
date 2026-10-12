"""The engine's supervisor inside the server: restart the engine with the settings now in the --config file, and
measure its speed (calibration), while the HTTP server keeps answering. The engine process stays in the server's
job object (serve/util/winjob.py, through ProcessEngine), so it still ends with the server however that ends."""
from __future__ import annotations

import json
import random
import sys
import threading
import time
from pathlib import Path

from serve import loader, settings
from serve.config import build_parser, engine_settings, parse_settings
from serve.engine.errors import EngineStartError
from serve.engine.vision import EngineVision, Vision, child_env, engine_args, gpu_list, gpus_in_play, vision_env

RELOAD_WAIT_S = 300.0          # how long a reload waits for the running request to finish
CALIBRATION_ANSWER = 512       # tokens of the reasoning-off answer
CALIBRATION_THINKING = 1024    # tokens of the reasoning-on answer
CALIBRATION_PROMPT = 8192      # tokens of the long prompt


def settings_info(a, flags: dict, argv: list[str]) -> dict:
    """What the settings page shows: the --config file, the model, the values this run uses and the fields the
    command line set (they win over the file)."""
    given = lambda name: any(w == f"--{name}" or w.startswith(f"--{name}=") for w in argv)   # noqa: E731
    running = {"server.port": a.port, "server.parallel": a.parallel or 1, "server.vision": a.vision,
               "server.slot-full-context": bool(a.slot_full_context)}
    for name in ("max-context", "kv", "spec", "spec-min-p", "expert-cache", "prefill", "resident-experts",
                 "vram-reserve-mib", "ple-io", "pool-workers"):
        value = flags.get(f"--{name}")
        running[f"engine.{name}"] = None if value is False else value
    if getattr(a, "shared_context", None):              # the total, before it was divided among the slots
        running["engine.max-context"] = a.shared_context
    fields = {f"{s}.{k}" for s, k in (*settings.FIELDS, *settings.LOADER_FIELDS)}
    return {"config": str(Path(a.config).resolve()) if a.config else None, "model": a.model, "running": running,
            "cli": sorted(n for n in fields if given(n.split(".", 1)[1]))}


def engine_config(a, flags: dict, mock: bool = False) -> dict:
    """The engine's spawn settings for a model and its flags (modelfile.model_config with the switches set off
    removed); ValueError when the model file cannot be read."""
    from serve.engine import modelfile
    cfg = {}
    if a.gpu is not None:
        cfg["gpu"] = a.gpu
    listed = gpu_list(cfg)
    vision_arg = False if mock or a.vision == "off" else a.vision
    try:
        cfg.update({k: v for k, v in modelfile.model_config(
            a.model, a.engine_bin, gpus=len(listed) if listed else None, vision=vision_arg,
            overrides={f: v for f, v in flags.items() if v is not False}).items() if k not in cfg})
    except (OSError, KeyError) as e:
        raise ValueError(f"{a.model}: {e}") from None
    off = {f for f, v in flags.items() if v is False}
    if off and cfg.get("args"):
        cfg["args"] = [x for x in cfg["args"] if x not in off]
    return cfg


def make_vision(cfg: dict, env: dict):
    """The image encoder the config asks for (None without one)."""
    if not cfg.get("vision"):
        return None
    if cfg["vision"].get("mode") == "dynamic":
        return EngineVision(cfg["vision"])
    return Vision(cfg["vision"], log=open(cfg["log"], "a", encoding="utf-8") if cfg.get("log") else None,
                  env=vision_env(cfg, env, "on" if cfg["vision"].get("mode") == "on" else "peer"))


class Supervisor:
    """Reload and calibrate jobs, one at a time, with their progress for GET /api/loader/status."""

    def __init__(self, svc, argv: list[str], mock: bool = False, facts: dict | None = None):
        self.svc, self.argv, self.mock = svc, list(argv), mock
        self.lock = threading.Lock()
        self.mock_delay = 1.5                            # the mock engine's "load"
        self.auto_calibrate = not mock
        self.facts = facts if facts is not None else loader.DEMO_FACTS
        self.state = {"phase": "idle", "job": None, "message": "", "started": None, "finished": None, "error": None,
                      "step": None}
        self.thread = None

    # ---- state
    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def busy(self) -> bool:
        with self.lock:
            return self.state["phase"] in ("waiting", "stopping", "starting", "calibrating")

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _begin(self, job: str) -> bool:
        with self.lock:
            if self.state["phase"] in ("waiting", "stopping", "starting", "calibrating"):
                return False
            self.state = {"phase": "waiting", "job": job, "message": "", "started": time.time(), "finished": None,
                          "error": None, "step": None}
            return True

    # ---- reload
    def reload(self, wait: bool = False) -> bool:
        """Restart the engine with the --config file's settings on a thread; False when a job is running."""
        if not self._begin("reload"):
            return False
        self.thread = threading.Thread(target=self._reload, name="engine-reload", daemon=True)
        self.thread.start()
        if wait:
            self.thread.join()
        return True

    def _parse(self):
        ap = build_parser()

        def error(message):
            raise ValueError(message)
        ap.error = error
        a, used, file_engine, cli_engine = parse_settings(ap, self.argv)
        flags, _ = engine_settings(file_engine, cli_engine)
        return a, flags

    def _reload(self):
        svc = self.svc
        try:
            a, flags = self._parse()
            if not svc.fifo.acquire(timeout=RELOAD_WAIT_S):
                raise RuntimeError("a request kept the model busy; try again when it has finished")
            try:
                svc.loading = True
                self._set(phase="stopping")
                print("[server] reloading the engine with the settings of the config file", flush=True)
                if self.mock:
                    self._reload_mock(a, flags)
                else:
                    self._reload_engine(a, flags)
                svc.settings_info = settings_info(a, flags, self.argv)
                svc.standby = False                      # the Loader loaded the model: requests may run now
            finally:
                svc.loading = False
                svc.fifo.release()
        except EngineStartError as e:
            self._set(phase="failed", error=str(e), finished=time.time())
            print(f"[server] error: the engine did not start with the new settings: {e}", flush=True)
            return
        except Exception as e:  # noqa: BLE001 - reported on the page and in the log
            self._set(phase="failed", error=str(e), finished=time.time())
            print(f"[server] error: reload failed: {e}", flush=True)
            return
        self._set(phase="ready", finished=time.time())
        print(f"[server] engine reloaded: context {svc.engine.max_context} tokens", flush=True)
        if self.auto_calibrate and not loader.read_calibrations(self.facts)["runs"]:
            self.calibrate()                             # the first load of this model

    def _reload_mock(self, a, flags):
        self._set(phase="starting")
        time.sleep(self.mock_delay)
        ctx = int(flags.get("--max-context") or 4096)
        self.svc.engine.max_context = ctx
        par = a.parallel or 1
        self.svc.engine.batch = par if par >= 2 else 0

    def _reload_engine(self, a, flags):
        svc, engine = self.svc, self.svc.engine
        cfg = engine_config(a, flags)
        env = child_env(cfg)
        old = svc.vision
        if old is not None and not getattr(old, "in_engine", False):
            old.close()                                  # its VRAM is free before the engine sizes its cache
        engine.close()
        self._set(phase="starting")
        spawn = list(engine.spawn)
        spawn[0], spawn[1], spawn[3], spawn[4] = cfg["engine_bin"], engine_args(cfg), cfg.get("log"), env
        engine.spawn = tuple(spawn)
        engine.restart(tries=1)
        vision = make_vision(cfg, env)
        if isinstance(vision, EngineVision):
            vision.attach(engine)
        svc.vision = vision
        svc.gpu_indices = gpus_in_play(cfg, engine_args(cfg))
        svc.gpu_index = svc.gpu_indices[0]

    # ---- calibration
    def calibrate(self, wait: bool = False) -> bool:
        """Measure this configuration's speeds on a thread; False when a job is running."""
        if self.thread is not None and self.thread is threading.current_thread():
            with self.lock:                              # chained from a reload: the job goes on
                self.state.update(phase="waiting", job="calibrate", started=time.time(), finished=None, error=None)
            self._calibrate()
            return True
        if not self._begin("calibrate"):
            return False
        self.thread = threading.Thread(target=self._calibrate, name="calibrate", daemon=True)
        self.thread.start()
        if wait:
            self.thread.join()
        return True

    def _measure(self, prompt: str, max_new: int, effort: str | None) -> dict:
        from serve.frontend import openai_to_messages
        svc = self.svc
        req = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "top_p": 0.95, "seed": 1234}
        if effort:
            req["reasoning_effort"] = effort
        messages, tools, kw = openai_to_messages(req)
        svc.load()
        ids, thinking, max_new = svc.prepare(messages, tools, kw, max_new)
        t0, first, done = time.time(), None, {}
        visible = 0
        for kind, x in svc.run(ids, thinking, tools, max_new, req, threading.Event()):
            if kind == "event" and first is None:
                first = time.time()
            if kind == "event" and x.kind == "content":
                visible += 1
            if kind == "done":
                done = x
        end = time.time()
        timings = done.get("timings") or {}
        n = int(done.get("completion_tokens") or 0)
        decode = timings.get("predicted_per_second") or (n / max(1e-6, end - (first or t0)) if n else None)
        prefill = timings.get("prompt_per_second") or (len(ids) / max(1e-6, (first or end) - t0))
        last = svc.history[-1] if svc.history else {}
        return {"prompt_tokens": len(ids), "tokens": n, "reasoning_tokens": int(done.get("reasoning_tokens") or 0),
                "decode": decode, "prefill": prefill, "hit_rate": last.get("hit_rate"),
                "drafts_offered": last.get("drafts_offered"), "drafts_accepted": last.get("drafts_accepted")}

    def _long_prompt(self) -> str:
        """About CALIBRATION_PROMPT tokens of varied text, with a fresh start so no cached prefix is reused."""
        rnd = random.Random()
        words = ("river", "granite", "ledger", "violet", "harbor", "engine", "lantern", "meadow", "copper", "signal",
                 "orchard", "quartz", "tunnel", "beacon", "saddle", "glacier", "pepper", "canyon", "marble", "falcon")
        head = f"Calibration {rnd.getrandbits(64):016x}. Read these notes, then answer in one word: ready.\n\n"
        lines, n = [head], 0
        tok = self.svc.tok
        while n < CALIBRATION_PROMPT - 64:
            line = f"Note {len(lines)}: the {rnd.choice(words)} near the {rnd.choice(words)} weighs " \
                   f"{rnd.randint(2, 9999)} units and points to the {rnd.choice(words)}.\n"
            lines.append(line)
            n += len(tok.encode(line))
        return "".join(lines)

    def _calibrate(self):
        svc = self.svc
        self._set(phase="calibrating")
        try:
            values = {**(svc.settings_info or {}).get("running", {})}
            ask = ("Explain, in about 400 words, how a mixture-of-experts language model chooses which experts to run "
                   "for each token, and why that makes caching experts in GPU memory effective.")
            self._set(step="decode")
            off = self._measure(ask, CALIBRATION_ANSWER, "none")
            self._set(step="reasoning")
            on = self._measure(ask, CALIBRATION_THINKING, None)
            self._set(step="prefill")
            pre = self._measure(self._long_prompt(), 16, "none")
            info = getattr(svc.engine, "info", {}) or {}
            the_plan = loader.plan(values, self.facts, loader.system_now(getattr(svc, "gpu_index", 0)))
            slots = info.get("expert_slots") or the_plan["vram"]["expert_slots"]
            accept = None
            if off.get("drafts_offered"):
                accept = off["drafts_accepted"] / off["drafts_offered"]
            record = {"at": loader.now_iso(), "fingerprint": loader.fingerprint(values), "values": values,
                      "decode_off": round(off["decode"] or 0, 1), "decode_on": round(on["decode"] or 0, 1),
                      "visible_share": round((on["tokens"] - on["reasoning_tokens"]) / on["tokens"], 3)
                      if on["tokens"] else None,
                      "tokens_off": off["tokens"], "tokens_on": on["tokens"], "reasoning_tokens": on["reasoning_tokens"],
                      "prefill": round(pre["prefill"] or 0, 0), "prefill_tokens": pre["prompt_tokens"],
                      "prefill_chunk": loader.prefill_chunk(loader.effective(values)),
                      "hit_rate": off.get("hit_rate"), "expert_slots": slots,
                      "cache_share": round(slots / max(1, the_plan["vram"]["expert_slots_total"]), 4),
                      "spec": int(loader.effective(values)["engine.spec"] or 0), "accept": accept,
                      "engine": info.get("version"), "mock": self.mock}
            if not record["decode_off"]:
                raise RuntimeError("no tokens were generated")
            path = loader.save_calibration(self.facts, record)
            print(f"[server] calibration saved to {path}: decode {record['decode_off']} tok/s, with reasoning "
                  f"{record['decode_on']} tok/s, prefill {record['prefill']:.0f} tok/s", flush=True)
            self._set(phase="ready", finished=time.time(), step=None, message=json.dumps(record))
        except Exception as e:  # noqa: BLE001 - reported on the page and in the log
            self._set(phase="failed", error=f"calibration: {e}", finished=time.time(), step=None)
            print(f"[server] error: calibration failed: {e}", file=sys.stderr, flush=True)
