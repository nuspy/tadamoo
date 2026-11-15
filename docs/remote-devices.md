# A person on another device

When the person uses TaDaMoo from a device that is not the machine the server runs on (a web page opened from another PC or a phone, the Android app, Telegram), an address that names
the server's machine (`localhost`, `127.0.0.1`, `0.0.0.0`, a path on its disk) means nothing to them. Every link to something the server holds has to be an address they can reach.

## What the server receives, and what it sends

| Received | Used for |
|---|---|
| the client's address (`client_address`) | loopback or one of this machine's own addresses: the person is at the machine; anything else: another device |
| the `Host` header of the request | the address the client reached the server at, so it works for that client (a Tailscale address or name, a LAN address) |
| the key (`Authorization`) | `phone` when it is a device key (`handler._reader`) |
| the Android app's configured address | not sent; it is the `Host` of every request the app makes |

What the server and the model put in front of the person, and whether it is right for a remote client:

| Where | Form | Remote client |
|---|---|---|
| Pictures and files of tools (`[file:<id>:<name>]`, `tadamoo-file:<id>`, `tadamoo-path:<path>`) | a reference, not an address | right: the page and the app resolve it against their own server address, with their own key |
| Exports (documents), downloads, audio and voice, avatars, artifacts, Telegram attachments | routes of this server (`api/...`), relative | right in the page and the app; Telegram has no key (see below) |
| Text of an answer or a tool result with an address of this machine (`http://localhost:8000`, `http://0.0.0.0:5173`, `http://127.0.0.1:7232/api/...`) | absolute URL written by the model or a program | **wrong**: opens the person's own `localhost`; a picture written so stayed a dead link |
| A path of the server's disk in an answer (`C:\...`, `/home/...`, `file:///...`) | path | **wrong** for a remote client; right only when it is inside the conversation's folder, where it can be a `tadamoo-path:` reference |
| The model's own idea of where the person is | nothing was told | **wrong**: it wrote local addresses and paths |
| MCP Apps sandbox (second port) | port bound to the address of the first request only | **wrong** (phone could not reach it); fixed here |
| Telegram answers | plain text | had addresses of this machine; a file route needs a key, so it is not turned into a link |

## What is done

- **Detection** (`serve/netaddr.py`): for each message the base address is `""` at the machine, else `http://host:port` with, in this order, the `Host` the client used (when it is a
  host: a header that is not one is ignored), the Tailscale address (the `tailscale` program, else an interface in 100.64.0.0/10), the LAN address. It is kept in the conversation
  (`address` of its record) and sent to its process with each message; a sender that knows nothing leaves it as it was.
  Behind a reverse proxy the server sees the proxy as the client: a request from a loopback, private-network or Tailscale address that carries `X-Forwarded-Proto` and/or `X-Forwarded-Host`
  gives `scheme://host` of what the proxy forwarded (the port only when the forwarded host names one), and the person is not at this machine; the same headers from any other address are
  ignored (`serve/netaddr.py` `forwarded`).
- **The model** is told once, and again only when it changes, in the `[state]` note (`address=` in `tadamoo/capstate.py`, the same mechanism as `reader=`): that the person is on another
  device, the address, and to give every link as a full address of it, never localhost, a relative path or a path of the disk. Nothing is added for a person at the machine, and nothing
  is in the cached start of the request.
- **Rewriting** (`tadamoo/netaddr.py`, `serve/web/netaddr.js`, `mobile/lib/src/net_address.dart`, the same rules): an address of this machine in an answer becomes the host the page or
  the app reaches the server at (port and rest as written); a file route of this server written as an address becomes `tadamoo-file:` or `tadamoo-path:` (so a picture loads with the
  page's own key); a path inside the conversation's folder as the target of a link or picture becomes `tadamoo-path:`. Code is left as it is. A page at the machine changes nothing.
- **Telegram** answers get the first rule (the host becomes the server's Tailscale or LAN address); a link to a file route is not made, because it would need a key in the address.
- **MCP Apps**: the sandbox is listened for on the address each request came to.

## Not done

- A link to a file of the server that works with no key (a signed address): it would put a credential in a link; the page and the app resolve references with their own key instead.
- The direct Chat (the model-only chat that does not go through a conversation) is not told where the person is.
- Rewriting in the page applies to answers; the text of a tool's output block is shown as the program wrote it.

Tests: `tests/test_remote_devices.py` (stub requests from loopback and from a Tailscale address, the note, the conversation's address), `serve/tests/test_netaddr_page.py` (the page's rewriting
in a real browser at 360x640, 390x844 and 1280x800).
