# Knowledge archives in the voice functions

The copilot, the meeting bot (spoken replies and minutes), phone calls and the avatar's direct brain can draw on the knowledge archives ([rag.md](../rag.md)).
The harness brain of the avatar is an agent conversation and takes the archives as any conversation does. This page is what is specific to a conversation with
somebody waiting and with other people listening.

## Where the choice lives

Archives are stored by **id** and shown by **name**. The pickers are checklists fed by `GET /api/rag/catalog`.

| Function | Stored in | Set in |
|---|---|---|
| avatar and voice chat (`brain.archives`) | `voice.json` brain block; the harness conversation is made with `knowledge: [ids]` and remade when they change | Settings > Voice |
| a meeting | the plan item's `archives` (`null`/absent: follow the settings), else `meetings.archives`; the ids are copied into the meeting's `meta.json` for the minutes | the item's form, Settings > Voice > Meetings |
| the copilot | `meetings.copilot_archives` | Settings > Voice > Meetings |
| phone calls | `telephony.archives` | Settings > Voice > Telephony |

A session's own choice lives in its own `Knowledge` object (`voice/callbridge/rag_client.py`): one for each forked `ModeBrain` (`fork()`), meeting, call or copilot
window. The shared `DirectBrain` and `HarnessBrain` never hold it.

## The search while somebody waits

The voice service cannot call `/api/rag/*`. It calls `POST /api/voice/rag/passages` (`serve/voice_rag.py`, listed in `SERVICE_ROUTES`), which resolves the
archives (`serve/voice_resolve.py`: the resolver of `tadamoo/rag/resolve.py`, with the cloud and `others_ok` rules), cleans the query, searches inside a budget and
returns the data block. The search is **hybrid** (cosine plus a small word share, as in [rag-embeddings.md](../rag-embeddings.md)); words alone are only the
fallback, and an answer that is not hybrid always says why in `marker`.

The question must be embedded by the model that made the archive's vectors. The 4B model of the usual search needs a VRAM lease, which the voice functions
hold while they are loaded, and a cold start takes many seconds. So real-time functions use a **second set of vectors** made by a small model
(`Qwen3-Embedding-0.6B`) that is kept beside the archive (`embeddings_rt.npy`, `rt.json`: model, version, fingerprint of the passages) and a **warm CPU worker** that
embeds only the question. Nothing of the usual search is changed: the second vectors are handed to `query.run` as the vectors of a view of the archive
(`tadamoo/rag/realtime.py`).

- The worker runs on the CPU with float32 weights and 4 threads (`tadamoo/rag/rt_worker.py`), never on a lease, so the voice and the avatar cannot refuse it and
  it takes no VRAM. It is loaded when a meeting, a call or the copilot starts (`POST /api/voice/rag/warm`) and given back after 15 minutes without a question.
- The second vectors are made **in the background**, only for archives that a real-time function uses, the first time one is searched
  (`realtime.Indexer`; the usual lease when the broker can lend one, else the CPU, slowly). Until they are there the search is by words and says
  "these archives have no vectors for the real-time search yet". A change to the archive's passages (another cutting) makes them stale and they are made again.
- The reranker is **not** used in real time. The minutes are not real time and use the full search (`store.search`).

### What was measured

ER100 patents: 2269 passages in English, French and Chinese, 96 questions in Italian, each with its true passage. The question embedding and the search, one at a
time, on the maintainer's PC (RTX 5090, 32 CPU threads; the GPU was shared with other jobs while the reranker ran). R@k is the share of questions whose
passage is among the first k answers.

| Profile | R@1 | R@5 | R@10 | MRR | mean ms | p95 ms |
|---|---|---|---|---|---|---|
| words only (BM25) | 3.1% | 11.5% | 19.8% | 0.069 | 3 | 1 |
| CPU 0.6B hybrid, 4 threads, float32 (chosen) | 32.3% | 56.2% | 66.7% | 0.427 | 390 | 525 |
| CPU 0.6B hybrid, 2 threads | 32.3% | 56.2% | 66.7% | 0.427 | 557 | 800 |
| CPU 0.6B hybrid, 4 threads, bfloat16 | 32.3% | 56.2% | 66.7% | 0.427 | 1882 | 2998 |
| GPU 0.6B hybrid (warm) | 32.3% | 56.2% | 65.6% | 0.429 | 71 | 90 |
| GPU 4B hybrid (warm) | 39.6% | 68.8% | 76.0% | 0.505 | 80 | 102 |
| GPU 4B hybrid, best 20 reranked by the 4B reranker | 54.2% | 79.2% | 83.3% | 0.655 | 3376 | 5196 |

