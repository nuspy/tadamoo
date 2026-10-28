# Security monitor

An optional, continuous monitor (GUI: Monitor > Security) that watches the network connections and the disk use of the
processes that run the model, and flags what looks suspicious. It never blocks anything: model downloads, providers and
the agent's tools keep working. Its purpose is measurable evidence: every report is written to a tamper-evident log.

Code: `serve/netwatch.py` (watcher, log, exceptions), `serve/netwatch_api.py` (routes), `serve/web/netwatch.js` (page and
alert dialog). Off by default; turn it on with the switch on the page.

## What is watched

- Engine processes, by executable name: `basalt`, `llama-server`, `strata`, plus the names added in the settings. They have
  the role `engine`.
- The Basalt server process and every descendant of it (the agent's shell and the programs it starts). Role `server` and
  `tool`. Tools such as `curl` or a browser are expected to use the network, so a plain public connection from them is not
  suspicious by itself; only tags make their reports suspicious.
- With `alert_on_system_wide` on, every process of the machine (role `other`). Off by default because it is noisy.

Each poll (default every 2 s) reads, per watched process, the inet connection table (TCP and UDP, IPv4 and IPv6) and the
open files. A new connection to a non-loopback, non-link-local remote becomes an event. Reverse DNS runs in a background
thread with a cache and never delays a poll. The optional geo database is a local CSV (`cidr,asn,country`); nothing is
downloaded.

## Reports

Fields: `id`, `ts`, `kind` (`network`, `disk`, `persistence`, `manual`), `pid`, `process`, `role`, `proto`, `remote_ip`,
`remote_port`, `host`, `asn`, `country`, `tags`, `behavior`, `resource`, `suspicious`, `suppressed`, `suppressed_by`,
`manual`.

Disk reports use `proto` `file` and put the path in `host`. Persistence reports use `proto` `run_key` or
`scheduled_task` and the process `(system)`.

### Tags

| Tag | Meaning |
|---|---|
| `alibaba_cn_domain` | host name contains aliyun, alicdn, dashscope, modelscope, qwen, alibaba, taobao, aliyuncs or tbcdn |
| `proxy_env` | the process environment has HTTP_PROXY, HTTPS_PROXY, ALL_PROXY or NO_PROXY (only when the OS lets us read it) |
| `system_proxy` | Windows Internet Settings has a proxy enabled |
| `local_proxy_listener` | a process listens on a loopback proxy port (1080, 8080, 3128, 8118, 9050, 9150, 7890, 7897 and similar) |
| `doh_or_tor` | remote port 853, 9001 or 9030, or a known DNS-over-HTTPS host (dns.google, cloudflare-dns.com, dns.quad9.net) |
| `no_agent_call` | the engine connected with no outward tool call recorded in the last 30 s; only used once the agent calls `NetWatch.note_tool_call(ts, tool, target)` |
| `disk_outside_expected` | a watched process has a file open for writing outside the expected folders |
| `archive_or_staging` | an archive-like file (zip, 7z, rar, tar, gz, tgz, xz, zst, enc, bin, dat), a hidden file, or an NTFS alternate stream (`:` in the name) outside the expected folders |
| `persistence` | a new Run-key value (HKCU or HKLM) or scheduled task since monitoring started (Windows only) |

### Suspicion rule

A report is suspicious when it carries any tag, or when an `engine` process has a connection to a public address (not
loopback, not private LAN, not the 100.64.0.0/10 range). Constants in `serve/netwatch.py`: `ALERT_TAGS`,
`ALERTING_ROLES`.

Expected folders default to the user data folder, the temp folder, the model folders and the engine cache; add project
folders in the settings.

## Exceptions

Two kinds, both Python regular expressions searched anywhere in a string (`re.search`):

- `behavior`: matched against `"{process} {proto} outbound port {remote_port} tags={comma-separated sorted tags}"`.
- `resource`: matched against `"{remote_ip} {host}"`.

A report is suppressed when any enabled behaviour exception or any enabled resource exception matches. Suppressed reports
are still written to the log with `suppressed: true` and `suppressed_by` (the exception id), so a later review sees
them; they only stop opening alerts. An invalid expression is rejected with a clear error.

An exception is `{id, kind, pattern, note, enabled, created_ts}`. They are stored with the settings.

## Alerts and the register

While the web page is open and monitoring is on, it polls `/api/netwatch/alerts` every few seconds and shows one dialog at
a time that says what happened in plain words. The dialog has two separate editable expressions (prefilled with the
escaped behaviour and resource strings), each with its own "Add this exception" box. "Add selected exceptions" saves the
ticked ones and closes the alert; "Ignore this time" closes it without adding anything.

The Security page also holds the register of all reports:

- add an exception from any row (same two-field form);
- delete one report, the selected ones, all, or those older than N days (one press, no confirmation);
- add a report by hand (process, protocol, remote address or host, port, tags, note); it is stored with `manual: true`
  and does not raise an alert.

## Log and integrity

Files in the user data folder: `netwatch.json` (settings and exceptions, atomic write) and `netwatch.log.jsonl` (reports).

The log is hash-chained JSONL. Each line is `{seq, ts, type, data, prev, hash}` with
`hash = sha256(prev + canonical json of {seq, ts, type, data})`. Record types: `event`, `resolved`, `exception`,
`deleted`, `compacted`. `GET /api/netwatch/verify` recomputes the chain and reports every broken line; editing, removing
or reordering a line breaks it.

Deleting reports keeps the log verifiable:

1. a `deleted` tombstone record lists the removed ids;
2. the log is compacted: the dropped lines are removed and the rest is re-chained from the start;
3. the new log ends with a `compacted` marker holding `previous_head` (the head hash before compaction),
   `previous_records`, `removed_records`, `removed_ids` and the reason.

So deletions are visible afterwards, and a log that was truncated without going through this path fails verification.
`max_log_mb` (default 0, no limit) trims the oldest reports the same way.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | run the watcher; started at server start only when true |
| `interval_s` | 2 | seconds between polls (0.5 to 3600) |
| `watch_extra_names` | [] | more process names treated as engines |
| `expected_folders` | [] | more folders where writing is expected |
| `geo_db_path` | "" | local CSV with `cidr,asn,country` |
| `alert_on_system_wide` | false | also watch every process |
| `max_log_mb` | 0 | log size limit, 0 = none |

## API

All routes are under the usual authentication and, because exceptions could hide evidence, accept loopback clients only.

| Route | Purpose |
|---|---|
| `GET /api/netwatch` | status: enabled, coverage, watched processes, settings, open alerts, counters, psutil availability |
| `POST /api/netwatch/settings` | change settings; starts or stops the watcher |
| `GET /api/netwatch/events?since=&limit=&only=suspicious\|all` | reports |
| `POST /api/netwatch/events` | add a report by hand |
| `POST /api/netwatch/events/delete` | body `{ids:[...]}`, `{all:true}` or `{older_than_days:n}` |
| `GET /api/netwatch/alerts` | open alerts, each with suggested escaped patterns |
| `POST /api/netwatch/alerts/<id>/resolve` | body `{add_behavior, behavior_pattern, add_resource, resource_pattern, note}` |
| `GET /api/netwatch/exceptions` | list |
| `POST /api/netwatch/exceptions` | create or update by id |
| `POST /api/netwatch/exceptions/<id>/delete` | delete |
| `GET /api/netwatch/verify` | check the log chain |
| `GET /api/netwatch/export` | reports as JSON |

## Limits

- The monitor reads the operating system's connection table at an interval. A connection that lasts only milliseconds can
  be missed. Full packet capture (ETW) is not implemented. Without administrator rights some processes cannot be read;
  `GET /api/netwatch` reports `coverage.admin`.
- A weights file does not execute code, and the monitor looks at processes, not at model files. It can show suspicious
  behaviour; it cannot prove that nothing was sent.
- `psutil` is optional; without it the monitor reports `unavailable` and does nothing.
- Persistence checks are Windows only.
- The `no_agent_call` tag stays inactive until the agent code reports its outward tool calls with `note_tool_call`.
- A long-run test (days of monitored use) is a separate repository, not part of this one.
