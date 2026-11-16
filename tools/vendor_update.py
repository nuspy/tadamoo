"""Brings the vendored Vercel Labs tools in third_party/ to a release: python tools/vendor_update.py <agent-desktop|agent-browser> [tag] [--from-tarball FILE]

It downloads the tag's source archive from GitHub (or reads FILE), replaces the kept folders and files of third_party/<name>/ with the archive's, applies every patch in
third_party/<name>/patches/*.patch (made against the upstream tree: `git diff` from an upstream checkout; none exist while the tree is used as it is), and writes UPSTREAM.md
(repository, tag, commit, date, what is kept, the patches applied). Nothing else in third_party/<name>/ is touched, so a local patch is applied again at every update.
Without a tag the latest release is taken. Nothing is pushed or published.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

# What each project needs to be compiled; everything else of the upstream repository (docs, sites, benchmarks, evals, images, scripts, tests) is not kept.
VENDORED: Dict[str, Dict[str, Any]] = {
    "agent-desktop": {
        "repo": "vercel-labs/agent-desktop",
        "keep": ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "clippy.toml", "deny.toml", "LICENSE", "README.md", "crates", "src", "skills", "providers"],
        "build": "cargo build --release -p agent-desktop (from third_party/agent-desktop)",
    },
    "agent-browser": {
        "repo": "vercel-labs/agent-browser",
        "keep": ["LICENSE", "README.md", "cli/Cargo.toml", "cli/Cargo.lock", "cli/build.rs", "cli/cdp-protocol", "cli/src"],
        "build": "cargo build --release (from third_party/agent-browser/cli); the dashboard is not built: the program embeds upstream's placeholder page",
    },
}
API = "https://api.github.com/repos/{repo}"


def fetch(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "TaDaMoo-vendor-update", "Accept": accept})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def latest_tag(repo: str) -> str:
    return str(json.loads(fetch(API.format(repo=repo) + "/releases/latest"))["tag_name"])


def commit_of(repo: str, tag: str) -> str:
    return str(json.loads(fetch(API.format(repo=repo) + f"/commits/{tag}"))["sha"])


def kept(path: str, keep: List[str]) -> bool:
    return any(path == k or path.startswith(k + "/") for k in keep)


def replace_tree(tarball: bytes, dest: Path, keep: List[str]) -> List[str]:
    """Removes the kept paths of `dest`, then unpacks those of the archive (its top folder stripped) in their place. Returns the files written."""
    for k in keep:
        target = dest / k
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    written: List[str] = []
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            rel = parts[1] if len(parts) == 2 else ""
            if not rel or not kept(rel, keep) or not member.isfile():
                continue
            out = (dest / rel).resolve()
            if dest.resolve() not in out.parents:
                raise SystemExit(f"the archive holds a path outside the folder: {member.name}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(tar.extractfile(member).read())
            written.append(rel)
    missing = [k for k in keep if not any(w == k or w.startswith(k + "/") for w in written)]
    if missing:
        raise SystemExit(f"the archive has none of: {', '.join(missing)}")
    return sorted(written)


def apply_patches(dest: Path, root: Path = ROOT) -> List[str]:
    """Applies third_party/<name>/patches/*.patch in name order, relative to the upstream tree; stops at the first one that does not apply."""
    applied: List[str] = []
    for patch in sorted((dest / "patches").glob("*.patch")):
        done = subprocess.run(["git", "apply", "-p1", "--directory", dest.relative_to(root).as_posix(), "--whitespace=nowarn", str(patch)], cwd=root, capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"patch {patch.name} does not apply to this release: {done.stderr.strip()}\nUpdate the patch (it is kept, the tree is already replaced) and run this again.")
        applied.append(patch.name)
    return applied


def write_upstream(dest: Path, name: str, spec: Dict[str, Any], tag: str, commit: str, patches: List[str], files: int) -> None:
    lines = [
        f"# {name} (vendored)", "",
        f"- Repository: https://github.com/{spec['repo']}",
        f"- Release: {tag}",
        f"- Commit: {commit}",
        f"- Fetched: {time.strftime('%Y-%m-%d')}",
        "- Licence: Apache-2.0 (the upstream LICENSE is kept here; upstream has no NOTICE file)",
        f"- Kept ({files} files): {', '.join(spec['keep'])}",
        "- Not kept: documentation, sites, images, benchmarks, evals, scripts and tests of the upstream repository",
        f"- Build: {spec['build']}",
        "- Local changes: " + (", ".join(f"patches/{p}" for p in patches) + " (applied again at every update)" if patches else "none: the upstream code is used as it is"),
        f"- Update: `python tools/vendor_update.py {name} [tag]`", "",
        "The integration with TaDaMoo is in `tadamoo/` (Python), not in this folder.", "",
    ]
    (dest / "UPSTREAM.md").write_text("\n".join(lines), encoding="utf-8")


def update(name: str, tag: str = "", tarball_path: Optional[Path] = None, commit: str = "", root: Path = ROOT) -> Tuple[str, int, List[str]]:
    spec = VENDORED[name]
    dest = root / "third_party" / name
    if tarball_path is not None:
        data, tag, commit = tarball_path.read_bytes(), tag or "local", commit or "local archive"
    else:
        tag = tag or latest_tag(spec["repo"])
        commit = commit_of(spec["repo"], tag)
        data = fetch(API.format(repo=spec["repo"]) + f"/tarball/{tag}", "application/vnd.github+json")
    dest.mkdir(parents=True, exist_ok=True)
    files = replace_tree(data, dest, spec["keep"])
    patches = apply_patches(dest, root)
    write_upstream(dest, name, spec, tag, commit, patches, len(files))
    return tag, len(files), patches


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Update a vendored Vercel Labs tool in third_party/ to a release")
    parser.add_argument("name", choices=sorted(VENDORED))
    parser.add_argument("tag", nargs="?", default="", help="a release tag, for example v1.3.1 (default: the latest)")
    parser.add_argument("--from-tarball", default="", help="a source archive already downloaded (offline)")
    args = parser.parse_args(argv)
    tag, files, patches = update(args.name, args.tag, Path(args.from_tarball) if args.from_tarball else None)
    print(f"{args.name} {tag}: {files} files; patches applied: {', '.join(patches) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