Cold start: 5.9 s for the CPU worker, 9.4 s for the 0.6B on the GPU, 18.9 s for the 4B, plus 9.5 s for the reranker. The reranker's time is high because the
GPU was in use by other jobs; the documented speed is 29 pairs a second.

Reading: words alone find almost nothing across languages ("extremely limited"). The CPU worker gets 81% of the 4B's R@1 and 73% of its R@5 at 390 ms, with no
GPU and no lease, so it works while the avatar or another voice function holds the VRAM. The 4B on the GPU is faster but needs a lease that is refused exactly then,
and costs 9 GB. The reranker adds 14 points of R@1 but takes seconds: right for the minutes, wrong for a reply. The bfloat16 row shows why the worker asks for
float32 on the CPU: this CPU has no native bfloat16 and is five times slower with it.

### Time budgets and the fallback

| Function | Budget | Why |
|---|---|---|
| copilot, meeting reply | 1.2 s | a suggestion or a reply is wanted while the other person is still talking; the model's own start is added |
| phone call, avatar | 0.9 s | every millisecond is silence the other side hears |
| minutes | 20 s | written once, afterwards; the full search (4B, reranker) |

An utterance of fewer than 3 words is not searched. The server runs the words search first (milliseconds) and the hybrid one in a worker; at the budget it
returns the best answer it has, the words, with `timed_out` and a `marker` ("words only: the semantic search took more than 1200 ms"). A worker still busy with
earlier questions is not queued behind. The service waits for the server at most the budget plus 1.5 s; past that it gives no passages and the marker says
"the search did not answer". The copilot shows the marker as a line in its window, the avatar's exchange keeps it in `note`, the minutes put it in `archives_notes`.
A search is never silently shortened.

## Where the passages go and how they are kept safe

- **At the tail, never in the system message.** The direct brain puts the block after the person's words in the last message (the history keeps the plain words,
  so passages do not pile up and the cached prefix stays); the copilot and the meeting prompt put it after the fenced words of the other people; the minutes put it
  after the segment.
- **Data, fenced.** The block is `tadamoo.untrusted.fence("knowledge-archive", ...)`; its closing line says the passages are data, not instructions. For speech the
  closing also says never to read out a heading, a name or a page, and `speak.clean_reply` removes any `[archive / document, p. 3]` that still comes out. For the
  copilot's screen the closing asks to name the archive in a few words.
- **Other people's words are untrusted.** The query is built on the server from them (`tadamoo/rag/rt_query.py`): look-alike and invisible characters folded, tags,
  links, addresses and code dropped, boundary tags and control tokens removed, sentences that address an assistant dropped (`inject_screen.detect`), the rest cut to
  300 characters (800 for a minutes segment), newest words first. It is only search text: it reaches no tool. The brains that read these words have no tools
  (`speak.direct_of` refuses the harness).
- **The cloud.** The server composes with `routed_to_cloud` when a heavy job has the local model out: only archives with `cloud_ok` are searched, and the block is
  fenced as `knowledge-archive-cloud`. A request that was composed before the swap began still carries the plain name, and the relay strips it (below).

## Letting other people perceive an archive: `others_ok`

Every archive has `others_ok`: may its content be **said, written or shown** to other people? `allow`, `deny`, `ask_dialog` (the default) or `ask_voice`. What
counts is what the assistant outputs in a meeting, on a call or as an avatar on a call: speaking, a chat message, captions, the screen. The private copilot window
shows only the person and **never asks**; its suggestions use any archive (the cloud rule still applies).

