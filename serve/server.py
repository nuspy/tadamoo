"""OpenAI, Anthropic and Responses endpoints over an engine that maps token ids to tokens.

    basalt serve --model FILE.basalt [--alias NAME] [--host H] [--port P] [--gpu 0,1] [--api-key KEY] ...
    python -m serve.server --model FILE.basalt --engine-bin bin/basalt ...
    python -m serve.server --config config.json
    python -m serve.server --mock-engine --port 7232

The engine boundary is `Engine.generate(prompt_ids, max_new, sampling, cancel)` -> token ids. ProcessEngine keeps
one `basalt --serve` process resident; MockEngine is a scripted stand-in for tests. Prompts that do not fit the
context are rejected with a 400, never truncated.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # also runnable as a script
from serve import ROOT, lengthprobe, loader, loader_api, model_list, service, settings, supervisor, voice_loader  # noqa: E402
from serve.config import (RELAUNCH_EXIT_CODE, build_parser, check_engine_flags, engine_flags_of,  # noqa: E402
                          engine_line, grammar_words, sampling_defaults_from_config, sampling_line,
                          single_settings, slot_share)
from serve.control import write_control_token  # noqa: E402
from serve.engine import process as engine_process  # noqa: E402
from serve.engine.errors import EngineStartError  # noqa: E402
from serve.engine.flags import flag_index, flag_value  # noqa: E402
from serve.engine.mock import MockEngine, NoEngine  # noqa: E402
from serve.engine.process import ProcessEngine  # noqa: E402
from serve.engine.vision import (EngineVision, Vision, check_gpu, child_env, engine_args, gpu_card_name, model_plan,  # noqa: E402
                                 gpu_text, gpus_in_play, vision_card, vision_env, vision_line)
from serve.handler import Server, serve, stop_sessions  # noqa: E402
from serve.origin_guard import ANY_ORIGIN  # noqa: E402
from serve import server_key, userdata  # noqa: E402
from serve.dialects import dialect_for  # noqa: E402
from serve.engine import modelfile  # noqa: E402
from serve.security import LOOPBACK_NAMES, api_key_from_file, api_key_of, exposed_without_key, lan_addresses  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.engine.progress import load_progress  # noqa: E402
from serve.util.logbuf import ColorStream, LogBuffer, TeeStream, tail_file  # noqa: E402
from serve.util.loglevel import ERR, WARN, LevelReader  # noqa: E402
from serve.util.reqlog import RequestLog  # noqa: E402
from serve.util.console import LEGEND, tagged  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402


def fail(msg: str) -> None:
    """Print a startup error and exit with status 2."""
    print(f"[server] error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def warn_tight_ram(arena_mib) -> None:
    """Warn at start when less than ~6 GB of RAM is left beside the expert arena (INFO arena_mib)."""
    if not isinstance(arena_mib, int) or arena_mib <= 0:
        return
    try:
        import psutil
        total = psutil.virtual_memory().total
    except Exception:  # noqa: BLE001 - psutil is optional here
        return
    left = total / 2**30 - arena_mib / 1024
    if left < 6:
        print(tagged("warning", f"low RAM: {left:.1f} of {total / 2**30:.0f} GB left after experts"), flush=True)


def grammar_entry(on: bool, cli: list[str], used: list[str]) -> tuple[bool, str] | None:
    """(on, origin) for the startup line when the command line or the config file set grammar; None for the default."""
    names = ("--grammar", "--no-grammar")
    origin = "cli" if any(w in names for w in cli) else "config" if any(w in names for w in used) else ""
    return (on, origin) if origin else None


def open_when_ready(svc, url, now=False, opener=None):
    """Open the page once, after the first weights load; at once when no load is under way (mock, lazy)."""
    if opener is None:
        import webbrowser
        opener = webbrowser.open
    if now:
        opener(url)
        return
    done, previous = [], svc.on_loaded

    def loaded():
        if previous is not None:
            previous()
        if not done:
            done.append(1)
            opener(url)
    svc.on_loaded = loaded


def drop_sidecar_vision(cfg: dict) -> None:
    """Only a sidecar encoder holds VRAM before the first request; `dynamic` encodes inside the engine, which the
    request loads first anyway."""
    if not cfg.get("vision") or cfg["vision"].get("mode") == "dynamic":
        return
    print(tagged("warning", "lazy loading drops a sidecar image encoder, vision off; --vision dynamic stays on"), flush=True)
    del cfg["vision"]


def engine_echo(engine):
    """The tail callback that repeats the engine log's warnings and errors (and the indented lines of an error's
    report) in the server window once the engine is ready; what it wrote while loading is the load bars' business."""
    level = LevelReader()
    in_err_block = False

    def echo_engine_line(text: str, end_pos: int) -> None:
        nonlocal in_err_block
        lvl = level.read(text)
        if lvl == ERR:
            in_err_block = True
            echo = True
        elif in_err_block and text.startswith(("  ", "\t")) and text.strip():
            echo = True
        else:
            in_err_block = False
            echo = lvl == WARN
        if echo and getattr(engine, "known_ctx", 0) > 0 and not engine.loading and end_pos > getattr(engine, "log_ready_pos", 0):
            out = getattr(sys.stdout, "_stream", sys.stdout)
            out.write(text.rstrip() + "\n")
            out.flush()

    return echo_engine_line


