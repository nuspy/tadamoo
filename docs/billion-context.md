# Billion Context in the chat

Billion Context is a local proxy shipped with the setup (the `billion-context` add-on, run by the add-on's Node.js). It sits between a conversation and the model and keeps long conversations inside the context window: it nudges the model to compress old turns, stores large tool outputs behind a pointer and, close to the limit, cuts the biggest outputs. Everything stays on this PC.

Switch: Chat settings > Billion Context (default on). One checkbox, a port field (default 18788), a status line with the proxy's own counters (sessions, requests, tokens in the context and the window) and a Restart button. API: `GET` and `POST /api/billion-context` (`{"enabled": bool, "port": int, "restart": bool}`; `POST` only from the PC the server runs on). The setting is `billion-context.json` in the user data folder.

## What goes through it

Only the streamed `POST /v1/chat/completions` of the Chat page and of the main agent loop, and only from the PC itself. The two ask with the header `X-Tadamoo-Compress: 1` (the page sets it; the agent processes get `TADAMOO_COMPRESS=1` and the client adds it). The server hands the body to `http://127.0.0.1:<port>/bili/http://127.0.0.1:<server port>/v1/chat/completions` and relays the answer chunk by chunk, so tokens arrive as they are produced, the final usage is intact and a closed browser tab or a Stop press cancels the request all the way to the engine. The relayed request comes back to the server marked with `X-Tadamoo-Bili-Hop` and is answered there as usual (logged once).

Everything else stays direct:
- requests to cloud providers (they carry your API keys; they never touch the proxy);
- requests that are not streamed: the planner, the memory worker, skill distillation, cron routines, evals (the proxy adds its own tools to a request and a model call to one of them would reach the caller in a non-streamed answer);
- requests from phones and other listeners (the proxy and its stored conversations are for this PC).

## It never gets in the way

The proxy is one extra local process (about 100 MB of RAM), started by the server after it is up and stopped when it exits. If Node.js or the add-on is missing, the port is taken, or the proxy dies, chat goes direct without an error; the status line says why (`not installed`, `error`, `stopped`). A dead proxy is started again after a growing wait (2 s, doubling to 5 minutes; the wait resets after 60 s of stable running). A request that cannot reach the proxy, or that the proxy refuses before the first byte, is answered directly from the same body. The port must differ from the server's own and from the port of any other proxy you run (the owner's Claude Code proxy on 18787 is never touched; the process gets its own configuration and data folder under the user data folder, and nothing inherited from `ACP_*` and `BILI_*` variables).

The process is started with: no self-update, no update or advisory check, `BILI_STRICT_PORT=1`, `ACP_MODEL_CONTEXT_LIMIT` set to the loaded model's context (the proxy restarts when it changes by more than 10%). Its log is `billion-context/proxy.log` in the user data folder.

## Working with Basalt's own compaction

Chat settings > When the context is full (`serve/chatctx.py`) is not replaced. It acts first, in the browser, before a message is sent: at 80% of the context it summarizes the older turns (policy `compact`), or drops them (`rolling`), or stops (`stop`). The proxy is configured (`proxy-config.json`, written at each start) to start working above that: `compress.maxContextLimit` 90% and `emergencyThresholdPercent` 97%. So a chat that Basalt keeps below 80% is passed through unchanged, and a conversation that still grows, typically the agent's with its large tool outputs, meets the proxy. Setting the Basalt policy to `stop` leaves compression to the proxy alone.

## What it does by default: keep aside, not summarise

Two kinds of summary stacked on each other lose more than one, so by default the proxy does not fold old turns itself. Its configuration leaves out the tools and nudges that ask the model to fold (`compress.injectTool` and `compress.injectNudge` false); it keeps what does not rewrite the conversation: large tool outputs stored aside behind a pointer, and the emergency cut near the limit. The only summaries are Basalt's, written from the original text ([context](context-layers.md)). Chat settings > Billion Context > *Let the model fold old turns itself* (`"compress": true` in `billion-context.json`) turns the proxy's own folding back on; the process restarts when it changes. Which of the two summarises better is measured, not assumed (experiment E9 in [experiments](experiments.md)).

## Known limits

- The proxy adds a short system note and its own tools to each request it relays (a few hundred tokens). In a streamed request it answers the model's calls to them itself and may show a short status line in the reply.
- Compression is model-driven: the proxy asks the model to compress; a small model may ignore it. Basalt's own compaction is the guarantee.
- The proxy hangs on `/v1/messages/count_tokens`; Basalt does not call it.
- The security monitor (Monitor > Security) may list the proxy's `node` loopback connections.
