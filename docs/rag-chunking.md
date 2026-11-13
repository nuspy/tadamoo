# Cutting documents: what was compared

`tools/rag_chunk_eval.py` builds every way of cutting the saved blocks of an archive in memory (the real archive is only read), embeds the passages with the embedder of
the archive (Qwen3-Embedding-4B, through the lease), searches the 96 ER100 questions with the same search as the program (`tadamoo/rag/query.py`: cosine plus a word
share, the same floors; with the reranker where the GPU could lend it) and judges each way by its own passages. The code is in `tadamoo/rag/ingest/chunker.py` (the
structural cut, `target` and `hard` are arguments and the settings `chunk_target` and `chunk_hard`) and `tadamoo/rag/ingest/semantic.py` (the semantic splitter).

## How a way of cutting is judged

The evidence of a question is the text of the passage the question was made from. A way of cutting is judged by whether the passages it makes carry that **text**, not by a
passage number, which moves with every cut: a passage `holds` the evidence when it contains at least 80% of it (pieces of 30 characters, normalized), `partly`
at 30%. R@k is the share of questions for which a hit among the first k holds at least 30% of the evidence of the right document (a parent counts by the text it hands over,
several hits of one parent count once). `split` is the number of questions (of 96) for which no single passage holds 80%: the direct measure of fragmentation. Languages
are by the evidence: en 48, fr 19, zh 24 questions, about.

Limits to keep in mind: 96 questions give about +-5 points of standard error, so differences under 5 points are noise; the questions were made from the current
structural passages, which favours that cut and makes every smaller or larger one split the evidence; one archive of seven patents; the reranker could be lent for
only part of the runs (the model was loading on the same GPU), so the reranked column is not complete.

## Size of a passage (structural cut), 96 questions

Without the reranker (cosine plus word share) and, where it ran, with it. `n` passages, mean and largest passage in tokens.

| target/hard | n | mean | max | split | R@1 | R@5 | R@10 | MRR | with reranker R@1 / R@5 / R@10 / MRR |
|---|---|---|---|---|---|---|---|---|---|
| 150/250 | 5117 | 143 | 589 | 84 | .438 | .646 | .698 | .513 | .479 / .719 / .740 / .576 |
| 250/400 | 3155 | 217 | 589 | 57 | .490 | .760 | .802 | .595 | .677 / .875 / .885 / .763 |
| **350/600 (now)** | 2269 | 289 | 601 | 0 | .458 | .729 | .781 | .559 | .635 / .854 / .885 / .733 |
| 500/800 | 1603 | 393 | 800 | 21 | .448 | .729 | .802 | .558 | .667 / .823 / .833 / .722 |
| 800/1200 | 1174 | 532 | 1192 | 11 | .458 | .719 | .833 | .567 | .604 / .844 / .875 / .708 |
| 1200/2000 | 932 | 667 | 1946 | 5 | .417 | .677 | .698 | .521 | .573 / .740 / .750 / .647 |
| 2000/3000 | 714 | 871 | 3000 | 1 | .344 | .635 | .698 | .464 | not run |
| old flat, 1000 characters, 150 overlap | 2955 | 252 | 329 | 59 | .479 | .750 | .781 | .586 | .615 / .812 / .844 / .705 |

Reading: from 250 to 1200 tokens the numbers are on a plateau (differences of 1 to 4 points, inside the noise); at 150 tokens and at 1200 and more they fall (-10 to -16
points at 150 with the reranker, -6 to -13 at 1200 to 3000), and at 150 nine questions in ten have their evidence cut across passages. The old flat cut is as good as
the structural one without the reranker and 2 to 4 points behind with it: the structural cut earns its keep by what it keeps (page, section, tables whole, citations), not
by these numbers. **The current 350/600 stays the default**; the size is now a setting (`chunk_target`, `chunk_hard`, tokens, `hard` at least 1.2 times `target`) that is part of the
`ingest` of the archive's meta, so changing it marks the archives "index again". Nothing here says a larger size would be better.

## Semantic splitting and other ways, no reranker (the GPU could not lend it)

