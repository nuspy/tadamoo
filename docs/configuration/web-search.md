# Web search

Settings > Web search chooses where the assistant searches the web and how it reads a page that a plain fetch cannot
get. The page talks to the `/api/websearch*` routes of the server, which are answered only on the PC the server runs on.
The choice applies from the next search; no restart is needed.

## The two choices

- **Search with.** One provider, marked on its card ("Use for searching"). The default is DuckDuckGo.
- **Page reader.** None, Firecrawl or Playwright ("Use as page reader", or "No page reader" at the top). The reader is
  used automatically when a site answers 403, 429 or a server error, or is too slow.
- **Start the local programs when a search needs them.** On by default. When off, a local program that is stopped is
  not started by a search.

## A card

Each provider has one card: what it is, a Local / Cloud choice when both exist (Local is the preferred one), the
settings of the chosen side, a Test button, and a Good / Weak list.

- **Cloud.** A link to create the account, a link to the page of the key, and a key field. The field is write-only: it
  is sent once, emptied, and never filled back. The page is only told whether a key is set. A key found in the
  provider's environment variable (for example `BRAVE_API_KEY`) also counts as set and cannot be removed here. SearXNG
  in the cloud has an address field instead of a key: the address of a public instance.
- **Local.** The state of the program (running, installed and stopped, not installed, answering or not at its address),
  which program is missing (Docker, Git, Node.js), the address, and the buttons below.
- **Test.** One real search (or one page read for Playwright) with the provider as it is set up now. It shows the number
  of results and the time, or why it failed.

## Install and remove

Nothing is installed by opening the page. "Show the steps" fetches the plan and lists each command; only then does an
Install button appear (with the warning that it downloads and runs software). Install starts a job on the server that
runs the listed commands one at a time; the page shows "Step n of m" and the output of the job, polled about once a
second. Remove asks for a second press.

| Provider | Local | Cloud | Installed by the page |
|---|---|---|---|
| DuckDuckGo | built in | built in | nothing to install |
| SearXNG | Docker container `tadamoo-searxng`, bound to 127.0.0.1 | a public instance (address) | yes |
| Brave Search | none | API key | no |
| Tavily | none | API key (its connector is started with npx) | no |
| Exa | none | API key (its hosted connector) | no |
| Firecrawl | self-hosted, set up by hand | API key | no: the steps are a guide with the project's link |
| Playwright | Microsoft Edge driven through `@playwright/mcp` (npx) | none | yes (downloads the connector) |

A SearXNG that the page installs listens only on this PC, so its address must be `127.0.0.1` or `localhost`.

## What is written, and where

| What | Where |
|---|---|
| The choice: search provider, reader, local or cloud per provider, addresses, autostart | `websearch.json` in the user data folder |
| The keys | the credential store, under the name `websearch/<provider>`; never in `websearch.json` |
| SearXNG's settings (`settings.yml` with the JSON format on) | `websearch/searxng` in the user data folder, mounted into the container |

## API

| Route | Use |
|---|---|
| `GET api/websearch` | the overview: the choice, the programs found on the PC, one entry per provider |
| `POST api/websearch/select` | `search`, `reader`, `mode` and `urls` as `{provider: value}`, `autostart` |
| `POST api/websearch/key` | `{provider, key}`; an empty key removes it |
| `POST api/websearch/test` | `{provider}` |
| `GET api/websearch/plan?provider=` | the steps an install would run |
| `POST api/websearch/install` | `{provider}`; starts a job |
| `GET api/websearch/job?id=` | the state, the step and the output lines of a job |
| `POST api/websearch/uninstall` | `{provider}` |

An error is `{"error": {"type", "code", "message"}}`; the page shows the message as text. The codes `manual`,
`missing_tool`, `busy` and `not_found` are used by the install routes.
