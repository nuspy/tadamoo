> Reference: [README](../../README.md).

## Using it as a server (API)

The server speaks the OpenAI and the Anthropic APIs at `http://127.0.0.1:<port>` (default port **7232**):

| endpoint | API |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions: messages, tools (function calling), images when vision is on, `response_format`, `stream` |
| `POST /v1/responses` | OpenAI Responses, **stateless**: send the whole conversation each time (`previous_response_id` and `GET /v1/responses/<id>` are not kept) |
| `POST /v1/messages` | Anthropic Messages: tools, thinking blocks, images, `stream` |
| `POST /v1/messages/count_tokens` | Anthropic token count: the same prompt `/v1/messages` would read, not run |
| `GET /v1/models` | the served model and every other model of the models folders it could serve (OpenAI or Anthropic shape) |
| `GET /v1/models/{id}` | one of them |
| `GET /health` | `{"service": "tadamoo", "loaded": true, ...}` once the model is in; `"standby": true` while a server started by the app waits for the Loader |

**Start it for agents** with `.\Avvia-TaDaMoo.ps1 -server` (it returns once the model is loaded; add `-riusa` in
unattended scripts) and stop it with `.\Ferma-TaDaMoo.ps1`. `tadamoo.pid` holds the PID, port and profile of a server
started by a script or the app.

**Model name.** The served model's id is the `--alias` (or `"alias"` in the config file's `server` section), else the
name stored in the model file. The `model` field of a request may be anything (the server runs one model at a time)
and the answer carries the served name, with one exception: a name that `GET /v1/models` lists as another, unloaded
model is refused (below).

**The model list.** `GET /v1/models` lists the served model first (`"status": {"value": "loaded"}`, or `"unloaded"`
while it loads on the first request), then every `.basalt` file of the models folders that the Loader shows as
**ready** (a GGUF to convert or a file Basalt-Plus cannot run is not listed), each with `"status": {"value": "unloaded"}`.
The id of such a model is its file name without `.basalt` (with a short hash after it when two folders hold the same
file name): the name to pick in a client. Each entry has `created` (the file's date), `owned_by`, `display_name`, the
context length when the file says it (`context_length`; the served model's `meta.n_ctx` is the context it runs with,
another model's `meta.n_ctx_train` the longest it was trained for), `architecture.input_modalities` (`image` when the
file has a vision projector) and, only on a server bound to 127.0.0.1, the file's `path`. The folders are scanned
from the files' headers on a thread, at most every 30 seconds, so neither the list nor a chat request waits for the
disk; the list works in standby too (no model loaded: only the folders' models). A request that names a model the
list shows as unloaded (by its id, its file name or its path) gets a 404 that says how to load it, instead of an answer
from the loaded model: OpenAI `{"error": {"code": "model_not_found", ...}}`, Anthropic
`{"type": "error", "error": {"type": "not_found_error", ...}}`.

```sh
# OpenAI shape
curl http://127.0.0.1:7232/v1/models
# {"object": "list", "data": [
#   {"id": "Qwen3.8 Flash Next Uncensored", "object": "model", "created": 1791904651, "owned_by": "tadamoo",
#    "display_name": "Qwen3.8 Flash Next Uncensored", "status": {"value": "loaded"}, "meta": {"n_ctx": 131702},
#    "context_length": 131702, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
#    "path": "D:/Modelli/Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt"},
#   {"id": "OrcaRouter-IQ3_XXS", "object": "model", "created": 1791904651, "owned_by": "tadamoo",
#    "display_name": "OrcaRouter", "status": {"value": "unloaded"}, "context_length": 262144,
#    "meta": {"n_ctx_train": 262144}, "quant": "IQ3_XXS", "path": "D:/Modelli/OrcaRouter-IQ3_XXS.basalt", ...}]}

# Anthropic shape: with an anthropic-version header (or x-api-key without Authorization)
curl http://127.0.0.1:7232/v1/models -H "anthropic-version: 2023-06-01"
# {"data": [{"type": "model", "id": "Qwen3.8 Flash Next Uncensored", "display_name": "Qwen3.8 Flash Next Uncensored",
#            "created_at": "2026-10-13T15:17:31Z"},
#           {"type": "model", "id": "OrcaRouter-IQ3_XXS", "display_name": "OrcaRouter",
#            "created_at": "2026-10-13T15:17:31Z"}],
#  "has_more": false, "first_id": "Qwen3.8 Flash Next Uncensored", "last_id": "OrcaRouter-IQ3_XXS"}

# one model, in either shape (an id with spaces is URL-encoded); 404 model_not_found / not_found_error otherwise
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS -H "anthropic-version: 2023-06-01"

# a chat that names the unloaded model
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "OrcaRouter-IQ3_XXS", "messages": [{"role": "user", "content": "hi"}]}'
# 404 {"error": {"message": "The model 'OrcaRouter-IQ3_XXS' is not loaded (this server is serving 'Qwen3.8 Flash
#   Next Uncensored'). Load it in the web page's Loader (choose it in the model list, then Load), or start the server
#   with it: .\Avvia-TaDaMoo.ps1 --model \"D:/Modelli/OrcaRouter-IQ3_XXS.basalt\"", "type": "invalid_request_error",
#   "param": "model", "code": "model_not_found"}}
```

TODO (possible future: load on request): the server does not switch models by itself when a request names another
listed model; it only says how to load it. Loading the asked model on request is left for later.

**API key.** The server listens on 127.0.0.1 only and needs no key: clients that insist on one can send any value.
`--api-key KEY` (command line only, never in a config file) makes `/v1/*` require it as `Authorization: Bearer KEY`
or `x-api-key: KEY`; it is needed to listen on another address (`--host 0.0.0.0`). `/v1/*` carries `Access-Control-Allow-Origin: *`.
Elsewhere a page of another site is refused, and without a key so is a DNS name that is not this PC's (`--allowed-hosts` adds names; see
[server security](../server-security.md)). The scripts and the app start the server on 127.0.0.1 without a key.

**Streaming.** `"stream": true` streams server-sent events in each API's own format (OpenAI `chat.completion.chunk`,
Responses events, Anthropic `message_start` / `content_block_delta` / ...), reasoning included as
`reasoning_content` (OpenAI) or `thinking` blocks (Anthropic).