| way | n | mean | max | split | R@1 | R@5 | R@10 | MRR | en R@5 | zh R@5 | build |
|---|---|---|---|---|---|---|---|---|---|---|---|
| structural 350/600 (reference) | 2269 | 289 | 601 | 0 | .458 | .729 | .781 | .559 | .667 | .708 | 0 s |
| structural, sentence valleys inside long paragraphs (`sem-valleys`) | 2350 | 281 | 601 | 1 | .438 | .750 | .802 | .555 | .708 | .708 | 3 s + 166 calls to the embedder |
| the same, small neighbours joined when close (`sem-valleys-merge`) | 2248 | 294 | 601 | 1 | .448 | .750 | .802 | .562 | .708 | .708 | same |
| structural, small neighbours joined when close (`struct-merge`) | 2180 | 301 | 601 | 0 | .458 | .729 | .802 | .564 | .667 | .708 | 0 s + 1 embedding of the passages |
| semantic, no size target (`sem-free`: valleys between units, soft minimum 120, safety maximum 2000, tables whole to 2400) | 1337 | 465 | 2000 | 27 | **.281** | .708 | .760 | .449 | .688 | .583 | 415 s (every unit embedded) |
| the same with no joining of small neighbours | 1371 | 453 | 2000 | 29 | .271 | .688 | .750 | .440 | .688 | .583 | same |
| **parent by neighbours**: match 250/400 passages, give the passage and the one before and after | 3155 | 217 (parent about 650) | 589 | 0 | **.573** | **.792** | **.854** | **.667** | .750 | .792 | 1.4 times the passages |
| parent by section: match 250/400 passages, give the section (8000 characters at most) | 3155 | 217 | 589 | 37 | .333 | .542 | .594 | .423 | .667 | .375 | same |
| structural with the facts read by rule written in front of each passage (`+facts`) | 2269 | 289 | 601 | 0 | .427 | .688 | .760 | .539 | .646 | .625 | 0 s + 1 embedding |

Fragmentation: `sem-free` leaves 27 questions across passages, the sentence valleys 1. The three facts that no agent answer held (F11, F13, F14) were reached in the first
five hits, with a search worded on the fact, by every way except 1200/2000 without the reranker (F14).

Reading, with what is measured:

- **A semantic splitter with no size target loses**: -18 points at R@1, -2 at R@5 and R@10, and it costs one embedding of every unit (415 s here) besides the embedding of the passages. Its passages
  are larger (465 tokens, up to 2000), which is what the size sweep says is neutral to slightly worse; the cuts it makes at valleys do not make up for it. **Not turned on.**
- **Valleys inside long paragraphs only** (the seam of the structural cut) and **joining small neighbours** that are close in meaning are within the noise (R@5 and R@10 +2, R@1 -2 to 0). They fix the
  fragmentation of 166 long paragraphs, but no gain in retrieval can be shown on 96 questions: **left off**, available as `semantic.sentence_valleys` and `semantic.semantic_chunks`.
- **Parent retrieval by neighbours is the one that earns something**: +11 points at R@1, +6 at R@5, +7 at R@10 (MRR +.11) against the current cut with no reranker,
  and it beats the single passages of the same size (250/400: .490 / .760 / .802) and the large ones of about the same text (800/1200: .458 / .719 / .833). It
  matches small, precise passages and hands the model the passage with its neighbours. Its costs: 1.4 times the passages to embed and keep, and about three times the text
  for each hit, so the injection budget (`MESSAGE_CHARS` 6000 for 4 passages, now about 1500 characters each) and the number of passages would have to be set again; the
  reranker, which judges the small passages, was not available for this run, so its effect with the reranker is **not measured**. Parent by section is bad (sections are too large and uneven).
- **The facts read by rule do not belong in the searched text**: -3 points. They are kept for filters and for display (`docs/rag-ingest.md`).
- **Summaries and questions written by a model** (`docs/rag-ingest.md`, "Descriptions by a model") are in the searched text, but their effect needs a model to write them: **not measured**.
  The plan for the live phase is one run on a copy of the archive, with the model the person sets, then this tool with the variants `struct-350-600` against the same with
  `summary` and `questions` in the searched text.

## What costs what

The embedder takes about 33 characters of text per millisecond on an idle GPU, so one way of cutting costs about the same to embed (every variant holds the same 2.2 million
characters; the small passages add the repeated path, +10% at 150/250). Measured in this run on a shared GPU: 12 634 texts, 8.5 million characters, 929 s. The reranker reads
20 passages for each question, so its cost goes with the mean passage: 5.8 thousand tokens a question at 289 tokens, 10.6 thousand at 532, 13.3 thousand at 667, and about 40 thousand for
a passage of 2000 (not timed: no GPU was lent for the reranker). The prompt budget follows the same rule: 4 passages of 289 tokens are 1.2 thousand tokens, 4 of 667 are 2.7 thousand, and a block
of `MESSAGE_CHARS` 6000 holds one passage of 1500 tokens, so a larger size would need the injection budget rescaled.

## What was decided

Keep the structural cut at 350/600 and the semantic splitter off; the size is a setting. Parent retrieval by neighbours is the next candidate: measure it with the reranker
(needs the GPU) and then give `knowledge_search` and the injection the passage with its neighbours, with the budget set again.
