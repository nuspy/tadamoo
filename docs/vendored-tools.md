# Vendored tools: agent-desktop and agent-browser

Two programs by Vercel Labs, both Apache-2.0, are part of TaDaMoo: **agent-desktop** (computer control: [computer-control.md](computer-control.md)) and **agent-browser** (the default driver of the browser tools: [browser-backends.md](browser-backends.md)). Their source, as it is upstream, is in `third_party/`; the integration is Python in `tadamoo/`. The Rust is not modified.

| | agent-desktop | agent-browser |
|---|---|---|
| Upstream | github.com/vercel-labs/agent-desktop | github.com/vercel-labs/agent-browser |
| In the repository | `third_party/agent-desktop/` (Cargo.toml and lock, rust-toolchain, crates, src, skills, providers) | `third_party/agent-browser/` (LICENSE, README, `cli/`: Cargo.toml and lock, build.rs, cdp-protocol, src) |
| Release in use | in `third_party/<name>/UPSTREAM.md` (release, commit, date) | same |
| Built with | `cargo build --release -p agent-desktop` (the toolchain its `rust-toolchain.toml` pins) | `cargo build --release` in `cli/` (two jobs at a time) |
| Licence | Apache-2.0, no NOTICE file; dependencies limited to Apache, MIT, MPL-2.0, Unicode and Unlicense by its `deny.toml` | Apache-2.0, no NOTICE file |

Only what compiles is kept: documentation, sites, images, benchmarks, evals, scripts and tests of upstream are not. agent-browser's dashboard (a web page it embeds) is not built: its own placeholder page is embedded. Both trees were compiled from these subsets on Windows (Rust 1.93, MSVC). `THIRD_PARTY_LICENSES` names both; `.gitattributes` keeps `third_party/` byte for byte.

## Updating

`python tools/vendor_update.py agent-desktop [tag]` and `... agent-browser [tag]` download the release's source (the latest without a tag), replace the kept paths, apply every `third_party/<name>/patches/*.patch` (made against the upstream tree) and rewrite `UPSTREAM.md`. A patch that no longer applies stops the update and is kept to be fixed. There are no patches now. After an update: run the tests (`tests/test_computer*.py`, `tests/test_browser_ab*.py`, and `TADAMOO_TEST_AGENT_BROWSER=<program> pytest tests/test_browser.py`), then the workflow.

## Building

The GitHub workflow **vendored-tools** (`.github/workflows/vendored-tools.yml`, started by hand: `gh workflow run vendored-tools.yml`) builds both programs for Windows x64 and Linux x64 (Linux in Ubuntu 22.04, the glibc floor of upstream's own release; agent-desktop's clipboard helper goes beside it), lists the licences of all dependencies (`cargo license`, `cargo deny list`, and agent-desktop's own `cargo deny check licenses`) and attaches per system `tadamoo-vendored-tools-<target>.tar.gz` (`tools/vendored/<name>/` with LICENSE and UPSTREAM.md, the licence files, a SHA-256). Nothing else runs it.

## Where a program is looked for

`tadamoo/vendored.py`: `<data>/tools/<name>/` first (a newer release the person took, for agent-desktop with the button in Wallet > Computer control or `python -m tadamoo.computer install`), then `<program folder>/tools/vendored/<name>/` (what the installers ship), then PATH. The program's own state (agent-desktop's snapshots, agent-browser's sockets and configuration) is in the `<data>/tools/<name>/` folder, not in the user's home.

## What the installers do

- **Windows** (`compile_and_package.ps1`): puts both into the stage's `tools\vendored\<name>\` from the workflow's archive (`-VendoredArchive`, or `dist\cache\vendored\`), else builds them with cargo from `third_party\`, else warns and goes on without (`-SkipVendored` skips); runs each once to see that it starts; copies their licences into `licenses\`. `third_party\` itself is not packaged.
- **Linux complete release**: `tools/vendored/` from the workflow's Linux archive is unpacked into the release tree before it is packed (`tar xzf tadamoo-vendored-tools-x86_64-unknown-linux-gnu.tar.gz -C tadamoo-<version>/`); `deploy/linux/install.sh` copies it with the program (not `third_party/`), makes the programs runnable and says when they are absent.
- With a program absent, computer control uses the system's own backend and the browser tools TaDaMoo's own driver.

## Defaults

Computer control is **on** (the permission levels decide what runs; Wallet > Computer control turns it off) and uses agent-desktop when it is found. The browser driver is agent-browser; the vault, sign-in and payment steps go to the own driver (a setting; see browser-backends.md). agent-browser's `install` (a download of Chrome for Testing) is not used: the Edge or Chrome already on the PC is, by path, or one already running through the DevTools port.
