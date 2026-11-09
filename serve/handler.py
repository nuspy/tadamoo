"""The HTTP server and its request handler: routes, authentication, CORS and streaming."""
from __future__ import annotations

import json
import os
import re
import secrets
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from pathlib import Path
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, unquote, urlsplit

from serve.api import responses as responses_api
from serve.api.responses import error_body as responses_error_body
from tadamoo import exec_level, nextsteps, persistence, side_uses
from tadamoo import telegram as tg
from serve import ROOT, benchrun, billion_context, canary_api, catalog, chatctx, checkpoints_api, downloads, engine_host, engine_install, engines, loader, llamacpp_api, loader_api, model_list, modelbench, netwatch_api, models, profiles, rea_api, settings, voice_api, voice_compat, voice_import, voice_loader, voice_service, voice_settings
from serve import voice_gateway_relay
from serve import lang as lang_mod
from serve.config import PARALLEL_MAX, checked_sampling
from serve.engine.vision import VISION_MODES
from serve.api.anthropic import anthropic_collect, anthropic_events
from serve.engine.errors import EngineDied, EngineError, EngineStarting, EngineStuck, GpuBusy, ModelBusy
from serve.api.grammar import REQUEST_KEY as GRAMMAR_KEY
from serve.api.logit_bias import REQUEST_KEY as BIAS_KEY, add_end_turn, checked as checked_bias
from serve.frontend import anthropic_to_messages, forced_call, openai_to_messages
from serve.api.longform import long_answer_chunks, long_collect, long_request
from serve.api.openai import (openai_chunks, openai_collect, retry_messages, structured_retries,
                              structured_retry_chunks)
from serve.api.responses import ResponsesError
from serve.prompt import primed
from serve.security import key_matches
from serve.service import LOG_FIRST_LINES, LOG_MAX_LINES, Service, _debug_req
from serve.api.structured import StructuredOutputError, anthropic_format, check_format, json_validator
from serve.util.redact import LEVELS as MASK_LEVELS, ExportMask, clean_words
from serve.util.reqlog import export_jsonl, export_text
from serve import plans as plans_mod, userdata
from serve import review as review_mod
from serve.agent_runs import AgentRunner, RunBusy, RunError
from tadamoo.browser import BrowserPrefs
from tadamoo.rule_of_two import RuleOfTwoPrefs
from serve.allow_rules import AllowRules
from tadamoo.payments_broker import PaymentsBroker
from tadamoo.waiver.dossier import sntp_offset
from tadamoo.vault import Vault, VaultError
from serve.agent_sessions import TOKEN_HEADER, SessionManager
from serve.target_api import TargetManager
from tadamoo.target import TargetError
from serve.files_api import FilesApi, FilesError
from serve.projects import ProjectStore
from serve.remote import KEY_TTL_DAYS, MODE_ORDER, ApiKeys, Installer, RemoteAccess, tailscale_peers, tailscale_status, tailscale_up
from serve.personal_skills_api import PersonalSkillsApi
from tadamoo.forget import Forgetter
from tadamoo.personal_memory import PersonalMemory, PersonalMemoryError
from serve.lessons_api import LessonsApi, LessonsError
from serve.bench_api import ACTIONS as BENCH_ACTIONS, BenchApi, BenchApiError
from serve.memories_api import MemoriesApi
from serve import export_api, traces_api
from serve.exportdoc import ExportError
from serve.memory_worker import MemoryWorker, gpu_free
from tadamoo.personal_skills.store import SkillError
from serve.routines_api import RoutinesApi
from tadamoo.scheduler import RoutineError
from serve.wallet_api import WalletApi, WalletError
from serve.websearch_api import WebSearchApi, WebSearchError
from serve.telegram_service import TelegramService, TelegramServiceError
from serve.personas_api import PersonasApi
from serve.rag_api import RagApi, upload_limit
from tadamoo.rag import RagError, RagStore
from tadamoo.personas import PersonaError, PersonaStore
from tadamoo.context_patterns import catalog as context_patterns_catalog
from serve.capabilities_api import CapabilityApi, CapabilityError
from serve.intake_prefs import IntakeError, IntakePrefs
from tadamoo.roles import CODING_ROLE, ROLE_IDS
from serve.coding_prefs import CodingError, CodingPrefs
from serve import sessions_move, tab_editor
from serve.file_owners import FileOwners, OwnersError
from tadamoo.file_owners import FILE as FILE_OWNERS_FILE
from serve.compaction_prefs import CompactionError, CompactionPrefs
from serve.gate_prefs import GateError, GatePrefs
from serve.knowledge_prefs import KnowledgeError, KnowledgePrefs, check_wiki, clean_url
from serve.llm_api import LlmApi, LlmError
from serve.mcp_auth_api import McpAuthApi, McpAuthError, result_page
from serve.approvals import ApprovalBoard, ApprovalError
from serve.tasks import ChangeJournal, TaskJournal
_TASKS = TaskJournal(userdata.path("journal"))        # runtime state lives in the user's data folder, not in the assets
_CHANGES = ChangeJournal(userdata.path("journal"))
_APPROVALS = ApprovalBoard()
_RUNNER = None                                        # made on first use: it needs this server's address
_SESSIONS = None                                      # the Chat's conversations with the agent, made on first use
_TARGETS = None                                       # the Target Loop of those conversations, made on first use
_PROJECTS = None                                      # the folders of the Agent tab, made on first use
_INSTALLER = Installer()                              # installs Tailscale when the person asks
_ROUTINES = None                                      # the agent's recurring routines, made with the conversations or on first use
_PSKILLS = None                                       # the person's personal skills, made on first use
_FILE_OWNERS = None                                    # which conversation owns a shared file, made on first use
_PERSONAL = None                                      # the person's own memories (long-term and short-term), made on first use
MEMORY_EXPORT = "memory-export"                        # the folder of the exported memories, in the user data folder
_PAST = None                                          # the index of earlier conversations, made on first use
_MEMORIES = None                                      # the Memories page's side of them
_LESSONS = None                                       # the Lessons page's side
_BENCHMARKS = None                                    # the Benchmarks tab's side, made on first use
GPU_BUSY_UTIL_PCT = 40                                # a GPU this busy while the server is idle belongs to another program
BENCH_GET = ("/api/bench", "/api/bench/preflight", "/api/bench/run", "/api/bench/report", "/api/bench/ranking")
_MEM_WORKER = None                                    # learns from what the person wrote when the GPU is free
_CARDS = None                                         # writes the card of each conversation when it has been quiet, and of the old ones when nothing else works
_EXPORT = export_api.ExportApi()                      # turns a conversation into a file; holds a PDF page for the desktop app's print
_CAPS = None                                          # the Settings page's side of capabilities, made on first use
_LLM = None                                           # the Providers page's side of llmswitch, made on first use


def _plans_service(svc: Service, port: int):
    """The planner's service for this server, made on first use and pointed at the model this server runs."""
    global _PLANS
    if _PLANS is None:
        _PLANS = plans_mod.PlanService(userdata.path("planner"))
    from tadamoo.client import TadamooClient
    _PLANS.client_factory = lambda: TadamooClient(base_url=f"http://127.0.0.1:{port}", api_key=svc.api_key or None)
    if _LLM is not None:
        _PLANS.registry = _LLM.registry
    return _PLANS


