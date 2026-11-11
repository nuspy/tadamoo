# Semantic search in the knowledge archives

Archives are searched by meaning, in any language, with a real embedding model, and the best passages are judged again by a reranker. The words
(BM25) stay: they are the search when there are no vectors, and a small tie-breaker when there are. The pipeline, the settings and the worker are
described here; the storage and the tools of a conversation are in [rag.md](rag.md), how documents become passages in [rag-ingest.md](rag-ingest.md),
where the GPU comes from in [vram-broker.md](vram-broker.md).

## What was measured (2026-11-11, the maintainer's PC)

A patent corpus: 264 passages in English, French and Chinese, and 147 real Italian questions written by the local model, each with its true
passage. Recall at 1 (R@1) unless said otherwise; the models are the Qwen3 embedding and reranker families in bf16.

| Method | R@1 | Note |
|---|---|---|
| BM25 alone | 12% | Chinese 0% |
| Qwen3-Embedding-0.6B | 60.5% | Chinese 40% |
| Qwen3-Embedding-4B | 69.4% | Chinese 72% |
| 4B and BM25 merged by reciprocal rank | 20-25% | merging **hurts** across languages |
| 4B cosine + 0.05 * BM25 / best BM25 of the question | 72.8% | MRR 0.816 |
| the same with weight 0.1 / 0.2 | 68.7% / 55% | a larger word share is worse |
| 4B, best 20 judged by Qwen3-Reranker-4B | 84.4% | MRR 0.896, Chinese 72%, R@5 100% |
| 4B, best 20 judged by Qwen3-Reranker-0.6B | 66.7% | too weak: it loses to no reranker at all |

Speed and memory: on the GPU the 4B embedder does about 40 passages a second (0.6B: 138) and the 4B reranker about 29 pairs a second. On the CPU the 4B
embeds a question in 2.2 s and a passage in 12.5 s (0.6B: 1.9 s a passage), so indexing needs the GPU. The 4B embedder peaks at 8.8 GiB, the 4B reranker at
12.5 GiB with batches of 8 by 1400 tokens (its weights are about 8 GiB).

The thresholds (`min_cosine`, `min_rerank`) and the question instruction are **not** measured: they are starting points, to tune with
`tools/rag_eval.py` on the person's own documents.

## The pipeline (`tadamoo/rag/query.py`)

1. The question is embedded (as a question: `Instruct: <task>\nQuery: <question>`; a passage is embedded as it is) and its cosine with every passage of the
   searched archives is one matrix product.
2. The score of a passage is `cosine + lexical_weight * bm25 / (best bm25 of this question)`, with `lexical_weight` 0.05. Only passages with a cosine of at
   least `min_cosine` count; a passage that only a word match finds is kept with just its word share. With no vectors for an archive: BM25 alone, as before.
3. Passages with the same text (case and spaces ignored) count once; the best 20 go on.
4. The reranker (Qwen3-Reranker prompt, probability of "yes") orders them, those below `min_rerank` are dropped, the first `top_k` are given. Without a
   reranker (off, no model, no GPU lent, an error) the order of step 2 stands and the answer's `note` says so.
5. Archives of the same search that have no vectors of this model are searched by words and come after the others, with a note to index them again.
6. Parent expansion and the citations are those of [rag.md](rag.md). `MIN_SHARE` of the recall applies to words-only hits and never drops a vector or reranked hit.

A hit carries `lexical` (BM25), `semantic` (cosine), `rerank`, `score` (the rerank score, else the blend, else BM25) and `stages` (which of `lexical`,
`dense`, `rerank` scored it); the answer carries `mode` (`hybrid` or `lexical`), `stages`, `reranked` and `note`. A rank fusion of the two lists is **not**
used (table above); `search.fuse` stays only for the evaluation tool to show it.

## Settings (`rag/settings.json`, the Knowledge tab, Semantic search)