**Reasoning.** The model thinks by default (the template's high level). OpenAI: `"reasoning_effort": "none" | "low" |
"medium" | "high"` (Responses: `"reasoning": {"effort": ...}`). Anthropic: `"thinking": {"type": "disabled"}`, or
`"enabled"` with `budget_tokens` (under 2048: low, under 8192: medium, else high). `reasoning_budget_tokens: N` on
any endpoint closes the thinking after N tokens and lets the model answer.

```sh
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "tadamoo", "messages": [{"role": "user", "content": "Say hello in three languages."}],
  "max_tokens": 512, "reasoning_effort": "low"}'

curl -N http://127.0.0.1:7232/v1/messages -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" -d '{
  "model": "tadamoo", "max_tokens": 1024, "stream": true,
  "messages": [{"role": "user", "content": "Write a haiku about basalt."}]}'

curl http://127.0.0.1:7232/v1/responses -H "Content-Type: application/json" -d '{
  "model": "tadamoo", "input": "What is 17 * 23?", "reasoning": {"effort": "none"}}'
```

(In PowerShell use `curl.exe` and put the JSON in a file: `-d "@body.json"`.)

Python, with the official SDKs (`pip install openai anthropic`):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:7232/v1", api_key="none")   # any key, unless --api-key was given
r = client.chat.completions.create(model="basalt", messages=[{"role": "user", "content": "Hello!"}],
                                   max_tokens=512, reasoning_effort="low")
print(r.choices[0].message.content)

for chunk in client.chat.completions.create(model="basalt", stream=True,
                                            messages=[{"role": "user", "content": "Count to five."}]):
    print(chunk.choices[0].delta.content or "", end="", flush=True)

print(client.responses.create(model="basalt", input="Name three volcanic rocks.").output_text)
```

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:7232", api_key="none")
msg = client.messages.create(model="basalt", max_tokens=1024,
                             messages=[{"role": "user", "content": "Hello!"}])
print(msg.content[-1].text)                      # a thinking block may come first

with client.messages.stream(model="basalt", max_tokens=1024, thinking={"type": "disabled"},
                            messages=[{"role": "user", "content": "Count to five."}]) as s:
    for text in s.text_stream:
        print(text, end="", flush=True)

print(client.messages.count_tokens(model="basalt", messages=[{"role": "user", "content": "Hello!"}]).input_tokens)
```

**Claude Code** talks to the Anthropic endpoint:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:7232"
$env:ANTHROPIC_AUTH_TOKEN = "none"                 # any value; the key given with --api-key, if any
$env:ANTHROPIC_MODEL = "basalt"                    # any name: the server has one model
$env:ANTHROPIC_SMALL_FAST_MODEL = "basalt"
claude
```

(On Linux/macOS: `export ANTHROPIC_BASE_URL=http://127.0.0.1:7232` and so on.) Agent tools send long prompts:
choose a context of at least 65,536 tokens in the load profile, and keep `--parallel 1` unless several agents work at
once (the context is shared between the slots, see [`--parallel`](upstream-basalt.md#concurrent-requests---parallel-n)).

**Structured output** (`/v1/chat/completions`): `"response_format": {"type": "json_object"}` or `{"type":
"json_schema", "json_schema": {"name": ..., "schema": {...}}}`. The answer is checked; when it is not valid JSON, or
does not match the schema, the request is generated again with the error fed back, up to `structured_retries` times
(0 to 5, default 2; 0 turns it off). The new try is the same conversation plus one user message that starts with the
error. An answer that was valid JSON (or had one plain syntax fault) and repeated nothing is sent back as the
assistant's turn, and the message asks for the one change the error names and nothing else; an answer that was cut
off or wrote an entry over and over is never sent back, so a model that looped is not shown its loop again (the
message then has the error, the JSON path where the text broke and the repeated entry, once, with its count). No
message holds a count of characters. Only the validated
answer is held until the check. Reasoning streams as it is written, rejected tries included; a rejected try is
announced by a chunk with an empty `delta` and `"structured_output": {"attempt": 1, "discarded": true, "reason": "...",
"retrying": true}`, its answer is never sent, and the final chunk carries the validated answer only, with
`"structured_output": {"validated": true, "attempts": 2, "discarded": [{"attempt": 1, "reason": "..."}]}` (a
non-streaming reply returns the last try's reasoning and answer, with the same field). `GET /v1/status` tells it under
`structured_output`: `content_buffered` and `reasoning_streamed` are true, and `stream_buffered` (kept for older
clients) means the validated answer is held until the check. `/v1/responses` with a JSON `text.format` streams its
reasoning too and checks the answer once, without a retry; `/v1/messages` has no structured output.

**Busy and errors.** With `--parallel 1` requests wait their turn in arrival order. While the engine is starting, or
while a server started by the app is in standby (no model loaded yet), a request gets a 503 that says why: try again
later, or load a model in the Loader. A prompt longer than the context is refused with a 400 that names the context.
