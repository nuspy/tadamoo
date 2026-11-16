# agent-desktop (vendored)

- Repository: https://github.com/vercel-labs/agent-desktop
- Release: v1.3.1
- Commit: b0b7277bea09b770ef9d1fb45aad667bc432cd57
- Fetched: 2026-11-16
- Licence: Apache-2.0 (the upstream LICENSE is kept here; upstream has no NOTICE file)
- Kept (2843 files): Cargo.toml, Cargo.lock, rust-toolchain.toml, clippy.toml, deny.toml, LICENSE, README.md, crates, src, skills, providers
- Not kept: documentation, sites, images, benchmarks, evals, scripts and tests of the upstream repository
- Build: cargo build --release -p agent-desktop (from third_party/agent-desktop)
- Local changes: none: the upstream code is used as it is
- Update: `python tools/vendor_update.py agent-desktop [tag]`

The integration with TaDaMoo is in `tadamoo/` (Python), not in this folder.
