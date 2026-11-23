# Summaries at four levels

Settings > Inference > *Summarise a text* writes a summary of a pasted text or a file (up to 8 MB) at the level chosen. It runs in the technical session
([harness.md](harness.md), *Technical session*); where the session is off or missing it takes an ordinary slot and can wait behind a conversation.
Code: `serve/summarylevels.py`, `POST /api/summarize`, `serve/web/summarylevels.js`. The use is `summary_levels` in `side_uses.json` (switch and the size of a part).

| Level | What it keeps | Size |
|---|---|---|
| `integral` | every fact, example and figure, in order; only repetition, filler and asides go | about 85 % of each part |
| `medium` | the main points with their reasons and figures, without examples | about 50 % of each part |
| `high` | the essential content under short headings | about 20 % of the whole text |
| `executive` | one sentence on what the text is and concludes, then at most 7 points | at most 600 tokens |

`integral` and `medium` are written part by part and the parts are joined, so the text can be many times larger than the session. `high` and `executive`
are made from notes of the parts, in rounds ([digest.py](../tadamoo/digest.py)). Code, commands, paths, URLs and numbers are copied as written at every level.
The language of the summary is the text's own unless one is asked for.

The harness, not a model, checks every call: the answer is not empty, does not repeat itself and is no longer than the level allows; if not it is asked again
with what was wrong (three attempts). An answer cut off halves its part; a part nobody could write stays as it was. The Stop button of the technical session ends the work.