def _start_planner(svc: Service, port: int) -> None:
    """With saved planner settings (or a service already made), the event pump and the sources start now and not at the first request."""
    try:
        if _PLANS is None and not (userdata.path("planner") / "planner.json").is_file():
            return
        _plans_service(svc, port).start()
    except Exception as exc:                                   # the planner never keeps the server from starting
        print(f"planner: not started ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)


def telegram_service(sessions) -> TelegramService:
    """The Telegram bot's service, made on first use; `sessions` gives the conversations it talks to."""
    global _TELEGRAM
    if _TELEGRAM is None:
        _TELEGRAM = TelegramService(tg.Settings(userdata.path("telegram.json")), tg.Token(folder=userdata.user_data_dir()), sessions,
                                    attachments_dir=userdata.path("attachments"), state_dir=userdata.path("telegram"))
    return _TELEGRAM


def _start_telegram(httpd) -> None:
    """With the server: the bot listens when the person turned it on and a token is kept. The conversations are taken from a handler that has no request."""
    handler = httpd.RequestHandlerClass
    bare = handler.__new__(handler)
    bare.server = httpd
    telegram_service(bare._agent_sessions).start_if_enabled()


def stop_sessions() -> None:
    """The server ends: the conversations' processes end with it (the next start finds the conversations again)."""
    if _TELEGRAM is not None:
        _TELEGRAM.stop()
    netwatch_api.stop()
    canary_api.shutdown()
    billion_context.stop()
    if _ROUTINES is not None:
        _ROUTINES.shutdown()
    if _MEM_WORKER is not None:
        _MEM_WORKER.stop()
    if _CARDS is not None:
        _CARDS.stop()
    if _PLANS is not None:
        _PLANS.close()
    if _SESSIONS is not None:
        _SESSIONS.shutdown()
_WALLET = None                                         # the vault and the payment systems, made on first use
_WEBSEARCH = None                                      # Settings > Web search, made on first use
_TELEGRAM = None                                       # the Telegram bot and its settings, made on first use
_PERSONAS = None                                       # Settings > Personalities, made on first use
_RAG = None                                            # the knowledge archives (the Knowledge tab), made on first use
_MCP_AUTH = None                                       # the sign-in of the remote MCP servers, made on first use
_PLANS = None                                          # the planner's service, made on first use
from serve.util.reqtrace import RequestTrace
from serve.util.text import IM_END, stop_strings
from serve.api.cache_slot import check_cache_slot


BODY_MAX = 256 << 20                        # the most a JSON request body may hold (read into memory)
CONTROL_BODY_MAX = 64 << 10                 # the unused body of the control routes
CONTROL_PATHS = ("/api/control/load", "/api/control/unload")   # not part of the interfaces other applications use
CONTROL_HEADER = "X-Tadamoo-Control"
API_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")      # the requests the request log records
REQLOG_CHARS_MAX = 64 << 20                 # the most one GET /api/reqlog answer may hold


class BadBody(Exception):
    """A request body that cannot be read (malformed or oversized): the status and the sentence."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _size_text(n: int) -> str:
    return f"{n >> 20} MiB" if n >= 1 << 20 else f"{n >> 10} KiB"


def make_handler(svc: Service, remote: bool = False):
    """`remote`: the handler of the listener for phones: device keys count only there, and every request needs a key."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"                       # SSE ends by closing the connection
        remote_listener = remote

        _held_body = None                                   # the body of a request read before it was answered here
        watch_done = None                                   # stops this request's disconnect watcher
        trace = None                                        # this request's entries in the request log

        def log_message(self, fmt, *args):
            pass

        def _chunked(self) -> bool:
            """The body is sent as Transfer-Encoding: chunked (it wins over a Content-Length, RFC 9112)."""
            te = self.headers.get("Transfer-Encoding", "") or ""
            return te.split(",")[-1].strip().lower() == "chunked"

        def _read_chunked(self, limit: int, keep: bool = True) -> bytes:
            """Decode a chunked body: at most `limit` bytes (413 beyond it; the size is checked before a chunk is
            read, so a huge announced chunk is never allocated), 400 for a malformed one. keep=False drops it."""
            parts, total = [], 0
            while True:
                line = self.rfile.readline(1025)
                if not line.endswith(b"\n"):
                    raise BadBody(400, "malformed chunked request body")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise BadBody(400, "malformed chunk size in the request body") from None
                if size < 0:
                    raise BadBody(400, "malformed chunk size in the request body")
                if size == 0:
                    for _ in range(64):                       # the trailers, up to the blank line
                        if self.rfile.readline(8193).strip() == b"":
                            break
                    return b"".join(parts)
                total += size
                if total > limit:
                    self.close_connection = True
                    raise BadBody(413, f"the request body is larger than {_size_text(limit)}")
                left = size
                while left > 0:
                    piece = self.rfile.read(min(left, 1 << 20))
                    if not piece:
                        raise BadBody(400, "the chunked request body ended early")
                    left -= len(piece)
                    if keep:
                        parts.append(piece)
                if self.rfile.read(2) != b"\r\n":
                    raise BadBody(400, "malformed chunked request body")

        def _body(self, limit: int | None = None) -> bytes:
            """The request body, chunked or by Content-Length, at most `limit` bytes (default BODY_MAX)."""
            limit = BODY_MAX if limit is None else limit     # read at call time: tests lower BODY_MAX
            if self._chunked():
                return self._read_chunked(limit)
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                raise BadBody(400, "invalid Content-Length") from None
            if length < 0:
                raise BadBody(400, "invalid Content-Length")
            if length > limit:
                self.close_connection = True
                raise BadBody(413, f"the request body is larger than {_size_text(limit)}")
            return self.rfile.read(length)

        def _watch_client(self, cancel: threading.Event) -> None:
            """Cancel the request when its client hangs up, polling every 0.5 s: a readable socket with nothing to
            read is EOF (the request is fully read, so data is not a hang-up)."""
            done = self.watch_done = threading.Event()
            sock = self.connection

            def watch():
                while not done.wait(0.5) and not cancel.is_set():
                    try:
                        readable, _, _ = select.select([sock], [], [], 0)
                        gone = bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
                    except (ConnectionError, TimeoutError):
                        gone = True
                    except (OSError, ValueError):            # socket closed: the request has ended
                        return
                    if gone:
                        cancel.set()
                        return

            threading.Thread(target=watch, daemon=True, name="client-watch").start()

        def _cors(self):
            """CORS headers for /v1/*: any web page may use it."""
            if not self.path.split("?")[0].startswith("/v1/"):
                return
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            # echo the requested headers: "*" does not cover Authorization
            asked = self.headers.get("Access-Control-Request-Headers")
            self.send_header("Access-Control-Allow-Headers",
                             asked or "Authorization, Content-Type, x-api-key, anthropic-version, anthropic-beta")
            self.send_header("Access-Control-Max-Age", "600")

        def do_OPTIONS(self):
            # preflights carry no credentials, so no API key check
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if self.trace is not None and code >= 400:
                self.trace.error_body(code, obj)

        _key_row = None                                      # the device key a phone came with
        _via_key = False                                     # a phone proved itself with a key
        _via_voice = False                                   # the voice service proved itself with its token (voice_service.accepts_token)

        def _outside(self) -> bool:
            """A request that came through the listener for phones."""
            return self.remote_listener

        def _local_or_device(self) -> bool:
            """This PC, or a phone that came through the listener for phones with one of the keys."""
            if not self.remote_listener:
                return settings.is_loopback(self.client_address[0])
            remote = getattr(svc, "remote", None)
            return self._via_key and remote is not None and remote.listening

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("x-api-key", "")
            if given and not self.remote_listener and voice_service.accepts_token(given, self.command, self.path, self.client_address[0]):
                self._via_voice = True
                return True
            if svc.api_key and key_matches(given, svc.api_key):
                self._via_key = self.remote_listener
                return True
            keys = getattr(svc, "keys", None)
            row = keys.verify(given) if self.remote_listener and keys is not None and given else None
            if row is not None:
                self._key_row, self._via_key = row, True
                return True
            if not svc.api_key and not self.remote_listener:      # no key set: the server's own page works as before
                return True
            gone = keys.expired_label(given) if self.remote_listener and keys is not None and given else None
            if gone is not None:
                self._json(401, {"error": {"type": "authentication_error", "code": "key_expired",
                                           "message": f"the key of '{gone}' has expired: renew it in Settings > Phones on the PC"}})
                return False
            self._json(401, {"error": {"type": "authentication_error", "message": "missing or wrong API key"}})
            return False

        def _phone_mode_ok(self, mode) -> bool:
            if mode in MODE_ORDER:
                return True
            self._json(400, {"error": {"type": "invalid_request_error", "message": "the mode is one of: " + ", ".join(MODE_ORDER)}})
            return False

        def _phone_limits(self, mode=None, rules=None, req=None) -> bool:
            """A conversation started or switched from a phone gets the modes and the rules the person fixed on the PC: a mode above the limit
            or rules of the phone's own are refused (the reply says where to change the limit), and the PC's rules take the place of the phone's."""
            if not self.remote_listener:
                return True
            remote = svc.remote
            if remote.phone_chooses:                                          # the person let phones choose: what the phone asks for stands
                return mode is None or self._phone_mode_ok(mode)
            if mode is not None and not remote.mode_allowed(mode):
                self._json(403, {"error": {"type": "forbidden", "code": "phone_mode_limit",
                                           "message": f"a phone can use the modes up to '{remote.max_mode}'; the limit is set in Settings > Phones on the PC"}})
                return False
            if rules:
                self._json(403, {"error": {"type": "forbidden", "code": "phone_rules",
                                           "message": "a phone cannot set permission rules; the rules for phones are set in Settings > Phones on the PC"}})
                return False
            if req is not None:
                req["allow"] = list(remote.rules)
            return True

        def _compat(self) -> bool:
            """callAPIcall's routes (serve/voice_compat.py): True when the request was theirs and has been answered."""
            if not voice_compat.owns(self.path.split("?")[0].rstrip("/")):
                return False
            voice_compat.handle(self, svc)
            return True

        def do_PUT(self):
            if not self._compat():
                self._discard_body()
                self._json(404, {"error": {"message": "not found"}})

        do_DELETE = do_PUT

        def do_GET(self):
            if self._compat():
                return
            if self.remote_listener and not self._authorized():          # every request to the listener for phones needs a key
                return
            path = self.path.split("?")[0].rstrip("/")
            if path.startswith("/fonts/"):
                name = path[len("/fonts/"):]
                f = ROOT / "serve" / "web" / "fonts" / name
                if "/" in name or "\\" in name or not name.endswith(".woff2") or not f.is_file():
                    self._json(404, {"error": {"message": "not found"}})
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/web/"):
                name = path[len("/web/"):]
                types = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                         ".svg": "image/svg+xml", ".png": "image/png"}
                f = ROOT / "serve" / "web" / name
                ext = os.path.splitext(name)[1]
                vendored = name.startswith("vendor/") and name.count("/") == 1 and "\\" not in name and not name.endswith(".txt")      # one folder of third-party scripts
                if (("/" in name or "\\" in name) and not vendored) or ext not in types or not f.is_file():
                    self._json(404, {"error": {"message": "not found"}})
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", types[ext])
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/api/export/held/"):
                self._export_held(path[len("/api/export/held/"):])
                return
            if path == "/metrics":
                if self._authorized():
                    self._json(200, svc.metrics())
            elif path == "/api/logs":
                if self._authorized():
                    self._logs()
            elif path == "/api/reqlog":
                if self._authorized():
                    self._reqlog()
            elif path == "/api/reqlog/export":
                if self._authorized():
                    self._reqlog_export()
            elif path == "/api/settings":
                if self._authorized():
                    self._settings_get()
            elif path == "/api/control-secret":
                if self._authorized():
                    self._control_secret_get()
            elif path == "/api/loader":
                if self._authorized():
                    self._json(200, loader_api.info(svc))
            elif path.startswith("/api/loader/engines/") and path.endswith("/models"):
                if self._authorized():
                    eid = path.split("/")[4]
                    self._json(200 if eid in engines.ENGINES else 404,
                               {"models": engine_host.models(eid), "profiles": profiles.engine_profiles(eid)})
            elif path == "/api/loader/engines":
                if self._authorized():
                    every = parse_qs(urlsplit(self.path).query).get("all", ["0"])[0] in ("1", "true")
                    self._json(200, loader_api.engines_info(every))
            elif path == "/api/wizard":
                if self._authorized():
                    model = parse_qs(urlsplit(self.path).query).get("model", [None])[0]
                    try:
                        self._json(200, loader_api.wizard_info(svc, model or None))
                    except ValueError as e:
                        self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            elif path == "/api/profiles":
                if self._authorized():
                    # the summaries are for ?model= (the Loader's chosen .basalt), else the running model
                    model = parse_qs(urlsplit(self.path).query).get("model", [None])[0]
                    try:
                        facts = loader_api.facts_of(svc, model or None)
                    except ValueError:
                        facts = loader_api.sup_of(svc).facts
                    # planned as the Loader plans a load: the running engine's memory is given back first
                    holds, holds_vram = loader_api.engine_holds(svc)
                    self._json(200, profiles.listing(loader.system_now(getattr(svc, "gpu_index", 0)), facts,
                                                     holds, holds_vram))
            elif path == "/api/profiles/export":
                if self._authorized():
                    body = json.dumps(profiles.export(), indent=2, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Disposition", 'attachment; filename="basalt-profiles.json"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif path == "/api/models":
                if self._authorized():
                    rescan = parse_qs(urlsplit(self.path).query).get("rescan", ["0"])[0] in ("1", "true")
                    self._json(200, self._models_listing(rescan))
            elif path == "/api/models/job":
                if self._authorized():
                    self._json(200, models.JOBS.status())
            elif path == "/api/bench/overview":
                if self._authorized():
                    self._json(200, self._bench_overview())
            elif path == "/api/bench/job":
                if self._authorized():
                    self._json(200, benchrun.JOBS.status())
            elif path == "/api/models/catalog":
                if self._authorized():
                    self._json(200, self._catalog())
            elif path == "/api/models/download":
                if self._authorized():
                    self._json(200, downloads.DOWNLOADS.status())
            elif path == "/api/models/hf":
                if self._authorized():
                    self._hf_status()
            elif path == "/api/loader/status":
                if self._authorized():
                    self._json(200, {**loader_api.sup_of(svc).status(), "loaded": svc.loaded(), "loading": svc.loading,
                                     "standby": bool(getattr(svc, "standby", False)),
                                     "max_context": svc.engine.max_context})
            elif path == "/api/tasks":
                if self._authorized():
                    self._json(200, _TASKS.load())
            elif path == "/api/changes":
                if self._authorized():
                    self._json(200, _CHANGES.view())
            elif path.startswith("/api/files/"):
                if self._authorized():
                    self._fs_get(path)
            elif path == "/api/remote/hello":
                if self._authorized():
                    self._json(200, {"name": "TaDaMoo", "ok": True, "device": (self._key_row or {}).get("label", ""), "loaded": bool(svc.loaded())})
            elif path == "/api/remote/peers":
                if self._authorized():
                    self._json(200, {"peers": tailscale_peers()})
            elif path == "/api/remote":
                if self._authorized():
                    self._remote_get()
            elif path.startswith("/api/agent/"):
                if self._authorized():
                    self._agent_get(path)
            elif path in ("/api/routines", "/api/routines/runs"):
                if self._authorized():
                    self._routines_get(path)
            elif path == "/api/file-owners":
                if self._authorized():
                    self._file_owners_get(path)
            elif path == "/api/sessions-base":
                if self._authorized():
                    self._sessions_base_get()
            elif path == "/api/attention":
                if self._authorized():
                    self._json(200, {"cron": _ROUTINES.attention() if _ROUTINES is not None else {"t": 0.0},
                                     "plans": _PLANS.attention() if _PLANS is not None else {"t": 0.0}})
            elif path == "/api/technical-session":
                if self._authorized():
                    self._json(200, {"on": side_uses.session_on()})
            elif path == "/api/persistence":
                if self._authorized():
                    self._json(200, {"on": persistence.Prefs(userdata.path(persistence.FILE)).read()})
            elif path == "/api/next-steps":
                if self._authorized():
                    self._json(200, {"on": nextsteps.Prefs(userdata.path(nextsteps.FILE)).read()})
            elif path == "/api/personal-skills" or path.startswith("/api/personal-skills/"):
                if self._authorized():
                    self._pskills_get(path)
            elif path in ("/api/capabilities", "/api/capabilities/review", "/api/capabilities/online", "/api/capabilities/requests"):
                if self._authorized():
                    self._capabilities_get(path)
            elif path == "/api/netwatch" or path.startswith("/api/netwatch/"):
                if self._authorized():
                    self._netwatch(path, None)
            elif path == "/api/rea":
                if self._authorized():
                    self._rea(path, False)
            elif path == "/api/llamacpp":
                if self._authorized():
                    self._llamacpp(path, False)
            elif path == "/api/canary" or path.startswith("/api/canary/"):
                if self._authorized():
                    self._canary(path, None)
            elif path == "/canary" or path.startswith("/canary/"):
                self._canary_gui("GET")
            elif path == "/api/billion-context":
                if self._authorized():
                    self._json(200, billion_context.get().view())
            elif path == "/api/side-uses":
                if self._authorized():
                    self._json(200, self._side_uses_view())
            elif path == "/api/knowledge":
                if self._authorized():
                    self._json(200, KnowledgePrefs(userdata.path("knowledge.json")).view())
            elif path == "/api/intake":
                if self._authorized():
                    self._json(200, IntakePrefs(userdata.path("intake.json")).view())
            elif path == "/api/gate":
                if self._authorized():
                    self._json(200, GatePrefs(userdata.path("gate.json")).view())
            elif path == "/api/coding":
                if self._authorized():
                    self._coding_get()
            elif path == "/api/compaction":
                if self._authorized():
                    self._json(200, CompactionPrefs(userdata.path("compaction.json")).view())
            elif path == "/api/memories":
                if self._authorized():
                    if not settings.is_loopback(self.client_address[0]):
                        return self._json(403, {"error": {"type": "forbidden", "message": "the memories are shown only on the PC the server runs on"}})
                    q = parse_qs(urlsplit(self.path).query)
                    self._json(200, self._memories().overview((q.get("q") or [""])[0][:200], (q.get("kind") or [""])[0], (q.get("section") or [""])[0]))
            elif path in BENCH_GET:
                if self._authorized():
                    self._benchmarks_get(path)
            elif path == "/api/lessons":
                if self._authorized():
                    if not settings.is_loopback(self.client_address[0]):
                        return self._json(403, {"error": {"type": "forbidden", "message": "the lessons are shown only on the PC the server runs on"}})
                    try:
                        self._json(200, self._lessons().overview((parse_qs(urlsplit(self.path).query).get("folder") or [""])[0][:1000]))
                    except LessonsError as e:
                        self._json(400, {"error": {"type": "lessons", "message": str(e)}})
            elif path == "/api/wallet":
                if self._authorized():
                    if not settings.is_loopback(self.client_address[0]):
                        return self._json(403, {"error": {"type": "forbidden", "message": "the vault is shown only on the PC the server runs on"}})
                    self._json(200, self._wallet().overview())
            elif path == "/api/telegram":
                if self._authorized():
                    if not settings.is_loopback(self.client_address[0]):
                        return self._json(403, {"error": {"type": "forbidden", "message": "the Telegram settings are shown only on the PC the server runs on"}})
                    self._json(200, self._telegram().overview())
            elif path in ("/api/websearch", "/api/websearch/plan", "/api/websearch/job"):
                if self._authorized():
                    if not settings.is_loopback(self.client_address[0]):
                        return self._json(403, {"error": {"type": "forbidden", "message": "the web search settings are shown only on the PC the server runs on"}})
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        api = self._websearch()
                        self._json(200, api.overview() if path == "/api/websearch" else api.plan((query.get("provider") or [""])[0]) if path == "/api/websearch/plan"
                                   else api.job((query.get("id") or [""])[0]))
                    except WebSearchError as e:
                        self._json(404 if e.code == "not_found" else 400, {"error": {"type": "websearch", "code": e.code, "message": str(e)}})
            elif path == "/api/personas" or path.startswith("/api/personas/"):
                if self._authorized():
                    self._personas_get(path)
            elif path == "/api/rag" or path.startswith("/api/rag/"):
                if self._authorized():
                    self._rag_route(path, "GET")
            elif path in ("/api/voice/gateway", "/api/voice/gateway/ws"):      # the phone's way to the voice gateway (serve/voice_gateway_relay.py)
                if self._authorized():
                    if not self._local_or_device():
                        return self._json(403, {"error": {"type": "forbidden", "message": "the voice gateway is reached from this PC or from a phone with its key, with phones turned on"}})
                    if path.endswith("/ws"):
                        given = self.headers.get("Authorization", "")[7:].strip() or self.headers.get("x-api-key", "")
                        keys = getattr(svc, "keys", None)
                        voice_gateway_relay.relay(self, (lambda: bool(svc.api_key and key_matches(given, svc.api_key)) or (keys is not None and keys.verify(given) is not None)) if self.remote_listener else None)
                    else:
                        self._json(200, voice_gateway_relay.info([{"slug": x["slug"], "name": x["name"]} for x in self._personas().store.list()]))     # names only: a phone may choose one, not read it
            elif path in ("/api/voice/models", "/api/voice/download/plan", "/api/voice/download/status", "/api/voice/service", "/api/voice/functions", "/api/voice/brain", "/api/voice/telephony"):
                if self._authorized():
                    if not settings.is_loopback(self.client_address[0]):
                        return self._json(403, {"error": {"type": "forbidden", "message": "the voice models and service are shown only on the PC the server runs on"}})
                    self._voice_get(path, parse_qs(urlsplit(self.path).query))
            elif path == "/api/mcp/auth":
                if self._authorized():
                    self._json(200, self._mcp_auth().overview())
            elif path == "/api/mcp/auth/callback":
                self._mcp_callback()
            elif path == "/api/context/patterns":
                if self._authorized():
                    self._json(200, context_patterns_catalog())
            elif path in ("/api/attachments", "/api/outputs") or path.startswith(("/api/attachments/", "/api/outputs/")):
                if self._authorized():
                    self._files_get(path)
            elif path == "/api/llm":
                if self._authorized():
                    self._json(200, self._llm().overview())
            elif path == "/api/planner/settings" or path == "/api/plans" or path.startswith("/api/plans/"):
                if self._authorized():
                    self._plans_get(path)
            elif path == "/api/prefs":
                self._json(200, self._prefs())
            elif path == "/api/session":
                # with an API key set the page gets its change secret here, after the key is typed; a page cannot send the key on a navigation
                if not svc.api_key:
                    self._json(404, {"error": {"message": "not found"}})
                elif self._authorized():
                    if not self._page_may_change_things(navigation=False):
                        return self._json(403, {"error": {"type": "forbidden", "message": "the change secret is given only to the page on this PC"}})
                    body = b"{}"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Set-Cookie", self._control_cookie_header())
                    self.end_headers()
                    self.wfile.write(body)
            elif path == "":
                # the page opens in the user's language (serve/lang.py) unless this browser chose another
                code = lang_mod.lang()
                body = (ROOT / "serve" / "web" / "index.html").read_bytes().replace(
                    b'<html lang="en">', f'<html lang="{code}" data-server-lang="{code}">'.encode(), 1)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if self._page_may_change_things(navigation=True):   # the page changes things with this; its own code never reads it
                    self.send_header("Set-Cookie", self._control_cookie_header())
                self.end_headers()
                self.wfile.write(body)
            elif path in ("/health", "/api/health"):
                self._json(200, {"status": "ok", "max_context": svc.engine.max_context, "model": svc.model,
                                 "images": svc.vision is not None, "api_key": bool(svc.api_key),
                                 "loaded": svc.loaded(), "loading": svc.loading, "standby": bool(getattr(svc, "standby", False)),
                                 # a test server (--mock-engine): the scripts and the app never take it for TaDaMoo
                                 "mock": type(svc.engine).__name__ == "MockEngine"
                                 or bool(getattr(getattr(svc, "supervisor", None), "mock", False)),
                                 "service": "tadamoo"})
            elif path == "/status":
                if not self._authorized():                  # it shows the last answer's tail
                    return
                with svc.status_lock:
                    s = dict(svc.status)
                now = time.time()
                if s.get("busy"):
                    s["elapsed_s"] = round(now - s["started"], 1)
                    if s.get("first_token"):
                        s["tokens_per_s"] = round(svc._tok_s(), 1)
                        s["tokens_per_s_mean"] = round(svc._tok_s_mean(), 1)
                for k in ("started", "first_token"):
                    s.pop(k, None)
                self._json(200, s)
            elif engine_host.HOST.active() and (path in engine_host.PROXIED_GET or path.startswith(("/v1/models/", "/models/"))):
                if self._authorized():                       # another engine is loaded: its answer, relayed
                    engine_host.HOST.forward(self, "GET", self.path)
            elif path in ("/v1/models", "/models"):
                if self._authorized():                       # the served model and the folders' others (model_list)
                    local = model_list.is_loopback(self.server.server_address[0])
                    self._json(200, model_list.anthropic_list(svc, local) if model_list.wants_anthropic(self.headers)
                               else model_list.openai_list(svc, local))
            elif path.startswith(("/v1/models/", "/models/")):
                if self._authorized():
                    local = model_list.is_loopback(self.server.server_address[0])
                    anthropic = model_list.wants_anthropic(self.headers)
                    model_id = unquote(path.split("/models/", 1)[1])
                    m = model_list.find(svc, model_id, local)
                    if m is None:
                        self._json(404, model_list.not_found(model_id, anthropic))
                    else:
                        self._json(200, model_list.anthropic_one(m) if anthropic else m)
            elif path == "/props":
                if self._authorized():
                    self._props()
            elif path == "/slots":
                if self._authorized():
                    loaded = not hasattr(svc.engine, "alive") or svc.engine.alive()
                    self._json(200, svc.slot_table()[0] if loaded else [])
            elif path == "/v1/status":
                if self._authorized():
                    self._json(200, svc.v1_status())
            else:
                self._json(404, {"error": {"message": "not found"}})

        def _discard_body(self, limit: int = 4 << 20) -> None:
            """Read and drop a request body that will not be used, so the client can finish sending it and read
            the answer instead of seeing the connection reset (what Windows does to an unread body)."""
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked" or length > limit:
                self.close_connection = True
                return
            while length > 0:
                chunk = self.rfile.read(min(length, 1 << 16))
                if not chunk:
                    break
                length -= len(chunk)

        def do_POST(self):
            if self._compat():
                return
            if not self._authorized():
                self._discard_body()
                return
            path = self.path.split("?")[0].rstrip("/")   # clients post /v1/messages?beta=true
            if path.startswith("/api/") and path not in CONTROL_PATHS and not self._write_secret():
                return
            if path == "/api/settings":
                self._settings_post()
                return
            if path == "/api/control-secret":
                self._control_secret_post()
                return
            if path == "/api/tasks":
                try:
                    req = json.loads(self._body(1 << 16) or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError("send a JSON object")
                    task_id = req.get("id")
                    if not task_id:
                        raise ValueError("task 'id' is required")
                    action = req.get("action", "record")
                    if action == "close":
                        _TASKS.close(task_id)
                        self._json(200, {"status": "closed", "id": task_id})
                    else:
                        status = req.get("status", "running")
                        result = req.get("result", "")
                        children = req.get("children")
                        metadata = req.get("metadata")
                        saved = _TASKS.record(task_id, status=status, result=result, children=children, metadata=metadata)
                        self._json(200, {"status": "ok", "task": saved})
                except BadBody as e:
                    self._json(e.status, {"error": {"message": str(e)}})
                except (ValueError, TypeError) as e:
                    self._json(400, {"error": {"message": str(e)}})
                return
            if path.startswith("/api/remote/"):
                self._remote_post(path)
                return
            if path.startswith("/api/agent/"):
                self._agent_post(path)
                return
            if path == "/api/routines" or path.startswith("/api/routines/"):
                self._routines_post(path)
                return
            if path in ("/api/file-owners/set", "/api/file-owners/remove"):
                self._file_owners_post(path)
                return
            if path in ("/api/sessions-base/check", "/api/sessions-base/move"):
                self._sessions_base_post(path)
                return
            if path == "/api/personal-skills" or path.startswith("/api/personal-skills/"):
                self._pskills_post(path)
                return
            if path.startswith("/api/llm/"):
                self._llm_post(path)
                return
            if path == "/api/attachments" or (path.startswith("/api/attachments/") and path.rsplit("/", 1)[-1] == "remove"):
                self._attachments_post(path)
                return
            if path.startswith("/api/capabilities/"):
                self._capabilities_post(path)
                return
            if path == "/api/billion-context":
                self._billion_context_post()
            if path == "/api/side-uses":
                self._side_uses_post()
                return
                return
            if path.startswith("/api/netwatch/"):
                self._netwatch(path, True)
                return
            if path.startswith("/api/rea/"):
                self._rea(path, True)
                return
            if path.startswith("/api/llamacpp/"):
                self._llamacpp(path, True)
                return
            if path.startswith("/api/canary/"):
                self._canary(path, True)
                return
            if path.startswith("/canary/"):
                self._canary_gui("POST")
                return
            if path in ("/api/knowledge", "/api/knowledge/wiki-check"):
                self._knowledge_post(path)
                return
            if path == "/api/intake":
                self._intake_post()
                return
            if path == "/api/gate":
                self._gate_post()
                return
            if path == "/api/coding":
                self._coding_post()
                return
            if path == "/api/compaction":
                self._compaction_post()
                return
            if path in ("/api/mcp/auth/start", "/api/mcp/auth/signout"):
                self._mcp_auth_post(path)
                return
            if path.startswith("/api/wallet/"):
                self._wallet_post(path)
                return
            if path.startswith("/api/websearch/"):
                self._websearch_post(path)
                return
            if path.startswith("/api/telegram/"):
                self._telegram_post(path)
                return
            if path == "/api/personas" or path.startswith("/api/personas/"):
                self._personas_post(path)
                return
            if path.startswith("/api/rag/"):
                self._rag_route(path, "POST")
                return
            if path.startswith("/api/voice/"):
                self._voice_post(path)
                return
            if path.startswith("/api/memories/"):
                self._memories_post(path)
                return
            if path.startswith("/api/lessons/"):
                self._lessons_post(path)
                return
            if path == "/api/next-steps":
                self._persistence_post(nextsteps.FILE, "next-steps")
                return
            if path == "/api/technical-session":
                self._technical_session_post()
                return
            if path == "/api/persistence":
                self._persistence_post()
                return
            if path == "/api/past/chat":
                self._past_chat_post()
                return
            if path == "/api/past/forget":
                self._past_forget_post()
                return
            if path == "/api/diagram/fix":
                self._diagram_fix_post()
                return
            if path == "/api/changes/review":
                if not settings.is_loopback(self.client_address[0]):
                    self._discard_body()
                    self._json(403, {"error": {"type": "forbidden",
                                               "message": "changes can be reviewed only from the PC the server runs on"}})
                    return
                try:
                    req = json.loads(self._body(1 << 12) or b"{}")
                    if not isinstance(req, dict) or not isinstance(req.get("id"), str):
                        raise ValueError("send the change's id and an action")
                    self._json(200, {"edit": _CHANGES.review_action(req["id"], req.get("action", ""), req.get("block"))})
                except review_mod.ReviewError as e:
                    self._json(409, {"error": {"type": "review", "message": str(e)}})
                except BadBody as e:
                    self._json(e.status, {"error": {"message": str(e)}})
                except (ValueError, TypeError) as e:
                    self._json(400, {"error": {"message": str(e)}})
                return
            if path == "/api/changes":
                try:
                    req = json.loads(self._body(4 << 20) or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError("send a JSON object")
                    if req.get("clear"):
                        _CHANGES.clear()
                        self._json(200, {"cleared": True})
                    else:
                        self._json(200, _CHANGES.add(req.get("edits", [])))
                except BadBody as e:
                    self._json(e.status, {"error": {"message": str(e)}})
                except (ValueError, TypeError) as e:
                    self._json(400, {"error": {"message": str(e)}})
                return
            if path == "/api/planner/settings" or path == "/api/plans" or path.startswith("/api/plans/"):
                self._plans_post(path)
                return
            if path == "/api/export":
                self._export_post()
                return
            if path == "/api/chat/context":
                self._chat_context()
                return
            if path == "/api/chat/abstract":
                self._chat_abstract()
                return
            if path == "/api/chat/code-abstract":
                self._chat_code_abstract()
                return
            if path == "/api/reqlog/clear":
                self._reqlog_clear()
                return
            if path.startswith("/api/loader/"):
                self._loader_post(path[len("/api/loader/"):])
                return
            if path.startswith("/api/wizard/"):
                self._wizard_post(path[len("/api/wizard/"):])
                return
            if path == "/api/prefs":   # the user's language and the RAM/commit share kept free: this PC's, every page's
                if not settings.is_loopback(self.client_address[0]):
                    self._json(403, {"error": {"type": "forbidden",
                                               "message": "preferences can be changed only from the PC the server runs on"}})
                    return
                try:
                    req = json.loads(self._body(1 << 16) or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError('send {"lang": "it" | "en" | null} and/or {"free_share": 0.15}')
                    share = loader.check_free_share(req["free_share"]) if "free_share" in req else None
                    if "lang" in req or "free_share" not in req:     # an empty body is "auto", as before
                        lang_mod.set_lang(req.get("lang"))
                    if share is not None:
                        loader.save_machine_settings({"free_share": share})
                    self._json(200, self._prefs())
                except BadBody as e:
                    self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                except ValueError as e:
                    self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            if path == "/api/profiles":
                self._profiles_post()
                return
            if path.startswith("/api/models/"):
                self._models_post(path[len("/api/models/"):])
                return
            if path.startswith("/api/bench/") and path[len("/api/bench/"):] in BENCH_ACTIONS:
                self._benchmarks_post(path[len("/api/bench/"):])
                return
            if path.startswith("/api/bench/"):
                self._bench_post(path[len("/api/bench/"):])
                return
            if path in CONTROL_PATHS and not self._control_body():
                return
            # this PC and the secret of this server: an application that only knows the API key must not control the model
            if path in CONTROL_PATHS and not (self._control_local() and self._control_secret()):
                return
            if path == "/api/control/unload" and engine_host.HOST.active():
                engine_host.HOST.stop()
                self._json(200, {"status": "unloaded"})
                return
            if path == "/api/control/unload":                # unload between requests
                try:
                    r = svc.unload()
                except EngineStuck as e:
                    self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                    return
                self._json(409 if r == "busy" else 200, {"status": r})
                return
            if path == "/api/control/load" and engine_host.HOST.active():
                st = engine_host.HOST.status()
                self._json(200 if st["phase"] == "running" else 503, {"status": "loaded" if st["phase"] == "running" else st["phase"]})
                return
            if path == "/api/control/load":                  # load ahead of a request
                try:
                    svc.load()
                    self._json(200, {"status": "loaded"})
                except (GpuBusy, EngineDied, EngineStarting) as e:
                    self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                return
            if path == "/v1/chat/completions" and not self.remote_listener and billion_context.wants(self.headers):
                try:
                    raw = self._body()
                except BadBody as e:
                    return self._json(e.status, {"error": {"message": str(e)}})
                if billion_context.forward(self, raw, billion_context.get().server_port):
                    return
                self._held_body = raw                         # not handed over: answered here, from the body already read
            if path in engine_host.PROXIED_POST and engine_host.HOST.active():
                try:
                    raw = self._held_body if self._held_body is not None else self._body()
                except BadBody as e:
                    self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                    return
                engine_host.HOST.forward(self, "POST", self.path, raw)
                return
            trace = None
            if path in API_PATHS and svc.reqlog.enabled:
                trace = self.trace = svc.request_ctx.trace = RequestTrace(
                    svc, path, "POST", ":".join(str(x) for x in self.client_address[:2]))
            try:
                raw = (self._held_body if self._held_body is not None else self._body()) or b"{}"
                try:
                    req = json.loads(raw)
                    if not isinstance(req, dict):
                        raise ValueError("send a JSON object")
                except ValueError:
                    if trace is not None:
                        trace.request(raw=raw)
                    raise
                if trace is not None:
                    trace.request(req)
                if path.startswith("/v1/responses/"):        # nothing is stored
                    self._json(404, responses_error_body("this server keeps no responses (stateless); send the "
                                                         "whole conversation to POST /v1/responses", code="not_found"))
                    return
                if path == "/v1/vram":                       # resize the VRAM reserve
                    r = req.get("reserve_mib")
                    if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
                        raise ValueError("reserve_mib: a whole number of MiB (0 or more), or null for the start's")
                    try:
                        self._json(200, svc.vram(r))
                    except EngineDied as e:
                        self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                    return
                svc.request_ctx.path = path
                svc.request_ctx.model = req.get("model") if isinstance(req.get("model"), str) else None
                if path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
                    # a listed model that is not loaded: say so instead of answering with the loaded one
                    refused = model_list.refusal(svc, req.get("model"),
                                                 model_list.is_loopback(self.server.server_address[0]))
                    if refused:
                        self._json(404, model_list.refused_body(
                            refused, "anthropic" if path == "/v1/messages" else "openai"))
                        return
                if path == "/v1/responses":
                    self._responses(req)
                elif path == "/v1/chat/completions":
                    self._openai(req)
                elif path == "/v1/messages":
                    self._anthropic(req)
                elif path == "/v1/messages/count_tokens":
                    self._count_tokens(req)
                else:
                    self._json(404, {"error": {"message": "not found"}})
            except EngineError as e:                         # the engine failed: not the request's fault
                if path == "/v1/responses":
                    self._json(500, responses_error_body(str(e), "server_error", code="server_error"))
                else:
                    self._json(500, {"error": {"type": "server_error", "message": str(e)}})
            except BadBody as e:
                if path == "/v1/responses":
                    self._json(e.status, responses_error_body(str(e)))
                else:
                    self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ValueError as e:
                if path == "/v1/responses":
                    self._json(400, responses_error_body(str(e)))
                else:
                    self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ModelBusy as e:
                self._json(409, {"error": {"type": "model_busy", "message": str(e)}})
            except StructuredOutputError as e:
                self._json(502, {"error": {"type": "structured_output_failed", "code": "structured_output_failed",
                                          "message": str(e)}})
            except (GpuBusy, EngineStarting) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})
            except EngineDied as e:                          # before the answer started
                self._json(503, {"error": {"type": "server_error", "message": f"{e}; the next request restarts it"}})
            except EngineStuck as e:                         # the engine could not be ended
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})

            finally:
                if self.watch_done is not None:
                    self.watch_done.set()
                if trace is not None:
                    trace.end()
                    self.trace = svc.request_ctx.trace = None

        def _props(self):
            model = parse_qs(urlsplit(self.path).query).get("model", [svc.model])[0]
            if model != svc.model:
                self._json(404, {"error": {"message": "model not found"}})
                return
            try:
                svc.wait_load()                              # a load in progress is waited for, not refused
            except (GpuBusy, EngineDied) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                return
            if not svc.loaded() and not getattr(svc.engine, "unloaded", False):
                self._json(503, {"error": {"message": "the engine is not running"}})
                return
            names = {"repetition_penalty": "repeat_penalty", "penalty_last_n": "repeat_last_n"}
            params = {names.get(k, k): v for k, v in svc.sampling_defaults.items()
                      if k in ("temperature", "top_p", "top_k", "min_p", "seed", "repetition_penalty",
                               "presence_penalty", "frequency_penalty", "penalty_last_n")}
            params["n_predict"] = -1
            props = {"default_generation_settings": {"n_ctx": svc.engine.max_context, "params": params},
                     "total_slots": svc.slot_stats()["conversations"], "model_alias": svc.model, "chat_template": svc.template.source,
                     "modalities": {"vision": svc.vision is not None}, "models_autoload": hasattr(svc.engine, "restart"),
                     "is_sleeping": not svc.loaded()}
            side = svc.side_slot_info()
            if side is not None:                             # the technical session: one more slot, with a context of its own
                props["side_slot"] = side
                props["slot_contexts"] = [svc._slot_context(i) for i in range(svc.slot_stats()["total"])]
            if getattr(svc.engine, "model_path", None):
                props["model_path"] = svc.engine.model_path
            version = getattr(svc.engine, "info", {}).get("version")
            if version:
                props["build_info"] = "basalt " + str(version)
            self._json(200, props)

        def _control_local(self) -> bool:
            if settings.is_loopback(self.client_address[0]):
                return True
            self._json(403, {"error": {"type": "forbidden", "message": "the model can be controlled only from the PC the server runs on"}})
            return False

        def _control_secret(self) -> bool:
            """With --control-secret the control routes need the secret the server wrote to its data folder at start; the API key is not enough."""
            if not svc.control_token:
                return True
            if not key_matches(self.headers.get(CONTROL_HEADER, ""), svc.control_token):
                self._json(403, {"error": {"type": "forbidden", "message": f"the {CONTROL_HEADER} header is missing or wrong"}})
                return False
            return True

        def _key_given(self) -> bool:
            """The request carries this server's API key (no reply is written)."""
            auth = self.headers.get("Authorization", "")
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("x-api-key", "")
            return bool(svc.api_key) and key_matches(given, svc.api_key)

        def _page_may_change_things(self, navigation: bool) -> bool:
            """Whether this request may be given the cookie of the change secret: the page of the person on this PC, never agent-run code.
            With an API key the key is the proof; without one the request must be a browser's own navigation to the page, which a web
            page cannot forge and a program has to imitate."""
            if not svc.control_token or self.remote_listener or not settings.is_loopback(self.client_address[0]):
                return False
            if svc.api_key:
                if not self._key_given():
                    return False
            elif navigation:
                h = self.headers
                if (h.get("Sec-Fetch-Dest", ""), h.get("Sec-Fetch-Mode", "")) != ("document", "navigate") or h.get("Sec-Fetch-Site", "") not in ("none", "same-origin"):
                    return False
            else:
                return False
            return True

        def _control_cookie_header(self) -> str:
            return f"{self._control_cookie()}={svc.control_token}; Path=/; HttpOnly; SameSite=Strict"

        def _control_cookie(self) -> str:
            """One cookie name for each port: cookies are shared by every port of an address, and each server has its own secret."""
            return f"tadamoo_control_{self.server.server_address[1]}"

        def _write_secret(self) -> bool:
            """Whatever /api/* changes needs the server's secret, in the header (programs) or in the cookie the page got when it
            loaded: a program that merely reaches the port, or a page of another site, changes nothing. With no secret set (an
            embedded service) only the checks of each route apply."""
            token = svc.control_token
            if not token or self._via_key or self._via_voice:                           # a phone's own key is the stronger proof; the voice service's token opens only its routes
                return True
            given = self.headers.get(CONTROL_HEADER, "")
            if not given:
                try:
                    jar = SimpleCookie(self.headers.get("Cookie", ""))
                except CookieError:
                    jar = {}
                given = jar[self._control_cookie()].value if self._control_cookie() in jar else ""
            if key_matches(given, token):
                return True
            if self._control_body():
                self._json(403, {"error": {"type": "forbidden", "code": "control_secret",
                                           "message": "this change needs the server's secret: reload the page (the server may have restarted)"}})
            return False

        def _control_body(self) -> bool:
            """Read the unused body before replying so the client sees no TCP reset."""
            chunked = self._chunked()
            length = 0
            if not chunked:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json(400, {"error": {"message": "invalid Content-Length"}})
                    return False
                if not 0 <= length <= CONTROL_BODY_MAX:
                    self._json(413, {"error": {"message": "control request body is limited to 64 KiB"}})
                    return False
            timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(2.0)
                if chunked:
                    self._read_chunked(CONTROL_BODY_MAX, keep=False)
                    complete = True
                else:
                    complete = len(self.rfile.read(length)) == length
            except BadBody as e:
                self._json(e.status, {"error": {"message": f"control request: {e}"}})
                return False
            except OSError:
                complete = False
            finally:
                self.connection.settimeout(timeout)
            if not complete:
                self._json(400, {"error": {"message": "incomplete control request body"}})
            return complete

        def _prefs(self) -> dict:
            """GET /api/prefs: the language (serve/lang.py) and the share of the RAM and of the commit limit every
            plan keeps free (loader.free_share: the Settings view's or the wizard's, else 15%)."""
            return {**lang_mod.info(),
                    "free_share": {"value": loader.free_share(), "default": loader.FREE_SHARE,
                                   "min": loader.FREE_SHARE_MIN, "max": loader.FREE_SHARE_MAX,
                                   "floor_bytes": loader.FREE_FLOOR}}

        def _settings_get(self):
            """GET /api/settings: the config file's main fields, what the server runs with, and whether this client
            may change them (only from this PC, only with a --config file)."""
            info = getattr(svc, "settings_info", None) or {}
            path = info.get("config")
            out = {"config": path, "model": info.get("model"), "running": info.get("running") or {},
                   "from_command_line": info.get("cli") or [], "values": {}, "editable": False, "reason": "",
                   "fields": [f"{s}.{k}" for s, k in settings.FIELDS],
                   "limits": {"parallel_max": PARALLEL_MAX, "spec_max": settings.SPEC_MAX, "kv": list(settings.KV_MODES),
                              "vision": list(VISION_MODES), "context_max": settings.CONTEXT_MAX,
                              "prefill_ceilings": list(settings.PREFILL_CEILINGS)},
                   "note": settings.RESTART_NOTE}
            if not path:
                out["reason"] = "The server was started without --config, so there is no settings file to save to."
            else:
                try:
                    out["values"] = settings.values_of(settings.read_file(path))
                except (OSError, ValueError) as e:
                    out["reason"] = f"Cannot read {path}: {getattr(e, 'strerror', None) or e}"
                else:
                    if not settings.is_loopback(self.client_address[0]):
                        out["reason"] = "Settings can be changed only from the PC the server runs on."
                    elif not os.access(path, os.W_OK):
                        out["reason"] = f"{path} is read-only."
                    else:
                        out["editable"] = True
            self._json(200, out)

        def _control_secret_file(self):
            """The --config file that holds the secret's switch and text, or None with the reply already sent."""
            path = (getattr(svc, "settings_info", None) or {}).get("config")
            if not path:
                self._json(409, {"error": {"type": "invalid_request_error", "message":
                                           "the server was started without --config: there is no settings file"}})
            return path

        def _control_secret_state(self, path):
            return {**settings.control_secret_state(path), "running": bool(svc.control_token), "note": settings.RESTART_NOTE}

        def _control_secret_get(self):
            """GET /api/control-secret: whether the secret is asked for, whether one is saved and whether this server runs with it; never the secret."""
            path = self._control_secret_file()
            if path:
                try:
                    self._json(200, self._control_secret_state(path))
                except (OSError, ValueError) as e:
                    self._json(500, {"error": {"type": "server_error", "message": f"cannot read {path}: {getattr(e, 'strerror', None) or e}"}})

        def _control_secret_post(self):
            """POST /api/control-secret {"enabled": bool, "secret": text | null}: save the switch and the chosen secret in the --config file (null removes the text).
            Only from this PC; it applies when the server starts again."""
            try:
                raw = self._body(settings.BODY_MAX)
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            if not settings.is_loopback(self.client_address[0]):
                self._json(403, {"error": {"type": "forbidden", "message": "the secret can be changed only from the PC the server runs on"}})
                return
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                req = None
            if not isinstance(req, dict) or not ({"enabled", "secret"} & set(req)):
                self._json(400, {"error": {"type": "invalid_request_error", "message": 'send {"enabled": true|false} and/or {"secret": "text" | null}'}})
                return
            path = self._control_secret_file()
            if not path:
                return
            changes = {}
            if "enabled" in req:
                if not isinstance(req["enabled"], bool):
                    self._json(400, {"error": {"type": "invalid_request_error", "message": "enabled is true or false"}})
                    return
                changes["server.control-secret"] = req["enabled"]
            if "secret" in req:
                changes["server.control-secret-value"] = req["secret"]
            try:
                result = settings.save(path, changes, settings.CONTROL_FIELDS)
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            except OSError as e:
                self._json(500, {"error": {"type": "server_error", "message": f"cannot write {path}: {e.strerror or e}"}})
                return
            print(f"[server] control secret settings saved to {path}: {', '.join(result['changed']) or 'nothing changed'}", flush=True)
            self._json(200, {"saved": True, "changed": result["changed"], **self._control_secret_state(path)})

        def _settings_post(self):
            """POST /api/settings {"changes": {"engine.spec": 7, "server.port": null, ...}}: validate and write them to
            the --config file. Only from this PC (the API key too, if set)."""
            try:                                            # read first: a refusal then reaches the client whole
                raw = self._body(settings.BODY_MAX)
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            if not settings.is_loopback(self.client_address[0]):
                self._json(403, {"error": {"type": "forbidden",
                                           "message": "settings can be changed only from the PC the server runs on"}})
                return
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                self._json(400, {"error": {"type": "invalid_request_error", "message": "the body is not valid JSON"}})
                return
            path = (getattr(svc, "settings_info", None) or {}).get("config")
            if not path:
                self._json(409, {"error": {"type": "invalid_request_error", "message":
                                           "the server was started without --config: there is no settings file"}})
                return
            try:
                result = settings.save(path, req.get("changes") if isinstance(req, dict) else None)
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            except OSError as e:
                self._json(500, {"error": {"type": "server_error",
                                           "message": f"cannot write {path}: {e.strerror or e}"}})
                return
            print(f"[server] settings saved to {path}: {', '.join(result['changed']) or 'nothing changed'}"
                  f"{' (apply on restart)' if result['changed'] else ''}", flush=True)
            self._json(200, {"saved": True, **result, "note": settings.RESTART_NOTE})

        def _loader_post(self, what: str):
            """POST /api/loader/plan | load | calibrate, as JSON (and with the API key, if set).
            From any client; the server loads from its own disks."""
            try:                                            # read first: a refusal then reaches the client whole
                raw = self._body(settings.BODY_MAX)
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            parts = what.split("/")
            if len(parts) == 3 and parts[0] == "engines":
                self._engine_post(parts[1], parts[2], raw)
                return
            if what not in ("plan", "load", "calibrate", "length"):
                self._json(404, {"error": {"message": "not found"}})
                return
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                self._json(400, {"error": {"type": "invalid_request_error", "message": "the body is not valid JSON"}})
                return
            try:
                if what == "plan":
                    model = (req or {}).get("model") if isinstance(req, dict) else None
                    self._json(200, loader_api.evaluate(svc, loader_api.clean((req or {}).get("values", {})),
                                                        model=model or None))
                elif what == "load":
                    engine_host.HOST.stop()                  # Basalt-Plus's engine takes the GPU back from another one
                    out = loader_api.load(svc, req)
                    self._json(200 if out.get("status") == "saved" else 202, out)
                elif what == "length":
                    sup = loader_api.sup_of(svc)
                    action = (req or {}).get("action") if isinstance(req, dict) else None
                    if action == "stop":
                        self._json(200, {"status": "stopping" if sup.stop_length() else "idle"})
                    elif action != "measure":
                        self._json(400, {"error": {"type": "invalid_request_error", "message": "action: measure or stop"}})
                    elif not svc.loaded() or svc.loading:
                        self._json(409, {"error": {"type": "invalid_request_error", "code": "not_loaded",
                                                   "message": "load the model first, then measure its length"}})
                    elif not sup.measure_length():
                        self._json(409, {"error": {"type": "model_busy", "code": "busy",
                                                   "message": "a load, a calibration or a measurement is already running"}})
                    else:
                        self._json(202, {"status": "measuring"})
                else:
                    sup = loader_api.sup_of(svc)
                    if not svc.loaded() or svc.loading:
                        self._json(409, {"error": {"type": "invalid_request_error", "code": "not_loaded",
                                                   "message": "load the model first, then calibrate"}})
                    elif not sup.calibrate():
                        self._json(409, {"error": {"type": "model_busy", "code": "busy",
                                                   "message": "a load or a calibration is already running"}})
                    else:
                        self._json(202, {"status": "calibrating"})
            except loader_api.Refused as e:
                self._json(e.status, e.body)
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except OSError as e:
                self._json(500, {"error": {"type": "server_error", "message": f"cannot write the config file: {e}"}})

        def _engine_post(self, engine_id: str, action: str, raw: bytes):
            """POST /api/loader/engines/<id>/install | load | stop | source (serve/engine_host.py, engine_install.py)."""
            if engine_id not in engines.ENGINES:
                self._json(404, {"error": {"type": "invalid_request_error", "code": "unknown_engine", "message": "not found"}})
                return
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if action == "install":
                    self._json(*engines.install(engine_id, req.get("source") or None, bool(req.get("remember"))))
                elif action == "source":
                    self._json(200, {"sources": engine_install.set_source(engine_id, req.get("path") or None)})
                elif action == "stop":
                    self._json(200, engine_host.HOST.stop())
                elif action == "load":
                    out = engine_host.HOST.activate(svc, engine_id, engine_host.load_request(engine_id, req))
                    self._json(202, out)
                else:
                    self._json(404, {"error": {"message": "not found"}})
            except engine_host.Refused as e:
                self._json(e.status, e.body)
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})

        def _wizard_post(self, what: str):
            """POST /api/wizard/plan | measure | save (serve/loader_api.py), as JSON."""
            try:
                raw = self._body(settings.BODY_MAX)
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            if what not in ("plan", "measure", "save"):
                self._json(404, {"error": {"message": "not found"}})
                return
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if what == "plan":
                    self._json(200, loader_api.wizard_plan(svc, req))
                elif what == "measure":
                    out = loader_api.wizard_measure(svc, req)
                    self._json(202, out)
                else:
                    self._json(200, loader_api.wizard_save(svc, req))
            except loader_api.Refused as e:
                self._json(e.status, e.body)
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except OSError as e:
                self._json(500, {"error": {"type": "server_error", "message": f"cannot write: {e}"}})

        def _profiles_post(self):
            """POST /api/profiles {"action": "save"|"rename"|"delete"|"default"|"import" (load profiles only), "kind": "load"|"inference",
            ...}: as JSON (serve/profiles.py)."""
            try:
                req = json.loads(self._body(1 << 20) or b"{}")
                self._json(200, profiles.change(req))
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except OSError as e:
                self._json(500, {"error": {"type": "server_error", "message": f"cannot write the profiles: {e}"}})

        def _models_listing(self, rescan: bool = False) -> dict:
            """GET /api/models: the models folders, every model under them with its status, the running job."""
            path = (getattr(svc, "settings_info", None) or {}).get("config")
            try:
                current = settings.model_of(path) if path else None
            except (OSError, ValueError):
                current = None
            out = models.listing(rescan, current or (getattr(svc, "settings_info", None) or {}).get("model"))
            for m in out.get("models") or []:                    # the Download catalog's entry for a file it knows
                found = catalog.describe(m.get("path") or "")
                m["catalog"] = found["id"] if found else None
            out["editable"] = settings.is_loopback(self.client_address[0])
            return out

        def _bench_overview(self) -> dict:
            """GET /api/bench/overview: the models with what was measured for each, the published references, the metrics, and what the Model
            converter page needs to offer (the models to convert, the profiles and drafter layers that can be reused, the standard settings)."""
            listing = self._models_listing(False)
            ready = [m for m in listing.get("models") or [] if m.get("kind") == "basalt" and m.get("role") == "model" and m.get("status") == "ready"]
            out = modelbench.overview(ready)
            out["convertible"] = [{k: m.get(k) for k in ("id", "name", "quant", "path", "size_bytes", "experts_bytes", "mmproj", "vision", "mtp", "compat_bf16")}
                                  for m in listing.get("models") or [] if m.get("role") == "model" and m.get("status") == "convert"]
            out["profiles"] = [{"name": m.get("name"), "path": m.get("path")} for m in ready if not m.get("profile_plain")]
            out["tuned_packs"] = [{k: p[k] for k in ("id", "name", "quant")} for p in models.tuned_sources()]
            out["projectors"] = models.projector_files()
            out["standard"] = benchrun.standard_settings()
            out["power_w"] = benchrun.power_limit_w()
            out["job"] = benchrun.JOBS.status()
            out["editable"] = listing.get("editable")
            out["out_dir"] = listing.get("output_dir") or models.default_output_dir()
            return out

        def _bench_precheck(self, path: str, settings_: dict) -> None:
            """A test needs the GPU and the memory of its model: nothing else may hold them, and the Loader's own check must say it fits."""
            if models.JOBS.running():
                raise benchrun.Refused(409, {"error": {"type": "busy", "code": "busy", "message": "a conversion or a verification is running: wait for it to end"}})
            held = loader.engine_holds(svc.engine)
            if held["commit"] > (1 << 30):
                raise benchrun.Refused(409, {"error": {"type": "model_loaded", "code": "model_loaded",
                                                       "message": "a model is loaded and holds the GPU and the memory: unload it first (the test loads its own copy)"},
                                             "holds": held})
            facts = {**loader.model_facts(path), "projectors": models.projector_files()}
            values = {(f"server.{k}" if k in ("parallel", "vision", "vision-hold", "vision-projector") else f"engine.{k}"): v for k, v in settings_.items()}
            plan = loader.plan(values, facts, loader.system_now(getattr(svc, "gpu_index", 0)))
            if not plan["fits"]:
                raise benchrun.Refused(422, {"error": {"type": "insufficient_memory", "code": "does_not_fit",
                                                       "message": " ".join(loader.problem_text(x, "en") for x in plan["problems"])},
                                             "problems": [{**x, "text": loader.problem_text(x, "en"), "text_it": loader.problem_text(x, "it")} for x in plan["problems"]]})

        def _bench_post(self, what: str):
            """POST /api/bench/availability | run | cancel: only from this PC. A test that was already
            made under the same settings is not repeated without asking: `reuse` is "ask" (the default, answered 409), "reuse", "redo" or
            a choice for each step."""
            try:
                raw = self._body(settings.BODY_MAX)
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            if what not in ("availability", "run", "cancel", "unload", "reload"):
                self._json(404, {"error": {"message": "not found"}})
                return
            if not settings.is_loopback(self.client_address[0]):
                self._json(403, {"error": {"type": "forbidden", "message": "models can be tested only from the PC the server runs on"}})
                return
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
            except ValueError:
                self._json(400, {"error": {"type": "invalid_request_error", "message": "the body is not valid JSON"}})
                return
            try:
                if what == "cancel":
                    self._json(200, {"cancelled": benchrun.JOBS.cancel(), **benchrun.JOBS.status()})
                    return
                if what == "unload":                         # a test loads its own copy of the model: this one has to give the GPU and the memory back
                    r = svc.unload()
                    self._json(409 if r == "busy" else 200, {"status": r})
                    return
                if what == "reload":
                    try:
                        svc.load()
                        self._json(200, {"status": "loaded"})
                    except (GpuBusy, EngineDied, EngineStarting) as e:
                        self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                    return
                path = str(req.get("path") or "")
                ready = [m for m in self._models_listing(False).get("models") or []
                         if m.get("kind") == "basalt" and m.get("status") == "ready" and models._norm(m.get("path") or "") == models._norm(path)]
                if not ready:
                    raise benchrun.Refused(404, {"error": {"type": "not_found", "message": "no such model among the ready ones"}})
                steps = [s for s in (req.get("steps") or modelbench.STEPS) if s in modelbench.STEPS]
                relax = [float(x) for x in (req.get("relax") or modelbench.RELAX_DEFAULT)]
                reps = max(1, min(5, int(req.get("reps") or 2)))
                n = max(10, min(500, int(req.get("n") or 200)))
                overrides = req.get("settings") if isinstance(req.get("settings"), dict) else None
                if what == "availability":
                    ident = modelbench.fingerprints(ready[0]["path"])
                    std = benchrun.standard_settings(overrides)
                    keyed = {**std, "relax": sorted({0.0, *relax}), "reps": reps, "power": benchrun.power_limit_w(), "n": n}
                    self._json(200, {"available": modelbench.availability(ident, keyed, steps), "settings": std})
                    return
                self._json(202, benchrun.JOBS.start(ready[0], steps, overrides, relax, reps, n, req.get("baseline") is not False, req.get("reuse") or "ask",
                                                    precheck=self._bench_precheck))
            except benchrun.Refused as e:
                self._json(e.status, e.body)
            except (ValueError, TypeError) as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})

        def _catalog(self) -> dict:
            """GET /api/models/catalog: the models the Download panel offers (serve/catalog.py); nothing secret, no network."""
            return {"entries": [catalog.public(e) for e in catalog.CATALOG], "families": catalog.FAMILIES,
                    "licenses": catalog.LICENSES, "generic": catalog.GENERIC,
                    "uncensored_warning": catalog.UNCENSORED_WARNING, "reviewed": catalog.REVIEWED,
                    "dirs": models.dirs(), "default_dest": models.default_output_dir(),
                    "editable": settings.is_loopback(self.client_address[0])}

        def _hf_status(self) -> None:
            """GET /api/models/hf: where the Hugging Face token comes from and whose it is (asks Hugging Face)."""
            if not settings.is_loopback(self.client_address[0]):
                self._json(403, {"error": {"type": "forbidden", "message": "only from the PC the server runs on"}})
                return
            self._json(200, downloads.hf_status())

        def _models_post(self, what: str):
            """POST /api/models/dirs | plan | convert | verify | cancel | default | download-plan | download |
            download-cancel | hf-token: only from this PC (serve/models.py,
            serve/downloads.py). A conversion or a download needs the plan's token and confirm: true."""
            try:                                            # read first: a refusal then reaches the client whole
                raw = self._body(settings.BODY_MAX)
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
            if what not in ("dirs", "plan", "convert", "verify", "cancel", "default", "download-plan", "download",
                            "download-cancel", "hf-token"):
                self._json(404, {"error": {"message": "not found"}})
                return
            if not settings.is_loopback(self.client_address[0]):
                self._json(403, {"error": {"type": "forbidden",
                                           "message": "the models can be managed only from the PC the server runs on"}})
                return
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
            except ValueError:
                self._json(400, {"error": {"type": "invalid_request_error", "message": "the body is not valid JSON"}})
                return
            try:
                if what == "dirs":
                    models.set_dirs(req.get("dirs"))
                    if getattr(svc, "model_catalog", None) is not None:
                        svc.model_catalog.invalidate()       # GET /v1/models scans the new folders
                    self._json(200, self._models_listing(True))
                elif what == "plan":
                    self._json(200, models.convert_plan(str(req.get("id") or ""), req.get("out_dir"), options=req.get("options")))
                elif what == "convert":
                    self._json(202, models.JOBS.start_convert(str(req.get("id") or ""), req.get("out_dir"),
                                                              req.get("token"), req.get("confirm"), options=req.get("options")))
                elif what == "verify":
                    self._json(202, models.JOBS.start_verify(req.get("path")))
                elif what == "download-plan":
                    self._json(200, downloads.plan(str(req.get("id") or ""), req.get("dest")))
                elif what == "download":
                    self._json(202, downloads.DOWNLOADS.start(str(req.get("id") or ""), req.get("dest"),
                                                              req.get("token"), req.get("confirm"), req.get("ack")))
                elif what == "download-cancel":
                    self._json(200, {"cancelled": downloads.DOWNLOADS.cancel(), **downloads.DOWNLOADS.status()})
                elif what == "hf-token":
                    self._json(200, downloads.set_session_token(req.get("token")))
                elif what == "default":
                    try:
                        models.set_default_model(req.get("path") or None)
                    except ValueError as e:
                        self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                        return
                    self._json(200, {"default_model": models.default_model()})
                else:
                    self._json(200, {"cancelled": models.JOBS.cancel(), **models.JOBS.status()})
            except models.DirsError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}, "fields": e.errors})
            except models.Refused as e:
                self._json(e.status, e.body)
            except downloads.DownloadError as e:
                self._json(502 if e.code in ("network", "gone") else 400,
                           {"error": {"type": "download_error", "code": e.code, "message": e.en, "message_it": e.it}})
            except OSError as e:
                self._json(500, {"error": {"type": "server_error", "message": f"{e.strerror or e}"}})

        def _chat_context(self):
            """POST /api/chat/context: the chat page's context meter and overflow policy (serve/chatctx.py)."""
            try:
                req = json.loads(self._body() or b"{}")
                self._json(200, chatctx.fit(svc, req))
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ModelBusy as e:
                self._json(409, {"error": {"type": "model_busy", "message": str(e)}})
            except (GpuBusy, EngineStarting, EngineDied, EngineStuck) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})

        def _chat_abstract(self):
            """POST /api/chat/abstract {"text": a reply}: the short record of that reply the model reads in its place (serve/chatctx.py)."""
            try:
                req = json.loads(self._body() or b"{}")
                text = req.get("text") if isinstance(req, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text: the reply to abstract")
                limit = chatctx._limit(svc)
                if limit <= 0:
                    raise ValueError("the model is not loaded yet: its context is unknown")
                self._json(200, chatctx.abstract_reply(svc, text, limit, chatctx.Counter(svc, {})))
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ModelBusy as e:
                self._json(409, {"error": {"type": "model_busy", "message": str(e)}})
            except (GpuBusy, EngineStarting, EngineDied, EngineStuck) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})

        def _chat_code_abstract(self):
            """POST /api/chat/code-abstract {"text": a message}: the message with each block of code replaced by a description, for the model only (serve/chatcode.py)."""
            from serve import chatcode
            try:
                req = json.loads(self._body() or b"{}")
                text = req.get("text") if isinstance(req, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text: the message with code")
                self._json(200, chatcode.abstract_message(svc, text, chatctx._limit(svc), chatctx.Counter(svc, {})))
            except BadBody as e:
                self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ValueError as e:
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ModelBusy as e:
                self._json(409, {"error": {"type": "model_busy", "message": str(e)}})
            except (GpuBusy, EngineStarting, EngineDied, EngineStuck) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})

        def _logs(self):
            """GET /api/logs?after=<id>&limit=<n>: the newest lines after that id, and the newest id."""
            query = parse_qs(urlsplit(self.path).query)

            def number(name, default):
                try:
                    return max(0, int(query.get(name, [default])[0]))
                except ValueError:
                    return default
            after = number("after", None) if "after" in query else None
            limit = min(number("limit", LOG_FIRST_LINES), LOG_MAX_LINES) if after is None else \
                min(number("limit", LOG_MAX_LINES), LOG_MAX_LINES)
            lines, last = svc.logs.since(after or 0, limit)
            self._json(200, {"lines": lines, "last": last})

        def _reqlog(self):
            """GET /api/reqlog?after=<id>&max_chars=<n>: the request log's events after that id (serve/util/reqlog.py).
            With the log off the answer is 200 with "enabled": false and no events."""
            query = parse_qs(urlsplit(self.path).query)

            def number(name, default, high):
                try:
                    return min(high, max(0, int(query.get(name, [default])[0])))
                except ValueError:
                    return default
            after = number("after", 0, 1 << 62)
            self._json(200, svc.reqlog.since(after, max(1024, number("max_chars", 2 << 20, REQLOG_CHARS_MAX))))

        def _reqlog_export(self):
            """GET /api/reqlog/export?format=text|jsonl[&mask=secrets|personal][&hide=word,word]: everything the request log holds, as a downloaded
            file. Secrets are always hidden; `personal` also hides e-mail addresses, phone numbers, IBANs and tax codes, and `hide` lists words to hide."""
            query = parse_qs(urlsplit(self.path).query)
            fmt = query.get("format", ["text"])[0]
            if fmt not in ("text", "jsonl"):
                self._json(400, {"error": {"type": "invalid_request_error", "message": "format: text or jsonl"}})
                return
            level = query.get("mask", ["secrets"])[0]
            if level not in MASK_LEVELS:
                self._json(400, {"error": {"type": "invalid_request_error", "message": "mask: " + " or ".join(MASK_LEVELS)}})
                return
            mask = ExportMask(level, clean_words(query.get("hide", [""])[0]), known=[svc.api_key or "", os.environ.get("TADAMOO_API_KEY", "")],
                              vault_mask=Vault(userdata.path("vault")).mask)
            name = f"tadamoo-requests-{time.strftime('%Y%m%d-%H%M%S')}." + ("log" if fmt == "text" else "jsonl")
            self.send_response(200)
            self.send_header("Content-Type", ("text/plain" if fmt == "text" else "application/x-ndjson") + "; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()                                  # the length is not known: the connection ends it
            try:
                for piece in (export_text if fmt == "text" else export_jsonl)(svc.reqlog, mask):
                    self.wfile.write(piece.encode("utf-8"))
            except OSError:
                pass

        def _reqlog_clear(self):
            """POST /api/reqlog/clear: empty the request log."""
            if not self._control_body():
                return
            svc.reqlog.clear()
            self._json(200, {"cleared": True})

        # -- planner -----------------------------------------------------------------------------

        def _plan_service(self):
            return _plans_service(svc, self.server.server_address[1])

        def _plan_skill(self, action, req):
            """POST /api/agent/plan-skill/propose | fill | chat | script: a plan that ended well, made into a personal skill (see tadamoo/personal_skills/from_plan.py)."""
            plan_id = req.get("plan_id")
            if not isinstance(plan_id, str) or not plan_id:
                return self._json(400, {"error": {"message": "send the plan_id"}})
            try:
                plan = self._plan_service().snapshot(plan_id)["plan"]
            except Exception as exc:
                return self._plan_error(exc)
            mgr = self._agent_sessions()
            if action == "propose":
                return self._json(200, mgr.plan_skill_propose(plan, plan_id))
            if action == "fill":
                return self._json(200, mgr.plan_skill_fill(plan, plan_id, req.get("nodes")))
            if action == "chat":
                return self._json(200, mgr.plan_skill_chat(plan, plan_id, req.get("params"), req.get("message")))
            return self._json(200, mgr.plan_skill_script(plan, plan_id, req.get("name"), req.get("description"), req.get("params"), req.get("approved")))

        def _plan_error(self, exc):
            if isinstance(exc, KeyError):
                self._json(404, {"error": {"message": f"not found: {exc.args[0] if exc.args else ''}"}})
            elif isinstance(exc, plans_mod.PlannerUnavailable):
                self._json(501, {"error": {"message": str(exc)}})
            elif isinstance(exc, BadBody):
                self._json(exc.status, {"error": {"message": str(exc)}})
            else:
                self._json(400, {"error": {"message": str(exc)}})

        def _agent_runner(self) -> AgentRunner:
            global _RUNNER
            if _RUNNER is None:
                _RUNNER = AgentRunner(userdata.path("journal"), f"http://127.0.0.1:{self.server.server_address[1]}",
                                      svc.api_key or "", tasks=_TASKS, board=_APPROVALS)
            return _RUNNER

        def _stores(self):
            from tadamoo.attachments import AttachmentStore, OutputStore
            return AttachmentStore(userdata.path("attachments")), OutputStore(userdata.path("outputs"))

        def _attachments_post(self, path):
            """Upload (the file as the request body) or remove an attached file. Only from this PC or a phone with a key."""
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "files can be attached only from the PC the server runs on"}})
            from tadamoo.attachments import MAX_STORED
            store, _ = self._stores()
            try:
                if path.rsplit("/", 1)[-1] == "remove":
                    _, _, session, att_id, _ = path.strip("/").split("/")
                    self._discard_body()
                    store.remove(session, att_id)
                    return self._json(200, {"removed": att_id})
                query = parse_qs(urlsplit(self.path).query)
                session, name = (query.get("session") or [""])[0], (query.get("name") or ["file"])[0]
                self._agent_sessions().get(session)                       # the conversation must exist
                if self._chunked():
                    self.close_connection = True
                    return self._json(411, {"error": {"message": "send the file with its length"}})
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._json(411, {"error": {"message": "send the file with its length"}})
                if length <= 0:
                    return self._json(400, {"error": {"message": "the file is empty"}})
                if length > MAX_STORED:
                    self.close_connection = True
                    return self._json(413, {"error": {"message": f"the file is larger than {_size_text(MAX_STORED)}"}})
                tmp_dir = userdata.path("attachments") / ".incoming"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                tmp = tmp_dir / ("up-" + secrets.token_hex(6))
                got = 0
                with tmp.open("wb") as f:
                    while got < length:
                        chunk = self.rfile.read(min(1 << 20, length - got))
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                if got != length:
                    tmp.unlink(missing_ok=True)
                    self.close_connection = True
                    return self._json(400, {"error": {"message": "the upload stopped before the end"}})
                meta = store.add(session, name, tmp)
                return self._json(200, {"attachment": meta})
            except RunError as e:
                return self._json(404, {"error": {"type": "agent", "message": str(e), "code": e.code}})
            except KeyError:
                return self._json(404, {"error": {"message": "That file is not there any more."}})
            except ValueError as e:
                return self._json(400, {"error": {"message": str(e)}})

        def _files_get(self, path):
            """The list of a conversation's files, a file itself (an image for preview), or a file the agent made (as a download)."""
            store, outputs = self._stores()
            try:
                parts = path.strip("/").split("/")                       # api, attachments|outputs, [session, id, [file]]
                kind = parts[1]
                query = parse_qs(urlsplit(self.path).query)
                if len(parts) == 2:
                    session = (query.get("session") or [""])[0]
                    return self._json(200, {"items": (store if kind == "attachments" else outputs).list(session)})
                if kind == "attachments" and len(parts) == 5 and parts[4] == "file":
                    meta, file = store.get(parts[2], parts[3]), store.path(parts[2], parts[3])
                    types = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}
                    return self._send_file(file, types.get(meta["kind"], "application/octet-stream"), meta["name"], inline=meta["family"] == "image")
                if kind == "outputs" and len(parts) == 5 and parts[4] == "file":              # a picture the agent fetched, shown in the answer
                    meta, file = outputs.get(parts[2], parts[3])
                    shown = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(file.suffix.lower().lstrip("."))
                    if shown is None:
                        raise KeyError(parts[3])
                    return self._send_file(file, shown, meta["name"], inline=True)
                if kind == "outputs" and len(parts) == 4:
                    meta, file = outputs.get(parts[2], parts[3])
                    return self._send_file(file, "application/octet-stream", meta["name"], inline=False)
            except KeyError:
                return self._json(404, {"error": {"message": "That file is not there any more."}})
            self._json(404, {"error": {"message": "not found"}})

        def _send_file(self, file, ctype, name, inline):
            size = file.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename*=UTF-8''" + urllib.parse.quote(name))
            self.send_header("Content-Security-Policy", "sandbox")          # what was attached never runs as a page
            self.send_header("Content-Length", str(size))
            self._cors()
            self.end_headers()
            with file.open("rb") as f:
                while True:
                    block = f.read(1 << 20)
                    if not block:
                        break
                    self.wfile.write(block)

        def _caps(self) -> CapabilityApi:
            global _CAPS
            if _CAPS is None:
                _CAPS = CapabilityApi(userdata.path("capabilities"), userdata.path("chat-workspace"))
            return _CAPS

        def _capabilities_get(self, path):
            try:
                api = self._caps()
                if path == "/api/capabilities/review":
                    return self._json(200, api.review((parse_qs(urlsplit(self.path).query).get("id") or [""])[0]))
                if path == "/api/capabilities/online":
                    return self._json(200, api.online())
                if path == "/api/capabilities/requests":
                    return self._json(200, api.requests())
                return self._json(200, api.overview())
            except CapabilityError as e:
                return self._json(404, {"error": {"type": "capabilities", "message": str(e), "code": e.code}})

        def _capabilities_post(self, path):
            phone_may = path[len("/api/capabilities/"):] in ("settings", "requests/approve", "requests/reject")
            if not (settings.is_loopback(self.client_address[0]) or (phone_may and self._local_or_device())):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "capabilities can be changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 16)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            action = path[len("/api/capabilities/"):]
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api = self._caps()
                if action == "rescan":
                    return self._json(200, api.rescan())
                if action == "move":
                    return self._json(200, api.move(req.get("id"), req.get("node")))
                if action == "settings":
                    return self._json(200, api.settings(req))
                if action == "online":
                    return self._json(200, api.online_settings(req))
                if action == "remove":
                    return self._json(200, api.remove_installed(req.get("id")))
                if action == "requests/approve":
                    return self._json(200, api.approve_request(req.get("id"), req.get("number"), req))
                if action == "requests/reject":
                    return self._json(200, api.reject_request(req.get("id")))
            except CapabilityError as e:
                return self._json(409 if e.code == "gone" else 400, {"error": {"type": "capabilities", "message": str(e), "code": e.code}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        def _side_uses_view(self) -> dict:
            """The technical session and every use of it: the switch, the numbers, the prompt, the work counted so far and the jobs running."""
            info = svc.side_slot_info() if hasattr(svc, "side_slot_info") else None
            stats = svc.side_stats_view() if hasattr(svc, "side_stats_view") else {}
            uses = side_uses.view()
            for row in uses:
                row["stats"] = stats.get(row["id"]) or {"calls": 0, "main": 0, "cut": 0, "cancelled": 0, "seconds": 0.0}
            route = self._technical_route()
            chosen = next((p for p in route["providers"] if p["id"] == route["provider"]), None)
            here = bool(chosen and chosen["runs_here"] and not chosen["this_server"])          # a second small model on this PC: nothing leaves it, so no master switch
            outside = bool(route["provider"]) and route["ok"] and (side_uses.external_on() or here)
            window = min(int((chosen or {}).get("context_window") or 0) or side_uses.EXTERNAL_WINDOW, side_uses.EXTERNAL_WINDOW)
            tokens = info["context"] if info else (window if outside else 0)
            return {"session": {"available": info is not None or outside, "tokens": tokens, "local": info is not None, "on_pc": here and info is None},
                    "external": {"on": side_uses.external_on(), "window": side_uses.EXTERNAL_WINDOW}, "route": route, "master": side_uses.session_on(),
                    "uses": uses, "running": side_uses.running()}

        def _technical_route(self) -> dict:
            """Where the quick jobs run: this PC unless the person pointed the function `technical` at a provider, and the providers to choose from."""
            try:
                view = self._llm().overview()
            except Exception:
                return {"provider": "", "ok": True, "why": "", "consent": False, "needs_consent": False, "providers": []}
            row = next((f for f in view["functions"] if f["id"] == "technical"), {})
            providers = [{"id": p["id"], "name": p.get("name") or p.get("label") or p.get("type") or p["id"], "model": p.get("model") or "",
                          "is_local": bool(p.get("is_local")), "this_server": bool(p.get("this_server")), "runs_here": bool(p.get("runs_here")),
                          "type": p.get("type") or "", "context_window": int(p.get("context_window") or 0)}
                         for p in view["providers"] if not p.get("agent_only") and not p.get("judge_only")]
            return {"provider": row.get("provider") or "", "ok": bool(row.get("ok", True)), "why": row.get("why") or "", "consent": bool(row.get("consent")),
                    "needs_consent": bool(row.get("needs_consent")), "providers": providers}

        def _side_uses_post(self):
            """Changing a use changes what the harness sends and where: only from the PC the server runs on."""
            if not settings.is_loopback(self.client_address[0]) or self.remote_listener:
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "this setting can be changed only from the PC the server runs on"}})
            try:
                req = json.loads(self._body(1 << 16) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if "external" in req:
                    side_uses.set_external(req["external"])
                elif req.get("stop") is True:
                    side_uses.stop_all()
                elif "reset" in req:
                    side_uses.reset(None if req["reset"] == "all" else req["reset"])
                else:
                    side_uses.save(str(req.get("use") or ""), req.get("on"), req.get("params"), req.get("system"))
                self._json(200, self._side_uses_view())
            except BadBody as e:
                self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                self._json(400, {"error": {"message": str(e)}})

        def _billion_context_post(self):
            """The Billion Context setting changes what a conversation sends to the model: only from the PC the server runs on."""
            if not settings.is_loopback(self.client_address[0]) or self.remote_listener:
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "this setting can be changed only from the PC the server runs on"}})
            try:
                req = json.loads(self._body(1 << 14) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                self._json(200, billion_context.set_enabled_port(req))
            except BadBody as e:
                self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                self._json(400, {"error": {"message": str(e)}})

        def _netwatch(self, path, post):
            """The security monitor: its exceptions can hide evidence, so it is shown and changed only on the PC the server runs on."""
            if not settings.is_loopback(self.client_address[0]):
                if post:
                    self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the security monitor is shown only on the PC the server runs on"}})
            try:
                if post:
                    try:
                        raw = self._body(1 << 16)
                    except BadBody as e:
                        return self._json(e.status, {"error": {"message": str(e)}})
                    req = json.loads(raw or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError("send a JSON object")
                    code, obj = netwatch_api.route_post(path, req)
                else:
                    query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                    code, obj = netwatch_api.route_get(path, query)
            except netwatch_api.NetwatchError as e:
                code, obj = netwatch_api.error_view(e)
            except (ValueError, TypeError) as e:
                code, obj = 400, {"error": {"message": str(e)}}
            self._json(code, obj)

        def _rea(self, path, post):
            """The REA switch and its installers: they start programs on this PC, so only the PC the server runs on may use them."""
            if not settings.is_loopback(self.client_address[0]):
                if post:
                    self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "reverse engineering is set up only on the PC the server runs on"}})
            try:
                if post:
                    try:
                        raw = self._body(1 << 16)
                    except BadBody as e:
                        return self._json(e.status, {"error": {"message": str(e)}})
                    req = json.loads(raw or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError("send a JSON object")
                    code, obj = rea_api.route_post(path, req)
                    if code == 200 and _CAPS is not None:
                        _CAPS.rescan()
                else:
                    code, obj = rea_api.route_get(path)
            except (ValueError, TypeError) as e:
                code, obj = 400, {"error": {"message": str(e)}}
            self._json(code, obj)

        def _llamacpp(self, path, post):
            """llama-server in the Loader: it starts a program and downloads files after a confirmation, so only the PC the server runs on may use it."""
            if not settings.is_loopback(self.client_address[0]) or self.remote_listener:
                if post:
                    self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "llama-server is managed only on the PC the server runs on"}})
            try:
                if post:
                    try:
                        raw = self._body(1 << 16)
                    except BadBody as e:
                        return self._json(e.status, {"error": {"message": str(e)}})
                    req = json.loads(raw or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError("send a JSON object")
                    code, obj = llamacpp_api.route_post(path, req)
                else:
                    code, obj = llamacpp_api.route_get(path)
            except (ValueError, TypeError) as e:
                code, obj = 400, {"error": {"message": str(e)}}
            self._json(code, obj)

        def _canary(self, path, post):
            """The exfiltration test: it starts a process and writes a config, so it is shown and changed only on the PC the server runs on."""
            if not settings.is_loopback(self.client_address[0]):
                if post:
                    self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the exfiltration test is shown only on the PC the server runs on"}})
            ctx = canary_api.Context(self.server.server_address[1], svc.api_key, svc.model if svc.loaded() else "")
            try:
                if post:
                    try:
                        raw = self._body(1 << 16)
                    except BadBody as e:
                        return self._json(e.status, {"error": {"message": str(e)}})
                    req = json.loads(raw or b"{}")
                    if not isinstance(req, dict):
                        raise ValueError("send a JSON object")
                    code, obj = canary_api.route_post(path, req, ctx)
                else:
                    query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                    code, obj = canary_api.route_get(path, query, ctx)
            except canary_api.CanaryError as e:
                code, obj = canary_api.error_view(e)
            except (ValueError, TypeError) as e:
                code, obj = 400, {"error": {"message": str(e)}}
            self._json(code, obj)

        def _canary_gui(self, method):
            """The Openweight Canary page (results by level, conversations, catalogs, settings, run): shown only on the PC the server runs on.
            The page itself needs no key; its API does."""
            post = method == "POST"
            if not settings.is_loopback(self.client_address[0]):
                if post:
                    self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the Canary page is shown only on the PC the server runs on"}})
            where = urlsplit(self.path).path
            if where == "/canary":
                self.send_response(308)
                self.send_header("Location", "/canary/" + ("?" + urlsplit(self.path).query if urlsplit(self.path).query else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if where != "/canary/" and not self._authorized():
                if post:
                    self._discard_body()
                return
            body = b""
            if post:
                try:
                    body = self._body(1 << 20)
                except BadBody as e:
                    return self._json(e.status, {"error": {"message": str(e)}})
            ctx = canary_api.Context(self.server.server_address[1], svc.api_key, svc.model if svc.loaded() else "")
            try:
                code, data, ctype, extra = canary_api.gui_response(method, self.path, self.headers, body, ctx)
            except canary_api.CanaryError as e:
                code, obj = canary_api.error_view(e)
                return self._json(code, obj)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _knowledge_post(self, path):
            """The settings of the project notes, the wiki and the reference documents: only from this PC and the page itself."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "these settings can be changed only from this PC or a paired device"}})
            try:
                raw = self._body(1 << 14)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            prefs = KnowledgePrefs(userdata.path("knowledge.json"))
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if path.endswith("wiki-check"):
                    return self._json(200, check_wiki(clean_url(req.get("wiki_url", prefs.read()["wiki_url"]))))
                prefs.write(req)
                return self._json(200, prefs.view())
            except KnowledgeError as e:
                return self._json(400, {"error": {"type": "knowledge", "message": str(e), "field": e.field}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})

        def _intake_post(self):
            """The settings of the look at a request, the questions and the suggestions: only from this PC and the page itself."""
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "these settings can be changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 12)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            prefs = IntakePrefs(userdata.path("intake.json"))
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if req.get("optimize_prompt") is True and svc.side_slot_info() is None:
                    raise IntakeError("Optimize prompt needs the technical session: turn it on in Loader > Technical session, then load the model.", "optimize_prompt")
                prefs.write(req)
                if "intake" in req and _SESSIONS is not None:
                    _SESSIONS.update_intake(prefs.read()["intake"])
                if "optimize_prompt" in req and _SESSIONS is not None:
                    _SESSIONS.update_optimize(prefs.read()["optimize_prompt"])
                return self._json(200, prefs.view())
            except IntakeError as e:
                return self._json(400, {"error": {"type": "intake", "message": str(e), "field": e.field}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})

        def _compaction_post(self):
            """The settings of the summary of older steps: only from this PC and the page itself."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "these settings can be changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 12)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            prefs = CompactionPrefs(userdata.path("compaction.json"))
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                prefs.write(req)
                return self._json(200, prefs.view())
            except CompactionError as e:
                return self._json(400, {"error": {"type": "compaction", "message": str(e), "field": e.field}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})

        def _gate_post(self):
            """The settings of the verify gate and the turn's end: only from this PC and the page itself, since the commands run on this PC."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "these settings can be changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 14)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            prefs = GatePrefs(userdata.path("gate.json"))
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                prefs.write(req)
                return self._json(200, prefs.view())
            except GateError as e:
                return self._json(400, {"error": {"type": "gate", "message": str(e), "field": e.field}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})

        def _coding_get(self):
            """The checks a folder suggests and what the person accepted for it; only this PC, since accepting lets commands run here."""
            if not settings.is_loopback(self.client_address[0]):
                return self._json(403, {"error": {"type": "forbidden", "message": "the project checks are shown only on the PC the server runs on"}})
            asked = (parse_qs(urlsplit(self.path).query).get("workspace") or [""])[0]
            if not asked:
                return self._json(200, {"roles": list(ROLE_IDS), "coding_role": CODING_ROLE})
            try:
                folder = AgentRunner.check_workspace(asked)
                return self._json(200, {**CodingPrefs(userdata.path("coding.json")).view(folder), "verify_gate": GatePrefs(userdata.path("gate.json")).read()["verify_gate"]})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e), "code": getattr(e, "code", "")}})

        def _coding_post(self):
            """Saves what the person accepted for a folder, and tells the conversations running in it."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the project checks can be changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 15)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                folder = AgentRunner.check_workspace(req.get("workspace"))
                view = CodingPrefs(userdata.path("coding.json")).accept(folder, req.get("accepted"), req.get("own"))
                told = self._agent_sessions().update_checks(folder)
                return self._json(200, {**view, "told": told, "verify_gate": GatePrefs(userdata.path("gate.json")).read()["verify_gate"]})
            except CodingError as e:
                return self._json(400, {"error": {"type": "coding", "message": str(e), "field": e.field}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e), "code": getattr(e, "code", "")}})

        def _mcp_auth(self) -> McpAuthApi:
            global _MCP_AUTH
            if _MCP_AUTH is None:
                from tadamoo.llm.credentials import default_store
                from tadamoo.mcp_oauth import OAuthStore
                port = self.server.server_address[1]
                _MCP_AUTH = McpAuthApi(self._caps(), OAuthStore(userdata.path("mcp_oauth.json"), default_store(userdata.user_data_dir())),
                                       lambda: f"http://127.0.0.1:{port}/api/mcp/auth/callback")
            return _MCP_AUTH

        def _mcp_callback(self):
            """The browser comes back from the authorization server with a code. Only this PC; the `state` says which sign-in it is."""
            if not settings.is_loopback(self.client_address[0]):
                return self._json(403, {"error": {"type": "forbidden", "message": "signing in is done only from the PC the server runs on"}})
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            ok, message = self._mcp_auth().callback(query)
            body = result_page(ok, message)
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _voice_error(self, e: Exception):
            if isinstance(e, voice_settings.VoiceSettingsError):
                return self._json(400, {"error": e.body()})
            if isinstance(e, models.Refused):
                return self._json(e.status, e.body)
            if isinstance(e, downloads.DownloadError):
                return self._json(502 if e.code in ("network", "gone") else 400,
                                  {"error": {"type": "download_error", "code": e.code, "message": e.en, "message_it": e.it}})
            return self._json(500, {"error": {"type": "server_error", "message": f"{getattr(e, 'strerror', None) or e}"}})

        def _voice_get(self, path, query):
            try:
                if path == "/api/voice/models":
                    return self._json(200, voice_api.overview())
                if path == "/api/voice/download/status":
                    return self._json(200, voice_api.status())
                if path == "/api/voice/service":
                    voice_service.ensure_token()
                    return self._json(200, voice_service.status())
                if path == "/api/voice/functions":
                    return self._json(200, voice_loader.info(svc))
                if path == "/api/voice/brain":
                    return self._json(200, voice_api.brain_view())
                if path == "/api/voice/telephony":
                    return self._json(200, voice_api.telephony_view())
                return self._json(200, voice_api.plan((query.get("id") or [""])[0]))
            except (voice_settings.VoiceSettingsError, models.Refused, downloads.DownloadError, OSError) as e:
                return self._voice_error(e)

        def _voice_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the voice models are changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 14)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
            except ValueError:
                return self._json(400, {"error": {"type": "invalid_request_error", "message": "the body is not valid JSON"}})
            routes = {"/api/voice/select": voice_api.select, "/api/voice/placement": voice_api.placement,
                      "/api/voice/models-dir": voice_api.set_models_dir, "/api/voice/download": voice_api.download,
                      "/api/voice/download/cancel": lambda _req: voice_api.cancel(), "/api/voice/enabled": voice_api.set_enabled, "/api/voice/brain": voice_api.set_brain, "/api/voice/telephony": voice_api.set_telephony,
                      "/api/voice/functions/plan": lambda r: voice_loader.plan(svc, r.get("desired")),
                      "/api/voice/functions/apply": lambda r: voice_loader.apply(svc, r),
                      "/api/voice/functions/reload-llm": lambda r: voice_loader.reload_llm(svc, r)}
            if path == "/api/voice/service/call":                 # the Voice tab's request to the voice service, from the list in voice_proxy
                status, out = voice_api.service_call(req)
                return self._json(status, out)
            importing = {"/api/voice/import/plan": voice_import.plan, "/api/voice/import/run": voice_import.run}     # the personalities of a callAPIcall folder
            if path in importing:
                try:
                    return self._json(200, importing[path](req, self._personas().store, self._rag_store()))
                except voice_import.Refused as e:
                    return self._json(e.status, {"error": {"type": "voice_import", "message": str(e)}})
            if path not in routes:
                return self._json(404, {"error": {"message": "not found"}})
            try:
                out = routes[path](req)
                self._json(202 if path in ("/api/voice/download", "/api/voice/functions/reload-llm") or out.get("status") == "started" else 200, out)
            except (voice_settings.VoiceSettingsError, models.Refused, downloads.DownloadError, OSError) as e:
                self._voice_error(e)

        def _telegram(self) -> TelegramService:
            return telegram_service(lambda: self._agent_sessions())

        def _telegram_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the Telegram settings are changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 14)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api, action = self._telegram(), path[len("/api/telegram/"):]
                routes = {"settings": lambda: api.set_settings(req), "token": lambda: api.set_token(req.get("token")), "allow": lambda: api.allow(req),
                          "pairing": lambda: api.pairing(req), "import": api.import_hermes, "test": api.test, "send-test": lambda: api.send_test(req.get("text"))}
                if action not in routes:
                    return self._json(404, {"error": {"message": "not found"}})
                return self._json(200, routes[action]())
            except TelegramServiceError as e:
                return self._json(e.status, {"error": {"type": "telegram", "code": e.code, "message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "telegram", "message": str(e)}})

        def _websearch(self) -> WebSearchApi:
            global _WEBSEARCH
            if _WEBSEARCH is None:
                _WEBSEARCH = WebSearchApi()
            return _WEBSEARCH

        def _websearch_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the web search settings are changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 14)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api, action = self._websearch(), path[len("/api/websearch/"):]
                if action == "select":
                    return self._json(200, api.select(req))
                if action == "key":
                    return self._json(200, api.set_key(req.get("provider"), req.get("key")))
                if action == "test":
                    return self._json(200, api.test(req.get("provider")))
                if action == "install":
                    return self._json(200, api.install(req.get("provider")))
                if action == "stop":
                    return self._json(200, api.stop(req.get("provider")))
                if action == "uninstall":
                    return self._json(200, api.uninstall(req.get("provider")))
                return self._json(404, {"error": {"message": "not found"}})
            except WebSearchError as e:
                return self._json(400, {"error": {"type": "websearch", "code": e.code, "message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "websearch", "message": str(e)}})

        def _personas(self) -> PersonasApi:
            global _PERSONAS
            if _PERSONAS is None:
                _PERSONAS = PersonasApi(PersonaStore(userdata.path("personas"), key_dir=userdata.path("vault")), rag=self._rag_store())
            return _PERSONAS

        def _rag_store(self) -> RagStore:
            global _RAG
            if _RAG is None:
                _RAG = RagStore(userdata.path("rag"), forbidden=[userdata.user_data_dir()])
            return _RAG

        def _rag(self) -> RagApi:
            return RagApi(self._rag_store(), self._personas().store)

        def _rag_route(self, path, method):
            """/api/rag/*: the knowledge archives, from this PC only."""
            if not settings.is_loopback(self.client_address[0]):
                if method == "POST":
                    self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the knowledge archives are used only on the PC the server runs on"}})
            parts = [p for p in path[len("/api/rag"):].split("/") if p]
            try:
                if method == "GET":
                    return self._json(200, self._rag().route_get(parts))
                try:
                    raw = self._body(upload_limit(parts) + 4096)
                except BadBody as e:
                    return self._json(e.status, {"error": {"message": str(e)}})
                return self._json(200, self._rag().route_post(parts, parse_qs(urlsplit(self.path).query), raw))
            except RagError as e:
                return self._json(404 if e.code == "not_found" else 400, {"error": {"type": "rag", "code": e.code, "message": str(e)}})
            except (PersonaError, ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "rag", "message": str(e)}})

        def _persona_error(self, e: PersonaError):
            return self._json(404 if e.code == "not_found" else 400, {"error": {"type": "personas", "code": e.code, "message": str(e)}})

        def _personas_get(self, path):
            if not settings.is_loopback(self.client_address[0]):
                return self._json(403, {"error": {"type": "forbidden", "message": "the personalities are shown only on the PC the server runs on"}})
            slug = path[len("/api/personas"):].strip("/")
            q = parse_qs(urlsplit(self.path).query)
            try:
                if not slug:
                    return self._json(200, self._personas().overview())
                return self._json(200, self._personas().get(slug, (q.get("q") or [""])[0][:200], (q.get("kind") or [""])[0], (q.get("section") or [""])[0]))
            except PersonaError as e:
                return self._persona_error(e)

        def _personas_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the personalities are changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 17)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api, parts = self._personas(), [p for p in path[len("/api/personas"):].split("/") if p]
                if not parts:
                    return self._json(200, api.create(req))
                if len(parts) == 2 and parts[1] == "update":
                    return self._json(200, api.update(parts[0], req))
                if len(parts) == 2 and parts[1] == "delete":
                    return self._json(200, api.delete(parts[0], req))
                if len(parts) == 2 and parts[1] == "compose":
                    return self._json(200, api.compose(parts[0], req))
                if len(parts) == 3 and parts[1] == "memory":
                    return self._json(200, api.memory(parts[0], parts[2], req))
                return self._json(404, {"error": {"message": "not found"}})
            except PersonaError as e:
                return self._persona_error(e)
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "personas", "message": str(e)}})

        def _wallet(self) -> WalletApi:
            global _WALLET
            if _WALLET is None:
                _WALLET = WalletApi(lambda: Vault(userdata.path("vault")), lambda: PaymentsBroker(userdata.path("payments"), app_root=Path(__file__).resolve().parents[1], network_time=sntp_offset), BrowserPrefs(userdata.path("browser.json")), userdata.path("browser-profile"),
                                     RuleOfTwoPrefs(userdata.path("rule_of_two.json")), AllowRules(userdata.path("allow_rules.json")),
                                     exec_level.Prefs(userdata.path(exec_level.FILE)))
            return _WALLET

        def _personal(self) -> PersonalMemory:
            global _PERSONAL
            if _PERSONAL is None:
                _PERSONAL = PersonalMemory(userdata.path("personal-memory"), key_dir=userdata.path("vault"))
            return _PERSONAL

        def _memory_worker(self) -> MemoryWorker:
            """Learns from what the person wrote, in the background; it also keeps its queue across restarts, so it starts with the conversations."""
            global _MEM_WORKER
            if _MEM_WORKER is None:
                from tadamoo.client import TadamooClient
                endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
                worker = MemoryWorker(self._personal(), lambda: TadamooClient(base_url=endpoint, api_key=svc.api_key or None),
                                      lambda: gpu_free(svc, mine=worker.extractor.last))
                _MEM_WORKER = worker
                worker.start()
            return _MEM_WORKER

        def _past(self):
            global _PAST
            if _PAST is None:
                from tadamoo.past import PastIndex
                _PAST = PastIndex(userdata.path("memory") / "past.db")
            return _PAST

        def _card_worker(self):
            global _CARDS
            if _CARDS is None:
                from tadamoo.client import TadamooClient
                from serve.card_worker import BACKFILL_QUIET_S, CardWorker
                endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
                worker = CardWorker(self._past(), lambda: TadamooClient(base_url=endpoint, api_key=svc.api_key or None),
                                    lambda: gpu_free(svc, quiet_s=BACKFILL_QUIET_S, mine=worker.last))
                _CARDS = worker
                worker.start()
            return _CARDS

        def _forgetter(self) -> Forgetter:
            return Forgetter(self._past(), self._personal(), userdata.path(MEMORY_EXPORT))

        def _diagram_fix_post(self):
            """POST /api/diagram/fix: the page could not draw a diagram; the source and the library's message come back as a corrected source (empty when none)."""
            try:
                raw = self._body(1 << 15)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                return self._json(400, {"error": {"type": "diagram", "message": "send JSON: {\"source\": ..., \"error\": ...}"}})
            if not isinstance(req, dict) or not isinstance(req.get("source"), str):
                return self._json(400, {"error": {"type": "diagram", "message": "send the diagram's source"}})
            from serve import diagram_fix
            fixed = diagram_fix.fix(self._agent_sessions().model_client(), req["source"], req.get("error"))
            return self._json(200, {"source": fixed or ""})

        def _past_forget_post(self):
            """POST /api/past/forget: the model-only chat page asks for everything taken from its conversation to be forgotten."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "conversations are forgotten only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 12)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                chat_id = str(req.get("id") or "") if isinstance(req, dict) else ""
                if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", chat_id):
                    raise ValueError("send the chat's id")
                return self._json(200, {"forgotten": self._forgetter().forget("chat-" + chat_id)})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "past", "message": str(e)}})

        def _technical_session_post(self):
            """POST /api/technical-session {"on": bool}: the switch of the whole technical session, shown in the chat header."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the switch is changed only from this PC or a paired device"}})
            try:
                req = json.loads(self._body(1 << 12) or b"{}")
                if not isinstance(req, dict) or not isinstance(req.get("on"), bool):
                    raise ValueError("send {\"on\": true} or {\"on\": false}")
                side_uses.set_session(req["on"])
                return self._json(200, {"on": side_uses.session_on()})
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "technical_session", "message": str(e)}})

        def _persistence_post(self, file=persistence.FILE, kind="persistence"):
            """POST /api/persistence {"on": bool}: the "work until done" switch, one choice for every conversation. /api/next-steps is the same
            for the card of next steps after a task."""
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the switch is changed only from the PC the server runs on"}})
            try:
                req = json.loads(self._body(1 << 12) or b"{}")
                if not isinstance(req, dict) or not isinstance(req.get("on"), bool):
                    raise ValueError("send {\"on\": true} or {\"on\": false}")
                return self._json(200, {"on": persistence.Prefs(userdata.path(file)).write(req["on"])})
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": kind, "message": str(e)}})

        def _past_chat_post(self):
            """POST /api/past/chat: the model-only chat page, whose conversation lives in the browser, hands over its turns to be indexed."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the chat is indexed only from the PC the server runs on"}})
            try:
                raw = self._body(8 << 20)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                chat_id = str(req.get("id") or "") if isinstance(req, dict) else ""
                if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", chat_id) or not isinstance(req.get("messages"), list):
                    raise ValueError("send the chat's id and its messages")
                kept = self._past().add_session("chat-" + chat_id, [m for m in req["messages"] if isinstance(m, dict)], title=str(req.get("title") or "")[:80], kind="chat")
                if kept:
                    self._card_worker().touch("chat-" + chat_id)
                return self._json(200, {"indexed": kept})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "past", "message": str(e)}})

        def _memories(self) -> MemoriesApi:
            global _MEMORIES
            if _MEMORIES is None:
                _MEMORIES = MemoriesApi(self._personal(), self._memory_worker(), userdata.path(MEMORY_EXPORT), self._card_worker())
            return _MEMORIES

        def _lessons(self) -> LessonsApi:
            global _LESSONS
            if _LESSONS is None:
                _LESSONS = LessonsApi(lambda: [(p["name"], Path(p["path"])) for p in self._projects().list() if p.get("exists")]
                                      + [("Chat", userdata.path("chat-workspace"))], userdata.path("lessons"))
            return _LESSONS

        def _served_model_info(self):
            """The model this server has loaded, as a benchmark run records it; None when none is loaded."""
            if not svc.loaded():
                return None
            path = getattr(svc.engine, "model_path", "") or ""
            return {"name": svc.model, "file": path, "quantisation": models._quant(Path(path).name) or "" if path else "",
                    "endpoint": f"http://127.0.0.1:{self.server.server_address[1]}"}

        def _gpu_busy_reason(self) -> str:
            """Why the GPU should not be given to a benchmark now ("" when it is free): a conversation, a job of the server, an agent, or another process."""
            if svc.loading:
                return "the model is loading"
            slots = svc.slot_stats()
            if slots["active"] or slots["queue_depth"]:
                return "the server is answering a conversation"
            if models.JOBS.running() or benchrun.JOBS.running():
                return "a conversion or a model test is running"
            if _RUNNER is not None and _RUNNER.view().get("active"):
                return "an agent run is working"
            try:
                util = ((svc.telemetry.snapshot().get("now") or {}).get("gpus") or [{}])[0].get("util")
            except Exception:  # noqa: BLE001 - no telemetry: nothing to add
                util = None
            if isinstance(util, (int, float)) and util >= GPU_BUSY_UTIL_PCT:
                return f"another program is using the GPU ({int(util)}% busy)"
            return ""

        def _benchmarks(self) -> BenchApi:
            global _BENCHMARKS
            if _BENCHMARKS is None:
                _BENCHMARKS = BenchApi(userdata.path("benchmarks"), benchrun.power_limit_w, self._served_model_info, self._gpu_busy_reason, userdata.path("lessons"))
            return _BENCHMARKS

        def _benchmarks_get(self, path):
            if not settings.is_loopback(self.client_address[0]):
                return self._json(403, {"error": {"type": "forbidden", "message": "the benchmarks are shown only on the PC the server runs on"}})
            query = {k: v[0][:200] for k, v in parse_qs(urlsplit(self.path).query).items() if v}
            try:
                return self._json(200, self._benchmarks().get(path[len("/api/bench"):].lstrip("/") or "overview", query))
            except BenchApiError as e:
                return self._json(e.status, {"error": {"type": e.kind, "message": str(e)}})

        def _benchmarks_post(self, what):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the benchmarks are changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 21)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                return self._json(200, self._benchmarks().post(what, req))
            except BenchApiError as e:
                return self._json(e.status, {"error": {"type": e.kind, "message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "bench", "message": str(e)}})

        def _lessons_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the lessons are changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 16)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                return self._json(200, self._lessons().post(path[len("/api/lessons/"):], req))
            except (LessonsError, ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "lessons", "message": str(e)}})

        def _memories_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the memories are changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 16)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                return self._json(200, self._memories().post(path[len("/api/memories/"):], req))
            except (PersonalMemoryError, ValueError, TypeError) as e:
                return self._json(400, {"error": {"type": "memories", "message": str(e)}})

        def _export_post(self):
            """The conversation as a file (docx, md, pdf, html, odt). The body holds the page's snapshot and the person's selection."""
            try:
                raw = self._body(export_api.BODY_MAX)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                local = not self.remote_listener and settings.is_loopback(self.client_address[0])
                out = _EXPORT.run(req, local)
            except PermissionError as e:
                return self._json(403, {"error": {"type": "forbidden", "message": str(e)}})
            except (ExportError, ValueError, TypeError, RecursionError) as e:
                return self._json(400, {"error": {"type": "export", "message": str(e)}})
            if out.token:
                return self._json(200, {"token": out.token, "name": out.name})
            self.send_response(200)
            self.send_header("Content-Type", out.content_type)
            self.send_header("Content-Disposition", export_api.disposition(out.name))
            self.send_header("X-Tadamoo-Filename", urllib.parse.quote(out.name, safe=""))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(out.data)))
            self.end_headers()
            self.wfile.write(out.data)

        def _export_held(self, token):
            """The page the desktop app prints to PDF, once, to this PC only; the token is the permission."""
            held = None
            if not self.remote_listener and settings.is_loopback(self.client_address[0]) and token.isalnum() and len(token) == 32:
                held = _EXPORT.take(token)
            if held is None:
                return self._json(404, {"error": {"message": "not found"}})
            data, ctype, _ = held
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy", export_api.HELD_CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _wallet_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the vault is changed only from the PC the server runs on"}})
            try:
                raw = self._body(10 << 20)                         # a file of logins can be large
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api, action = self._wallet(), path[len("/api/wallet/"):]
                if action == "entry":
                    return self._json(200, {"entry": api.put_entry(req)})
                if action == "reveal":
                    return self._json(200, api.reveal(req))
                if action == "import":
                    return self._json(200, api.import_logins(req))
                if action == "backup":
                    return self._json(200, api.backup(req))
                if action == "restore":
                    return self._json(200, api.restore(req))
                if action == "entry/delete":
                    return self._json(200, api.delete_entry(req.get("id")))
                if action == "system":
                    return self._json(200, api.put_system(req))
                if action == "system/remove":
                    return self._json(200, api.remove_system(req.get("id")))
                if action == "phrase":
                    return self._json(200, api.phrase(req))
                if action == "grant":
                    return self._json(200, api.grant(req))
                if action == "revoke":
                    return self._json(200, api.revoke(req.get("id")))
                if action == "browser":
                    return self._json(200, api.set_browser(req))
                if action == "rule-of-two":
                    return self._json(200, api.set_rule_of_two(req))
                if action == "exec-level":
                    return self._json(200, api.set_exec_level(req))
                if action == "allow-rules":
                    return self._json(200, api.set_allow_rules(req))
                if action == "browser/signin":
                    return self._json(200, api.signin_window(req))
                if action == "browser/forget":
                    return self._json(200, api.forget_signins())
                return self._json(404, {"error": {"message": "not found"}})
            except (WalletError, VaultError, ValueError, TypeError, PermissionError) as e:
                return self._json(400, {"error": {"type": "wallet", "message": str(e)}})

        def _mcp_auth_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "signing in is done only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 16)
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api = self._mcp_auth()
                if path.rsplit("/", 1)[-1] == "start":
                    return self._json(200, api.start(req.get("id")))
                return self._json(200, api.sign_out(req.get("id")))
            except McpAuthError as e:
                return self._json(409 if e.code == "gone" else 400, {"error": {"type": "mcp_auth", "message": str(e), "code": e.code}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})

        def _llm(self) -> LlmApi:
            global _LLM
            if _LLM is None:
                from tadamoo import llm
                _LLM = LlmApi(llm.registry())
                plans_mod_registry = _LLM.registry
                if _PLANS is not None:
                    _PLANS.registry = plans_mod_registry
            return _LLM

        def _llm_post(self, path):
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "providers can be changed only from the PC the server runs on"}})
            try:
                raw = self._body(1 << 16)                              # read first: a refusal must not leave a body unread
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            parts = path[len("/api/llm/"):].split("/")
            try:
                req = json.loads(raw or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api = self._llm()
                if parts == ["providers"]:
                    return self._json(200, {"provider": api.add_provider(req)})
                if len(parts) == 2 and parts[0] == "providers":
                    return self._json(200, {"provider": api.update_provider(parts[1], req)})
                if len(parts) == 3 and parts[0] == "providers" and parts[2] == "remove":
                    return self._json(200, api.remove_provider(parts[1]))
                if len(parts) == 3 and parts[0] == "providers" and parts[2] == "test":
                    return self._json(200, api.test(parts[1]))
                if parts == ["technical"]:
                    return self._json(200, api.setup_technical(req))
                if parts == ["assign"]:
                    return self._json(200, api.assign(req.get("function"), req.get("provider")))
                if parts == ["consent"]:
                    return self._json(200, api.consent(req.get("function"), req.get("allowed")))
                if parts == ["import"]:
                    return self._json(200, api.import_shared(req.get("path")))
                # --- council settings: begin ---
                if parts == ["council"]:
                    return self._json(200, api.set_council(req))
                # --- council settings: end ---
            except LlmError as e:
                return self._json(409 if e.code in ("not_allowed", "gone") else 400, {"error": {"type": "llm", "message": str(e), "code": e.code}})
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        def _routines(self) -> RoutinesApi:
            global _ROUTINES
            sessions = self._agent_sessions()
            if _ROUTINES is None:
                tel = getattr(svc, "telemetry", None)       # the power the GPUs draw, for the energy of each run
                _ROUTINES = RoutinesApi(userdata.path("journal"), sessions,
                                        power_source=(lambda: tel.snapshot()["now"].get("gpus") or []) if tel else None,
                                        plan_paused=lambda plan_id: self._plan_service().is_paused(plan_id),
                                        plan_context=lambda plan_id: self._plan_service().template_context(plan_id))
                _ROUTINES.scheduler.tick_hooks.append(self._watch_plans)
            return _ROUTINES

        def _watch_plans(self, now):
            """The clock of the routines also lets the plans work out their own waits and values; no plan on disk, nothing is made or opened."""
            if _PLANS is None and next((userdata.path("planner") / "plans").glob("*.json"), None) is None:
                return
            self._plan_service().watch_tick(now)

        def _file_owners(self) -> "FileOwners":
            global _FILE_OWNERS
            if _FILE_OWNERS is None:
                _FILE_OWNERS = FileOwners(userdata.path(FILE_OWNERS_FILE))
            return _FILE_OWNERS

        def _file_owners_get(self, path):
            if path == "/api/file-owners":
                return self._json(200, {"files": self._file_owners().list()})
            self._json(404, {"error": {"message": "not found"}})

        def _file_owners_post(self, path):
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "file ownership can be changed only from the PC the server runs on"}})
            store = self._file_owners()
            try:
                req = json.loads(self._body() or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if path == "/api/file-owners/set":
                    owner = self._agent_sessions().resolve(req.get("owner"))
                    if owner is None:
                        return self._json(404, {"error": {"type": "target", "message": f"No conversation is called '{req.get('owner')}'.", "code": "gone"}})
                    return self._json(200, {"file": store.set(req.get("path"), owner, req.get("mode", "owner"))})
                if path == "/api/file-owners/remove":
                    return self._json(200, {"removed": store.remove(req.get("path"))})
            except OwnersError as e:
                return self._json(400, {"error": {"message": str(e), "field": e.field}})
            self._json(404, {"error": {"message": "not found"}})

        def _sessions_base_get(self):
            chosen = userdata.chosen_sessions_base()
            self._json(200, {"current": str(userdata.sessions_base() / "sessions"), "default": str(userdata.path("journal") / "sessions"),
                             "chosen": chosen is not None, "running": _SESSIONS.running_count() if _SESSIONS is not None else 0})

        def _sessions_base_post(self, path):
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the conversations' folder can be changed only from the PC the server runs on"}})
            try:
                req = json.loads(self._body() or b"{}")
                if not isinstance(req, dict) or not isinstance(req.get("path"), str) or not req["path"].strip():
                    raise ValueError("send the full path of the folder in `path`")
            except ValueError as e:
                return self._json(400, {"error": {"message": str(e), "field": "path"}})
            running = _SESSIONS.running_count() if _SESSIONS is not None else 0
            try:
                if path == "/api/sessions-base/check":
                    return self._json(200, sessions_move.inspect(userdata.sessions_base(), Path(req["path"].strip()), running))
                self._json(200, sessions_move.move(userdata.sessions_base(), Path(req["path"].strip()), running))
            except sessions_move.MoveError as e:
                self._json(409, {"error": {"type": "move", "code": e.code, "message": e.message}})

        def _pskills(self) -> PersonalSkillsApi:
            global _PSKILLS
            if _PSKILLS is None:
                _PSKILLS = PersonalSkillsApi(userdata.path("personal-skills"))
            return _PSKILLS

        def _pskills_get(self, path):
            if not self._local_or_device():
                return self._json(403, {"error": {"type": "forbidden", "message": "personal skills can be used only from the PC the server runs on or a phone with a key"}})
            parts = path[len("/api/personal-skills"):].strip("/").split("/")
            try:
                api = self._pskills()
                if parts == [""]:
                    return self._json(200, api.overview())
                if len(parts) == 1:
                    return self._json(200, api.get(parts[0]))
                if len(parts) == 2 and parts[1] == "export":
                    data = api.export(parts[0])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="{parts[0]}.pskill"')
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            except SkillError as e:
                return self._json(400, {"error": {"type": "skill", "message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        def _pskills_post(self, path):
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "personal skills can be changed only from the PC the server runs on or a phone with a key"}})
            parts = path[len("/api/personal-skills"):].strip("/").split("/")
            try:
                api = self._pskills()
                req = json.loads(self._body(1 << 20) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if parts == ["import"]:                                     # the .pskill file, a small zip, in base64
                    return self._json(200, api.import_file(req.get("data")))
                if parts == [""]:
                    return self._json(200, api.save(req))
                if parts == ["check"]:
                    return self._json(200, api.check(req.get("script")))
                if parts == ["preview"]:
                    return self._json(200, api.preview(req.get("params"), req.get("values")))
                if len(parts) == 2 and parts[1] == "delete":
                    return self._json(200, api.delete(parts[0]))
                if len(parts) == 2 and parts[1] == "restore":
                    return self._json(200, api.restore(parts[0], req.get("version")))
            except SkillError as e:
                return self._json(400, {"error": {"type": "skill", "message": str(e)}})
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        def _routines_get(self, path):
            try:
                if path == "/api/routines":
                    return self._json(200, self._routines().overview())
                q = parse_qs(urlsplit(self.path).query)
                return self._json(200, self._routines().runs((q.get("id") or [""])[0], (q.get("limit") or ["50"])[0]))
            except RoutineError as e:
                return self._json(400, {"error": {"type": "routine", "message": str(e), "code": e.code}})

        def _routines_post(self, path):
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "routines can be changed only from the PC the server runs on"}})
            try:
                req = json.loads(self._body(1 << 16) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                api, parts = self._routines(), path[len("/api/routines"):].strip("/").split("/")
                if parts == [""]:
                    return self._json(200, api.create(req))
                if parts == ["cancel"]:
                    return self._json(200, api.cancel())
                if parts == ["stop"]:
                    return self._json(200, api.stop(req))
                if len(parts) == 2:
                    return self._json(200, api.act(parts[0], parts[1], req))
            except RoutineError as e:
                return self._json(404 if e.code == "gone" else 400, {"error": {"type": "routine", "message": str(e), "code": e.code}})
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        def _fs_get(self, path):
            """The files of this PC a phone browses: where to start, what is in a folder, and a file."""
            api = FilesApi(lambda: [(p["name"], Path(p["path"])) for p in self._projects().list() if p.get("exists")]
                           + [("Chat", userdata.path("chat-workspace"))])
            query = parse_qs(urlsplit(self.path).query)
            try:
                if path == "/api/files/roots":
                    return self._json(200, {"roots": api.roots()})
                if path == "/api/files/list":
                    return self._json(200, api.list((query.get("path") or [""])[0]))
                if path == "/api/files/read":
                    file, name = api.file((query.get("path") or [""])[0])
                    return self._send_file(file, "application/octet-stream", name, inline=False)
            except FilesError as e:
                return self._json(404 if e.code in ("not_file", "not_folder") else 400, {"error": {"type": "files", "message": str(e), "code": e.code}})
            self._json(404, {"error": {"message": "not found"}})

        def _remote_view(self) -> dict:
            remote = svc.remote
            tailscale = tailscale_status()
            return {"tailscale": tailscale, "enabled": remote.enabled, "listening": remote.listening, "address": remote.address(tailscale.get("ip") or ""),
                    "listen": remote.listen, "error": remote.error, "keys": svc.keys.list(), "install": _INSTALLER.view(), "port": remote.port,
                    "limits": {"phone_chooses": remote.phone_chooses, "max_mode": remote.max_mode, "rules": remote.rules, "modes": list(MODE_ORDER), "key_days": KEY_TTL_DAYS}}

        def _remote_get(self):
            if not settings.is_loopback(self.client_address[0]):
                return self._json(403, {"error": {"type": "forbidden", "message": "phones are set up only from the PC the server runs on"}})
            self._json(200, self._remote_view())

        def _remote_post(self, path):
            """Set phones up: turn them on or off, make and take away keys, install and connect Tailscale. Only from this PC."""
            if not settings.is_loopback(self.client_address[0]):
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "phones are set up only from the PC the server runs on"}})
            try:
                req = json.loads(self._body(1 << 14) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                action = path[len("/api/remote/"):]
                if action in ("enable", "disable"):
                    svc.remote.set_enabled(action == "enable")
                    return self._json(200, self._remote_view())
                if action == "keys/create":
                    row, key = svc.keys.create(req.get("label"), req.get("days"))
                    return self._json(200, {"key": key, "row": row, **self._remote_view()})
                if action == "keys/renew":
                    row = svc.keys.renew(req.get("id"), req.get("days"))
                    if row is None:
                        return self._json(404, {"error": {"message": "That key is not there any more."}})
                    return self._json(200, {"row": row, **self._remote_view()})
                if action == "listen":
                    svc.remote.set_listen(req.get("listen"))
                    return self._json(200, self._remote_view())
                if action == "limits":
                    svc.remote.set_limits(req.get("max_mode"), req.get("rules"), req.get("phone_chooses"))
                    return self._json(200, self._remote_view())
                if action == "keys/show":
                    found = svc.keys.reveal(req.get("id"))
                    if found is None:
                        return self._json(404, {"error": {"message": "That key cannot be shown again: take it away and make a new one."}})
                    return self._json(200, {"key": found[1], "row": {"label": found[0]}, **self._remote_view()})
                if action == "keys/revoke":
                    if not svc.keys.revoke(req.get("id")):
                        return self._json(404, {"error": {"message": "That key is not there any more."}})
                    return self._json(200, self._remote_view())
                if action == "tailscale/install":
                    return self._json(200, {**self._remote_view(), "install": _INSTALLER.start()})
                if action == "tailscale/up":
                    result = tailscale_up()
                    return self._json(200, {**self._remote_view(), "up": result})
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        def _projects(self) -> ProjectStore:
            global _PROJECTS
            if _PROJECTS is None:
                _PROJECTS = ProjectStore(userdata.path("journal"))
            return _PROJECTS

        def _agent_sessions(self) -> SessionManager:
            global _SESSIONS
            if _SESSIONS is None:
                _SESSIONS = SessionManager(userdata.sessions_base(), f"http://127.0.0.1:{self.server.server_address[1]}",
                                           svc.api_key or "", board=_APPROVALS, default_workspace=userdata.path("chat-workspace"),
                                           file_owners_path=userdata.path(FILE_OWNERS_FILE),
                                           attachments_dir=userdata.path("attachments"), outputs_dir=userdata.path("outputs"),
                                           vault_dir=userdata.path("vault"), payments_dir=userdata.path("payments"),
                                           browser_prefs=BrowserPrefs(userdata.path("browser.json")).read, knowledge_flags=KnowledgePrefs(userdata.path("knowledge.json")).flags,
                                           intake_flags=IntakePrefs(userdata.path("intake.json")).flags,
                                           gate_flags=GatePrefs(userdata.path("gate.json")).flags, coding=CodingPrefs(userdata.path("coding.json")),
                                           compaction_flags=CompactionPrefs(userdata.path("compaction.json")).flags,
                                           rule_of_two=RuleOfTwoPrefs(userdata.path("rule_of_two.json")).read, allow_rules=AllowRules(userdata.path("allow_rules.json")),
                                           rule_of_two_prefs=RuleOfTwoPrefs(userdata.path("rule_of_two.json")),
                                           browser_profile=userdata.path("browser-profile"), plan_effect=self._plan_effect_sink(),
                                           skills_dir=userdata.path("personal-skills"), projects=self._projects(),
                                           personal_dir=userdata.path("personal-memory"), personal_keys=userdata.path("vault"), personas_dir=userdata.path("personas"), rag_dir=userdata.path("rag"), learn=self._memory_worker().offer, past=self._past(),
                                           exec_level_file=userdata.path(exec_level.FILE), persistence_file=userdata.path(persistence.FILE), next_steps_file=userdata.path(nextsteps.FILE), lessons_dir=userdata.path("lessons"), forgetter=self._forgetter(), cards=self._card_worker())
                threading.Thread(target=_SESSIONS.index_all_past, name="past-backfill", daemon=True).start()      # the conversations saved before the index
                self._routines()                          # the clock of the routines starts with the conversations
            return _SESSIONS

        def _targets(self) -> TargetManager:
            global _TARGETS
            mgr = self._agent_sessions()
            if _TARGETS is None or _TARGETS.sessions is not mgr:
                _TARGETS = TargetManager(mgr, _TASKS, lambda: _CHANGES.load().get("edits", []))
            return _TARGETS

        def _plan_effect_sink(self):
            """What the conversations tell the plans: an action done while a plan's step was being worked on is written on the step."""
            service = self._plan_service()

            def note(plan_id, node_id, tool, effect, what):
                service.handle_event(plan_id, {"type": "plan:note_effect", "node_id": node_id, "tool": tool, "effect": effect, "what": what, "actor": "system"})
            return note

        def _agent_get(self, path):
            parts = path[len("/api/agent/"):].split("/")
            try:
                if parts[0] == "traces":
                    return self._traces_get(parts)
                if parts == ["deliveries"]:
                    query = parse_qs(urlsplit(self.path).query)
                    return self._json(200, {"deliveries": self._agent_sessions().deliveries.entries((query.get("state") or [""])[0], (query.get("session") or [""])[0])})
                if parts[0] == "sessions":
                    mgr = self._agent_sessions()
                    if parts == ["sessions"]:
                        return self._json(200, mgr.view())
                    if parts == ["sessions", "tree"]:
                        return self._json(200, mgr.tree())
                    if len(parts) == 3 and parts[2] == "status":
                        return self._json(200, mgr.status(parts[1]))
                    if len(parts) == 3 and parts[2] == "digest":
                        return self._json(200, mgr.digest(parts[1], (parse_qs(urlsplit(self.path).query).get("n") or ["20"])[0]))
                    # --- conversation nodes: begin ---
                    if parts == ["sessions", "nodes"]:
                        return self._json(200, mgr.node_tree())
                    if len(parts) == 3 and parts[2] == "switch-cost":
                        return self._json(200, mgr.switch_cost(parts[1], (parse_qs(urlsplit(self.path).query).get("current") or [""])[0]))
                    # --- conversation nodes: end ---
                    if parts == ["sessions", "compares"]:
                        return self._json(200, mgr.compare_list())
                    if len(parts) == 3 and parts[1] == "compares":
                        return self._json(200, mgr.compare_result(parts[2]))
                    if len(parts) == 3 and parts[2] == "back-impact":
                        return self._json(200, mgr.back_impact(parts[1], int((parse_qs(urlsplit(self.path).query).get("turn", ["0"])[0]) or 0)))
                    if len(parts) >= 3 and parts[2] == "targets":
                        return self._json(200, self._targets().get_route(parts[1], parts[3:]))
                    if len(parts) == 3 and parts[2] == "events":
                        after = int((parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]) or 0)
                        return self._json(200, mgr.events(parts[1], after))
                    if len(parts) == 3 and parts[2] == "history":
                        return self._json(200, mgr.history(parts[1]))
                    if len(parts) == 3 and parts[2] == "file":
                        file, name = mgr.workspace_file(parts[1], (parse_qs(urlsplit(self.path).query).get("path") or [""])[0])
                        return self._send_file(file, "application/octet-stream", name, inline=False)
                    if len(parts) == 3 and parts[2] == "checkpoints":
                        return self._json(200, checkpoints_api.listing(mgr.get(parts[1])))
                    if len(parts) == 3 and parts[2] == "snapshots":
                        return self._json(200, mgr.snapshots(parts[1]))
                    if len(parts) == 3 and parts[2] == "copy":
                        return self._json(200, mgr.copy_changes(parts[1]))
                    if len(parts) == 4 and parts[2] == "copy" and parts[3] == "diff":
                        return self._json(200, mgr.copy_diff(parts[1], (parse_qs(urlsplit(self.path).query).get("path") or [""])[0]))
                    if len(parts) == 3 and parts[2] == "diff":
                        q = parse_qs(urlsplit(self.path).query)
                        return self._json(200, mgr.snapshot_diff(parts[1], (q.get("a") or [""])[0], (q.get("b") or [""])[0]))
                    if len(parts) == 2:
                        return self._json(200, {"session": mgr.get(parts[1])})
                if parts == ["projects"]:
                    return self._json(200, {"projects": self._projects().list()})
                if parts == ["runs"]:
                    return self._json(200, self._agent_runner().view())
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "log":
                    lines = int((parse_qs(urlsplit(self.path).query).get("lines", ["300"])[0]) or 300)
                    return self._json(200, self._agent_runner().log(parts[1], lines))
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "checkpoints":
                    return self._json(200, {"checkpoints": self._agent_runner().checkpoints(parts[1])})
                if parts == ["checkpoint-mode"]:
                    return self._json(200, checkpoints_api.mode_view())
                if parts == ["approvals"]:
                    return self._json(200, _APPROVALS.view())
                if len(parts) == 2 and parts[0] == "approvals":
                    item = _APPROVALS.get(parts[1])
                    if item is None:
                        return self._json(404, {"error": {"message": "That request is not there any more."}})
                    return self._json(200, {"status": item["status"]})
            except TargetError as e:
                return self._json(404, {"error": {"type": "target", "message": str(e), "code": e.code}})
            except RunError as e:
                return self._json(404, {"error": {"type": "agent", "message": str(e), "code": e.code}})
            except ValueError as e:
                return self._json(400, {"error": {"message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        def _agent_post(self, path):
            if not self._local_or_device():
                self._discard_body()
                return self._json(403, {"error": {"type": "forbidden", "message": "the agent can be used only from the PC the server runs on"}})
            parts = path[len("/api/agent/"):].split("/")
            try:
                req = json.loads(self._body() or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if parts[0] == "traces":
                    return self._traces_post(parts, req)
                if parts[0] == "deliveries" and len(parts) == 3 and parts[2] in ("retry", "dismiss"):
                    mgr = self._agent_sessions()
                    return self._json(200, {"delivery": (mgr.retry_delivery if parts[2] == "retry" else mgr.dismiss_delivery)(parts[1])})
                if parts == ["sessions"] or parts == ["runs"]:
                    if not self._phone_limits(req.get("mode", "default"), req.get("allow"), req):
                        return
                if len(parts) == 3 and parts[:1] == ["sessions"] and parts[2] == "mode" and not self._phone_limits(req.get("mode")):
                    return
                if parts == ["place", "forget"]:
                    self._agent_sessions().forget_place()
                    return self._json(200, {"forgotten": True})
                if parts == ["skill-param"]:
                    return self._json(200, self._agent_sessions().skill_param(req.get("action"), req.get("spec"), req.get("known")))
                if len(parts) == 2 and parts[0] == "plan-skill" and parts[1] in ("propose", "fill", "chat", "script"):
                    return self._plan_skill(parts[1], req)
                if parts == ["editor"]:
                    return self._json(200, tab_editor.instruct(self._agent_sessions(), req.get("kind"), req.get("target"), req.get("instruction")))
                if parts == ["projects"]:
                    return self._json(200, {"project": self._projects().add(req.get("path"))})
                if parts == ["projects", "remove"]:
                    return self._json(200, {"removed": self._projects().remove(req.get("path"))})
                if parts[0] == "sessions":
                    mgr = self._agent_sessions()
                    if parts == ["sessions"]:
                        return self._json(200, {"session": mgr.create(req.get("workspace"), req.get("mode", "default"), req.get("allow"),
                                                                      req.get("ask_only") is True, req.get("title", ""),
                                                                      req.get("max_turns", -1),
                                                                      req.get("context_patterns"), req.get("context_policy", "stop"),
                                                                      req.get("context_threshold", 0.8), req.get("end_turn_bias"),
                                                                      req.get("worktree", "auto"), req.get("effort", ""),
                                                                      req.get("images", ""), req.get("charts", ""),
                                                                      req.get("unattended") is True, req.get("role", "general"), req.get("private") is True,
                                                                      req.get("alias", ""), req.get("cross_commands"), req.get("parent", ""), req.get("rules"),
                                                                      req.get("budget_day_tokens"), req.get("persona", ""))})
                    if len(parts) == 4 and parts[2] == "copy" and parts[3] == "apply":
                        return self._json(200, mgr.copy_apply(parts[1], req.get("paths")))
                    if len(parts) == 4 and parts[2] == "copy" and parts[3] == "discard":
                        return self._json(200, {"session": mgr.copy_discard(parts[1])})
                    if len(parts) == 3 and parts[2] == "back":
                        return self._json(200, {"session": mgr.go_back(parts[1], req.get("turn"))})
                    if len(parts) == 3 and parts[2] == "compare":
                        return self._json(200, {"compare": mgr.compare_start(parts[1], req.get("turn"), req.get("variants"))})
                    if len(parts) == 3 and parts[2] == "branch":
                        return self._json(200, {"session": mgr.branch(parts[1], req.get("turn"))})
                    # --- conversation nodes: begin ---
                    if parts == ["sessions", "merge"]:
                        return self._json(200, {"session": mgr.merge(req.get("a"), req.get("b"), req.get("instruction", ""))})
                    # --- conversation nodes: end ---
                    if len(parts) >= 3 and parts[2] == "targets":
                        return self._json(200, self._targets().post_route(parts[1], parts[3:], req))
                    if len(parts) == 3 and parts[2] == "deliver":
                        sender = mgr.sender_of(self.headers.get(TOKEN_HEADER))
                        if sender is None:                                  # only a conversation's own process presents the token the manager gave it
                            return self._json(403, {"error": {"type": "forbidden", "code": "not_a_conversation", "message": "only a conversation can send a message to another"}})
                        return self._json(200, mgr.deliver(parts[1], req.get("message"), sender=sender))
                    if len(parts) == 3 and parts[2] == "alias":
                        return self._json(200, {"session": mgr.set_alias(parts[1], req.get("alias"))})
                    if len(parts) == 3 and parts[2] == "cross-commands":
                        return self._json(200, {"session": mgr.set_cross_commands(parts[1], req.get("senders"))})
                    if len(parts) == 3 and parts[2] == "budget":
                        return self._json(200, {"session": mgr.set_budget(parts[1], req.get("budget_day_tokens"))})
                    if len(parts) == 3 and parts[2] == "send":
                        return self._json(200, mgr.send(parts[1], req.get("text"), req.get("attachments"), req.get("place"), req.get("optimize")))
                    if len(parts) == 3 and parts[2] in ("promote", "drop"):
                        return self._json(200, {"session": getattr(mgr, parts[2])(parts[1], req.get("turn"))})
                    if len(parts) == 3 and parts[2] == "answer":
                        return self._json(200, {"session": mgr.answer(parts[1], req.get("ask"), req.get("value"))})
                    if len(parts) == 3 and parts[2] == "mode":
                        return self._json(200, {"session": mgr.set_mode(parts[1], req.get("mode"))})
                    if len(parts) == 3 and parts[2] == "role":
                        return self._json(200, {"session": mgr.set_role(parts[1], req.get("role"))})
                    if len(parts) == 4 and parts[2] == "checkpoints" and parts[3] == "restore":
                        return self._json(200, checkpoints_api.restore(mgr.get(parts[1]), req.get("checkpoint")))
                    if len(parts) == 4 and parts[2] == "checkpoints" and parts[3] == "delete":
                        return self._json(200, checkpoints_api.delete(mgr.get(parts[1]), req.get("checkpoint"), req.get("all") is True))
                    if len(parts) == 3 and parts[2] == "effort":
                        return self._json(200, {"session": mgr.set_effort(parts[1], req.get("level", ""))})
                    if len(parts) == 3 and parts[2] == "settings":
                        return self._json(200, {"session": mgr.set_settings(parts[1], req.get("max_turns"), req.get("end_turn_bias"),
                                                                           **{k: req[k] for k in ("temperature", "max_tokens") if k in req})})
                    if len(parts) == 3 and parts[2] == "visuals":
                        return self._json(200, {"session": mgr.set_visuals(parts[1], req.get("images", ""), req.get("charts", ""))})
                    if len(parts) == 3 and parts[2] == "persona":
                        return self._json(200, {"session": mgr.set_persona(parts[1], req.get("persona", ""))})
                    if len(parts) == 3 and parts[2] == "telegram":
                        if not settings.is_loopback(self.client_address[0]):
                            return self._json(403, {"error": {"type": "forbidden", "message": "a conversation is moved to Telegram only from the PC the server runs on"}})
                        try:
                            return self._json(200, {"session": self._telegram().move(parts[1], req.get("chat"))})
                        except TelegramServiceError as e:
                            return self._json(e.status, {"error": {"type": "telegram", "code": e.code, "message": str(e)}})
                    if len(parts) == 3 and parts[2] == "rename":
                        return self._json(200, {"session": mgr.rename(parts[1], req.get("title"))})
                    if len(parts) == 3 and parts[2] == "skill-propose":
                        return self._json(200, mgr.skill_propose(parts[1], req.get("turns")))
                    if len(parts) == 3 and parts[2] == "skill-script":
                        return self._json(200, mgr.skill_script(parts[1], req.get("turns"), req.get("name"), req.get("description"), req.get("params")))
                    if len(parts) == 3 and parts[2] == "skill":
                        return self._json(200, mgr.run_skill(parts[1], req.get("slug"), req.get("values")))
                    if len(parts) == 3 and parts[2] == "favorite":
                        return self._json(200, {"session": mgr.favorite(parts[1], req.get("favorite"))})
                    if len(parts) == 3 and parts[2] == "private":
                        return self._json(200, {"session": mgr.set_private(parts[1], req.get("private"))})
                    if len(parts) == 3 and parts[2] == "forget":
                        if not settings.is_loopback(self.client_address[0]):
                            return self._json(403, {"error": {"type": "forbidden", "message": "conversations are forgotten only from the PC the server runs on"}})
                        return self._json(200, mgr.forget(parts[1]))
                    if len(parts) == 3 and parts[2] == "delete":
                        return self._json(200, mgr.delete(parts[1]))
                    if len(parts) == 3 and parts[2] == "command":
                        return self._json(200, mgr.slash(parts[1], req.get("name"), req.get("args", "")))
                    if len(parts) == 3 and parts[2] == "cancel":
                        return self._json(200, {"session": mgr.cancel(parts[1])})
                    if len(parts) == 3 and parts[2] == "resume":
                        return self._json(200, {"session": mgr.resume(parts[1])})
                    if len(parts) == 3 and parts[2] == "close":
                        return self._json(200, {"session": mgr.close(parts[1])})
                if parts == ["runs"]:
                    run = self._agent_runner().start(req.get("workspace"), req.get("goal"), req.get("mode", "auto"), req.get("allow"),
                                                     req.get("max_turns", -1))
                    return self._json(200, {"run": run})
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "stop":
                    return self._json(200, {"run": self._agent_runner().stop(parts[1])})
                if parts == ["checkpoint-mode"]:
                    return self._json(200, checkpoints_api.set_mode(req.get("mode", "")))
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "rewind":
                    return self._json(200, self._agent_runner().rewind(parts[1], req.get("checkpoint")))
                if parts == ["approvals"]:
                    return self._json(200, _APPROVALS.request(req.get("run", ""), req.get("tool"), req.get("arguments", {}), req.get("effect", "")))
                if len(parts) == 2 and parts[0] == "approvals":
                    if req.get("decision") not in ("approve", "deny"):
                        raise ValueError('decision must be "approve" or "deny"')
                    return self._json(200, {"approval": _APPROVALS.decide(parts[1], req["decision"] == "approve")})
            except RunBusy as e:
                return self._json(409, {"error": {"type": "agent", "message": str(e), "code": e.code}})
            except TargetError as e:
                return self._json(404 if e.code == "gone" else 409 if e.code in ("busy", "not_running", "closed", "running", "achieved") else 400,
                                  {"error": {"type": "target", "message": str(e), "code": e.code}})
            except RunError as e:
                return self._json(409 if e.code in ("not_running", "gone", "needs_yes", "no_checkpoint", "not_blocked", "closed", "not_asking", "asking", "busy", "copy_conflict", "alias_taken", "not_dead",
                                   "too_deep", "too_many_children") else 400,
                                  {"error": {"type": "agent", "message": str(e), "code": e.code}})
            except ApprovalError as e:
                return self._json(409 if "already" in str(e) or "not there" in str(e) else 400,
                                  {"error": {"type": "agent", "message": str(e), "code": "approval"}})
            except BadBody as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": {"message": str(e)}})
            self._json(404, {"error": {"message": "not found"}})

        # -- traces of the conversations (serve/traces_api.py): list, masked export, audit ledger, replay -----------

        def _traces_get(self, parts):
            """GET /api/agent/traces, /traces/<id>/audit?limit=<n>, /traces/<id>/export (a JSONL file with secrets masked)."""
            mgr = self._agent_sessions()
            if parts == ["traces"]:
                return self._json(200, traces_api.listing(mgr))
            if len(parts) == 3 and parts[2] == "audit":
                return self._json(200, traces_api.audit(mgr, parts[1], parse_qs(urlsplit(self.path).query).get("limit", [""])[0]))
            if len(parts) == 3 and parts[2] == "export":
                body, name = traces_api.export(mgr, parts[1])
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(404, {"error": {"message": "not found"}})

        def _traces_post(self, parts, req):
            """POST /api/agent/traces/<id>/replay {mode, results}: the conversation again with the recorded answers; nothing runs."""
            if len(parts) == 3 and parts[2] == "replay":
                return self._json(200, traces_api.replay(self._agent_sessions(), parts[1], req.get("mode", "default"), req.get("results") is True))
            self._json(404, {"error": {"message": "not found"}})

        def _plans_get(self, path):
            try:
                service = self._plan_service()
                if path == "/api/planner/settings":
                    return self._json(200, service.settings())
                query = parse_qs(urlsplit(self.path).query)
                if path == "/api/plans":
                    return self._json(200, {"plans": service.list(archived=query.get("archived", ["0"])[0] == "1")})
                if path == "/api/plans/telemetry":
                    return self._json(200, service.telemetry())
                parts = path[len("/api/plans/"):].split("/")
                plan_id, action = parts[0], (parts[1] if len(parts) > 1 else "")
                after = int(query.get("after", ["0"])[0] or 0)
                if action == "":
                    return self._json(200, service.snapshot(plan_id))
                if action == "mermaid":
                    from tadamoo.planner.mermaid import to_mermaid
                    return self._json(200, {"mermaid": to_mermaid(service.snapshot(plan_id)["plan"])})
                if action == "alignment":
                    return self._json(200, service.alignment(plan_id))
                if action == "events":
                    events = service.events_since(plan_id, after)
                    return self._json(200, {"events": events, "seq": events[-1]["seq"] if events else after})
                if action == "stream":
                    return self._plan_stream(service, plan_id, after)
                self._json(404, {"error": {"message": "not found"}})
            except (KeyError, ValueError, plans_mod.PlannerUnavailable) as exc:
                self._plan_error(exc)

        def _plan_stream(self, service, plan_id, after):
            """Server-sent events of one plan: what happened since `after`, then everything as it happens."""
            import queue
            bridge = service.bridge(plan_id)
            inbox = queue.Queue()
            unsubscribe = bridge.subscribe(inbox.put)           # subscribe first so nothing falls between backlog and live
            try:
                self._sse()
                last = after

                def send(event):
                    self.wfile.write(f"id: {event['seq']}\nevent: {event['type']}\ndata: "
                                     f"{json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()

                for event in bridge.events_since(after):
                    send(event)
                    last = event["seq"]
                while True:
                    try:
                        event = inbox.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if event["seq"] > last:
                        send(event)
                        last = event["seq"]
            except (BrokenPipeError, ConnectionError, OSError):
                pass                                            # the viewer went away
            finally:
                unsubscribe()

        def _plans_post(self, path):
            try:
                service = self._plan_service()
                req = json.loads(self._body(1 << 20) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if path == "/api/planner/settings":
                    if not settings.is_loopback(self.client_address[0]):
                        return self._json(403, {"error": {"type": "forbidden",
                                                          "message": "planner settings can be changed only from this PC"}})
                    return self._json(200, service.update_settings(req))
                if path == "/api/plans":
                    return self._json(201, service.create(req))
                if path == "/api/plans/events":
                    return self._json(200, service.ingest(req))
                parts = path[len("/api/plans/"):].split("/")
                plan_id, action = parts[0], (parts[1] if len(parts) > 1 else "")
                if action == "events":
                    result = service.handle_event(plan_id, req)
                    return self._json(200 if result.get("ok") else 400, result)
                if action == "alignment":
                    return self._json(200, service.alignment(plan_id, model=req.get("model") is True))
                if action == "triage":
                    return self._json(200, service.triage(plan_id, req.get("events"), bool(req.get("replan"))))
                if action == "archive":
                    return self._json(200, service.archive(plan_id, service._actor(req)))
                if action == "restore":
                    return self._json(200, service.restore(plan_id, service._actor(req)))
                if action == "duplicate":
                    return self._json(201, service.duplicate(plan_id, service._actor(req), req.get("title")))
                if action == "pause":
                    return self._json(200, service.set_paused(plan_id, service._actor(req), True))
                if action == "resume":
                    return self._json(200, service.set_paused(plan_id, service._actor(req), False))
                if action == "schedule":
                    return self._json(200, service.set_schedule(plan_id, service._actor(req), req.get("schedule") or {}))
                if action == "schedule_remove":
                    return self._json(200, service.remove_schedule(plan_id, service._actor(req)))
                if action == "delete":
                    if not service.delete(plan_id):
                        raise KeyError(plan_id)
                    return self._json(200, {"deleted": plan_id})
                self._json(404, {"error": {"message": "not found"}})
            except json.JSONDecodeError as exc:
                self._json(400, {"error": {"message": f"invalid JSON: {exc.msg}"}})
            except (KeyError, ValueError, BadBody, plans_mod.PlannerUnavailable) as exc:
                self._plan_error(exc)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")   # proxies pass each event at once
            self._cors()
            self.end_headers()

        def _openai(self, req):
            if req.get("long_answer"):
                return self._openai_long(req)
            messages, tools, kw = openai_to_messages(req)
            tools, force = forced_call(req.get("tool_choice"), tools)   # a bad choice is a 400 before streaming
            check_format(req.get("response_format"))
            retries = structured_retries(req)                 # 0: the engine's grammar alone; tries only when a request asks
            validator = json_validator(req.get("response_format")) if retries else None
            trace = self.trace
            svc.require_grammar(req.get("response_format"))
            if check_format(req.get("response_format")):
                req["relaxed_drafting"] = False                # JSON output is sampled exactly
            svc.load()
            add_end_turn(req, svc.tok.encode(IM_END, parse_special=True))
            req[BIAS_KEY] = checked_bias(req, svc.engine)     # a bad logit_bias is a 400 before streaming
            req[GRAMMAR_KEY] = svc.grammar_for(tools, req.get("tool_choice"), req.get("tools"),
                                               req.get("response_format"))
            asked = int(req.get("max_completion_tokens") or req.get("max_tokens") or 0)   # 0/-1: the rest of the context
            svc.reasoning_budget(req)                         # a bad value is a 400 before streaming
            checked_sampling(req)                             # likewise a sampling value out of range
            svc.answer_cap(req)                               # likewise a bad answer cap
            stop_strings(req)                                 # likewise a bad stop
            check_cache_slot(req)                             # likewise a bad cache_slot
            ids, thinking, max_new = svc.prepare(messages, tools, kw, asked, force=force)
            _debug_req("openai", req, messages, tools, max_new, thinking, len(ids))
            cancel = threading.Event()
            self._watch_client(cancel)
            if validator is None:
                chunks = primed(openai_chunks(svc, req, ids, thinking, tools, max_new, cancel, force=force), ids)
            else:                                             # validated, and tried again with the error fed back
                def start(feedback):
                    if trace is not None:
                        trace.attempt = (trace.attempt or 0) + 1
                    if feedback is None:
                        return primed(openai_chunks(svc, req, ids, thinking, tools, max_new, cancel, force=force), ids)
                    again = retry_messages(messages, *feedback)
                    ids2, thinking2, max_new2 = svc.prepare(again, tools, kw, asked, force=force)
                    return primed(openai_chunks(svc, req, ids2, thinking2, tools, max_new2, cancel, force=force), ids2)
                chunks = structured_retry_chunks(start, validator, retries, trace.note if trace is not None else None,
                                                 constrained=bool(req.get(GRAMMAR_KEY)))
            if not req.get("stream"):
                return self._json(200, openai_collect(chunks))
            self._send_chunks(chunks, cancel)

        def _openai_long(self, req):
            """A chat completion with `long_answer`: written in segments (serve/longform.py), answered as the others are."""
            spec, system, prior = long_request(req)
            svc.load()
            cancel = threading.Event()
            self._watch_client(cancel)
            chunks = long_answer_chunks(svc, req, spec, system, prior, cancel)
            if not req.get("stream"):
                return self._json(200, long_collect(chunks))
            self._send_chunks(chunks, cancel)

        def _send_chunks(self, chunks, cancel):
            """The chunks of a streamed chat completion as server-sent events; an error after the start is told in the stream."""
            self._sse()
            try:
                for c in chunks:
                    if c is None:
                        self.wfile.write(b": keep-alive\n\n")      # SSE comment
                    else:
                        self.wfile.write(b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
            except OSError:
                cancel.set()                                 # client left: stop the engine
                chunks.close()
                self._left()
            except EngineDied as e:                          # mid-stream
                err = {"error": {"type": "server_error", "message": f"{e}; the next request restarts it"}}
                self._stream_failed(err, b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")
            except StructuredOutputError as e:
                err = {"error": {"type": "structured_output_failed", "code": "structured_output_failed", "message": str(e)}}
                self._stream_failed(err, b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")
            except ValueError as e:                          # engine ERR after the stream started: no 400 now
                err = {"error": {"type": "server_error", "message": str(e)}}
                self._stream_failed(err, b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")

        def _left(self) -> None:
            """The client closed the connection while the answer was being sent."""
            if self.trace is not None:
                self.trace.cancel("the client closed the connection")

        def _stream_failed(self, body: dict, wire: bytes) -> None:
            """An error after the stream started: told to the client in the stream, and to the request log."""
            if self.trace is not None:
                self.trace.error_body(None, body)
            self.wfile.write(wire)

        def _responses(self, req):
            """POST /v1/responses (serve/api/responses.py); after the stream starts, errors are response.failed."""
            try:
                ids, thinking, tools, max_new, asm, req, force = self._responses_prepare(req)
            except ResponsesError as e:
                return self._json(e.status, e.body())
            except ModelBusy as e:
                return self._json(409, responses_error_body(str(e), "server_error", code="model_busy"))
            except (GpuBusy, EngineStarting, EngineStuck, EngineDied) as e:
                return self._json(503, responses_error_body(str(e), "server_error", code="server_error"))
            cancel = threading.Event()
            self._watch_client(cancel)

            def events():
                yield from asm.start()
                done = None
                for kind, x in svc.run(ids, thinking, tools, max_new, req, cancel, force=force):
                    if kind == "ping":
                        yield None
                    elif kind == "event":
                        yield from asm.feed(x)
                    elif kind == "done":
                        done = x
                if done is not None and done["finish"] != "cancel" and not cancel.is_set():
                    yield from asm.finish(done)
            try:
                items = primed(events(), ids)
            except EngineError:
                raise
            except ValueError as e:                          # too long for the context: found when the engine counted
                err = ResponsesError(str(e), "input", "context_length_exceeded" if "context" in str(e) else None)
                return self._json(err.status, err.body())
            if not req.get("stream"):
                try:
                    result = responses_api.collect(items)
                except EngineDied as e:
                    return self._json(503, responses_error_body(f"{e}; the next request restarts it", "server_error",
                                                                code="server_error"))
                except EngineError as e:                     # the engine failed
                    return self._json(500, responses_error_body(str(e), "server_error", code="server_error"))
                except ValueError as e:                      # the engine refused the request
                    err = ResponsesError(str(e), "input", "context_length_exceeded" if "context" in str(e) else None)
                    return self._json(err.status, err.body())
                return self._json(200, result)
            self._sse()
            last = time.monotonic()

            def send(e):
                self.wfile.write(f"event: {e['type']}\n".encode() + b"data: " +
                                 json.dumps(e, ensure_ascii=False).encode() + b"\n\n")

            try:
                for e in items:
                    if e is None:
                        # clients' idle timers count events, not SSE comments: send in_progress every 15 s
                        if time.monotonic() - last >= 15:
                            send(asm.in_progress())
                            last = time.monotonic()
                        else:
                            self.wfile.write(b": keep-alive\n\n")
                    else:
                        send(e)
                        last = time.monotonic()
                    self.wfile.flush()
            except OSError:
                cancel.set()                                 # client left: stop the engine
                items.close()
                self._left()
            except EngineDied as e:                          # mid-stream: response.failed
                self._responses_failed(asm, f"{e}; the next request restarts it", "server_error", send)
            except ValueError as e:                          # engine ERR after the stream started
                self._responses_failed(asm, str(e), "server_error", send)

        def _responses_failed(self, asm, message, code, send):
            e = asm.failed(message, code)
            if self.trace is not None:
                self.trace.error(code, message)
            try:
                send(e)
                self.wfile.flush()
            except OSError:
                pass

        def _responses_prepare(self, req):
            responses_api.check_request(req)
            messages = responses_api.input_messages(req)
            tools, names, skipped = responses_api.request_tools(req)
            try:
                tools, force = forced_call(req.get("tool_choice"), tools)
            except ValueError as e:
                raise ResponsesError(str(e), "tool_choice") from None
            kw = responses_api.template_kwargs(req)
            try:
                check_format(responses_api.text_format(req))
            except ValueError as e:
                raise ResponsesError(str(e).replace("response_format", "text.format"), "text.format") from None
            try:
                svc.require_grammar(responses_api.text_format(req))
            except ValueError as e:
                raise ResponsesError(str(e).replace("response_format", "text.format"), "text.format") from None
            req[GRAMMAR_KEY] = svc.grammar_for(tools, req.get("tool_choice"), req.get("tools"),
                                               responses_api.text_format(req))
            noted = svc.__dict__.setdefault("hosted_tools_noted", set())   # once per tool
            if set(skipped) - noted:
                print(f"[server] warning: /v1/responses ignores hosted tools {', '.join(sorted(set(skipped) - noted))}", flush=True)
                noted.update(skipped)
            try:
                svc.reasoning_budget(req)                    # a bad value is a 400 before streaming
            except ValueError as e:
                raise ResponsesError(str(e), "reasoning_budget_tokens") from None
            try:
                checked_sampling(req)
            except ValueError as e:
                raise ResponsesError(str(e), str(e).split(":")[0]) from None
            try:
                stop_strings(req)
            except ValueError as e:
                raise ResponsesError(str(e), "stop") from None
            try:
                check_cache_slot(req)
            except ValueError as e:
                raise ResponsesError(str(e), "cache_slot") from None
            svc.load()
            try:
                ids, thinking, max_new = svc.prepare(messages, tools, kw, req.get("max_output_tokens") or 0, force=force)
            except ResponsesError:
                raise
            except ValueError as e:                          # too long for the context, or an image without vision
                raise ResponsesError(str(e), "input", "context_length_exceeded" if "context" in str(e) else None) \
                    from None
            _debug_req("responses", req, messages, tools, max_new, thinking, len(ids))
            include = req.get("include") if isinstance(req.get("include"), list) else []
            asm = responses_api.Assembler(req, svc.model, len(ids), names,
                                          "reasoning.encrypted_content" in include)
            return ids, thinking, tools, max_new, asm, req, force

        def _count_tokens(self, req):
            """Anthropic's token count: the rendered, tokenized prompt, without running the model."""
            messages, tools, kw = anthropic_to_messages(req, svc.anthropic_think_unasked)
            tools, _ = forced_call(req.get("tool_choice"), tools)   # same 400 as the request itself ("none": no tools)
            stop_strings(req)                                 # same 400 as the request itself
            self._json(200, {"input_tokens": len(svc.encode_prompt(messages, tools, kw))})

        def _anthropic(self, req):
            svc.load()
            messages, tools, kw = anthropic_to_messages(req, svc.anthropic_think_unasked)
            tools, force = forced_call(req.get("tool_choice"), tools)   # "any" / {"type": "tool", "name": N}
            max_new = int(req.get("max_tokens") or 0)                  # 0/-1: the rest of the context
            response_format = anthropic_format(req)            # structured outputs
            try:
                svc.require_grammar(response_format)
            except ValueError as e:
                raise ValueError(str(e).replace("response_format json_schema and json_object", "output_config.format json_schema")) from None
            req[GRAMMAR_KEY] = svc.grammar_for(tools, req.get("tool_choice"), req.get("tools"), response_format)
            svc.reasoning_budget(req)                         # a bad value is a 400 before streaming
            checked_sampling(req)                             # likewise a sampling value out of range
            stop_strings(req)                                 # likewise a bad stop
            check_cache_slot(req)                             # likewise a bad cache_slot
            ids, thinking, max_new = svc.prepare(messages, tools, kw, max_new, force=force)
            _debug_req("anthropic", req, messages, tools, max_new, thinking, len(ids))
            cancel = threading.Event()
            self._watch_client(cancel)
            events = primed(anthropic_events(svc, req, ids, thinking, tools, max_new, cancel, force=force), ids)
            if not req.get("stream"):
                return self._json(200, anthropic_collect(events))
            self._sse()
            try:
                for item in events:
                    if item is None:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        name, e = item
                        self.wfile.write(f"event: {name}\n".encode() + b"data: " +
                                         json.dumps(e, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
            except OSError:
                cancel.set()
                events.close()
                self._left()
            except EngineDied as e:                          # mid-stream: an Anthropic error event
                err = {"type": "error", "error": {"type": "api_error", "message": f"{e}; the next request restarts it"}}
                self._stream_failed(err, b"event: error\ndata: " + json.dumps(err).encode() + b"\n\n")
            except ValueError as e:                          # engine ERR after the stream started
                err = {"type": "error", "error": {"type": "api_error", "message": str(e)}}
                self._stream_failed(err, b"event: error\ndata: " + json.dumps(err).encode() + b"\n\n")

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is already serving, and requests then land on
    # either one (a forgotten second start).  Without it the second start fails loudly instead.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 256      # socketserver's backlog of 5 resets some clients of a burst of simultaneous connections

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):   # a hang-up needs no stack trace
            super().handle_error(request, client_address)


def _listen(svc: Service, host: str, port: int, remote: bool = False) -> ThreadingHTTPServer:
    httpd = Server((host, port), make_handler(svc, remote))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def serve(svc: Service, host="127.0.0.1", port=7232) -> ThreadingHTTPServer:
    svc.start_telemetry()
    httpd = _listen(svc, host, port)
    threading.Thread(target=_start_planner, args=(svc, httpd.server_address[1]), daemon=True, name="planner-start").start()
    threading.Thread(target=netwatch_api.start_if_enabled, daemon=True, name="netwatch-start").start()
    threading.Thread(target=_start_telegram, args=(httpd,), daemon=True, name="telegram-start").start()
    threading.Thread(target=rea_api.start_if_enabled, args=(lambda: _CAPS.rescan() if _CAPS is not None else None,), daemon=True, name="rea-start").start()
    threading.Thread(target=canary_api.reattach, daemon=True, name="canary-reattach").start()
    if getattr(svc, "keys", None) is None:
        svc.keys = ApiKeys(userdata.path("api-keys.json"), key_dir=userdata.path("vault"))
    if getattr(svc, "remote", None) is None:
        svc.remote = RemoteAccess(userdata.path("remote.json"), httpd.server_address[1], lambda ip, p: _listen(svc, ip, p, remote=True))
        threading.Thread(target=svc.remote.apply, daemon=True, name="remote-listener").start()      # asking Tailscale for its address can take a moment
    return httpd
