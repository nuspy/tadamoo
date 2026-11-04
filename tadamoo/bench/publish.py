"""Publishing a result is manual and takes two separate calls. `prepare_package` writes exactly the files that would leave, with a manifest (names, sizes,
checksums, destination, who receives it) and a summary a person can read. `begin_publish` shows that and returns a token; `finish_publish` must carry the
token and the exact phrase it asked for, and sends nothing if the package has changed since, the token has expired or the phrase differs. Destinations that
only prepare (e-mail, a closed board) never send; a pull-request destination needs a transport to be injected."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from . import analysis
from .registry import SPECS, BenchSpec
from .runner import read_json

PACKAGES = "publish"
TOKEN_LIFETIME_S = 600
MAX_WRONG_PHRASES = 3
SUMMARY = "SUMMARY.txt"
MANIFEST = "manifest.json"
PHRASE_PREFIX = "PUBLISH "

Transport = Callable[[Path, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


class PublishError(Exception):
    """A publish step that was refused; the message says why in plain words."""


class NotOffered(PublishError):
    pass


class NotConfigured(PublishError):
    pass


class BadConfirmation(PublishError):
    pass


class PackageChanged(PublishError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prepared_only(folder: Path, manifest: Dict[str, Any], destination: Dict[str, Any]) -> Dict[str, Any]:
    """The transport of e-mail and closed boards: nothing is sent; the person sends the folder themselves."""
    return {"sent": False, "transport": "prepared_only", "folder": str(folder),
            "message": f"Nothing was sent. The package is in {folder}; send it yourself to: {manifest['destination']['who']}."}


def csv_pr_unconfigured(folder: Path, manifest: Dict[str, Any], destination: Dict[str, Any]) -> Dict[str, Any]:
    raise NotConfigured("Sending a pull request is not set up in this build; the package is prepared in " + str(folder))


TRANSPORTS: Dict[str, Transport] = {"email": prepared_only, "closed": prepared_only, "csv_pr": csv_pr_unconfigured}


def _destination(kind: Union[str, Mapping[str, Any]], spec: BenchSpec) -> Dict[str, Any]:
    d = {"kind": kind} if isinstance(kind, str) else dict(kind)
    k = d.get("kind")
    if spec.leaderboard == "none" or not spec.leaderboard:
        raise NotOffered(f"{spec.title} has no route for publishing a result from here")
    if k != spec.leaderboard:
        raise NotOffered(f"{spec.title} takes results by '{spec.leaderboard}', not '{k}'")
    d["who"] = spec.leaderboard_target or "to confirm"
    d["text"] = {"csv_pr": "A row for the results file, sent as a pull request", "email": "An e-mail you send yourself", "closed": "A package for a board that is closed now; nothing can be sent"}[k]
    return d


def prepare_package(run: Any, destination_kind: Union[str, Mapping[str, Any]], specs: Optional[Mapping[str, BenchSpec]] = None,
                    include_trajectories: bool = False) -> Dict[str, Any]:
    """Writes `<run>/publish/<kind>/` holding exactly the files that would leave, manifest.json and SUMMARY.txt; returns {folder, files, manifest, summary}."""
    run_dir = analysis.run_dir_of(run)
    record = read_json(run_dir / "settings.json")
    if record is None:
        raise PublishError(f"{run_dir} is not a benchmark run")
    results = read_json(run_dir / "results.json")
    if results is None:
        raise PublishError("The run has not finished, so there is no result to publish")
    spec = (specs or SPECS)[record["benchmark"]]
    dest = _destination(destination_kind, spec)
    rep = analysis.report(run_dir)
    folder = run_dir / PACKAGES / dest["kind"]
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    model = record.get("model") or {}
    entry = {"benchmark": spec.id, "benchmark_title": spec.title, "benchmark_commit": record.get("benchmark_commit"),
             "harness_commit": (record.get("harness") or {}).get("commit"), "harness_uncommitted_changes": (record.get("harness") or {}).get("dirty"),
             "model": {"name": model.get("name"), "quantisation": model.get("quantisation")}, "gpu_power_limit_w": record.get("power_limit_w"),
             "metrics": results.get("metrics"), "provisional": bool(record.get("provisional")), "provisional_label": record.get("provisional_label", ""),
             "trials": rep["trials"], "passed": rep["passed"], "failed": rep["failed"]}
    files: Dict[str, str] = {"entry.json": json.dumps(entry, indent=2, ensure_ascii=False)}
    if dest["kind"] == "csv_pr":
        buf = io.StringIO()
        row = {"model": model.get("name"), "harness": "TaDaMoo", **(results.get("metrics") or {}), "gpu_power_limit_w": record.get("power_limit_w")}
        w = csv.DictWriter(buf, fieldnames=list(row), lineterminator="\n")
        w.writeheader()
        w.writerow(row)
        files["results_row.csv"] = buf.getvalue()
    else:
        files["trials_summary.json"] = json.dumps([{"key": f["key"], "class": f["class"]} for f in rep["failures"]] + [{"passed": rep["passed"], "of": rep["trials"]}], indent=2)
        files["report.md"] = analysis.markdown(rep)
        files["message.txt"] = (f"Result of {spec.title} with TaDaMoo.\nMetrics: {json.dumps(results.get('metrics'))}\nGPU power limit: {record.get('power_limit_w')} W\n"
                                + (f"Note: {record['provisional_label']}\n" if record.get("provisional") else ""))
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    if include_trajectories and (run_dir / "trials").is_dir():
        shutil.copytree(run_dir / "trials", folder / "trials")
    leaving = sorted(p for p in folder.rglob("*") if p.is_file())
    listed = [{"name": p.relative_to(folder).as_posix(), "size": p.stat().st_size, "sha256": _sha256(p)} for p in leaving]
    manifest = {"run_id": record["run_id"], "benchmark": spec.id, "destination": dest, "files": listed, "provisional": bool(record.get("provisional")),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    summary = _summary(manifest, spec)
    (folder / SUMMARY).write_text(summary, encoding="utf-8")
    return {"folder": str(folder), "files": listed, "manifest": manifest, "summary": summary}


def _digest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps([[f["name"], f["sha256"]] for f in manifest["files"]] + [manifest["destination"]], sort_keys=True).encode()).hexdigest()


def _summary(manifest: Mapping[str, Any], spec: BenchSpec) -> str:
    d = manifest["destination"]
    lines = [f"Result of {spec.title}, run {manifest['run_id']}", f"Destination: {d['text']}", f"Goes to: {d['who']}", "", "Files that would leave this PC:"]
    lines += [f"  {f['name']}  ({f['size']} bytes, sha256 {f['sha256'][:12]}...)" for f in manifest["files"]]
    if manifest["provisional"]:
        lines += ["", "This score is provisional: unofficial judge, not comparable with the board."]
    lines += ["", "manifest.json and SUMMARY.txt describe the package and are not part of it."]
    return "\n".join(lines) + "\n"


class PublishDesk:
    """Holds the tokens between the two calls. Nothing is sent by `begin`; `finish` sends only with the right token, an unexpired token, the exact phrase and an unchanged package."""

    def __init__(self, transports: Optional[Mapping[str, Transport]] = None, specs: Optional[Mapping[str, BenchSpec]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.transports: Dict[str, Transport] = {**TRANSPORTS, **(transports or {})}
        self.specs, self.clock = specs, clock
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def begin_publish(self, run: Any, destination: Union[str, Mapping[str, Any]]) -> Dict[str, Any]:
        run_dir = analysis.run_dir_of(run)
        record = read_json(run_dir / "settings.json") or {}
        if record.get("provisional"):
            raise PublishError(f"This score is provisional ({record.get('provisional_label')}); it is not published to a board. The package can still be prepared.")
        package = prepare_package(run, destination, self.specs)
        manifest = package["manifest"]
        phrase = PHRASE_PREFIX + _digest(manifest)[:8]
        token = secrets.token_urlsafe(24)
        expires = self.clock() + TOKEN_LIFETIME_S
        summary = package["summary"] + f"\nNothing has been sent. To send it, confirm with the exact phrase: {phrase}\n"
        with self.lock:
            self.pending[token] = {"folder": Path(package["folder"]), "manifest": manifest, "digest": _digest(manifest), "phrase": phrase, "expires": expires, "wrong": 0}
        return {"token": token, "summary": summary, "expires": expires, "confirm_phrase": phrase, "folder": package["folder"], "files": package["files"]}

    def finish_publish(self, token: str, typed_confirmation: str) -> Dict[str, Any]:
        with self.lock:
            p = self.pending.get(token)
            if p is None:
                raise BadConfirmation("Unknown or already used token; start again from the first step")
            if self.clock() > p["expires"]:
                del self.pending[token]
                raise BadConfirmation("The token has expired; start again from the first step")
            if not secrets.compare_digest((typed_confirmation or "").strip().encode(), p["phrase"].encode()):
                p["wrong"] += 1
                if p["wrong"] >= MAX_WRONG_PHRASES:
                    del self.pending[token]
                raise BadConfirmation("The phrase does not match; nothing was sent")
            del self.pending[token]
        self._verify(p)
        kind = p["manifest"]["destination"]["kind"]
        result = self.transports[kind](p["folder"], p["manifest"], p["manifest"]["destination"])
        return {"kind": kind, "folder": str(p["folder"]), **result}

    @staticmethod
    def _verify(p: Mapping[str, Any]) -> None:
        folder: Path = p["folder"]
        names = {f["name"] for f in p["manifest"]["files"]}
        on_disk = {q.relative_to(folder).as_posix() for q in folder.rglob("*") if q.is_file()} - {MANIFEST, SUMMARY}
        if on_disk != names:
            raise PackageChanged("The files in the package changed after you saw them; nothing was sent")
        for f in p["manifest"]["files"]:
            if _sha256(folder / f["name"]) != f["sha256"]:
                raise PackageChanged(f"{f['name']} changed after you saw it; nothing was sent")
