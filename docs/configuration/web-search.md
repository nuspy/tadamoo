# Web search

Settings > Web search chooses where the assistant searches the web and how it reads a page that a plain fetch cannot
get. The page talks to the `/api/websearch*` routes of the server, which are answered only on the PC the server runs on.
The choice applies from the next search; no restart is needed.

## The choices

- **Search with.** One provider, marked on its card ("Use for searching"). The default is DuckDuckGo.
- **Page reader.** None, Firecrawl, Playwright or open-websearch ("Use as page reader", or "No page reader" at the
  top). The reader is used automatically when a site answers 403, 429 or a server error, or is too slow.
- **Engine.** When open-websearch is the search provider, a select chooses which engine it asks: DuckDuckGo, Bing,
  Brave or Startpage.

The top of the page shows the detected system (Windows, Linux or macOS, and x64 or arm64) and whether Node.js was found
on the PC, was installed by TaDaMoo, or is missing. On a system the installers do not support (no official Node.js build,
or a musl Linux) the page shows the reason and offers no Install button.

## A card

Each provider has one card: what it is, a Local / Cloud choice when both exist (Local is the preferred one), the
settings of the chosen side, a Test button, and a Good / Weak list.

- **Cloud.** A link to create the account, a link to the page of the key, and a key field. The field is write-only: it
  is sent once, emptied, and never filled back. The page is only told whether a key is set. A key found in the
  provider's environment variable (for example `BRAVE_API_KEY`) also counts as set and cannot be removed here. SearXNG
  in the cloud has an address field instead of a key: the address of a public instance.
- **Local, installed by TaDaMoo** (open-websearch, Playwright). The state (not installed, installed with its version,
  running), "Show the steps", Install, Remove, and "Stop now" while the server runs.
- **Local, set up by hand** (SearXNG, Firecrawl). The state (answering at the address or not), the address field, and
  "Show how to set it up", which shows the server's note and the project's link.
- **Test.** One real search (or one page read for Playwright) with the provider as it is set up now. It shows the number
  of results and the time, or why it failed.

## Install, stop and remove

Nothing is installed by opening the page. "Show the steps" fetches the plan for this system and lists each command; only
then does an Install button appear, with a warning that it downloads software and runs it. Install starts a job on the
server that runs the steps one at a time; the page shows "Step n of m" and the output, polled about once a second.
Remove asks for a second press.

The installers need no administrator rights and change no setting of the PC. They work in the folder `websearch/` of the
user data folder:

- `websearch/node/`: a private Node.js, downloaded from nodejs.org for the detected system and checked against its
  published checksum. It is used only when the PC has no suitable Node.js of its own.
- `websearch/packages/`: the npm packages (`open-websearch`, `@playwright/mcp`), installed with install scripts off.

A server starts by itself when a search needs it and stops after 5 quiet minutes, so "Stop now" is rarely needed.
Playwright drives the Microsoft Edge or Google Chrome already on the PC; only when none is found does the plan add a
download of Playwright's own Chromium, which is large.

| Provider | Local | Cloud | Installed by TaDaMoo |
|---|---|---|---|
| DuckDuckGo | built in | built in | nothing to install |
| open-websearch | local server over Node.js | none | yes |
| SearXNG | your own instance (address) | a public instance (address) | no: no Windows support from its project |
| Brave Search | none | API key | no |
| Tavily | none | API key (its connector is started with npx) | no |
| Exa | none | API key (its hosted connector) | no |
| Firecrawl | self-hosted by hand (address) | API key | no: its self-hosting guide uses Docker |
| Playwright | Edge or Chrome driven through `@playwright/mcp` | none | yes |

## What is written, and where

| What | Where |
|---|---|
| The choice: search provider, reader, engine, local or cloud per provider, addresses | `websearch.json` in the user data folder |
| The keys | the credential store, under the name `websearch/<provider>`; never in `websearch.json` |
| Node.js and the npm packages | `websearch/node/` and `websearch/packages/` in the user data folder |

## API

| Route | Use |
|---|---|
| `GET api/websearch` | the overview: `system`, `runtime`, the choice, `engine` and `engines`, one entry per provider |
| `POST api/websearch/select` | `search`, `reader`, `engine`, and `mode` and `urls` as `{provider: value}` |
| `POST api/websearch/key` | `{provider, key}`; an empty key removes it |
| `POST api/websearch/test` | `{provider}` |
| `GET api/websearch/plan?provider=` | the steps an install would run, and a note |
| `POST api/websearch/install` | `{provider}`; starts a job |
| `GET api/websearch/job?id=` | the state, the step and the output lines of a job |
| `POST api/websearch/stop` | `{provider}`; stops the local servers now |
| `POST api/websearch/uninstall` | `{provider}` |

An error is `{"error": {"type", "code", "message"}}`; the page shows the message as text. The codes `manual`,
`unsupported`, `busy` and `not_found` are used by the install routes.