def settings_info(a, words: list[str]) -> dict:
    """What the settings page shows (supervisor.settings_info over this process's command line)."""
    return supervisor.settings_info(a, words, sys.argv[1:])


def main() -> int:
    ap = build_parser()
    try:
        a, used_argv, file_engine, cli_engine = single_settings(ap, sys.argv[1:])
    except ValueError as e:
        fail(f"--config: {e}")
    service.DEBUG, engine_process.REQUEST_LINES = a.debug, a.request_lines
    logs = LogBuffer()                                  # this process's and the engine's output
    sys.stdout, sys.stderr = (TeeStream(ColorStream.wrap(sys.stdout), logs, "srv"),
                              TeeStream(ColorStream.wrap(sys.stderr), logs, "srv"))
    print(tagged("legend", LEGEND), flush=True)          # first line, so the key precedes every value
    cfg = {}                                            # what the model file implies
    listen_first = False
    loaded_memory = None                                # (system before, after) the engine loaded at start
    has_grammar = False                                 # the engine takes the grammar key (set when one starts)
    try:
        check_gpu(a.gpu)
    except ValueError:
        fail(f"--gpu {gpu_text(a.gpu)}: expected numbers like 0 or 0,1, or auto")
    standby = bool(a.standby)                           # HTTP and the Loader now, the engine on the Loader's Load
    if not a.mock_engine and not (a.model and a.engine_bin) and not standby:
        fail("the server needs --model and the engine binary: run it as basalt serve --config FILE (or --mock-engine)")
    if (file_engine or cli_engine) and not a.model and not standby:     # a standby server keeps them for the Loader's Load
        fail("engine flags need --model")
    words: list[str] = []                               # the engine words in force (config file, then command line)
    if a.model:
        if not a.engine_bin:
            fail("--model needs --engine-bin BINARY")
        try:
            known = engine_flags_of(a.engine_bin)
            check_engine_flags(file_engine, known, f"--config {a.config}")
            check_engine_flags(cli_engine, known, "command line")
        except ValueError as e:
            fail(str(e))
        words = file_engine + cli_engine
        mode = a.vision
        vision_arg = False if a.mock_engine or mode == "off" else mode
        if vision_arg == "dynamic" and a.vision_hold == "release" and known is not None and "--vision-release" not in known:
            fail("--vision-hold release: this engine binary does not know --vision-release; build the engine again")
        try:
            projector = loader.projector_path(a.vision_projector, a.model) if vision_arg else None
            spec = modelfile.ModelSpec(a.model, alias=a.alias, gpu=a.gpu, vision=vision_arg,
                                       engine_words=tuple(file_engine + cli_engine), projector=projector,
                                       hold=a.vision_hold)
            cfg.update({k: v for k, v in modelfile.model_config(spec, a.engine_bin).items() if k not in cfg})
        except (OSError, ValueError, KeyError) as e:
            fail(f"{a.model}: {e}")
        if not a.mock_engine:
            # the engine's own choice, printed once here; the engine then sees only these cards, main first
            try:
                plan = model_plan(a.engine_bin, a.model, a.gpu, list(spec.engine_words))
            except ValueError as e:
                fail(f"{a.model}: {e}")
            card = vision_card(mode)
            if card is not None:                       # a numbered sidecar card: the engine's sm_120 rule is the
                if gpu_card_name(card) is None:        # engine's; the sidecar is its own build and says whether
                    fail(f"vision: GPU {card} is not found by nvidia-smi")   # the sidecar checks the card itself
            for note in plan.get("notes") or []:
                print(f"[server] {note}", flush=True)
            if plan.get("warning"):
                print(f"[server] warning: {plan['warning']}", flush=True)
            cfg["gpu"] = plan["devices"]
            cfg["spec"] = plan.get("spec")
            if getattr(a, "split_default", False) and plan.get("max_context"):
                # the engine's default context, shared by the slots
                a.shared_context = int(plan["max_context"])
                part = ["--max-context", str(slot_share(a.shared_context, a.parallel or 1))]
                cfg["args"] += part
                cli_engine += part
            cfg["gpu_smi"] = [c.get("smi", -1) for c in plan.get("cards") or []]
            cfg["args"] += ["--gpu", ",".join(str(i) for i in range(len(plan.get("cards") or [])))]
        if not a.mock_engine:
            print(engine_line(file_engine, cli_engine,
                                    (a.shared_context, a.parallel) if getattr(a, "shared_context", None) else None,
                                    grammar_entry(a.grammar, sys.argv[1:], used_argv)), flush=True)
    if a.alias:
        cfg["model_name"] = a.alias
    try:                                                # fail fast if the port is taken
        Server((a.host, a.port), BaseHTTPRequestHandler).server_close()
    except OSError:
        fail(f"port {a.port} is in use (--port N)")
    # the tokenizer files, by name: the model file's own sections, else the folder --tokenizer names
    files = cfg.get("tokenizer")
    if not isinstance(files, dict):
        tpath = Path(a.tokenizer)
        files = {n: (tpath / n).read_bytes() for n in modelfile.TOKENIZER_SECTIONS if (tpath / n).exists()}
    tok = ByteTokenizer()
    if not a.mock_engine and a.model and "vocab.json" not in files:
        fail(f"tokenizer missing: {a.tokenizer}/vocab.json")
    if "vocab.json" in files:
        import tokenizer as ST
        vocab = json.loads(files["vocab.json"].decode("utf-8"))
        tokens = [None] * len(vocab)
        for t, i in vocab.items():
            tokens[i] = t
        merges = files["merges.txt"].decode("utf-8").split("\n")
        types = json.loads(files["token_type.json"])
        pre = json.loads(files["tokenizer.json"].decode("utf-8")).get("pre", "qwen35") if "tokenizer.json" in files else "qwen35"
        tok = ST.Tokenizer(tokens, merges, types, pre=pre)
    if not a.mock_engine and not a.model:               # --standby without a model: the Loader chooses one
        engine, vision, sampling_defaults = NoEngine(), None, {}
        cfg.setdefault("model_name", "no model loaded")
        print("[server] standby without a model: choose one in the Loader", flush=True)
    elif not a.mock_engine:
        vision = None
        env = child_env(cfg, loader.free_share_chosen())
        sampling_defaults = {**sampling_defaults_from_config(cfg), **a.sampling_launch}
        if sampling_defaults:
            print(sampling_line(sampling_defaults, a.sampling_cli, set(a.sampling_launch) - a.sampling_cli), flush=True)
        lazy = a.lazy or standby
        if standby:                                     # the Loader's Load starts the engine and the encoder
            cfg = {**cfg, "vision": None}
            print("[server] standby: the engine starts when the Loader loads the model", flush=True)
        if lazy and not standby:
            drop_sidecar_vision(cfg)
        if cfg.get("vision"):
            mode = cfg["vision"].get("mode")
            if mode == "dynamic":
                vision = EngineVision(cfg["vision"])
            else:
                vision = Vision(cfg["vision"], log=open(cfg["log"], "a", encoding="utf-8") if cfg.get("log") else None,
                                env=vision_env(env, mode))
        print(vision_line(cfg.get("vision"), cfg, vision.ready_s if vision else 0.0), flush=True)
        if lazy and not standby:
            print(tagged("model", "unloaded, the first request loads it"), flush=True)
        echo: dict = {}                                 # the engine's warnings and errors in this window, once the engine exists
        if cfg.get("log"):                              # follow the engine's log file
            threading.Thread(target=tail_file, args=(logs, cfg["log"], "eng"),
                             kwargs={"on_line": lambda text, end: echo["line"](text, end) if "line" in echo else None}, daemon=True).start()
        else:
            ProcessEngine.stderr_sink = lambda line: logs.add("eng", line)
        listen_first = bool(a.listen_first) and not lazy   # the engine starts after the HTTP server, on a thread
        known_flags = engine_flags_of(cfg["engine_bin"])
        has_grammar = known_flags is not None and "--grammar" in known_flags
        if a.grammar and not has_grammar:
            print(tagged("warning", "grammar is on but this engine has no --grammar; requests go without one"), flush=True)
        gwords = grammar_words(known_flags)
        if not (lazy or listen_first):                  # the engine starts now: the voice functions that load at the Load go in first
            try:
                voice_loader.ensure_first(voice_loader.Booting(settings_info(a, words), loader.model_facts(a.model)))
            except (OSError, ValueError, KeyError) as e:
                print(f"[server] warning: the voice functions were not loaded before the model ({e})", flush=True)
        try:
            started_from = loader.system_now()            # RAM and commit before the engine starts
            engine = ProcessEngine(cfg["engine_bin"], engine_args(cfg) + gwords, log=cfg.get("log"), env=env,
                                   lazy=lazy or listen_first)
            echo["line"] = engine_echo(engine)
            if not (lazy or listen_first):              # loaded now: what it took, against the plan, once facts are read
                loaded_memory = (started_from, loader.system_now(0))
        except EngineStartError as e:
            print(f"[server] error: {e}", file=sys.stderr, flush=True)
            if e.log:
                print(f"[server] engine log: {e.log}", file=sys.stderr, flush=True)
            return 1
        if isinstance(vision, EngineVision):
            vision.attach(engine)
        if not listen_first:
            warn_tight_ram(engine.info.get("arena_mib"))
    else:
        engine, vision, sampling_defaults = MockEngine(tok, a.script or [
            "Thinking about it.</think>\n\nHello from the mock engine."], batch=a.parallel or 0,
            max_context=int(flag_value(words, "--max-context") or 32768)), None, dict(a.sampling_launch)
    # the model's exported template, else the bundled one
    dialect = dialect_for(cfg.get("arch"))
    template = files["chat_template.jinja"].decode("utf-8") if "chat_template.jinja" in files else \
        (ROOT / "serve/chat_template.jinja").read_text(encoding="utf-8")
    svc = Service(engine, tok, dialect.template(template),
                  model_name=cfg.get("model_name", "qwen3.8-flash-next"), vision=vision,
                  sampling_defaults=sampling_defaults, fit_max_tokens=a.fit_max_tokens, grammar=a.grammar,
                  grammar_engine=bool(a.mock_engine or has_grammar), dialect=dialect,
                  video_max_tokens=a.video_max_tokens)
    svc.logs = logs
    if not a.request_log_mib > 0 or a.request_log_mib == float("inf"):
        fail(f"--request-log-mib {a.request_log_mib:g}: expected a number of MiB above 0")
    svc.reqlog = RequestLog(max_chars=int(a.request_log_mib * (1 << 20)), enabled=a.request_log != "off")
    state = f"on, up to {a.request_log_mib:g} MiB in memory" if svc.reqlog.enabled else "off"
    print(f"[server] request log: {state} (--request-log on|off, --request-log-mib N)", flush=True)
    svc.settings_info = settings_info(a, words)
    svc.standby = standby
    svc.relaunch_on_model_change = bool(a.relaunch_on_model_change)
    try:
        facts = loader.model_facts(a.model) if a.model else dict(loader.DEMO_FACTS)
    except (OSError, ValueError, KeyError) as e:
        print(f"[server] warning: the Loader cannot read {a.model} ({e}); it uses the demo sizes", flush=True)
        facts = dict(loader.DEMO_FACTS)
    if a.mock_engine:
        os.environ["TADAMOO_FAKE_SYSTEM_OK"] = "1"        # TADAMOO_FAKE_SYSTEM: demo memory readings for the mock
    svc.supervisor = supervisor.Supervisor(svc, sys.argv[1:], mock=bool(a.mock_engine), facts=facts)
    svc.model_catalog = model_list.Catalog()            # GET /v1/models lists the folders' other models too
    svc.model_catalog.items()                           # the first scan starts now, on a thread
    if loaded_memory:
        supervisor.record_memory(facts, svc.settings_info.get("running") or {}, *loaded_memory)
    if any(x == "--api-key" and i + 1 < len(used_argv) and not used_argv[i + 1].strip() or x.strip() == "--api-key="
           for i, x in enumerate(used_argv)):
        # an empty key would silently disable authentication
        print("[server] error: --api-key is empty (give a key, or leave it out)", file=sys.stderr)
        return 2
    if a.max_payload_mib < 1:
        fail(f"--max-payload-mib {a.max_payload_mib} must be at least 1")
    svc.max_payload = a.max_payload_mib << 20
    if a.api_key_file:
        if a.api_key:
            print("[server] error: give --api-key or --api-key-file, not both", file=sys.stderr)
            return 2
        try:
            a.api_key = api_key_from_file(a.api_key_file)
        except ValueError as e:
            print(f"[server] error: {e}", file=sys.stderr)
            return 2
    try:
        svc.api_key = api_key_of(a.api_key)
    except ValueError as e:
        print(f"[server] error: {e}: give a key, or leave --api-key out", file=sys.stderr)
        return 2
    kept = server_key.KeyStore(userdata.path("server_key.json")).stored()
    if kept:                                            # a key changed from a client wins over --api-key
        svc.api_key = kept
        print("[server] the API key in force is the one kept in the data folder (changed from a client)", flush=True)
    svc.idle_unload_s = a.idle_unload or 0
    svc.min_free_vram_mib = a.min_free_vram_mib or 0
    svc.apply_limits(a)
    svc.lengths = lengthprobe.Lengths(lambda: svc.supervisor.facts)
    svc.gpu_indices = gpus_in_play(cfg, engine_args(cfg) if cfg.get("args") else [])   # the cards /metrics reads
    svc.gpu_index = svc.gpu_indices[0]
    os.environ["TADAMOO_URL"] = f"http://127.0.0.1:{a.port}"      # the workers and helpers this server starts ask it for VRAM leases here
    svc.control_token = write_control_token(a.port, a.control_secret_value) if a.control_secret else ""
    from tadamoo.llm.usage import UsageLog
    svc.usage_log = UsageLog()
    svc.engine_installed = bool(a.mock_engine or a.engine_bin)
    svc.web = not a.no_web
    svc.remote_admin = bool(getattr(a, "remote_admin", False) and svc.api_key)
    svc.public_name = a.public_name or ""
    svc.allowed_hosts = tuple(dict.fromkeys([*(a.allowed_hosts or ()), *([a.public_name] if a.public_name else [])]))
    svc.allowed_origins = tuple(dict.fromkeys(a.allowed_origins or ()))
    if ANY_ORIGIN in svc.allowed_origins:
        print("[server] allowed-origins is *: pages of any website may use this server", file=sys.stderr, flush=True)
    httpd = serve(svc, host=a.host, port=a.port)
    from serve import billion_context, chatctx, llamacpp_api, voice_settings
    threading.Thread(target=voice_settings.ensure_models_dir, daemon=True, name="voice-models-dir").start()    # the voice service reads the same folder from voice.json
    threading.Thread(target=voice_settings.write_server_address, args=(httpd.server_address[1],), daemon=True, name="voice-server-address").start()    # and finds this server
    threading.Thread(target=billion_context.start, args=(httpd.server_address[1], lambda: chatctx._limit(svc)), daemon=True, name="billion-context-start").start()
    threading.Thread(target=llamacpp_api.start_if_enabled, daemon=True, name="llamacpp-start").start()   # only when the person left it set to start with TaDaMoo
    svc.start_idle_unload()
    here = "127.0.0.1" if a.host in ("0.0.0.0", "", "::") else a.host

    def say_ready():
        print(f"[server] ready: http://{here}:{a.port}/v1 (OpenAI /v1/chat/completions, Anthropic /v1/messages), "
              f"context {engine.max_context} tokens{', images on' if vision else ''}"
              f"{', API key required' if svc.api_key else ''}", flush=True)
        if getattr(engine, "batch", 0):
            side = getattr(engine, "side_slot", None)
            main = engine.batch - (0 if side is None else 1)
            print(f"[server] parallel: {main} requests at once, the rest wait in order; each slot holds "
                  f"{engine.max_context} tokens" + (f" ({main} x {engine.max_context} of KV cache: "
                                                    "--slot-full-context)" if a.slot_full_context else ""), flush=True)
            if side is not None:
                print(f"[server] technical session: slot {side + 1} of {engine.side_context()} tokens for the short calls of "
                      "the harness; end_turn_bias, logit_bias and relaxed drafting are off while it is on", flush=True)
            elif getattr(a, "side_slot", 0):
                print("[server] warning: the technical session was asked for but the engine runs without it "
                      "(see the engine log: its slot did not fit)", flush=True)
        if getattr(a, "side_off_why", None):
            print(f"[server] technical session off: {a.side_off_why}", flush=True)
        if getattr(a, "kv_changed", None):
            print(f"[server] kv       int8 instead of {a.kv_changed} (the technical session needs it; --side-slot 0 keeps {a.kv_changed})", flush=True)
        if getattr(a, "kv_added", False):
            print("[server] kv       int8 (the parallel slots need it)", flush=True)
        if getattr(a, "side_clamp", None):
            print(f"[server] warning: --parallel {a.side_clamp[0]} and the technical session need {a.side_clamp[0] + 1} slots, "
                  f"the engine's most is 8: {a.side_clamp[1]} requests at once", flush=True)

    failed = threading.Event()                          # --listen-first: the model did not load

    started = {"before": None, "done": False}

    def after_load():
        """--listen-first: once the engine has loaded, the memory it took and the ready line (the first load only)."""
        if started["done"]:
            return
        started["done"] = True
        warn_tight_ram(engine.info.get("arena_mib"))
        supervisor.record_memory(svc.supervisor.facts, svc.settings_info.get("running") or {}, started["before"],
                                 loader.system_now(getattr(svc, "gpu_index", 0)))
        say_ready()
        first_calibration()

    def first_calibration():
        """The first load of a model measures its speeds for the Loader's predictions (or the configuration wizard's
        measurement, when the wizard started this server with the model it measures)."""
        if loader_api.pending_measure(svc):
            return
        svc.supervisor.after_load()                     # the model's lengths are read; the first calibration and length are measured

    if listen_first:
        print(f"[server] listening on http://{here}:{a.port}/ while the model loads (the Monitor shows Loading)",
              flush=True)
        started["before"] = loader.system_now(getattr(svc, "gpu_index", 0))
        svc.on_loaded = after_load
        svc.start_load()
    elif standby:
        print(f"[server] standby: http://{here}:{a.port}/ answers; no model is loaded until the Loader's Load",
              flush=True)
    else:
        say_ready()
        if not a.lazy:
            first_calibration()
    if svc.web:
        print(f"[server] web UI: http://{here}:{a.port}/ (chat, monitor, settings; Ctrl+C stops)", flush=True)
    else:
        print("[server] web page off (--no-web): the API only (Ctrl+C stops)", flush=True)
    if a.host not in LOOPBACK_NAMES:
        # reachable from other devices: print the addresses
        ips = lan_addresses()
        for ip in ips:
            print(tagged("network", f"http://{ip}:{a.port}/v1"), flush=True)
        if not ips:
            print(tagged("network", f"port={a.port}"), flush=True)
        if exposed_without_key(a.host, svc.api_key):
            print(tagged("warning", "no api key, open to the network (--api-key KEY)"), flush=True)
    if a.open and svc.web:
        open_when_ready(svc, f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/",
                        now=not listen_first)
    # SIGTERM takes Ctrl+C's path (QUIT to the engine) so a container stop does not orphan it
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except (ValueError, OSError, AttributeError):         # not the main thread
        pass
    try:
        while not failed.is_set() and not svc.exit_request.is_set():
            if svc.boot_error is not None:              # the start-up load failed: the requests waiting on it have the error
                print(f"[server] error: {svc.boot_error}", file=sys.stderr, flush=True)
                if getattr(svc.boot_error, "log", None):
                    print(f"[server] engine log: {svc.boot_error.log}", file=sys.stderr, flush=True)
                failed.set()
                break
            time.sleep(1)                               # a timed wait keeps Ctrl+C deliverable
        if failed.is_set():
            print("[server] stopping: the model did not load", flush=True)
        else:
            print(f"[server] stopping so the program that started me starts me again with the new model "
                  f"(exit code {RELAUNCH_EXIT_CODE})", flush=True)
    except KeyboardInterrupt:
        print("[server] stopping (Ctrl+C again to force)", flush=True)
    engine = svc.engine
    closers = [stop_sessions, (lambda: svc.avatar_on_request.shutdown() if getattr(svc, "avatar_on_request", None) else None), (lambda: svc.voice_lend.shutdown() if getattr(svc, "voice_lend", None) else None), (lambda: svc.vram_broker.shutdown() if getattr(svc, "vram_broker", None) else None), (lambda: svc.heavy_swap.shutdown() if getattr(svc, "heavy_swap", None) else None), svc.stop, (lambda: svc.remote.close() if getattr(svc, "remote", None) else None), httpd.shutdown, getattr(engine, "close", None), svc.vision.close if svc.vision else None]
    for close in filter(None, closers):
        try:
            close()
        except KeyboardInterrupt:                       # second Ctrl+C: do not wait for the engine
            if getattr(engine, "proc", None):
                engine.proc.kill()
    print("[server] stopped", flush=True)
    if failed.is_set():
        return 1
    return RELAUNCH_EXIT_CODE if svc.exit_request.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