| Setting | Default | |
|---|---|---|
| `embed_enabled` | true | off: every archive is searched by words |
| `embed_model` | "" | the folder name of the embedding model; empty: the biggest under 9 GiB |
| `rerank`, `rerank_model` | true, "" | the reranker, empty: the one of the embedder's size |
| `lexical_weight` | 0.05 | 0 to 0.3 |
| `min_cosine`, `min_rerank` | 0.3, 0.2 | 0 to 0.9, 0 to 0.95 |
| `idle_s` | 120 | 10 to 3600: seconds without a request before the worker and the lease are given back |
| `embed_python` | "" | a Python with torch and transformers; empty: `E:\Dev\venvs\docling-gpu`, else a system Python that has them (looked for once, in the background) |
| `embed_models_dir` | "" | a folder holding the model folders; empty: `rag/embedding` in each models folder of the program |

A model folder is one with a `config.json`; a name that holds `rerank` is a reranker. Changing the model marks every archive "index again": its vectors are
of another model and are never mixed with the new ones. Turning the service off is not a reason to index again.

## The worker (`tadamoo/rag/embed_worker.py`, `embed_client.py`)

The server's own Python has no torch, so the models run in a worker process of another Python: one JSON command per line on stdin, one answer per line on
stdout, `{"event": "progress"}` lines during a long job, a closed stdin ends it. Commands: `load(role, model_dir, device)`, `embed(texts, kind, instruction,
batch, max_len, dim?, out?)` (float16 vectors, base64 or an `.npy` file for big batches; `dim` cuts and normalizes again), `rerank(query, passages)`,
`unload`, `ping`, `quit`. `EmbedService` runs it, owns the lease and gives both back after `idle_s`.

The lease (owner `embedding`) is asked for in the biggest of three sizes the broker can give, from the weights of the two models (for the 4B pair):

| Size | Holds | MiB |
|---|---|---|
| both | embedder and reranker resident | 18000 |
| one | one at a time: the worker unloads one to load the other | 13000 |
| embed | the embedder alone: no reranking | 9500 |

Only `not_enough` goes down a size; `busy`, `unavailable` and `no_engine` mean no GPU. **Without a lease** a question is embedded on the CPU with the same
model (slow, correct), nothing is reranked, and indexing runs on the CPU with the tab saying that it is very slow and offering Cancel (a cancelled
document is not added; add it again when the GPU is free). A refused lease is asked for again after 20 s, and a worker on the CPU moves to the GPU when it
is granted. A worker that dies, or whose lease was lost, is started again by the next request. The harness (agent) does not start a second worker: its
store asks the server's `/api/rag/search` (loopback only) and falls back to words if the server does not answer.

## Storage

`embeddings.npy` is float16 on disk and read back as normalized float32. `meta.json` keeps `embed_model`, `embed_dim` and `embed_version` (how a passage
becomes text and a vector, `q3e-1`): another model or version means "index again". The text embedded is the passage with its `path` in front (the chunker's
`searchable`). Adding a document embeds only its passages, out of the store's lock, with progress and cancel as the reading has them; "Index again" is a job
too and leaves the archive searchable meanwhile.

Not done: splitting long prose where the meaning changes (the `valleys` hook of the chunker). It would make the cutting depend on the state of the embedder,
and nothing measured says it helps.

## Measuring: `tools/rag_eval.py`

```
python tools/rag_eval.py --archive <id> --questions questions.json [--rag-dir DIR] [--url http://127.0.0.1:7242] [--json report.json] [--fake]
```

`questions.json` is `{"questions": [{"passage": P, "question": "..."}]}`; `P` is the passage's position in the archive, `"<document id>:<passage number>"`, or
text the passage holds. It prints R@1, R@5, R@10, MRR and the time per question for BM25, dense, fused (rank fusion), dense with the word share, and the
whole search, over all questions and by the language of the passage (en, fr, zh, other, guessed from the script and the stop words). It uses the archive's
own vectors, so index the archive first; a real run borrows the GPU from the running server. `--fake` uses a deterministic stand-in and needs nothing.
`--weight`, `--min-cosine`, `--min-rerank` and `--no-rerank` try other settings.

Tests: `tests/test_rag_embed.py` (a stub worker that speaks the real protocol, a fake lease: GPU path, refused lease, sizes, idle exit, cancel, model change,
fusion formula, thresholds, rerank, duplicates), `tests/test_rag_eval.py`.
