# MCP Apps

The host side of the official MCP extension `io.modelcontextprotocol/ui` (SEP-1865, revision 2026-01-26, with the draft's additions). An MCP server can tie a tool to a page
(a `ui://` resource, HTML of type `text/html;profile=mcp-app`). When the model calls the tool, the person sees the page next to the conversation, and the page can call the server's
tools, read its resources, send a message and tell the model something for its next turn. The text result of the tool is always there as well: a host that shows no app, an app that
fails to start and a conversation read later all fall back to it.

## What the person sees

- In the tool's bubble: a compact placeholder (the name of the app, its server, its state, a button **Open** / **Show**). No iframe sits in the conversation.
- The app opens by itself while the tool runs, in a **panel pinned beside the conversation** (the default) or a **separate resizable dialog**. An icon in the window's header
  switches between the two (the app is not restarted); the dialog has a maximise button and moves by its header. On a phone the panel is a full-screen panel and the dialog a
  full-screen sheet.
- A conversation opened later shows the placeholder again (the page is kept in the user data folder, `mcp_apps/templates/`); **Open** starts it with the saved input and result.
- A line under the app tells which tool calls the app itself made ("The app called save_answers").

## Choices (Settings, Capabilities, MCP apps) and where their defaults are

The defaults are in one place, `DEFAULTS` of `tadamoo/mcp_apps.py`; the choices are saved in `capabilities/settings.json` under `apps`.

| Choice | Default | Meaning |
|---|---|---|
| `on` | none | the apps (`<server id>\|<ui:// uri>`) kept running. Every other app is **off**: it starts when a tool of its server returns it (or when its button is pressed) and stops when nothing shows it (closed, replaced in the panel, conversation left, its server stopped); an app that is on is only hidden |
| `display` | `panel` | where an app opens: `panel` or `dialog` |
| `tool_calls` | `run` | an app's own call of a tool of its server: `run` (the conversation's permissions, nobody is asked), `ask` (asked as for the model), `refuse` |

There is no global switch: apps exist for the servers in use (a server the conversation started offers the extension in its handshake). The Settings list shows every app seen so far with
its switch and whether it runs now.

## How it works

- **Negotiation** (`tadamoo/mcp_client.py`): the client capabilities carry `extensions["io.modelcontextprotocol/ui"] = {mimeTypes: ["text/html;profile=mcp-app"]}`; the server's own
  `capabilities.extensions` is read (`server_apps`). The demo server answers a client without the extension with the same tool without a page.
- **Tools**: `_meta.ui.resourceUri` (and the deprecated `_meta["ui/resourceUri"]`) and `_meta.ui.visibility`. A tool whose visibility leaves out `model` is not offered to the
  model or called by it (`call_capability`, `read_capability`, the eager registry, `mcp_search`); a tool without `app` is refused to the app.
- **Page**: `AppHost.begin` (in `tadamoo/mcp_apps.py`) reads the page with `resources/read` while the tool runs, merges `_meta.ui` from the content item over the `resources/list` entry,
  stores it by hash (`AppCache`, 300 kept) and emits `mcp_app` (then `mcp_app_result` or `mcp_app_cancelled`). Kept with the conversation in `apps.json` next to its history.
- **Sandbox** (`serve/mcp_sandbox.py`): a second port of the server (a different origin) serves a shim with `Content-Security-Policy` as an HTTP header built from `ui.csp` (the
  specification's default when none; domains that could end a directive are dropped). The page puts it in an iframe with `sandbox="allow-scripts allow-same-origin"` and `allow` from the
  permissions; the shim sends `ui/notifications/sandbox-proxy-ready`, receives `ui/notifications/sandbox-resource-ready` and puts the HTML in an inner iframe, passing every other
  message on. The host sends nothing to the app before `ui/notifications/initialized`. The port is started when the first app is shown, never 8080, bound like the server; a page
  reached through something that does not forward that port shows the text result and says why.
- **Bridge** (`serve/web/mcpapp.js`): JSON-RPC 2.0 over postMessage. `ui/initialize` answers with host capabilities and context (theme, CSS variables, locale, time zone, platform, touch/hover,
  container size, safe-area insets, display mode); then `tool-input`, `tool-result` or `tool-cancelled`, `host-context-changed` on any change, `ui/resource-teardown` (answered or 2 s) before
  anything is closed. From the app: `tools/call`, `tools/list`, `resources/read` (to the conversation's process, `POST /api/agent/sessions/<id>/app`), `ui/update-model-context`, `ui/message`,
  `ui/open-link`, `ui/request-display-mode`, `ui/download-file` (confirmed first), `ping`, `notifications/message`, `ui/notifications/size-changed`, `ui/notifications/request-teardown`.
- **An app's tool call** (`Session.run_app_call`, `AgentLoop.run_for_app`): through `read_capability` (a tool its server declares read-only) or `call_capability`, the same gate as the model's call
  (arguments checked, hooks, permissions, checkpoint, audit with origin `app:<server>`), but nothing is added to the model's messages. With `tool_calls = run` every question of the call is answered yes.
- **What an app tells the model**: `ui/update-model-context` is kept (the last thing each app said); before the model's next turn it is added once, as `[app context]`, marked as text from a program and
  counted as outside content.
- **Display modes**: `inline` is the panel, `pip` the dialog, `fullscreen` the dialog maximised. A request for a mode the app did not declare gets the current mode back.

## What is not done

- `ui/notifications/tool-input-partial`: the harness does not stream the model's tool arguments to the page (the specification says MAY).
- Apps that register their own tools (draft: host asks the app `tools/list` and `tools/call`): not offered to the model, because that would change the tool list (and the cached start of every request).
- `sampling/createMessage` from an app (draft): not offered (`hostCapabilities.sampling` is left out, as the specification asks of a host that does not).
- `ui.domain` (a dedicated origin for the app): host-defined, so there is one sandbox origin.
- `styles.css.fonts`: the host's own font is a file of the page, not reachable from the sandbox; apps use the system fonts.
- Messages and resources of the server forwarded to the app (`notifications/tools/list_changed`, `resources/list_changed`): `serverTools.listChanged` and `serverResources.listChanged` are false.
- Branches of a conversation do not carry its apps.
- Servers started from `cfg.mcp_servers` (headless, ACP) do not offer the extension: no page to show it in.

## Try it

`tools/mcp-app-demo/server.py` is a small MCP server (standard library only) with an app: a form, a chart, a tool only the app may call (`compute_series`), one that saves
(`save_answers`), buttons for `ui/update-model-context`, `ui/message`, `ui/open-link` and the display mode. Put it in a project's `.mcp.json`:

```json
{"mcpServers": {"app-demo": {"command": "python", "args": ["<path>/tools/mcp-app-demo/server.py"]}}}
```

Import it (Settings, Imported; it comes in turned off), turn it on, and ask the assistant to open the survey with a title and some options.

Tests: `tests/test_mcp_apps.py` (negotiation, visibility, page, bridge operations, permissions), `tests/test_mcp_apps_session.py`, `tests/test_mcp_apps_server.py` (sandbox, process
requests, history, routes, settings), `serve/tests/test_mcpapp_page.py` (a real browser at 360x640, 390x844 and 1280x800, in English and Italian).
