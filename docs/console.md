# The console and the stats page

What the server prints while it runs and the page it serves. The settings behind them are in [running](running.md) and [configuration](configuration/index.md).

## The console log

Every server line starts with `[server]` and a label padded to a column; values are `key=value`. Rates are `t/s`,
sizes `GiB`/`MiB`, times `s`. Startup prints one block, starting with a legend:

```
[server] legend   pp=prompt read  tg=generation  c=cached  acc=drafts accepted  dl=tokens per window  hit=expert cache  pcie=routed experts over PCIe
[server] engine   ctx=262144 kv=int8 vram-reserve=500MiB pool=8 batch=4 (from config)
[server] sampling temp=1.0 top_p=0.95 top_k=20 min_p=0.0 pres=0.0
[server] vision   mode=dynamic gpu=0 (RTX 5090) load=per-image
[server] auto     peer-excl=off resident=64.0GB pool-affinity=auto
[server] ready    http://127.0.0.1:8080/v1  openai+anthropic  stats http://127.0.0.1:8080/
[server] loaded   68.4GiB 37.5s  cuda0=26.6GiB ram=41.2GiB cuda1=0.6GiB cuda1-copy=14.2GiB
[server] kv       2.6/16.3GiB cuda0 elastic slots=4
[server] slots    par=4 ctx/slot=262144 (slot-full-context), others queue
[server] weights  loaded, ctx=262144 images=on
```

`sampling` marks a value set on the command line `(cli)` or in the config file `(config)`; `auto` is the engine's
picked settings (see [configuration](configuration/index.md)). A warning or error that comes later reads
`[server] warning: text` or `[server] error: text`.

While a prompt is read, a slot prints `pp=<read>/<total>` with the read rate and the elapsed time about every 5 s:

```
[server] s2 read  pp=24576/66836 9150t/s 3s
```

While requests generate, one `live` line every 2 s covers every generating slot: `tg=<tokens>/<max> <rate>`.
Slots are counted from 1, as on the stats page. `total` is the sum of the slots' rates. Acceptance is not shown
live; the `done` line carries the engine's exact figures.

```
[server] live | s1 tg=454/2048 113.7t/s | s2 tg=436/2048 119.0t/s | total 233t/s
```

Each request ends with one slot-tagged `done` line:

```
[server] s1 done  pp=96/89c 0.1s  tg=1720 119.6t/s acc=65% dl=2.9  ttft=0.1s  end=stop  hit=89.7%  pcie=39.3%
[server] s2 done  pp=66836/0c 7.3s 9204t/s  tg=512 281.0t/s acc=58% dl=2.7  ttft=7.3s  end=length
```

- `pp=<prompt tokens>/<cached>c <read time>`, with the read rate once at least 512 tokens were newly read; the rate
  counts newly read tokens only.
- `tg=<tokens> <rate>`. `acc` is drafts accepted over drafts offered for the request, `dl` the average tokens per
  window; both are left out when the request did not draft.
- `ttft` is the time to the first token; `end` is the finish reason (`stop`, `length`, `cancelled`, `disconnect`,
  `error`).
- `hit` is the expert cache hit rate and `pcie` the share of routed experts read over PCIe; both appear when the
  engine reports them.

## The stats page

The server's own address (`stats` on the `[server] ready` line above) serves one page that reads `/metrics`, `/slots` and `/api/logs`
once a second:

- GPUs: every card the engine uses, with VRAM and 60 s charts of load and power (one card adds temperature; three or
  more cards are a table with a chart of their mean load and total power). System: CPU, RAM, disk reads, driver and
  CUDA. Experts: where the last request's routed lookups were served (VRAM, peer card, CPU).
- Speed: decode and prefill rates with 60 s charts (one line per slot with `--parallel`), the last prompt and its prefix
  cache hit, and the fastest whole request since the server started (at least 256 output tokens for decode, 4096
  newly read prompt tokens for prefill).
- Slots (with `--parallel`): each busy slot's rate, output against its limit, context, time to first token, prefix
  hit and tokens per window; Accept is the slot's last finished request. Requests: the newest finished requests.
  Queue: the requests waiting for a slot or for the single stream.
- Logs: the console lines.

`/metrics` carries the history behind the charts in `history` (`tok_s`, `prefill_tok_s_mean`, `slot_tok_s` one series
per slot, `gpus` with `util`, `power` and `temp` per card) and the fastest requests in `best`.
