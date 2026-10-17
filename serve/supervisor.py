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
from serve.config import build_parser, parse_settings, sampling_defaults_from_config, sampling_line
from serve.engine.errors import EngineStartError
from serve.engine.flags import flag_index, flag_value
from serve.engine.vision import EngineVision, Vision, child_env, engine_args, gpu_list, gpus_in_play, vision_env

RELOAD_WAIT_S = 300.0          # how long a reload waits for the running request to finish
BUSY = ("waiting", "stopping", "starting", "calibrating", "measuring")   # a job runs: no other may start
MEASURE_ANSWER = 384           # tokens of each answer the wizard's measurement times
SPEC_MIN_P_TRIES = (0.3, 0.5, 0.7)   # draft floors tried per request, without reloading
SPEC_TRIES = (5, 7, 8)         # verify windows the wizard's measurement tries (set at the start: one reload each)
CALIBRATION_ANSWER = 512       # tokens of the reasoning-off answer
CALIBRATION_THINKING = 1024    # tokens of the reasoning-on answer
CALIBRATION_PROMPT = 8192      # tokens of the long prompt


def flag_setting(words: list[str], flag: str):
    """The last `flag` in `words` as a setting: a number, a string, True for a bare switch, None when absent."""
    at = flag_index(words, flag)
    if at is None:
        return None
    value = words[at + 1] if at + 1 < len(words) else None
    if value is None or value.startswith("--"):
        return True
    for kind in (int, float):
        try:
            return kind(value)
        except ValueError:
            pass
    return value


def settings_info(a, words: list[str], argv: list[str]) -> dict:
    """What the settings page shows: the --config file, the model, the values this run uses and the fields the
    command line set (they win over the file)."""
    given = lambda name: any(w == f"--{name}" or w.startswith(f"--{name}=") for w in argv)   # noqa: E731
    running = {"server.port": a.port, "server.parallel": a.parallel or 1, "server.vision": a.vision,
               "server.slot-full-context": bool(a.slot_full_context)}
    for name in ("max-context", "kv", "spec", "spec-min-p", "expert-cache", "prefill", "resident-experts",
                 "vram-reserve-mib", "ple-io", "pool-workers"):
        running[f"engine.{name}"] = flag_setting(words, f"--{name}")
    if getattr(a, "shared_context", None):              # the total, before it was divided among the slots
        running["engine.max-context"] = a.shared_context
    fields = {f"{s}.{k}" for s, k in (*settings.FIELDS, *settings.LOADER_FIELDS)}
    return {"config": str(Path(a.config).resolve()) if a.config else None, "model": a.model, "running": running,
            "cli": sorted(n for n in fields if given(n.split(".", 1)[1]))}


def engine_config(a, words: list[str], mock: bool = False) -> dict:
    """The engine's spawn settings for a model and its engine words (modelfile.model_config); ValueError when the
    model file cannot be read."""
    from serve.engine import modelfile
    cfg = {}
    if a.gpu is not None:
        cfg["gpu"] = a.gpu
    listed = gpu_list(cfg)
    vision_arg = False if mock or a.vision == "off" else a.vision
    try:
        cfg.update({k: v for k, v in modelfile.model_config(
            a.model, a.engine_bin, gpus=len(listed) if listed else None, vision=vision_arg,
            engine_words=words).items() if k not in cfg})
    except (OSError, KeyError) as e:
        raise ValueError(f"{a.model}: {e}") from None
    return cfg


def sampling_defaults_of(a, cfg: dict) -> dict:
    """The sampling defaults in force once the engine runs `cfg`: the model file's, and over them the launch options
    (as at start); ValueError for a bad value, before anything is stopped."""
    try:
        return {**sampling_defaults_from_config(cfg), **a.sampling_launch}
    except SystemExit as e:
        raise ValueError(str(e).removeprefix("[server] error: ")) from None


