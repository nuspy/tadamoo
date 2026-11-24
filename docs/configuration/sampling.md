# Sampling and thinking

A request's sampling fields win. A field it leaves out takes the server option of the same name, then the model file's `sampling`
default, then the engine's. `/props` reports what is in effect. An out-of-range value is a 400 that names the field.

| Field | Type, values | What it does |
| --- | --- | --- |
| `temperature` | number >= 0 | `0` is greedy (argmax); higher is more random. Greedy requests stay exact under every other setting. |
| `top_p` | number in (0, 1] | Nucleus sampling: keep the smallest set of tokens whose probability adds up to this. `1` is off. |
| `top_k` | integer >= 0 | Keep only the K likeliest tokens. `0` (or `-1`) is off: every token of the vocabulary. |
| `min_p` | number in [0, 1] | Sampling cutoff: drop tokens less likely than `min_p` times the likeliest token's probability. `0` is off. It acts on the model's tokens. |
| `repetition_penalty` | number > 0 | Multiplicative penalty on tokens seen in the window; `1` is off. |
| `presence_penalty` | number | Flat penalty on any token seen in the window; `0` is off. |
| `frequency_penalty` | number | Penalty that grows with how often a token was seen; `0` is off. |
| `penalty_last_n` | integer 0..4096 | The window, in tokens, the three penalties look back over (`0`: none). Applies only when a penalty is set; 64 when omitted. Penalties apply in batch windows too; pipelined batch groups refuse them (400). |
| `seed` | integer | Fixes the random draw for a reproducible sampled reply. |
| `relaxed_drafting` | number in [0, 1], or `false` | This request's relaxed-drafting threshold; `false` or `0` forces exact sampling. Needs speculation; see [Speculative decoding](index.md#speculative-decoding-mtp). |

Other request fields the API reads: `max_tokens` / `max_completion_tokens` (the Anthropic `max_tokens`; `0` or
`-1` means the rest of the context), `stop` / `stop_sequences`, `logit_bias`, `reasoning_effort`, `enable_thinking`
and `reasoning_budget_tokens` (a cap on thinking tokens; `0` or `-1` is no limit). Anthropic's `thinking.budget_tokens` and
llama.cpp's `thinking_budget_tokens` are the same budget, and `reasoning_budget_tokens` wins when more than one is
sent. A request that names none of them has no thinking budget.

The thinking is never cut by default. With `answer-room` switched on (off by default) and an effective output cap of at least 1024 tokens, thinking is limited to the cap minus
`min(max(512, cap // 4), cap // 2)`. An 8192-token cap allows up to 6144 thinking tokens; the remaining 2048
include the closing of the thinking and the answer. Smaller positive budgets stay unchanged. Context fitting uses the reduced
cap. Explicit request budgets of `0` or `-1` disable automatic budgeting; smaller output caps
keep their existing behavior. Reserved space reduces empty answers but does not guarantee a complete answer.

### Thinking loops

With `thinking-loop-tokens` above 0 (0 = never, the default), when the model repeats one token that many times in a row inside its thinking, the server ends the thinking with the
template's own close (`\n</think>\n\n`) and the model answers. The reply's `finish_reason` stays honest: `stop` when
the answer completes, `length` only at `max_tokens`. The server logs `thinking repeated one token N times, ending it`.
Only the thinking is watched; separators, padding, base64 and digit strings in an answer are never cut. Set
[`thinking-loop-tokens`](index.md#server) to a count to turn the safeguard on (256 is a usual one). All of these limits are set in the
Loader page, section Server, as well as by option.

### The model file's sampling defaults

The model's GGUF metadata (the `gguf_kv` of the model file's `meta`) carries llama.cpp's `general.sampling.*` keys.
The server takes them as the defaults of these request fields:

| Key | Request field | Allowed values |
| --- | --- | --- |
| `general.sampling.temp` | `temperature` | number >= 0 |
| `general.sampling.top_p` | `top_p` | 0 < value <= 1 |
| `general.sampling.top_k` | `top_k` | integer >= 0 (`0` or `-1` = off) |
| `general.sampling.min_p` | `min_p` | 0 <= value <= 1 |
| `general.sampling.penalty_repeat` | `repetition_penalty` | number > 0 (`1` = off) |
| `general.sampling.penalty_last_n` | `penalty_last_n` | integer 0..4096 |

A float key is read as the shortest decimal with the same float32 value (0.95, not 0.949999988).

