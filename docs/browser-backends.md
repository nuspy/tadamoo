# The browser tools: two drivers

The `browser_*` tools (open, read, click, type, sign in, select, press, back, screenshot) have two drivers, chosen in **Wallet > Browser** (`backend` in `browser.json`, `--browser-backend` of the agent):

- **agent-browser** (default): [vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser), Apache-2.0, kept in `third_party/agent-browser/` and built from there; a program that keeps a browser open between steps and reads a page as the accessibility tree. `tadamoo/browser_ab.py`.
- **TaDaMoo's own driver** (`tadamoo/browser.py`): talks to Edge or Chrome over the DevTools protocol itself.

The model sees the same tools with the same names and arguments and the same description of a page, so it does not notice which is behind them. If agent-browser is not found (not shipped, not in `<data>/tools/agent-browser/`, not on PATH) the tools use the own driver. The browser is the Edge or Chrome already on the PC (`--executable-path`) or one already running (`--cdp`), as with the own driver: agent-browser's own download of Chrome for Testing is not used.

## One choice, the same care for both

There is **one** selector, Wallet > Browser > "What drives the browser" (`backend` in `browser.json`, `--browser-backend`), and it holds for everything the browser tools do, the vault, sign-ins and payments included. Both drivers are meant to be prepared the same way and to have the same capacity for payments and private information; where agent-browser is not yet at parity, it is listed under "What is different" below, so the person knows what they choose. The choice is read when a conversation starts, as the other browser options are. The web page carries it; the desktop app shows that page in its window; the Android app has no browser or wallet screen.

## What is the same (it is the same code, or the same rules)

| | |
|---|---|
| Tool names, arguments, descriptions, the page description (numbered elements, then the text; read in pages with `contains`, `role`, `offset`) | `register_browser_tools` in `browser.py` serves both |
| Permission levels, effect classes, allow rules, untrusted results, the Rule of Two, the audit record under the actor `browser` | the registry and the stack, above the driver |
| The vault: `{{vault:ENTRY.FIELD}}` and `{{secret:NAME}}` are resolved by the tool layer against **the site that receives the value**, `browser_sign_in`, second-factor codes, a guarded card released only after an approved payment, no vault value over plain http on the internet, the page text shows a field that holds one as `(set)` | `type_into`, `sign_in` and `describe` in `browser.py` |
| What a picture could show: after a vault value was typed, a screenshot asks the person first (taken at Auto) | `screenshot` |
| Private addresses: opening or ending up on a page inside this PC or its network is asked about the same way (and taken off with `about:blank` if refused) | `check_url`, `_private_ok` |
| Consent banners and pop-ups: the same detection script and the same fixed rules and policies (`browser_consent.py`); the click is a real mouse click made at the control only if nothing lies over it | `run_consent` |
| Confirm and prompt dialogs: the same rule (allowed at Auto, by an allow rule, or by the person's answer); an alert is accepted | `_dialog_ok` |
| Edge or Chrome as found on the PC, headless or with a window, a kept profile (`--profile`), attaching to a running browser (`--cdp`) | the stack's options |

## What is different

| | agent-browser | own driver |
|---|---|---|
| Speed | each tool call is a few short programs (`open` is about ten), a first start takes a second or two; the same tests took about three times as long | one connection, fast |
| What a page is | the accessibility tree: roles and names. An element with no role or name (a clickable `div` with an `onclick`) may not be listed; options of a list are shown by label | the DOM: every link, button, field, `[role]`, `[onclick]` and `contenteditable` |
| Typing | `fill` and `type` of the program; the person's own words are in its arguments, **a vault value is not**: it is sent to the program on its standard input as a `batch` (`--omit-command`, so the answer does not repeat it) | key events through the connection |
| The site of a frame | the host of the frame's `src` attribute (resolved against the page): a frame that is sent elsewhere after it loads is not noticed | the frame's real address |
| `browser_back` on the first page | stays on it | goes to the empty page |
| Downloads | the file lands in `<data>/browser-downloads/<session>/`; the question is asked **after** it arrived and the file is removed if refused | asked before it starts |
| A page's request for a permission (camera, location, notifications) | refused by a browser switch (`--deny-permission-prompts`); `grant` lets the camera and microphone through; `ask` and `grant-safe` are not carried over | by the person's choice, site by site |
| A page that asks the network for something inside this PC (an image or a script of `192.168.x.x`) | not stopped: only the address of the page itself is checked | every request is checked and refused |
| Consent banners | in the page itself; a banner in the frame of another site (a consent manager) is not found | also in frames |
| A window the page opens (a payment sheet) | followed after a click, key or selection: the new tab becomes the page | followed |
| One browser at a time on a kept profile | not enforced here (two conversations on the profile would clash) | a lock |
| A browser someone else started (`--cdp`) | is never closed from here; its session stays attached until the program's idle limit (20 minutes) | the same |
| Cold start of the program | the daemon starts with the first command; `--idle-timeout 20m` ends it when no conversation uses it | no daemon |

## What cannot be carried over without changing the program

Nothing has been patched: the Rust of `third_party/agent-browser/` is used as it is. Three things would need a patch (`third_party/agent-browser/patches/`, applied again at each update by `tools/vendor_update.py`), and are listed as differences above instead: refusing a page's sub-requests to this PC's network (the program only has an allow-list of domains for everything, which would be wrong for a shop that loads from many hosts), asking before a download starts, and reading a frame's real address (`get url` inside `frame` answers with the page's). The `eval --stdin` command, the other way to put text through standard input, does not reach a frame of another site, so the vault does not use it.