| Function | Audience | What happens |
|---|---|---|
| copilot | the person | passages always used; `sources` (archive and its `others_ok`) come back with the suggestion |
| minutes | the person | passages used; the archives that served are listed in the minutes |
| meeting reply, call, avatar on a call | others | `deny`: the archive is not searched. `allow`: used. `ask_*`: its passages stay out of the block until the person says yes; the server names the archive in `pending` and the service opens a question |
| avatar talking to the person alone (`brain.archives`) | the person | `audience: "me"`: no question |

- **A question is one per archive and session**, never blocks a reply (until it is answered the archive's content is simply left out), and expires as a no after 3
  minutes. A question nobody answered is not put again for 2 minutes and is not remembered as a no.
- **The dialog** (`serve/web/voicearchives.js`, a modal on any page, also in the desktop app): it names the archive and shows what would be output, with *Yes, this
  time* / *Yes, for this meeting* / *Always for this archive* / *No*. "Always" sets the archive's `others_ok` to `allow`; when the store cannot keep it the
  answer is a "for this meeting" and the question's `saved` is false. "This time" is used up by the next passage of that archive; "for this meeting" and "no" are
  remembered for the session.
- **The copilot's click.** A suggestion that used passages of an archive that asks is checked when it is said aloud ("reply with audio"): the thread that speaks
  asks and waits for the answer (the person asked for the output); a no or no answer leaves it unsaid and the window says which archive stopped it. A written or
  shown output (`how` = written, shown) goes through the same `Knowledge.confirm_output`.
- **`ask_voice`**: the question is also asked **aloud**, but only over a channel the other people cannot hear: an output device the person named
  (`meetings.private_output`, part of a device name such as the headphones; never the virtual microphone, the loopback or the default device) and the answer is taken
  from the person's own microphone through the service's recognizer (`callbridge/private_channel.py`). The words are read in English or Italian
  (`rag_client.parse_choice`): yes / this time, for this meeting, always, no; a bare yes is the narrowest yes ("this time") and any word of refusal is a no. Two
  tries, 25 seconds each. The dialog is always open as well and the first answer wins. With no private output (or one that is not found, or no TTS loaded) the
  question goes to the dialog alone and the dialog says that the archive asks by voice but has no private channel. The assistant never asks aloud on the meeting's
  own audio route.

## Heavy jobs

While a heavy job has the local model out ([heavy-swap.md](../heavy-swap.md)) the voice service's requests (header `X-Tadamoo-Function: voice`, sent by
`TadamooApi.chat_stream`, accepted only with the voice token, from this PC) go to the swap's provider: the copilot, the replies of a meeting, the calls and the
direct brain of the avatar keep working instead of getting a 503. `voice` is a function of the registry like `chat` (so it can also be assigned to a provider, with
the per-function consent for personal data). The relay removes every `knowledge-archive` block from the messages; only `knowledge-archive-cloud` blocks, made for the
cloud from cleared archives, stay.

## Routes

From this PC only. The first group takes the voice service's token (`SERVICE_ROUTES`), the second the page.

| Route | |
|---|---|
| `POST /api/voice/rag/passages` | `{function, archives, query, how?, granted?, denied?, budget_ms?, audience?}` -> `{block, sources, pending, blocked, marker, profile, mode, timed_out, elapsed_ms, dropped, routed_to_cloud, audience}` |
| `POST /api/voice/rag/warm` | loads the small embedder now |
| `POST /api/voice/rag/ask` | `{session, function, archive_id, preview?, how?, voice_ok?}` -> `{question}` |
| `GET /api/voice/rag/ask/<id>`, `POST .../answer {answer}` | the question; an answer given by voice |
| `POST`/`GET /api/voice/rag/index` | queue / see the second vectors of archives: `ready`, `queued`, `building`, `missing`, `failed: why` |
| `GET /api/voice/consent`, `POST /api/voice/consent/answer {id, answer}` | the open questions; the person's answer (`once`, `meeting`, `always`, `no`) |

Tests: `tests/test_rag_realtime.py`, `tests/test_voice_rag.py`, `tests/test_heavy_forward_voice.py`, `voice/tests/test_rag_client.py`,
`voice/tests/test_rag_voice_wiring.py`, `voice/tests/test_rag_voice_settings.py` (stand-in embedder, fake server, fake devices; nothing needs a model or a GPU).
