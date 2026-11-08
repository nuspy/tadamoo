# The length of a reply a model writes

Some models write a long text well in one reply and others lose the thread, repeat themselves or stop early. The server measures it once for
each model file and keeps the number with the model.

## The measurement

A model cannot count what it writes, so it is not asked for a length. When a model is loaded for the first time (and `measure-length` is on),
the server asks it, with the thinking off, for a text with no end: numbered encyclopedia entries, one after the other, never concluding. The
request has a cap of 30,000 tokens, set and counted by the server (less when the context leaves less). A model usually closes the reply long
before that, whatever it is told, so each time it stops by itself it is asked to go on, with its own text before it, until it reaches the cap,
falls into a loop or will not go on. What is kept is what came out:

- **the tokens written**, as the server counted them, in all the pieces, and the checkpoints at 10,000, 20,000 and 30,000 tokens: reached or not,
  and whether the text was still clean there;
- **where it first closed a reply** (`first_stop_tokens`): the length of a single reply to a request for a long text, which is not the length it can write;
- **where it would not go on**, if it did not (`natural_tokens`);
- **where a loop began**, if one did: each piece is looked at every 4,000 characters and the try is cancelled as soon as it is a loop, so GPU time is
  not spent on it. The tokens before the loop count as reliable.

The number is a limit (`bounded`) only when the cap or a loop bounded it. A model that stops going on has not shown where it degrades, and its
number is not used until a reply shows one. If the first text does not reach the cap clean, a second kind (a novel with no last chapter) is tried
too, so that the limit of one prompt is not taken for the limit of the model. It takes minutes of GPU and holds the model while it runs. The Loader page
shows the step, has a **Stop** button, and **Measure length** repeats it. Nothing is measured for the mock engine, or when `measure-length` is off.

## The record

One JSON file for each model, in the user data folder, `lengths/<hash of the model file's meta section>.json`:

| Field | Meaning |
| --- | --- |
| `model` | `name`, `quant`, `path` (full), `size_bytes`, `modified`, `meta_hash` |
| `engine`, `context` | the engine version and the context the measurement ran in |
| `settings` | what was in force: `relaxed_drafting`, `spec`, `temperature`, `top_p`, `reasoning_effort`. The Loader says when relaxed drafting is not what it was |
| `rungs` | for each text: `prompt`, `cap`, `tokens`, `words`, `pieces`, `stops`, `first_stop_tokens`, `finish`, `clean_tokens`, `looped`, `natural`, `reached_cap`, `checkpoints`, `seconds` |
| `reliable_tokens`, `reliable_words` | the most written before any loop |
| `first_stop_tokens` | the longest first reply of a text, before it was asked to go on |
| `natural_tokens`, `natural_words` | the most written when the model would not go on |
| `reached_cap`, `bounded` | a text went to the cap, clean; the cap or a loop bounded the number |
| `observed_max_tokens` | raised by use (below) |
| `complete`, `stopped_by`, `measured_at` | whether it finished, why it stopped, when |

## Use

The record is read when the model loads. `GET /v1/status` returns it as `length` (`limit_tokens` is the larger of `reliable_tokens`, when `bounded`, and
`observed_max_tokens`; `{"known": false}` before a measurement). A reply, thinking and answer together, that ends by the model's own stop, without
a loop or a cap, and is longer than the number, becomes the new number. The number only rises. It cuts nothing: it is where the
harness writes a text in checked parts instead of one reply (see [long answers](../longform.md)). A record made by an older method is ignored
and measured again.
