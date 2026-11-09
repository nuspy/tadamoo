# Voice stack: security review

Scope: the voice stack on `feature/voice-implementation` compared with `master`, read from the committed HEAD (a clean export).
Nothing was run. Line numbers refer to HEAD. "Default config" means no `--api-key` and no `--control-secret`, which are both off by default
(`serve/config.py:484-490`).

## Top 5

1. **V-01 (Critical)**: in the default config, `/api/*` writes check only that the client is on loopback. A page of any website open in the
   person's browser can send simple cross-site POSTs to `127.0.0.1:7232`. The voice branch puts several high-impact actions behind that one
   check: the engines' Python path, the Telegram bot token, its allow-list and its permission level, meeting joins, and the models folder.
2. **V-02 (High)**: two HTTP routes store an arbitrary executable path as "the engines' Python", and another route runs it. Combined with V-01,
   this lets a website run a program on the PC.
3. **V-03 (High)**: the gateway WebSocket relay has no `Origin` check. For a loopback client with no key set, it adds the service token on the
   way through, so any website can open a fully authenticated gateway session and talk to the brain (harness tools at the configured level, and
   the personality's recalled notes).
4. **V-04 (High)**: through V-01, Telegram can be taken over: a website can set its own bot token, its own user id in the allow-list, and
   `mode: auto`. That gives a remote agent with tools.
5. **V-05 (Medium)**: Hugging Face voice sources are not pinned (`revision: "main"`, whole repositories). The SHA-256 is whatever HF reports at
   plan time, and some files are pickle checkpoints loaded with `torch.load` (once with `weights_only=False`).

## Findings

### V-01 Critical: cross-site requests reach the voice and Telegram writes in the default config
- Where: `serve/handler.py:342-361` (`_authorized` returns True when no API key is set), `:799` (`_write_secret` is skipped when
  `control_token` is empty), `:2437` `_voice_post`, `:2476` `_telegram_post`, `_personas_post` and `_rag_route`. The only gate is
  `settings.is_loopback`. There is no `Origin`, `Sec-Fetch-Site`, `Host` or `Content-Type` check; the body goes through `json.loads`
  whatever its type. `serve/tests/test_security.py` asserts on purpose that any `Host` is answered.
- Abuse: the person's browser is on loopback, so a web page they visit can send a `text/plain` POST, which needs no preflight. It cannot read
  the answer, but the change is made. The routes it reaches include `/api/voice/service/call` (the whole `voice_proxy` list: meeting join,
  calendar autojoin, engines' Python, training), `/api/voice/models-dir`, `/api/voice/meetings` (the autojoin master switch),
  `/api/telegram/*` and `/api/personas/*`. DNS rebinding also works, because `Host` is not checked: it gives same-origin access, so the page
  can read answers as well (for example pending call approvals and then approve them). Chromium's Local Network Access prompt reduces this in
  recent Chrome and Edge, but other browsers and older builds do not have it.
- Note: the trade-off is documented (`docs/WINDOWS.md:219-224`), but the branch adds routes where it leads to code execution.
- Fix: for every `/api/*` write (and every `/api/voice/*` request), refuse a request whose `Origin` is present and not the server's own, or
  whose `Sec-Fetch-Site` is `cross-site`. Refuse a `Host` that is not a loopback name unless a key was presented. That is the check already in
  `serve/voice_compat.py:142-148`; move it into the handler. Alternatively, make `--control-secret` the default.
- Test: none for `/api/voice/*` or `/api/telegram/*` with a foreign `Origin` or `Host`.

### V-02 High: an HTTP-settable executable path, then run by a probe or check route
- Where: `serve/voice_proxy.py:101-105` `_train_python` and `:122-126` `_python_path` (they accept any string without control characters, up
  to 1000 or 520 characters), routed at `:199,203`. Service side: `voice/avatar_engines/setup_routes.py:74-84` and `pack.py:71-80`;
  `voice/avatar_training.py:280` and `avatar_engines/personalised.py:183-193`. The only check is "absolute path and the file exists".
  `personalised.py:215` runs it (`[python, "-c", PROBE_CODE, ...]`) from `POST /avatar/train/setup {probe:true}`, which needs no model. The
  check route (`setup_routes.py:46-72`) and training run it too.
- Abuse: with V-01 or DNS rebinding, a website stores a path to a program it controls (on Windows, a UNC path passes `is_file`) and then
  triggers the probe. The result is code execution as the user.
- Fix: do not take an executable path over HTTP at all; use the environment variable or a native file picker in the desktop app. If the
  route must stay, accept only a local drive (no UNC or `\\?\`), require the file name `python.exe` or `python`, keep the path only after the
  person confirms it in the UI, and gate the route behind V-01's fix.
- Test: `serve/tests/test_voice_avatar_proxy.py` checks the shape of the body only. No test refuses a UNC path or a non-Python file.

### V-03 High: cross-site WebSocket hijacking of the voice gateway
- Where: `serve/handler.py:661-667`. A loopback client passes `_authorized` (no key) and `_local_or_device`, and `still_allowed=None`.
  `serve/voice_gateway_relay.py:62-86` then adds `Authorization: Bearer <service token>` upstream. No `Origin` header is checked or forwarded.
  `voice/callbridge/voice_gateway.py:245` treats the handshake as proof (`self.authed`).
- Abuse: browsers do not apply CORS to WebSockets. Any website can open `ws://127.0.0.1:7232/api/voice/gateway/ws`, send `auth` and then `text`
  turns, switch `brain` to `harness` and choose any `persona` slug. A harness turn runs at `brain_permission`, which can be `auto`
  (`voice/callbridge/brains.py:189-191`). A direct turn returns the personality's recalled notes and archive passages. The turns also feed
  persona learning, which can poison the profile (`tadamoo/persona_learning.py`). Four open sockets (`MAX_CLIENTS`) also lock real phones out.
- Fix: refuse the upgrade unless `Origin` is absent or same-origin (or the client came through the phone listener with a key), and require a
  key or the control secret for the loopback case too. Add an idle timeout.
- Test: `serve/tests/test_voice_gateway_relay.py` has no `Origin` case.

### V-04 High: Telegram takeover through V-01
- Where: `serve/telegram_service.py:198-230`: `set_settings` takes `enabled`, `mode` (including `auto`) and `allowed` in one request, and
  `set_token` restarts the bot. Routes are at `serve/handler.py:2476-2497`.
- Abuse: cross-site POSTs set a bot token the attacker owns, their user id and `mode: auto`. The attacker's messages are then the person's own
  prompts (`tadamoo/telegram_bot.py:8`), at `auto`.
- Fix: V-01's fix. In addition, accept a token change, an allow-list addition or a raised level only with a confirmation shown on the PC
  (the pairing flow already exists).
- Test: `serve/tests/test_telegram_api.py:394` checks that the token is never shown, but nothing covers a cross-site write.

### V-05 Medium: unpinned Hugging Face sources and pickle loading
- Where: `serve/voice_catalog.py:102-104` (`revision: "main"`, `files=None` means the whole repository) and `serve/downloads.py`
  `_source_files`. The SHA-256 is HF's own value, read at plan time; `if f["sha256"]:` (`:670`) skips the check for non-LFS files.
  `voice/callbridge/engines/catalog.py:257` calls `torch.load(speakers_xtts.pth, weights_only=False)` when the voices are listed. Many
  `torch.load` calls without `weights_only` are in `voice/avatar/musetalk/**` and `voice/avatar_engines/vendor/**`, and `pickle.load` is used
  on cache files (`voice/avatar/avatar_worker.py:497-500`).
- Abuse: a change to, or compromise of, an upstream repository is downloaded and "verified" against its own hash, and then a pickle runs
  inside the voice service.
- Fix: pin each HF source to a commit with a fixed per-file SHA-256 table, as the engine packs already are (`_pack`). Use
  `weights_only=True` where a state dict is expected (the XTTS speakers file is a dict of tensors).
- Test: `serve/tests/test_voice_api.py:274` asserts that the commit is resolved; none asserts a pin.

### V-06 Medium: phone pairing token and the full environment passed to engine subprocesses
- Where: `voice/callbridge/models_store.py:178-185` `helper_env` copies all of `os.environ`, which includes `TADAMOO_PHONE_TOKEN`
  (`voice/service.py:36,139`) and any `HF_TOKEN`. `personalised.py:215` passes `{**os.environ}` to the user-chosen Python.
- Abuse: vendored third-party scripts, or the Python chosen in V-02, read the dialer's pairing token.
- Fix: build child environments from an allow-list (PATH, SYSTEMROOT, TEMP, CUDA_*, the offline variables, the models root).
- Test: none.

### V-07 Medium: meeting join and calendar autojoin depend on client booleans
- Where: `serve/voice_proxy.py:149-163` and `voice/meetings_service.py:132`. `rights_confirmed: true` is a field any caller sets. The
  autojoin master switch is written by `POST /api/voice/meetings`.
- Abuse: through V-01, the PC joins an attacker's Meet, Zoom or Teams meeting, with the person's display name and `on_behalf_of` and with
  STT listening. The platform allow-list (`adapter_for`) limits the targets.
- Fix: V-01's fix. Turning on the master switch should need a confirmation shown on the PC.
- Test: the shape of the body only.

### V-08 Medium: Android device key in plain SharedPreferences and in cloud backup
- Where: `mobile/lib/src/settings.dart:15,55` (`_prefs.setString('key', ...)`).
  `mobile/android/app/src/main/res/xml/data_extraction_rules.xml` and `backup_rules.xml` explicitly include `FlutterSharedPreferences.xml` in
  cloud backup and device transfer; `allowBackup="true"`. This predates the branch, but the key now also opens the voice gateway.
- Abuse: anyone with access to the Google backup, or to a rooted device or an adb backup, gets a key that reaches the PC over Tailscale.
- Fix: store the key with `flutter_secure_storage` (Keystore) and leave it out of backups.
- Test: none.

### V-09 Low: the voice service compares its token in non-constant time
- Where: `voice/callbridge/control_server.py:101-104` (`==`). The gateway (`voice/gateway.py:113`) and `serve/voice_service.py`
  `accepts_token` use `hmac.compare_digest`. The service binds to loopback only (`voice/service.py:190-192`), and `/health` is open.
- Fix: use `hmac.compare_digest`. Test: none.

### V-10 Low: `telegram_text_bridge` is an unauthenticated agent entry point
- Where: `voice/callbridge/telegram_text_bridge.py:60-70,119-124`. `POST /telegram/message` on `127.0.0.1:8790` has no token, and it uses
  `make_brain(cfg)`, which follows `brain_mode` and so may be the harness. It is started by hand only. Flask's `get_json` needs a JSON
  content type, so a simple cross-site POST does not parse; any local process can call it.
- Fix: delete it (the Telegram path is now `tadamoo/telegram_bot.py`), or require a token and force the direct brain.

### V-11 Low: no block on premium-rate numbers, and a narrow emergency rule
- Where: `voice/callbridge/call_policy.py:61-75,288-333`. With `outgoing_approval: auto`, a `program` caller can dial any 6-15 digit number,
  including premium-rate prefixes. National forms outside the fixed set are not recognised as emergency numbers. Through the TaDaMoo port,
  `X-Call-Origin` is not forwarded (`serve/voice_compat.py:227-233`), so a program stays `program`. That part is correct.
- Fix: add a configurable deny-list of premium prefixes, and let `auto` apply only to numbers in the contacts.
- Test: `voice/tests/test_call_policy.py` covers emergency numbers; it has no premium case.

### V-12 Low: the service token is masked only in text answers
- Where: `serve/voice_compat.py:258-283` streams binary answers without masking. The service never puts the token in a binary answer
  today. Fix: none needed now; keep it in mind for new routes.

### V-13 Info: provenance of the vendored code
- Ultralight and FeatherTalk: per-file SHA-256 is checked by `voice/tests/test_avatar_engine_vendor.py`. MuseTalk
  (`voice/avatar/musetalk/VENDORED.md`) names a commit but has no hash table or test, and is edited in place. Its scripts build shell
  commands with `os.system` (`scripts/inference.py:122-123`, `realtime_inference.py:297-303`); the app does not call them.
- `rtx_video_path` (`serve/voice_settings.py:378-380`) is an HTTP-settable DLL path that nothing loads yet; once it is loaded, it becomes V-02.

## Claims in docs/voice checked
- "the token never leaves the server": holds for `voice_proxy`, `voice_compat` (textual answers) and the relay.
- "these routes answer this PC only / a page of another site may not use them" (`docs/voice/api.md:16`): true for the `voice_compat`
  routes only. It does not hold for `/api/voice/*`, `/api/telegram/*` or the gateway WebSocket (V-01, V-03).
- "a meeting's words never reach an agent": holds. `meetbot/speak.py:89-100`, `copilot.py` and `analysis.py:41` force the direct brain and
  fence the text.
- "a call to an emergency number is refused for every origin": holds for the listed set (V-11).
