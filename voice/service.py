"""The voice service: a process that starts in standby and serves the control routes of callbridge on loopback.

    python -m voice.service --settings <voice.json> --host 127.0.0.1 --port P --token-file F --info-file I [--default-models-dir D] [--no-gateway]

It starts with no model loaded and no hardware touched, except that an enabled STT with its model installed loads at once (dictation and the
clipboard do not wait for the VRAM plan); the other functions load on `POST /functions/apply`. Settings come from TaDaMoo's `voice.json` (voice/settings_bridge.py) and are read
again when the file changes. Every route but /health needs `Authorization: Bearer <the token in F>`. Models are read only from the
models folder and never downloaded (voice/callbridge/models_store.py). When the server listens it prints one JSON line on stdout,
`{"ready": true, "port": P, "pid": N}`, and writes `{host, port, pid, started, gateway_port}` to I; I is removed again on a clean stop.
The phone's voice gateway (voice/gateway.py) starts with the service, on loopback, on a port the service chooses."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import voice  # noqa: F401  (puts voice/ on sys.path so that `callbridge` imports by that name)

from callbridge import models_store  # noqa: E402
from callbridge.config import Config  # noqa: E402

from callbridge.brains import make_brain  # noqa: E402

from voice import avatar_routes, brain_service, functions, gateway, settings_bridge, speech_routes  # noqa: E402
from voice.avatar_service import AvatarService  # noqa: E402
from voice.meetings_service import MeetingsService  # noqa: E402

log = logging.getLogger("voice.service")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
PHONE_TOKEN_ENV = "TADAMOO_PHONE_TOKEN"


class StartError(Exception):
    """The service cannot start; the message is for the person who started it."""


def read_token(path) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise StartError(f"cannot read the token file {path}: {e.strerror or e}") from None
    if not token:
        raise StartError(f"the token file {path} is empty")
    return token


def write_json_atomic(path, data: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, target)


class State:
    """The settings the service runs with, re-read when `voice.json` changes, and the `Config` they are applied to."""

    def __init__(self, settings_path, config: Config, default_models_dir: str = ""):
        self.path = Path(settings_path)
        self.config = config
        self.default_models_dir = default_models_dir
        self.started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self.settings = settings_bridge.load(self.path, default_models_dir)
        self.brains = make_brain(config)
        self.functions = None
        self.gateway = None
        self.avatar = None                              # the AvatarService, set by build_app
        self.calls = None                               # the CallService, set by build_app
        self.meetings = None                            # the MeetingsService (meetings and copilot), set by build_app
        self.session = brain_service.Session(self)
        self.refresh(force=True)

    def _file_stamp(self) -> tuple | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def refresh(self, force: bool = False) -> None:
        """Read the settings again when the file changed (or appeared, or went), and apply them to the `Config`."""
        stamp = self._file_stamp()
        with self._lock:
            if not force and stamp == self._stamp:
                return
            self._stamp = stamp
            self.settings = settings_bridge.load(self.path, self.default_models_dir)
            before = settings_bridge.tts_snapshot(self.config)
            settings_bridge.apply(self.config, self.settings)
            after = settings_bridge.tts_snapshot(self.config)
            engines = sys.modules.get("callbridge.engines")
            if before != after and engines is not None:
                # A TTS router that already exists takes the new choice at once; nothing is loaded.
                engines.aggiorna_tts(self.config, *settings_bridge.tts_changes(before, after))
        self.session.sync_hotkeys(self.settings.brain.hotkeys)
        if self.calls is not None:
            threading.Thread(target=self.calls.sync, name="voice-telephony", daemon=True).start()    # opening the modem can take seconds

    def status(self) -> dict:
        """Per service: the model chosen, its placement, whether it is installed and, when not, why. Nothing is loaded here."""
        self.refresh()
        root = self.settings.models_dir
        services = {}
        for name in models_store.SERVICES:
            c = self.settings.services[name]
            installed = models_store.is_installed(c.model, root)
            entry = {"model": c.model, "name": models_store.MODELS[c.model][2] if c.model in models_store.MODELS else "",
                     "engine": c.engine, "device": c.device, "memory": c.memory, "installed": installed}
            if not installed:
                entry["reason"] = models_store.reason(c.model, root) if c.model else "this service has no model in the catalog yet"
                if name == "vad":
                    entry["degraded"] = True
                    entry["degraded_reason"] = entry["reason"] + " Speech detection uses the energy detector instead."
            services[name] = entry
        return {"service": "voice", "state": "standby", "pid": os.getpid(), "started": self.started, "models_dir": root,
                "offline": os.environ.get("HF_HUB_OFFLINE") == "1", "services": services,
                "telephony": self.calls.summary() if self.calls is not None else None,
                "gateway": self.gateway.info() if self.gateway is not None else None}


def build_app(settings_path, token: str, default_models_dir: str = "", host: str = "127.0.0.1", port: int = 0, with_gateway: bool = True):
    """The Flask app of the control server (callbridge) with the standby status, and its `State`. No model is loaded, no hardware opened;
    the gateway object exists but listens only when `serve` starts it."""
    from callbridge.control_server import create_app
    models_store.apply_offline()
    config = Config()
    config.control_host, config.control_port, config.control_auth_token = host, port, token
    config.tadamoo_token = token                        # the same token, sent to TaDaMoo's server (it accepts it on loopback for the brains' routes)
    config.stt_backend = config.tts_backend = "native"  # the engines of this process, never a separate server
    config.auth_token = os.environ.get(PHONE_TOKEN_ENV, "")      # the pairing token of the Android dialer, put there by the desktop app from the credential store
    config.analysis_artifacts_dir = str(Path(settings_path).resolve().parent / "voice" / "analyses")     # the analyses live in the user data folder
    config.meetings_dir = str(Path(settings_path).resolve().parent / "voice" / "meetings")              # so do the meetings the person let the bot keep
    state = State(settings_path, config, default_models_dir)
    app = create_app(config, status_fn=state.status, ptt=state.session)
    app.before_request(lambda: state.refresh())
    state.calls = app.config["CB_CALLS"]
    state.functions = functions.Functions(state, app=app)
    functions.register(app, state.functions)
    state.avatar = AvatarService(state, app)
    avatar_routes.register(app, state.avatar)
    state.meetings = MeetingsService(state)
    app.config["CB_MEETINGS"] = state.meetings
    app.config["CB_COPILOT"] = state.meetings.copilot
    brain_service.register(app, state)
    if with_gateway:
        state.gateway = gateway.Gateway(gateway.Host(state, token))
        gateway.register(app, state.gateway)
    app.register_blueprint(speech_routes.bp)
    return app, state


def _install_stop(stop: threading.Event, done: threading.Event) -> None:
    """SIGINT and SIGTERM stop the service; on Windows so do Ctrl+Break and the closing of the console (the handler waits for the clean-up)."""
    def handler(signum, frame):
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        def on_console(kind):
            if kind in (2, 5, 6):                       # close, logoff, shutdown
                stop.set()
                done.wait(5)
                return True
            return False

        _install_stop.keep = on_console                 # the callback must outlive this function
        ctypes.windll.kernel32.SetConsoleCtrlHandler(on_console, True)


def serve(args) -> int:
    host = args.host
    if host not in LOOPBACK:
        raise StartError(f"the service listens on loopback only, not on {host}")
    token = read_token(args.token_file)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app, state = build_app(args.settings, token, args.default_models_dir, host, args.port, with_gateway=not args.no_gateway)
    from werkzeug.serving import make_server
    try:
        server = make_server(host, args.port, app, threaded=True)
    except OSError as e:
        raise StartError(f"cannot listen on {host}:{args.port}: {e.strerror or e}") from None
    port = server.server_port
    state.config.control_port = port                    # the gateway reaches the avatar worker through the control routes
    gateway_port = 0
    if state.gateway is not None:
        try:
            gateway_port = state.gateway.start()
        except OSError as e:
            server.server_close()
            raise StartError(f"cannot start the voice gateway on {host}: {e.strerror or e}") from None
        state.config.gateway_port = gateway_port
    stop, done = threading.Event(), threading.Event()
    _install_stop(stop, done)
    thread = threading.Thread(target=server.serve_forever, name="voice-http", daemon=True)
    thread.start()
    write_json_atomic(args.info_file, {"host": host, "port": port, "pid": os.getpid(), "started": state.started, "gateway_port": gateway_port})
    sys.stdout.write(json.dumps({"ready": True, "port": port, "pid": os.getpid()}) + "\n")
    sys.stdout.flush()
    log.info("standby on %s:%d (models folder %s)", host, port, state.settings.models_dir)
    threading.Thread(target=state.functions.autostart, name="voice-autostart", daemon=True).start()
    threading.Thread(target=state.calls.sync, name="voice-telephony", daemon=True).start()
    try:
        while not stop.wait(0.5):
            pass
    finally:
        if state.gateway is not None:
            state.gateway.stop()
        if app.config.get("CB_AVATAR") is not None:
            app.config["CB_AVATAR"].stop()              # the worker is a child of this process: it ends with it
        server.shutdown()
        thread.join(5)
        server.server_close()
        _remove_info(args.info_file)
        done.set()
        log.info("stopped")
    return 0


def _remove_info(path) -> None:
    """Remove the info file, unless another service instance has already written its own."""
    try:
        if json.loads(Path(path).read_text(encoding="utf-8")).get("pid") == os.getpid():
            os.unlink(path)
    except (OSError, ValueError, AttributeError):
        pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m voice.service", description="TaDaMoo voice service (standby)")
    p.add_argument("--settings", required=True, help="path of TaDaMoo's voice.json")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True, help="0 picks a free port")
    p.add_argument("--token-file", required=True)
    p.add_argument("--info-file", required=True)
    p.add_argument("--default-models-dir", default="", help="the models folder when voice.json names none")
    p.add_argument("--no-gateway", action="store_true", help="do not start the phone gateway")
    args = p.parse_args(argv)
    try:
        return serve(args)
    except StartError as e:
        sys.stderr.write(f"voice service: {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
