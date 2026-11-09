"""Adopts the voice models that are already on this disk: a file somewhere else whose SHA-256 equals the catalog's pin is hard-linked to
the place the downloader would write it, so it costs no space and is not downloaded again.

    python -m serve.voice_adopt plan  [--models-dir PATH] [--root PATH ...] [--report FILE]
    python -m serve.voice_adopt apply [--models-dir PATH] [--root PATH ...] [--report FILE] [--copy]

`plan` only reads. `apply` creates the links (`--copy` copies a file that cannot be linked, for example from another volume). A source is
never modified, moved or deleted; a destination that already exists is never overwritten. An entry whose files are all in place gets the
marker the downloader writes (so it counts as installed); an entry with only some of them gets none, and the downloader then fetches
only the missing files (it skips a file already there with the pinned size)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from serve import downloads, voice_catalog

CHUNK = 8 << 20
_REPARSE = 0x400


def default_roots() -> list[str]:
    home = Path.home()
    roots = [home / ".cache" / "whisper", r"C:\Projects\callAPIcall\call-bridge\models", r"C:\Projects\callAPIcall\tts-venvs\modelli",
             r"C:\Projects\callAPIcall\avatar\MuseTalk\models", home / "AppData" / "Roaming" / "com.pais.handy" / "models",
             home / ".cache" / "huggingface" / "hub"]
    return [str(r) for r in roots]


def _is_link(path: str) -> bool:
    """A symbolic link or a junction (a reparse point)."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str, folder: str) -> bool:
    p, q = _real(path), _real(folder)
    try:
        return os.path.commonpath([p, q]) == q
    except ValueError:
        return False


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def _free(path: str) -> int | None:
    p = os.path.abspath(path)
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


class Sources:
    """The files under the roots: by size, by name, and the Hugging Face blobs by their name (the SHA-256 they are stored under). Links are
    not followed and are never sources. A hash is computed once per file."""

    def __init__(self, roots: list[str], skip: str | None = None):
        self.roots = [r for r in roots if os.path.isdir(r)]
        self.missing_roots = [r for r in roots if not os.path.isdir(r)]
        self.by_size: dict[int, list[str]] = {}
        self.by_name: dict[str, list[str]] = {}
        self.blobs: dict[str, str] = {}
        self.links_ignored = 0
        self._hash: dict[tuple, str] = {}
        for root in self.roots:
            for dp, dn, fn in os.walk(root, followlinks=False):
                keep = []
                for d in dn:
                    if _is_link(os.path.join(dp, d)):
                        self.links_ignored += 1
                    else:
                        keep.append(d)
                dn[:] = keep
                for f in fn:
                    p = os.path.join(dp, f)
                    if _is_link(p) or (skip and _inside(p, skip)):
                        self.links_ignored += 1
                        continue
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    if size <= 0:
                        continue
                    self.by_size.setdefault(size, []).append(p)
                    self.by_name.setdefault(f, []).append(p)
                    if os.path.basename(dp) == "blobs" and len(f) == 64:
                        self.blobs[f.lower()] = p

    def hash(self, path: str) -> str:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
        if key not in self._hash:
            self._hash[key] = sha256_of(path)
        return self._hash[key]

    def candidates(self, sha: str, size: int | None, name: str) -> list[str]:
        """Files that may be this one: the blob stored under its hash, then those of its size (or, when the size is not known, of its name)."""
        out: list[str] = []
        for p in ([self.blobs[sha]] if sha in self.blobs else []) + (self.by_size.get(size, []) if size else self.by_name.get(name, [])):
            if p not in out:
                out.append(p)
        return out

    def find(self, sha: str, size: int | None, name: str) -> tuple[str | None, list[dict]]:
        """(the first candidate whose SHA-256 equals the pin, or None; the candidates that differ)."""
        differ = []
        for p in self.candidates(sha, size, name):
            try:
                got = self.hash(p)
            except OSError as e:
                differ.append({"path": p, "error": str(e)})
                continue
            if got == sha:
                return p, differ
            differ.append({"path": p, "sha256": got})
        return None, differ


def wanted(entry: dict) -> list[dict]:
    """The pinned files of an entry, as the downloader would name them: {path, name (inside the entry's folder), sha256, size or None}."""
    out = []
    for src in entry["sources"]:
        if src["type"] == "url":
            out.append({"path": src["name"], "name": downloads.safe_relpath(src["name"]), "sha256": src["sha256"], "size": src.get("size")})
            continue
        sizes = src.get("sizes") or {}
        for path in src["files"] or []:
            out.append({"path": path, "name": downloads.source_name(src, path), "sha256": (src.get("pins") or {}).get(path),
                        "size": sizes.get(path)})
    return out