def record_memory(facts: dict, values: dict, before: dict, after: dict) -> None:
    """The real memory of a load against its plan, for later plans (loader.record_load_memory); never fails a load."""
    try:
        r = loader.record_load_memory(facts, values, before, after)
    except Exception as e:  # noqa: BLE001 - a reading or a write that fails leaves the plans as they were
        print(f"[server] warning: the memory of this load was not recorded: {e}", flush=True)
        return
    if r and (r["commit_extra"] > 256 * loader.MiB or r["ram_extra"] > 256 * loader.MiB):
        print(f"[server] this load took {r['commit_extra'] / loader.GiB:.1f} GiB of commit and {r['ram_extra'] / loader.GiB:.1f}"
              " GiB of RAM more than planned; the Loader's plans for this model add it from now on", flush=True)


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
            return self.state["phase"] in BUSY

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _begin(self, job: str) -> bool:
        with self.lock:
            if self.state["phase"] in BUSY:
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
        return a, file_engine + cli_engine

    def _reload_now(self, phase: str = "stopping"):
        """Restart the engine with the config file's settings on this thread; raises when it does not start."""
        svc = self.svc
        a, words = self._parse()
        if not svc.fifo.acquire(timeout=RELOAD_WAIT_S):
            raise RuntimeError("a request kept the model busy; try again when it has finished")
        try:
            svc.loading = True
            self._set(phase=phase)
            print("[server] reloading the engine with the settings of the config file", flush=True)
            if self.mock:
                self._reload_mock(a, words)
            else:
                self._reload_engine(a, words)
            svc.settings_info = settings_info(a, words, self.argv)
            svc.standby = False                          # the Loader loaded the model: requests may run now
        finally:
            svc.loading = False
            svc.fifo.release()

    def _reload(self):
        svc = self.svc
        try:
            self._reload_now()
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

    def _reload_mock(self, a, words):
        self._set(phase="starting")
        time.sleep(self.mock_delay)
        ctx = int(flag_value(words, "--max-context") or 4096)
        self.svc.engine.max_context = ctx
        par = a.parallel or 1
        self.svc.engine.batch = par if par >= 2 else 0
        self.svc.sampling_defaults = dict(a.sampling_launch)

    def _reload_engine(self, a, words):
        svc, engine = self.svc, self.svc.engine
        cfg = engine_config(a, words)
        defaults = sampling_defaults_of(a, cfg)         # a bad value refuses the reload while the engine still runs
        env = child_env(cfg)
        old = svc.vision
        if old is not None and not getattr(old, "in_engine", False):
            old.close()                                  # its VRAM is free before the engine sizes its cache
        engine.close()
        self._set(phase="starting")
        spawn = list(engine.spawn)
        spawn[0], spawn[1], spawn[3], spawn[4] = cfg["engine_bin"], engine_args(cfg), cfg.get("log"), env
        engine.spawn = tuple(spawn)
        before = loader.system_now(getattr(svc, "gpu_index", 0))
        engine.restart(tries=1)
        svc.sampling_defaults = defaults
        if defaults:
            print(sampling_line(defaults, a.sampling_cli, set(a.sampling_launch) - a.sampling_cli), flush=True)
        record_memory(self.facts, settings_info(a, words, self.argv)["running"], before,
                      loader.system_now(getattr(svc, "gpu_index", 0)))
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

    def _measure(self, prompt: str, max_new: int, effort: str | None, extra: dict | None = None) -> dict:
        from serve.frontend import openai_to_messages
        svc = self.svc
        req = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "top_p": 0.95, "seed": 1234,
               **(extra or {})}
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

    ASK = ("Explain, in about 400 words, how a mixture-of-experts language model chooses which experts to run for each "
           "token, and why that makes caching experts in GPU memory effective.")

    def _calibration_record(self) -> dict:
        """Measure the running configuration (an answer, one with reasoning, a long prompt) and save it."""
        svc = self.svc
        values = {**(svc.settings_info or {}).get("running", {})}
        self._set(step="decode")
        off = self._measure(self.ASK, CALIBRATION_ANSWER, "none")
        self._set(step="reasoning")
        on = self._measure(self.ASK, CALIBRATION_THINKING, None)
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
        return record

    def _calibrate(self):
        self._set(phase="calibrating")
        try:
            record = self._calibration_record()
            self._set(phase="ready", finished=time.time(), step=None, message=json.dumps(record))
        except Exception as e:  # noqa: BLE001 - reported on the page and in the log
            self._set(phase="failed", error=f"calibration: {e}", finished=time.time(), step=None)
            print(f"[server] error: calibration failed: {e}", file=sys.stderr, flush=True)

    # ---- the configuration wizard's measurement
    def measure(self, values: dict, wait: bool = False) -> bool:
        """Load `values` (the wizard's computed Loader values) for this server's model and measure them: the real
        memory after the load against the plan (a smaller context, then no resident experts, while it is above), the
        calibration, the drafting floor per request (SPEC_MIN_P_TRIES, no reload) and the verify windows SPEC_TRIES (one
        reload each; the fastest decode is kept, a tie keeps the lower window). The result {"values", "measured" (every
        candidate's speed), "log"} is the job's message; False when a job is running."""
        if not self._begin("measure"):
            return False
        self.thread = threading.Thread(target=self._measure_job, args=(dict(values),), name="measure", daemon=True)
        self.thread.start()
        if wait:
            self.thread.join()
        return True

    def _write_values(self, values: dict) -> None:
        path = (self.svc.settings_info or {}).get("config")
        if not path:
            raise RuntimeError("the server was started without --config: there is no file to load from")
        fields = [f"{s}.{k}" for s, k in settings.LOADER_FIELDS]
        settings.save(path, {f: values.get(f) for f in fields if f != "server.port"}, settings.LOADER_FIELDS)

    def _over_plan(self, values: dict) -> str | None:
        """What the real memory after the load says against the plan: None when within it, else the reason."""
        now = loader.system_now(getattr(self.svc, "gpu_index", 0))
        v = loader.effective(values)
        share = loader.free_share()
        free, reserve = now.get("vram_free"), int(v["engine.vram-reserve-mib"]) * loader.MiB
        if free is not None and v["engine.expert-cache"] in ("auto", 0, "0", None) and free < reserve // 2:
            return f"VRAM free after the load {free // loader.MiB} MiB, the reserve is {reserve // loader.MiB} MiB"
        limit, used = now.get("commit_limit"), now.get("commit_used")
        if limit and used is not None and used > limit - loader.keep_bytes(limit, share):
            return f"commit in use {used / loader.GiB:.1f} GiB of {limit / loader.GiB:.1f} GiB"
        total, avail = now.get("ram_total"), now.get("ram_available")
        if total and avail is not None and avail < loader.keep_bytes(total, share):
            return f"RAM available {avail / loader.GiB:.1f} GiB"
        return None

    def _decode(self, extra: dict | None = None) -> float:
        return float(self._measure(self.ASK, MEASURE_ANSWER, "none", extra)["decode"] or 0)

    def _measure_job(self, values: dict):
        log = []

        def say(text):
            log.append(text)
            print(f"[server] wizard: {text}", flush=True)
        self._set(phase="measuring", step="check")
        try:
            now = loader.system_now(getattr(self.svc, "gpu_index", 0))
            limit, used = now.get("commit_limit"), now.get("commit_used")
            if limit and used is not None and used / limit >= loader.MEASURE_COMMIT_MAX:
                raise RuntimeError(f"the commit in use is {used / limit:.0%} of the limit: the measurement starts only "
                                   f"below {loader.MEASURE_COMMIT_MAX:.0%} (close other programs)")
            current = dict(values)
            for attempt in range(3):
                self._set(step="load")
                self._write_values(current)
                self._reload_now(phase="measuring")
                self._set(phase="measuring")
                over = self._over_plan(current)
                if not over:
                    say(f"loaded with context {current.get('engine.max-context')}: the memory is within the plan")
                    break
                smaller = loader.shrink(current)
                say(f"above the plan ({over}); trying {'nothing smaller' if smaller is None else 'smaller values'}")
                if smaller is None or attempt == 2:
                    raise RuntimeError(f"the real memory stays above the plan: {over}")
                current = smaller
            self._set(step="calibrate")
            record = self._calibration_record()
            self._set(phase="measuring")
            measured = {"decode": record["decode_off"], "decode_reasoning": record["decode_on"],
                        "prefill": record["prefill"], "hit_rate": record.get("hit_rate"), "accept": record.get("accept")}
            v = loader.effective(current)
            if self.facts.get("has_mtp") and int(v["engine.spec"] or 0) >= 2:
                self._set(step="spec_min_p")
                tries = {p: self._decode({"spec_min_p": p}) for p in SPEC_MIN_P_TRIES}
                best_p = max(tries, key=tries.get)
                say("drafting floor: " + ", ".join(f"{p} -> {tries[p]:.1f} tok/s" for p in SPEC_MIN_P_TRIES))
                current["engine.spec-min-p"] = best_p
                windows = {}
                for spec in SPEC_TRIES:                  # set only at the engine's start: one reload each
                    self._set(step="spec")
                    self._write_values({**current, "engine.spec": spec})
                    self._reload_now(phase="measuring")
                    self._set(phase="measuring")
                    windows[spec] = round(self._decode({"spec_min_p": best_p}), 1)
                best = max(windows, key=lambda s: (windows[s], -s))         # the fastest; a tie keeps the lower
                say("verify window: " + ", ".join(f"{s} -> {windows[s]:.1f} tok/s" for s in SPEC_TRIES)
                    + f"; kept {best}")
                current["engine.spec"] = best
                if best != SPEC_TRIES[-1]:
                    self._write_values(current)
                    self._reload_now(phase="measuring")
                    self._set(phase="measuring")
                measured.update(decode=windows[best], spec_min_p=tries, spec=windows)
            result = {"values": current, "measured": measured, "log": log}
            self._set(phase="ready", finished=time.time(), step=None, message=json.dumps(result))
            print(f"[server] wizard: measurement done: {json.dumps(measured)}", flush=True)
        except Exception as e:  # noqa: BLE001 - reported on the page and in the log
            self._set(phase="failed", error=f"measurement: {e}", finished=time.time(), step=None,
                      message=json.dumps({"log": log}))
            print(f"[server] error: the wizard's measurement failed: {e}", file=sys.stderr, flush=True)