## Payments

The payment code is the vault's and `browser.py`'s, the same as before: a card entry is released only for an approved payment (`request_payment`, its amount and site checked), the value goes only to a site the entry names, and **a frame of a payment provider is a site of its own**: the card field in it is typed to the provider's host, not the shop's.

**Checked with the real program and a real Edge** (local pages only, no network, no money): `tests/test_browser.py` runs against both drivers: a vault value typed on its site and never shown, refused on a site the entry does not name, a guarded card typed only after an approved payment, a second-factor code, stale element numbers, sign-in (also in two steps) and its refusals; `tests/test_browser_ab_frames.py`: a shop page on one host with a card field in a frame on another (an out-of-process frame): the card went into the frame's field from the vault, was refused in the shop's own field, reached the provider's server through the frame's form, and was in no process's arguments and not in what the model read.

**Checked only with a stand-in program** (`tests/test_browser_ab.py`, no browser): the rules around the real program: private addresses, dialogs, downloads, banners, pictures, permission switches, the errors and the advice, that a failure message cannot repeat a secret. Also the selector in the web page in a real headless browser at 360x640, 390x844 and 1280x800 in both languages, with the saved choice read back after a reload and the agent stack built from it (`serve/tests/test_browser_backend_page.py`).

**Not checked**: a real payment provider (Stripe, Adyen, PayPal...), nested or redirected frames, 3-D Secure windows and challenges, wallet buttons, a payment completed end to end, a real card or a real amount. Providers change their pages; a frame that loads its fields late, hides them, or sends the frame elsewhere after loading may not be filled, or may be judged by the address it was declared at. The person chooses this risk (Wallet > Browser and the payment settings); `docs/browser-backends.md` is where the limits are said. `tests/QUEUE_daytime.md` has the real runs to do.

## Settings and files

- `browser.json`: `{"enabled": true, "headless": false, ..., "backend": "agent-browser"}`; `GET /api/wallet` shows the choice, whether Edge or Chrome is found and where agent-browser is (`bundled`, `installed` in `<data>/tools/agent-browser/`, or `path`).
- The program's state (daemon sockets, its config) is in `<data>/tools/agent-browser/home/`; its configuration file is ours (`<data>/tools/agent-browser/config.json`, empty), so neither the user's `~/.agent-browser/config.json` nor a project's `agent-browser.json` changes how it runs. Each conversation has its own session `tadamoo-<random>`.
- Where the programs come from, how they are built and updated: [vendored-tools.md](vendored-tools.md).