def _marker(entry: dict, files: list[dict]) -> dict:
    return {"id": entry["id"], "files": [{"name": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in files]}


def _write_marker(folder: str, body: dict) -> str:
    final = os.path.join(folder, voice_catalog.MARKER)
    try:
        with open(final, encoding="utf-8") as fh:
            if json.load(fh) == body:
                return "exists"
    except (OSError, ValueError):
        pass
    tmp = final + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
    os.replace(tmp, final)
    return "written"


def _place(f: dict, dest: str, src: str | None, differ: list, apply: bool, copy: bool, sources: Sources) -> None:
    """Sets f["state"] (and f["reason"]) for one file: present | linked | copied | linkable | missing | source_differs | dest_differs | skipped."""
    sha = f["sha256"]
    if os.path.lexists(dest):
        try:
            if _is_link(dest):
                f["state"], f["reason"] = "dest_differs", "the destination is a link or reparse point"
            elif src and os.path.samefile(src, dest):
                f["state"] = "present"
            elif (f["size"] is None or os.path.getsize(dest) == f["size"]) and sha256_of(dest) == sha:
                f["state"], f["size"] = "present", os.path.getsize(dest)
            else:
                f["state"], f["reason"] = "dest_differs", "a different file is already there; left untouched"
        except OSError as e:
            f["state"], f["reason"] = "dest_differs", f"the destination cannot be read: {e}"
        return
    if src is None:
        f["state"] = "source_differs" if differ else "missing"
        if differ:
            f["differs"] = differ
        return
    f["source"] = src
    if not apply:
        f["state"] = "linkable"
        return
    before = os.stat(src)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if (os.stat(src).st_size, os.stat(src).st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            f["state"], f["reason"] = "skipped", "the source changed while it was being read"
            return
        os.link(src, dest)
        f["state"] = "linked"
    except FileExistsError:
        f["state"], f["reason"] = "dest_differs", "the destination appeared meanwhile; left untouched"
    except OSError as e:
        if not copy:
            f["state"], f["reason"] = "skipped", f"cannot link ({e}); another volume needs --copy"
            return
        tmp = dest + ".part"
        try:
            shutil.copyfile(src, tmp)
            if sha256_of(tmp) != sha:
                os.remove(tmp)
                f["state"], f["reason"] = "skipped", "the copy did not verify and was discarded"
                return
            os.replace(tmp, dest)
            f["state"] = "copied"
        except OSError as e2:
            f["state"], f["reason"] = "skipped", f"cannot copy ({e2})"


def run(models_dir: str, roots: list[str], apply: bool = False, copy: bool = False, catalog: list[dict] | None = None) -> dict:
    """Plans or applies the adoption; the report says, for each entry and file, what is or would be linked."""
    models_dir = os.path.abspath(models_dir)
    free_before = _free(models_dir)
    sources = Sources(roots, skip=models_dir)
    rep: dict = {"mode": "apply" if apply else "plan", "models_dir": models_dir, "roots": sources.roots, "roots_not_found": sources.missing_roots,
                 "links_ignored": sources.links_ignored, "free_before": free_before, "entries": []}
    refused = next((r for r in sources.roots if _inside(models_dir, r)), None)
    if refused:
        rep["error"] = f"the models folder is inside a source root ({refused}); nothing done"
        return rep
    cat = voice_catalog.CATALOG if catalog is None else catalog
    sizes_by_sha = {}
    for e in cat:
        for f in wanted(e):
            if f["sha256"] and f["size"]:
                sizes_by_sha[f["sha256"]] = f["size"]
    for entry in cat:
        ok, why = voice_catalog.downloadable(entry)
        folder = os.path.join(models_dir, *voice_catalog.folder_of(entry).split("/"))
        item: dict = {"id": entry["id"], "folder": folder, "downloadable": ok, "files": []}
        files = wanted(entry)
        for f in files:
            row = {"name": f["name"], "sha256": f["sha256"], "size": f["size"] or sizes_by_sha.get(f["sha256"])}
            item["files"].append(row)
            dest = os.path.normpath(os.path.join(folder, *f["name"].split("/")))
            try:
                if not row["sha256"]:
                    row["state"], row["reason"] = "missing", "the catalog has no SHA-256 for this file"
                elif not _inside(dest, models_dir) or any(_inside(dest, r) for r in sources.roots):
                    row["state"], row["reason"] = "skipped", "the destination is outside the models folder or inside a source root"
                else:
                    src, differ = sources.find(row["sha256"], row["size"], os.path.basename(f["path"]))
                    row["dest"] = dest
                    if src:
                        row["size"] = os.path.getsize(src)
                    _place(row, dest, src, differ, apply, copy, sources)
            except Exception as e:                      # one bad file never stops the others
                row["state"], row["reason"] = "skipped", f"{type(e).__name__}: {e}"
        states = [r["state"] for r in item["files"]]
        have = [s in ("present", "linked", "copied") for s in states]
        usable = [s in ("present", "linked", "copied", "linkable") for s in states]
        if not files:
            item["status"] = "none"
        elif all(have):
            item["status"] = "complete"
        elif all(usable):
            item["status"] = "would_be_complete"
        elif any(usable):
            item["status"] = "partial"
        else:
            item["status"] = "missing"
        item["marker"] = "no"
        if ok and files and all(have):
            if apply:
                try:
                    item["marker"] = _write_marker(folder, _marker(entry, item["files"]))
                    item["installed"] = voice_catalog.installed(entry, models_dir)
                except OSError as e:
                    item["marker"] = f"failed: {e}"
            else:
                item["marker"] = "exists" if voice_catalog.installed(entry, models_dir) else "would_write"
        elif ok and files and all(usable) and not apply:
            item["marker"] = "would_write"
        item["bytes_linked"] = sum(r["size"] or 0 for r in item["files"] if r["state"] in ("linked", "linkable"))
        rep["entries"].append(item)
    count = lambda st: sum(1 for e in rep["entries"] if e["status"] == st)
    rep["summary"] = {"complete": count("complete"), "would_be_complete": count("would_be_complete"), "partial": count("partial"),
                      "missing": count("missing"), "none": count("none"),
                      "bytes_saved": sum(e["bytes_linked"] for e in rep["entries"]),
                      "files_to_download": sum(1 for e in rep["entries"] for r in e["files"]
                                               if r["state"] in ("missing", "source_differs", "skipped") and e["downloadable"]),
                      "bytes_to_download": sum(r["size"] or 0 for e in rep["entries"] for r in e["files"]
                                               if r["state"] in ("missing", "source_differs", "skipped") and e["downloadable"])}
    rep["free_after"] = _free(models_dir)
    return rep


def _print(rep: dict) -> None:
    if rep.get("error"):
        print(rep["error"])
        return
    gib = lambda n: "?" if n is None else f"{n / 2**30:.2f} GiB"
    print(f"{rep['mode']}: models folder {rep['models_dir']}; free {gib(rep['free_before'])} -> {gib(rep['free_after'])}")
    for r in rep["roots_not_found"]:
        print("  root not found:", r)
    for e in rep["entries"]:
        n = len(e["files"])
        done = sum(1 for r in e["files"] if r["state"] in ("present", "linked", "copied", "linkable"))
        print(f"  {e['id']:28s} {e['status']:17s} {done}/{n} files  marker: {e['marker']}")
        for r in e["files"]:
            if r["state"] not in ("present", "linked", "copied", "linkable"):
                print(f"      {r['state']}: {r['name']} {r.get('reason', '')}")
    s = rep["summary"]
    print(f"  entries: complete {s['complete']}, would be complete {s['would_be_complete']}, partial {s['partial']}, missing {s['missing']}, "
          f"not listed {s['none']}; linked or linkable {gib(s['bytes_saved'])}; still to download {s['files_to_download']} files, {gib(s['bytes_to_download'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m serve.voice_adopt", description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", nargs="?", choices=("plan", "apply"), default="plan")
    ap.add_argument("--models-dir")
    ap.add_argument("--root", action="append", dest="roots")
    ap.add_argument("--report")
    ap.add_argument("--copy", action="store_true", help="copy a file that cannot be linked (another volume)")
    a = ap.parse_args(argv)
    from serve import voice_settings
    rep = run(a.models_dir or voice_settings.models_dir(), a.roots or default_roots(), apply=a.mode == "apply", copy=a.copy)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
    _print(rep)
    return 1 if rep.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
