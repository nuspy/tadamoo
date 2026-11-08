# The length of a reply a model writes

Some models write a long text well in one reply and others lose the thread, repeat themselves or stop early. The server measures it once for
each model file and keeps the number with the model.

## The measurement

When a model is loaded for the first time (and `measure-length` is on), the server asks it, with the thinking off, for a chapter of a book of
about 2,000 words, then 5,000, 10,000, 20,000 and a whole book of 60,000. It stops at the first try that:

- loops (a stretch of the text is mostly the same units over and over, or the server's passage-loop check fired);
- is cut by the context or the output cap (nothing longer can be asked);
- ends by itself with much less than was asked (less than 60 % of the words).

It takes minutes of GPU and holds the model while it runs. The Loader page shows the step, has a **Stop** button, and **Measure length** repeats it.
Nothing is measured for the mock engine, or when `measure-length` is off.

## The record

One JSON file for each model, in the user data folder, `lengths/<hash of the model file's meta section>.json`:

| Field | Meaning |
| --- | --- |
| `model` | `name`, `quant`, `path` (full), `size_bytes`, `modified`, `meta_hash` |
| `engine`, `context` | the engine version and the context the measurement ran in |
| `rungs` | for each try: `asked_words`, `words`, `tokens`, `finish`, `clean`, `natural`, `looped`, `seconds` |
| `reliable_tokens`, `reliable_words` | the longest reply without a loop |
| `natural_tokens`, `natural_words` | the longest reply the model ended by itself |
| `observed_max_tokens` | raised by use (below) |
| `complete`, `stopped_by`, `measured_at` | whether the ladder finished, why it stopped, when |

## Use

The record is read when the model loads. `GET /v1/status` returns it as `length` (`limit_tokens` is the larger of `reliable_tokens` and
`observed_max_tokens`; `{"known": false}` before a measurement). A reply, thinking and answer together, that ends by the model's own stop, without
a loop or a cap, and is longer than the number, becomes the new number. The number only rises. It cuts nothing: it is where the
harness writes a text in checked parts instead of one reply (see [long answers](../longform.md)).
