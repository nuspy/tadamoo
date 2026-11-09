# Server security: the origin guard

Without an API key the server trusts a request because it comes from this PC. A browser on this PC is on this PC too, so a page of any
website could send requests to `127.0.0.1:7232`, or point a DNS name of its own at `127.0.0.1` (DNS rebinding) and read the answers as if
it were the server's page. The origin guard (`serve/origin_guard.py`) refuses those requests. `serve/handler.py` asks it once per
request, before any route, for every method and for WebSocket upgrades.

## The two rules

**Host rule.** The `Host` header must name this PC by:

- an IP address (`127.0.0.1`, `192.168.1.20`, `100.x.y.z`, `[::1]`, any other);
- `localhost` or a name ending in `.localhost`;
- the PC's own name, as `hostname` prints it (for example `PLUTO-MAX`, in any case);
- a name listed in `server.allowed-hosts`.

Any other name is refused with `403` and `"code": "host_not_allowed"`. A request without `Host` (HTTP/1.0) passes.

**Origin rule.** A request that carries `Origin` must come from this server's own pages: the same host and port as its `Host`, the other
loopback spelling of the same port (`localhost` and `127.0.0.1`), or a name in `server.allowed-hosts`. A request marked
`Sec-Fetch-Site: cross-site` that is not a GET, HEAD or OPTIONS is refused as well. Both answer `403` with `"code": "cross_origin"`.
A browser always sends `Origin` on a WebSocket, so a page of another site cannot open the voice gateway's socket.

callAPIcall's routes (`docs/voice/api.md`) answer the refusal in their own shape: `{"error": "...", "code": "forbidden", "reason": ...}`.

## What is not affected

- **Programs.** curl, the SDKs, Python programs, the desktop app's own calls and the phone app send neither `Origin` nor `Sec-Fetch-*`,
  and reach the server by an IP address, `localhost` or the PC's name: nothing changes for them.
- **The server's page.** The web page and the desktop app (WebView2 on `http://127.0.0.1:<port>/`) send an `Origin` equal to `Host`.
- **`/v1/*`.** The OpenAI and Anthropic interface keeps `Access-Control-Allow-Origin: *`: every page may use it, so neither rule applies.
- **A secret in a header.** A request with the API key (`Authorization: Bearer` or `x-api-key`) or the control secret
  (`X-Tadamoo-Control`) passes both rules: a page of another site cannot send those headers. The cookie does not count.
- **With an API key set**, the Host rule is off: every route but the open ones (`/health`, the page's files) needs the key, which a
  rebound page does not have. A tunnel's name (for example `*.trycloudflare.com`) works with the key.
- **The listener for phones** has no Host rule either: every request there needs a key.
- The loopback-only checks of each route stay as they were.

## Reaching the PC by name

By IP address and by the PC's own name nothing is needed. For any other name, list it in the `--config` file:

```json
{"server": {"allowed-hosts": ["pluto-max.tail1234.ts.net", "pluto-max.lan"]}}
```

or give `--allowed-hosts NAME` on the command line, once per name. Names only: no scheme, port, path or wildcard; an IP address is always
allowed and is refused in the list. The change applies when the server is started again.

- **Tailscale MagicDNS**: the name is `<machine>.<tailnet>.ts.net`; `tailscale status --json` shows it as `Self.DNSName` (without the
  final dot). The short MagicDNS name (`pluto-max`) is usually the PC's own name and passes already.
- **The LAN**: a router's name such as `pluto-max.lan`, `pluto-max.home` or `pluto-max.fritz.box` must be listed; the bare name passes.
- **Docker**: a container that calls `http://host.docker.internal:7232/api/...` needs `host.docker.internal` in the list
  (`/v1/*` works without it).
- **A reverse proxy or tunnel that rewrites `Host`** to `127.0.0.1`: list the public name, so the page's `Origin` is accepted.

`"allowed-hosts": ["*"]` turns the Host rule off (every name is answered, as before the guard). The Origin rule has no switch.

## Logs

A refused request writes one line on the server's stderr, which `GET /api/logs` (the Logs view) shows too:
`[server] refused POST /api/voice/models-dir: cross_origin`.