A bad value stops the server at startup; an unknown key is named in a warning and ignored. The defaults in effect
are printed at startup as `[server] sampling ...`, each marked `(cli)` or `(config)` when a launch
option set it and unmarked when it comes from the model file. A launch option overrides the model file's value for
that key; the others stay.
| `temperature` | number >= 0 | `0` is greedy. Greedy requests stay exact under every other setting. |
| `top_p` | (0, 1] | Keep the smallest set reaching this probability. `1` is off. |
| `top_k` | integer >= 0 | Keep the K likeliest tokens. `0` or `-1` is off. |
| `min_p` | [0, 1] | Drop tokens below `min_p` times the top token's probability. |
| `repetition_penalty` | > 0 | Multiplicative penalty on tokens in the window. `1` is off. |
| `presence_penalty`, `frequency_penalty` | number | Flat, and count-based, penalties. `0` is off. |
| `penalty_last_n` | 0..4096 | Window for the penalties (default 64). |
| `seed` | integer | Fixes the draw for a reproducible sampled reply on Qwen3.8-27B; on Flash-Next the same seeded request repeats run to run for the same session history (the same requests in the same order, one at a time; see [adaptive tier](adaptive.md)). Without one, a request with `top_k` 1 to 64 on a model with a block drafter (Qwen3.8-27B) draws its drafts and keeps them by speculative sampling ([precision](../precision/qwen38-27b.md#sampling-with-drafts)): the same distribution, more drafts kept, but the text then depends on the windows the draft policy picks. |
| `relaxed_drafting` | [0, 1] or `false` | This request's relaxed-drafting threshold. `false` or `0` forces exact sampling. |

Also read: `max_tokens` / `max_completion_tokens` (`0` or `-1` = the rest of the context), `stop`, `logit_bias`, `reasoning_effort`,
`enable_thinking`, `reasoning_budget_tokens` (`0` = no thinking budget). Anthropic's `thinking.budget_tokens` is the same budget.

llama.cpp clients also work: `POST /tokenize` (`content`, `parse_special`) and `/detokenize` (`tokens`) use the model's tokenizer, `/props` reports `chat_template_caps` for the model (tool calls, parallel calls, reasoning effort,
system role, preserve reasoning), `chat_template_kwargs.preserve_reasoning` keeps earlier turns' thinking in the prompt, and
`reasoning_budget` is read on the chat endpoint: `0` is thinking off, `-1` is no cap at the highest effort, and N > 0 is an N-token
budget at the level it names (up to 2048 low, up to 8192 medium, else high; DeepSeek renders medium as high, 75 of 100; Hy4-flash renders low and medium as high). When fields disagree, off from any of them wins, then
`chat_template_kwargs.reasoning_effort`, then `reasoning_effort`, then the level a `reasoning_budget` implies, and last the entry's
`reasoning-effort` (unset: the template's default). For the token
cap, `reasoning_budget_tokens` wins over `thinking_budget_tokens`, then `reasoning_budget`, then `thinking.budget_tokens`.

## Prompt progress

A streamed Chat Completions request with `return_progress: true` (llama.cpp's field) gets `prompt_progress` on the chunks sent
while the prompt is read: `{total, cache, processed, time_ms}`. `total` is the prompt's tokens, `cache` the ones taken from the
conversation cache, `processed` the ones read so far including the cache, and `time_ms` the milliseconds spent reading. The overall
progress is `processed / total`, the timed progress `(processed - cache) / (total - cache)`. As in llama.cpp, such a chunk has the
delta `{"role": "assistant", "content": null}`; the last one has `processed` equal to `total`. Without the field, or when it is
`false`, the stream is unchanged; a value that is not a boolean is a 400.

## Logprobs

A request can ask for each generated token's log probability and the likeliest tokens at its place. The engine computes them
on the GPU from the row's full head logits: the natural log of the token's probability under the model's own distribution,
before temperature, penalties, `logit_bias`, top-k/top-p and any grammar mask. The reply's tokens are the same with or without
them, and decode speed stays within a few percent.

- **Chat Completions:** `logprobs: true` and `top_logprobs` (0 to 20). `choices[0].logprobs.content` has one entry per answer
  token, `{token, logprob, bytes, top_logprobs: [{token, logprob, bytes}]}`, likeliest first. Streaming: each chunk carries the
  entries of its tokens.
- **Thinking and tool calls:** Basalt adds `logprobs.reasoning_content` (the thinking's tokens) and `logprobs.tool_calls`
  (`[{index, content}]`, each call's tokens as the model wrote them, markup included): the least likely token of a call is where
  the model guessed.
- **Responses:** `include: ["message.output_text.logprobs"]` with `top_logprobs` fills `output_text.logprobs` and each
  `response.output_text.delta`; Basalt adds `logprobs` to reasoning and call items (and to `response.reasoning_text.delta` and
  `response.function_call_arguments.delta`).
- **Anthropic Messages** (a Basalt extension; the API has no such field): the same `logprobs` and `top_logprobs`. Each
  `content_block_delta` carries `logprobs`, the entries of its tokens; the message, or else `message_delta`, carries the
  `logprobs` object above.
- **Uses:** with `response_format` set to a schema whose field is an `enum`, the `top_logprobs` at the value's first
  token give each label's probability (a classifier); in an agent, a tool call whose least likely token sits far below
  its alternative is one the model was unsure of.
- Tokens the server writes itself (the close of a thinking budget) have none.

## Thinking

- **Budget:** `answer-room` keeps output tokens for the close and the answer by limiting the thinking to the cap minus the reserve. A smaller
  budget (the request's, else the entry's `reasoning-budget-tokens`) still wins. Reserved space does not guarantee a complete answer.
  - `auto` (the default): the reserve is `min(max(512, cap // 4), cap // 2)`. An 8,192-token cap allows 6,144 thinking tokens and keeps
    2,048; a 700-token cap allows 350 and keeps 350. A cap under 32 tokens is left as it is.
  - A whole number `N`: a fixed reserve at any cap, larger or smaller than `auto`'s, so thinking is limited to `cap - N` (`1024` with a
    4,096-token cap leaves 3,072). `0` reserves nothing. An `N` of the cap or more leaves thinking one token.
  - In a config file the value is `"auto"` or a whole number, written `100` or `"100"`; on the command line, `--answer-room auto` or
    `--answer-room 100`. A negative number, `true`, `false` or any other text is refused at start.
  Examples: `"answer-room": "auto"`, `"answer-room": 2048` (think longer at large caps), `"answer-room": 0` (no reserve).
  - A request's explicit budget of 0 or less opts out in every mode.

## Answer now

A client can end a reply's thinking while it runs: `POST /v1/answer_now` closes the thinking and the reply goes on to its answer
in the same response. The engine finishes the decode window under way, so a few more thinking tokens may arrive first; the close
then follows them, the way the budget closes, and the engine reads only the close, however long the thinking was. It works on
`/v1/chat/completions`, `/v1/messages` and `/v1/responses`, streaming or not, and with `--parallel` slots. A request nobody asks
runs exactly as before.

- **Naming the request:** every chat response carries `X-Request-Id`, the response's own id (`chatcmpl-...`, `msg_...`, `resp_...`,
  also the `id` of a stream's chunks). Send `{"request_id": "<id>"}`. A client that sends `x-client-request-id` with its request can
  use that value instead, before any reply arrives. Or name the session: `{"session_id": "<id>"}` or the request's
  [session header](../running.md#session-affinity) on the call, which picks the session's newest running request.
- **Reply:** `200 {"object": "answer_now", "request_id": "...", "state": "thinking"}`. A request still `queued` or reading its
  `prompt` takes it too, and closes its thinking at its first token.
- **Refusals:** `409` with `error.code` `not_thinking` and `error.state` `answering` or `finished`, or a message naming thinking off;
  `404` with `unknown_request` or `unknown_session`; `400` `no_target` without a request id or session.

```sh
curl -s localhost:8080/v1/answer_now -H 'Content-Type: application/json' -d '{"request_id": "chatcmpl-..."}'
```

The call takes the API key like every `/v1` route. A page on another origin reads `X-Request-Id` through `--cors-origin`.

### Detecting Basalt

`/props` (llama.cpp's route, unchanged for its clients) carries one extra key, `basalt`: `{"version": "...", "capabilities":
["answer_now", "logprobs", "documents"]}`. A client that finds `basalt` there talks to Basalt; `"answer_now" in basalt.capabilities` says the
endpoint above is served, `"logprobs"` that logprobs come back on all three APIs with the thinking's and tool calls' tokens
([logprobs](#logprobs)), `"documents"` that messages may carry PDF and text documents ([documents](documents.md)), and `"video"` (only
while video is on) that this model takes video parts now (image input on, a model that reads video, FFmpeg found:
[video](../running.md#video)). Later features add their names to the same list.

## Thinking loops

- **Loops:** when one token repeats 256 times inside the thinking, the server closes the thinking and the model answers
  (`thinking-loop-tokens`). Only the thinking is watched. `finish_reason` stays honest.

## The model file's defaults

The model's GGUF metadata keys `general.sampling.temp`, `top_p`, `top_k`, `min_p`, `penalty_repeat` and `penalty_last_n` are the defaults of
the matching fields. A bad value stops the start. The defaults in effect print on the model's line (`temp=1.0 top_p=0.95 ...`).
