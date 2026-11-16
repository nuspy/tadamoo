# agent-browser (vendored)

- Repository: https://github.com/vercel-labs/agent-browser
- Release: v0.43.0
- Commit: 1cc19526f3641c8543d55aa6700e7c55b37ac356
- Fetched: 2026-11-16
- Licence: Apache-2.0 (the upstream LICENSE is kept here; upstream has no NOTICE file)
- Kept (129 files): LICENSE, README.md, cli/Cargo.toml, cli/Cargo.lock, cli/build.rs, cli/cdp-protocol, cli/src
- Not kept: documentation, sites, images, benchmarks, evals, scripts and tests of the upstream repository
- Build: cargo build --release (from third_party/agent-browser/cli); the dashboard is not built: the program embeds upstream's placeholder page
- Local changes: none: the upstream code is used as it is
- Update: `python tools/vendor_update.py agent-browser [tag]`

The integration with TaDaMoo is in `tadamoo/` (Python), not in this folder.
